from __future__ import annotations

import json
import shutil
import subprocess
import tempfile
import unittest
import urllib.error
import urllib.parse
from pathlib import Path
from unittest.mock import patch

from processor.ai.client import OpenAIResponsesClient
from processor.ai.dedupe import deduplicate_candidates
from processor.ai.pipeline import ProductAnalysisPipeline, SYSTEM_PROMPT, _select_candidates
from processor.models import (
    AnalysisConfigurationError,
    AnalysisError,
    Appearance,
    BoundingBox,
    FrameManifest,
    MatchKind,
    ProductCandidate,
)
from processor.search import RetailerSearchService
from processor.search.retailer import _cropped_frame_path
from processor.search.lens import GoogleLensSearchClient, LensCandidate
from processor.search.validation import ProductPageMetadata


def candidate(
    identifier: str,
    *,
    name: str = "Sony WH-1000XM5 headphones",
    category: str = "headphones",
    brand: str | None = "Sony",
    model: str | None = "WH-1000XM5",
    timestamp: float = 1.0,
    instance_key: str | None = None,
    confidence: float = 0.9,
) -> ProductCandidate:
    return ProductCandidate(
        id=identifier,
        name=name,
        category=category,
        brand=brand,
        model=model,
        visual_description="Black over-ear headphones with oval earcups and a slim headband",
        confidence=confidence,
        instance_key=instance_key,
        appearances=[Appearance(start_sec=timestamp, evidence=f"visible at {timestamp}")],
    )


class FakeClient:
    def __init__(self, responses: list[dict | Exception]):
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def create_json(self, payload: dict) -> dict:
        self.payloads.append(payload)
        response = self.responses.pop(0)
        if isinstance(response, Exception):
            raise response
        return response


class FakeLensClient:
    enabled = True

    def __init__(self) -> None:
        self.calls: list[tuple[str, str | None, int]] = []

    def public_crop_url(self, thumbnail_url: str | None, crop_name: str) -> str | None:
        return f"https://spotted.example/api/jobs/demo/crops/{crop_name}"

    def public_frame_url(self, thumbnail_url: str | None) -> str | None:
        return f"https://spotted.example{thumbnail_url}"

    def search(self, image_url: str, *, query: str | None = None, limit: int = 8) -> list[LensCandidate]:
        self.calls.append((image_url, query, limit))
        return [
            LensCandidate(
                title="Green ceramic vessel table lamp",
                link="https://lighting.example/green-vessel-lamp",
                source="Lighting Store",
                image_url="https://lighting.example/green-vessel-lamp.jpg",
                price="$299",
            )
        ]


class FakeHTTPResponse:
    def __init__(self, payload: dict) -> None:
        self.payload = payload

    def __enter__(self):
        return self

    def __exit__(self, *_args) -> None:
        return None

    def read(self) -> bytes:
        return json.dumps(self.payload).encode("utf-8")


class LensClientTests(unittest.TestCase):
    def test_lens_is_optional_and_requires_a_secure_transport(self) -> None:
        self.assertFalse(
            GoogleLensSearchClient(api_key="", public_base_url="https://spotted.example").enabled
        )
        self.assertFalse(
            GoogleLensSearchClient(api_key="key", public_base_url="http://localhost:3000").enabled
        )
        self.assertFalse(
            GoogleLensSearchClient(
                api_key="key",
                relay_url="https://spotted.example/api/lens-crops",
                relay_token="",
            ).enabled
        )
        self.assertTrue(
            GoogleLensSearchClient(
                api_key="key",
                relay_url="https://spotted.example/api/lens-crops",
                relay_token="relay-secret",
            ).enabled
        )

    def test_lens_parses_and_deduplicates_visual_matches(self) -> None:
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            return FakeHTTPResponse(
                {
                    "visual_matches": [
                        {
                            "title": "Green ceramic vessel table lamp",
                            "link": "https://lighting.example/green-lamp",
                            "source": "Lighting Store",
                            "image": "https://lighting.example/green-lamp.jpg",
                            "price": {"value": "$299"},
                            "exact_matches": True,
                        },
                        {
                            "title": "Duplicate",
                            "link": "https://lighting.example/green-lamp",
                        },
                        {"title": "Unsafe", "link": "http://localhost/item"},
                    ]
                }
            )

        client = GoogleLensSearchClient(
            api_key="serp-key",
            public_base_url="https://spotted.example",
            opener=opener,
        )
        result = client.search(
            "https://spotted.example/api/jobs/one/crops/lamp.jpg",
            query="green ceramic lamp",
        )

        self.assertEqual(len(result), 1)
        self.assertEqual(result[0].price, "$299")
        self.assertTrue(result[0].exact_hint)
        query = urllib.parse.parse_qs(urllib.parse.urlparse(requests[0].full_url).query)
        self.assertEqual(query["engine"], ["google_lens"])
        self.assertEqual(query["type"], ["products"])
        self.assertEqual(query["q"], ["green ceramic lamp"])

    def test_relay_uploads_searches_and_deletes_one_crop(self) -> None:
        requests = []

        def opener(request, **_kwargs):
            requests.append(request)
            if request.get_method() == "POST":
                return FakeHTTPResponse(
                    {
                        "url": "https://spotted.example/api/lens-crops/crop.jpg",
                        "deleteUrl": "https://spotted.example/api/lens-crops/crop.jpg",
                    }
                )
            if request.get_method() == "DELETE":
                return FakeHTTPResponse({})
            return FakeHTTPResponse(
                {
                    "visual_matches": [
                        {
                            "title": "Green ceramic vessel table lamp",
                            "link": "https://lighting.example/green-lamp",
                            "source": "Lighting Store",
                        }
                    ]
                }
            )

        with tempfile.TemporaryDirectory() as directory:
            crop = Path(directory) / "lamp.jpg"
            crop.write_bytes(b"jpeg-crop")
            client = GoogleLensSearchClient(
                api_key="serp-key",
                relay_url="https://spotted.example/api/lens-crops",
                relay_token="relay-secret",
                opener=opener,
            )
            result = client.search_crop(crop, query="green ceramic lamp")

        self.assertEqual(len(result), 1)
        self.assertEqual([request.get_method() for request in requests], ["POST", "GET", "DELETE"])
        self.assertEqual(requests[0].data, b"jpeg-crop")
        self.assertEqual(requests[0].get_header("Authorization"), "Bearer relay-secret")
        self.assertEqual(requests[2].get_header("Authorization"), "Bearer relay-secret")

    def test_lens_network_failure_is_distinct_from_zero_results(self) -> None:
        def failing_opener(*_args, **_kwargs):
            raise urllib.error.URLError("secret upstream detail")

        client = GoogleLensSearchClient(
            api_key="serp-key",
            public_base_url="https://spotted.example",
            opener=failing_opener,
        )
        outcome = client.search_with_diagnostics(
            "https://spotted.example/api/jobs/one/crops/lamp.jpg"
        )

        self.assertEqual(outcome.status, "error")
        self.assertEqual(outcome.candidates, ())
        self.assertEqual(outcome.diagnostics[0].code, "network_error")
        self.assertNotIn("secret upstream detail", json.dumps(outcome.to_dict()))

    def test_missing_relay_token_has_safe_configuration_diagnostic(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            crop = Path(directory) / "lamp.jpg"
            crop.write_bytes(b"jpeg-crop")
            client = GoogleLensSearchClient(
                api_key="serp-key",
                relay_url="https://spotted.example/api/lens-crops",
                relay_token="",
            )
            outcome = client.search_crop_with_diagnostics(crop)

        self.assertEqual(outcome.status, "disabled")
        self.assertEqual(outcome.diagnostics[0].code, "missing_relay_token")


class ModelContractTests(unittest.TestCase):
    def test_manifest_accepts_media_worker_camel_case_shape(self) -> None:
        manifest = FrameManifest.from_dict(
            {
                "version": 1,
                "durationSec": 20.5,
                "searchFocus": "Find all the lamps",
                "sourceUrl": "https://youtube.com/watch?v=demo",
                "sourceTitle": "2026 Interior Design Trends with Shea McGee",
                "sourceChannel": "Studio McGee",
                "sourcePlatform": "youtube",
                "captionSegments": [
                    {"startSec": 2.0, "endSec": 5.0, "text": "This is the reading lamp."}
                ],
                "frames": [
                    {
                        "id": "frame-0002",
                        "timestampSec": 4.0,
                        "path": "/tmp/two.jpg",
                        "thumbnailUrl": "/api/jobs/a/frames/frame-0002.jpg",
                        "width": 1280,
                        "height": 720,
                        "source": "scene_or_interval",
                    },
                    {"timestamp_sec": 1.0, "image_path": "/tmp/one.jpg"},
                ],
            }
        )
        self.assertEqual([frame.timestamp_sec for frame in manifest.frames], [1.0, 4.0])
        self.assertEqual(manifest.duration_sec, 20.5)
        self.assertEqual(manifest.search_focus, "Find all the lamps")
        self.assertEqual(manifest.source_title, "2026 Interior Design Trends with Shea McGee")
        self.assertEqual(manifest.source_channel, "Studio McGee")
        self.assertEqual(manifest.source_platform, "youtube")
        self.assertEqual(manifest.caption_segments[0].text, "This is the reading lamp.")
        self.assertEqual(manifest.frames[1].thumbnail_url, "/api/jobs/a/frames/frame-0002.jpg")

    def test_finding_serializes_frontend_contract(self) -> None:
        service = RetailerSearchService(
            client=FakeClient([{"matches": []}]),
            metadata_fetcher=lambda *args, **kwargs: None,
        )
        finding = service.enrich(candidate("one"))
        payload = finding.to_dict()
        self.assertEqual(payload["matchKind"], "possible")
        self.assertEqual(payload["detectionConfidence"], 0.9)
        self.assertNotIn("matchConfidence", payload)
        self.assertEqual(payload["appearances"][0]["startSec"], 1.0)
        self.assertNotIn("productUrl", payload)


class DedupeTests(unittest.TestCase):
    def test_extended_shopping_categories_are_explicit_detection_targets(self) -> None:
        for category in ("watches", "clothing", "shoes", "tools"):
            self.assertIn(category, SYSTEM_PROMPT.lower())

    def test_same_brand_model_merges_and_preserves_timestamps(self) -> None:
        first = candidate("first", timestamp=1.0, confidence=0.81)
        second = candidate("second", name="Sony XM5 wireless headphones", timestamp=12.0, confidence=0.94)
        result = deduplicate_candidates([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual([appearance.start_sec for appearance in result[0].appearances], [1.0, 12.0])
        self.assertEqual(result[0].confidence, 0.94)
        self.assertTrue(result[0].id.startswith("product-"))

    def test_same_sku_merges_across_batches_with_different_instance_keys(self) -> None:
        first = candidate("first", timestamp=1.0, instance_key="batch-one-headphones")
        second = candidate("second", timestamp=22.0, instance_key="batch-two-headphones")
        result = deduplicate_candidates([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual([appearance.start_sec for appearance in result[0].appearances], [1.0, 22.0])

    def test_distinct_instance_keys_do_not_merge(self) -> None:
        left = candidate("left", instance_key="lamp-left", name="brass table lamp", category="lamp", brand=None, model=None)
        right = candidate("right", instance_key="lamp-right", name="brass table lamp", category="lamp", brand=None, model=None)
        self.assertEqual(len(deduplicate_candidates([left, right])), 2)

    def test_near_duplicate_appearances_are_collapsed(self) -> None:
        first = candidate("first", timestamp=4.0)
        second = candidate("second", timestamp=4.4)
        result = deduplicate_candidates([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual(len(result[0].appearances), 1)

    def test_visual_duplicate_merges_across_batches_and_category_labels(self) -> None:
        first = candidate(
            "batch-1-candidate-1",
            name="brass table lamp",
            category="table lamp",
            brand=None,
            model=None,
            timestamp=10.0,
            instance_key="lamp-a",
        )
        second = candidate(
            "batch-2-candidate-1",
            name="brass table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=55.0,
            instance_key="main-light",
        )
        result = deduplicate_candidates([first, second])
        self.assertEqual(len(result), 1)
        self.assertEqual([item.start_sec for item in result[0].appearances], [10.0, 55.0])

    def test_reused_generic_instance_key_across_batches_does_not_force_merge(self) -> None:
        white = candidate(
            "batch-1-candidate-1",
            name="table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=217.0,
            instance_key="table-lamp",
        )
        white.visual_description = "Glossy white spherical ceramic base and broad tapered shade"
        gray = candidate(
            "batch-2-candidate-1",
            name="table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=458.0,
            instance_key="table-lamp",
        )
        gray.visual_description = "Tall gray stone base with a narrow rectangular shade"

        result = deduplicate_candidates([white, gray])

        self.assertEqual(len(result), 2)

    def test_detection_prompt_requires_matching_pairs_to_be_separate(self) -> None:
        self.assertIn("matching pair", SYSTEM_PROMPT)
        self.assertIn("one candidate per physical object", SYSTEM_PROMPT)

    def test_visually_conflicting_colors_do_not_merge(self) -> None:
        pale = candidate(
            "batch-1-candidate-1",
            name="ceramic table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=10.0,
        )
        pale.color = "pale green"
        pale.material = "ceramic"
        red = candidate(
            "batch-2-candidate-1",
            name="ceramic table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=55.0,
        )
        red.color = "red"
        red.material = "ceramic"
        self.assertEqual(len(deduplicate_candidates([pale, red])), 2)

    def test_lighting_subtypes_and_silhouettes_do_not_merge(self) -> None:
        table_lamp = candidate(
            "batch-1-candidate-1",
            name="white ceramic table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=10.0,
        )
        table_lamp.visual_description = "Spherical ceramic base with tapered cone shade"
        pendant = candidate(
            "batch-2-candidate-1",
            name="white globe pendant lamp",
            category="pendant lamp",
            brand=None,
            model=None,
            timestamp=55.0,
        )
        pendant.visual_description = "Suspended glass globe on a cord"
        self.assertEqual(len(deduplicate_candidates([table_lamp, pendant])), 2)

    def test_contradictory_lamp_construction_does_not_merge(self) -> None:
        angled = candidate(
            "batch-1-candidate-1",
            name="black desk lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=10.0,
        )
        angled.visual_description = "Articulated angled arm and metal dome shade"
        column = candidate(
            "batch-2-candidate-1",
            name="black desk lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=55.0,
        )
        column.visual_description = "Straight column stem with a drum shade"
        self.assertEqual(len(deduplicate_candidates([angled, column])), 2)

    def test_spatially_distinct_same_scene_instances_do_not_merge(self) -> None:
        left = candidate(
            "left",
            name="white table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=20.0,
        )
        right = candidate(
            "right",
            name="white table lamp",
            category="lamp",
            brand=None,
            model=None,
            timestamp=20.0,
        )
        left.visual_description = right.visual_description = "White ceramic lamp with tapered shade"
        left.appearances[0] = Appearance(
            start_sec=20.0,
            evidence="Lamp on left nightstand",
            bounding_box=BoundingBox(x=0.04, y=0.2, width=0.18, height=0.5),
        )
        right.appearances[0] = Appearance(
            start_sec=20.0,
            evidence="Lamp on right nightstand",
            bounding_box=BoundingBox(x=0.78, y=0.2, width=0.18, height=0.5),
        )
        result = deduplicate_candidates([left, right])
        self.assertEqual(len(result), 2)
        self.assertEqual(len({item.id for item in result}), 2)

    def test_precision_selection_rejects_one_frame_generic_guesses(self) -> None:
        one_frame = candidate(
            "one-frame",
            name="table lamp",
            category="lamp",
            brand=None,
            model=None,
            confidence=0.99,
        )
        repeated = candidate(
            "repeated",
            name="black angled lamp",
            category="lamp",
            brand=None,
            model=None,
            confidence=0.82,
        )
        repeated.appearances.append(
            Appearance(start_sec=6.0, evidence="Same black angled lamp")
        )
        self.assertEqual(_select_candidates([one_frame, repeated], limit=8), [repeated])

    def test_focused_selection_keeps_clear_single_frame_match(self) -> None:
        focused = candidate(
            "focused",
            name="pale ceramic lamp",
            category="lamp",
            brand=None,
            model=None,
            confidence=0.88,
        )
        focused.appearances[0] = Appearance(
            start_sec=7.0,
            evidence="Clear lamp in the background",
            bounding_box=BoundingBox(x=0.4, y=0.1, width=0.2, height=0.5),
        )
        self.assertEqual(
            _select_candidates(
                [focused], limit=8, allow_single_frame=True
            ),
            [focused],
        )


class LivePipelineTests(unittest.TestCase):
    def test_model_defaults_to_luna_and_allows_env_override(self) -> None:
        self.assertEqual(OpenAIResponsesClient(api_key="test").model, "gpt-5.6-luna")
        with patch.dict("os.environ", {"SPOTTED_OPENAI_MODEL": "custom-vision-model"}):
            self.assertEqual(OpenAIResponsesClient(api_key="test").model, "custom-vision-model")

    def test_missing_api_key_raises_clear_error_without_network(self) -> None:
        client = OpenAIResponsesClient(api_key="")
        with self.assertRaisesRegex(AnalysisConfigurationError, "OPENAI_API_KEY"):
            client.create_response({"input": "test"})

    def test_pipeline_uses_real_structured_response_and_emits_events(self) -> None:
        response = {
            "candidates": [
                {
                    "name": "Black over-ear headphones",
                    "category": "headphones",
                    "brand": None,
                    "model": None,
                    "color": "black",
                    "material": None,
                    "visualDescription": "Black oval earcups with a narrow padded headband",
                    "visibleText": ["WH-1000XM5"],
                    "instanceKey": "headphones-main",
                    "confidence": 0.72,
                    "appearances": [
                        {
                            "frameIndex": 1,
                            "evidence": "Black headphones worn by presenter",
                            "boundingBox": {"x": 0.2, "y": 0.1, "width": 0.4, "height": 0.5},
                        }
                    ],
                }
            ]
        }
        fake = FakeClient([response])
        events: list[str] = []
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"not-decoded-locally")
            manifest = {
                "searchFocus": "Find all the headphones",
                "sourceUrl": "https://youtube.com/watch?v=demo",
                "sourceTitle": "Headphone studio tour",
                "sourceChannel": "Example Channel",
                "sourcePlatform": "youtube",
                "captionSegments": [
                    {"startSec": 1.0, "endSec": 3.0, "text": "These headphones are black."}
                ],
                "frames": [{"timestampSec": 2.0, "path": str(image), "thumbnailUrl": "/thumb.jpg"}],
            }
            second_image = Path(directory) / "frame-two.jpg"
            second_image.write_bytes(b"also-not-decoded-locally")
            manifest["frames"].append(
                {
                    "timestampSec": 7.0,
                    "path": str(second_image),
                    "thumbnailUrl": "/thumb-two.jpg",
                    "similarTimestamps": [12.0],
                }
            )
            results = ProductAnalysisPipeline(client=fake).analyze(
                manifest, event_callback=lambda event, payload: events.append(event)
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].source_title, "Headphone studio tour")
        self.assertEqual(results[0].source_channel, "Example Channel")
        self.assertEqual(results[0].source_platform, "youtube")
        self.assertEqual(results[0].search_focus, "Find all the headphones")
        self.assertEqual(results[0].appearances[0].start_sec, 7.0)
        self.assertEqual(
            [item.start_sec for item in results[0].appearances],
            [7.0, 12.0],
        )
        self.assertEqual(results[0].appearances[0].thumbnail_url, "/thumb-two.jpg")
        self.assertIn("analyzing_frame", events)
        self.assertIn("candidate_found", events)
        self.assertIn("merging_duplicates", events)
        image_inputs = fake.payloads[0]["input"][0]["content"]
        self.assertTrue(any(item.get("type") == "input_image" for item in image_inputs))
        self.assertEqual(fake.payloads[0]["reasoning"], {"effort": "none"})
        prompt = image_inputs[0]["text"]
        self.assertIn("Find all the headphones", prompt)
        self.assertIn("These headphones are black", prompt)
        self.assertTrue(any(item.get("text", "").startswith("FRAME_INDEX=1 timestampSec=7.000") for item in image_inputs))
        self.assertTrue(all(item.get("detail") == "high" for item in image_inputs if item.get("type") == "input_image"))

    def test_pipeline_skips_candidates_below_fifty_percent(self) -> None:
        response = {
            "candidates": [
                {
                    "name": "Possible mug",
                    "category": "mug",
                    "brand": None,
                    "model": None,
                    "color": None,
                    "material": None,
                    "visualDescription": "Partially visible plain mug",
                    "visibleText": [],
                    "instanceKey": "mug",
                    "confidence": 0.49,
                    "appearances": [
                        {
                            "frameIndex": 0,
                            "evidence": "Partial object",
                            "boundingBox": None,
                        }
                    ],
                }
            ]
        }
        fake = FakeClient([response])
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "frame.jpg"
            image.write_bytes(b"not-decoded-locally")
            results = ProductAnalysisPipeline(client=fake).analyze(
                {"frames": [{"timestampSec": 5, "path": str(image)}]}
            )
        self.assertEqual(results, [])


class RetailSearchTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which("ffmpeg") and shutil.which("ffprobe"), "ffmpeg required")
    def test_small_object_crop_is_padded_and_upscaled_to_512_pixels_wide(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            subprocess.run(
                [
                    shutil.which("ffmpeg") or "ffmpeg",
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-f",
                    "lavfi",
                    "-i",
                    "color=white:size=320x180",
                    "-frames:v",
                    "1",
                    str(frame),
                ],
                check=True,
                timeout=20,
            )
            crop = _cropped_frame_path(
                str(frame), BoundingBox(x=0.4, y=0.2, width=0.2, height=0.4)
            )
            self.assertIsNotNone(crop)
            probe = subprocess.run(
                [
                    shutil.which("ffprobe") or "ffprobe",
                    "-v",
                    "error",
                    "-select_streams",
                    "v:0",
                    "-show_entries",
                    "stream=width,height",
                    "-of",
                    "json",
                    str(crop),
                ],
                check=True,
                capture_output=True,
                text=True,
                timeout=20,
            )
            dimensions = json.loads(probe.stdout)["streams"][0]
            self.assertGreaterEqual(dimensions["width"], 512)
            self.assertGreater(dimensions["height"], dimensions["width"])

    def test_google_lens_candidates_seed_the_openai_search_once_per_product(self) -> None:
        fake_openai = FakeClient([{"matches": []}])
        fake_lens = FakeLensClient()
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame-0001.jpg"
            frame.write_bytes(b"frame")
            detected = candidate(
                "lamp",
                name="green ceramic vessel table lamp",
                category="lamp",
                brand=None,
                model=None,
            )
            detected.color = "green"
            detected.material = "ceramic"
            detected.source_url = "https://youtube.com/watch?v=demo"
            detected.source_title = "2026 Interior Design Trends with Shea McGee"
            detected.source_channel = "Studio McGee"
            detected.source_platform = "youtube"
            detected.search_focus = "lamps"
            detected.appearances[0] = Appearance(
                start_sec=7.0,
                evidence="Green ceramic lamp with a white drum shade",
                thumbnail_url="/api/jobs/demo/frames/frame-0001.jpg",
                bounding_box=BoundingBox(x=0.4, y=0.1, width=0.25, height=0.6),
                source_path=str(frame),
            )
            service = RetailerSearchService(
                client=fake_openai,
                lens_client=fake_lens,
                metadata_fetcher=lambda *args, **kwargs: None,
            )
            finding = service.enrich(detected)

        self.assertEqual(finding.match_kind, MatchKind.POSSIBLE)
        self.assertEqual(len(fake_lens.calls), 1)
        search_prompt = fake_openai.payloads[0]["input"][0]["content"][0]["text"]
        self.assertIn("Google Lens candidates", search_prompt)
        self.assertIn("Green ceramic vessel table lamp", search_prompt)
        self.assertIn("https://lighting.example/green-vessel-lamp", search_prompt)
        self.assertIn("2026 Interior Design Trends with Shea McGee", search_prompt)
        self.assertIn("Studio McGee", search_prompt)
        self.assertIn("Studio McGee", fake_lens.calls[0][1] or "")

    def test_retrieval_stage_counts_and_reasons_are_emitted(self) -> None:
        fake_openai = FakeClient([{"matches": []}])
        events: list[tuple[str, dict]] = []
        service = RetailerSearchService(
            client=fake_openai,
            lens_client=GoogleLensSearchClient(api_key=""),
            metadata_fetcher=lambda *args, **kwargs: None,
        )
        service.enrich(
            candidate("lamp", name="table lamp", category="lamp", brand=None, model=None),
            event_callback=lambda event, payload: events.append((event, payload)),
        )

        diagnostic = next(
            payload for event, payload in events
            if event == "retailer_search_diagnostics"
        )
        self.assertEqual(diagnostic["candidateId"], "lamp")
        self.assertEqual(diagnostic["counts"]["lensCandidates"], 0)
        self.assertEqual(diagnostic["counts"]["webCandidates"], 0)
        self.assertEqual(diagnostic["counts"]["verifiedMatches"], 0)
        self.assertIn("no_target_crops", {item["code"] for item in diagnostic["reasons"]})

    def test_exact_match_requires_brand_model_and_verified_page(self) -> None:
        fake = FakeClient(
            [
                {
                    "matches": [
                        {
                            "productName": "Sony WH-1000XM5",
                            "retailerName": "Sony",
                            "productUrl": "https://electronics.example/sony-wh-1000xm5",
                            "matchKind": "exact",
                            "confidence": 0.95,
                            "evidence": "Official model page matches visible model text",
                        }
                    ]
                }
            ]
        )
        metadata = ProductPageMetadata(
            url="https://electronics.example/sony-wh-1000xm5",
            title="Sony WH-1000XM5 Wireless Headphones",
            image_url="https://electronics.example/xm5.jpg",
        )
        service = RetailerSearchService(client=fake, metadata_fetcher=lambda *args, **kwargs: metadata)
        finding = service.enrich(candidate("sony"))
        self.assertEqual(finding.match_kind, MatchKind.EXACT)
        self.assertEqual(finding.match_confidence, 0.95)
        self.assertEqual(finding.detection_confidence, 0.9)
        self.assertEqual(finding.product_url, metadata.url)

    def test_unverified_url_is_never_exposed(self) -> None:
        fake = FakeClient(
            [
                {
                    "matches": [
                        {
                            "productName": "Mystery Lamp",
                            "retailerName": "Unknown",
                            "productUrl": "https://invalid.example/lamp",
                            "matchKind": "similar",
                            "confidence": 0.7,
                            "evidence": "Visually similar",
                        }
                    ]
                }
            ]
        )
        service = RetailerSearchService(client=fake, metadata_fetcher=lambda *args, **kwargs: None)
        result = service.enrich(candidate("lamp", name="table lamp", category="lamp", brand=None, model=None))
        self.assertEqual(result.match_kind, MatchKind.POSSIBLE)
        self.assertIsNone(result.product_url)

    def test_exact_claim_downgrades_without_model_evidence(self) -> None:
        fake = FakeClient(
            [
                {
                    "matches": [
                        {
                            "productName": "Nike Black Running Shoe",
                            "retailerName": "Nike",
                            "productUrl": "https://nike.example/shoe",
                            "matchKind": "exact",
                            "confidence": 0.99,
                            "evidence": "Similar black shoe",
                        }
                    ]
                }
            ]
        )
        metadata = ProductPageMetadata(url="https://nike.example/shoe", title="Nike Black Running Shoe")
        service = RetailerSearchService(client=fake, metadata_fetcher=lambda *args, **kwargs: metadata)
        result = service.enrich(candidate("shoe", name="black running shoe", category="shoe", brand="Nike", model=None))
        self.assertEqual(result.match_kind, MatchKind.POSSIBLE)
        self.assertIsNone(result.product_url)

    def test_similar_match_requires_and_passes_visual_verification(self) -> None:
        search_response = {
            "matches": [
                {
                    "productName": "Black Angled Brass Desk Lamp",
                    "retailerName": "Lighting Store",
                    "productUrl": "https://lighting.example/black-angled-brass-lamp",
                    "matchKind": "similar",
                    "confidence": 0.9,
                    "evidence": "Black angled arm and brass hardware match",
                }
            ]
        }
        visual_response = {
            "comparisons": [
                {
                    "index": 0,
                    "verdict": "similar",
                    "confidence": 0.86,
                    "categoryMatch": "match",
                    "shapeMatch": "match",
                    "colorMatch": "match",
                    "materialMatch": "match",
                    "constructionMatch": "match",
                    "identityEvidence": False,
                    "contradictions": [],
                    "evidence": "Matching angled arm, black shade, and brass joints",
                }
            ]
        }
        fake = FakeClient([search_response, visual_response])
        metadata = ProductPageMetadata(
            url="https://lighting.example/black-angled-brass-lamp",
            title="Black Angled Brass Desk Lamp",
            image_url="https://lighting.example/lamp.jpg",
        )
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"frame")
            detected = candidate(
                "lamp",
                name="black angled brass desk lamp",
                category="desk lamp",
                brand=None,
                model=None,
            )
            detected.color = "black"
            detected.material = "brass metal"
            detected.appearances[0] = Appearance(
                start_sec=1.0,
                evidence="Black angled lamp with brass joints",
                bounding_box=BoundingBox(
                    x=0.2, y=0.1, width=0.35, height=0.65
                ),
                source_path=str(frame),
            )
            service = RetailerSearchService(
                client=fake,
                metadata_fetcher=lambda *args, **kwargs: metadata,
            )
            result = service.enrich(detected)

        self.assertEqual(result.match_kind, MatchKind.SIMILAR)
        self.assertAlmostEqual(result.confidence, 0.868)
        self.assertEqual(result.product_url, metadata.url)
        self.assertEqual(len(fake.payloads), 2)
        search_content = fake.payloads[0]["input"][0]["content"]
        self.assertTrue(
            any(item.get("type") == "input_image" for item in search_content)
        )
        self.assertEqual(fake.payloads[0]["model"], "gpt-5.6-terra")

    def test_blocked_retailer_image_does_not_fail_entire_scan(self) -> None:
        blocked_image_error = AnalysisError(
            'OpenAI Responses API returned HTTP 400: {"error": {'
            '"message": "Error while downloading file. Upstream status code: 403.", '
            '"param": "url"}}'
        )
        fake = FakeClient(
            [
                {
                    "matches": [
                        {
                            "productName": "Blocked Lamp",
                            "retailerName": "Blocked Store",
                            "productUrl": "https://blocked.example/lamp",
                            "matchKind": "similar",
                            "confidence": 0.9,
                            "evidence": "Plausible lamp",
                        },
                        {
                            "productName": "Accessible Lamp",
                            "retailerName": "Accessible Store",
                            "productUrl": "https://accessible.example/lamp",
                            "matchKind": "similar",
                            "confidence": 0.88,
                            "evidence": "Matching round base and shade",
                        },
                    ]
                },
                blocked_image_error,
                blocked_image_error,
                {
                    "comparisons": [
                        {
                            "index": 0,
                            "verdict": "similar",
                            "confidence": 0.86,
                            "categoryMatch": "match",
                            "shapeMatch": "match",
                            "colorMatch": "match",
                            "materialMatch": "match",
                            "constructionMatch": "match",
                            "identityEvidence": False,
                            "contradictions": [],
                            "evidence": "Round base and tapered shade agree",
                        }
                    ]
                },
            ]
        )

        def metadata(url: str, **_: object) -> ProductPageMetadata:
            host = "blocked" if "blocked.example" in url else "accessible"
            return ProductPageMetadata(
                url=url,
                title=f"{host.title()} Lamp",
                image_url=f"https://{host}.example/lamp.jpg",
            )

        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"frame")
            detected = candidate(
                "lamp",
                name="white round table lamp",
                category="lamp",
                brand=None,
                model=None,
            )
            detected.appearances[0] = Appearance(
                start_sec=1.0,
                evidence="White round table lamp",
                bounding_box=BoundingBox(x=0.2, y=0.1, width=0.35, height=0.65),
                source_path=str(frame),
            )
            result = RetailerSearchService(
                client=fake,
                metadata_fetcher=metadata,
            ).enrich(detected)

        self.assertEqual(result.product_url, "https://accessible.example/lamp")
        self.assertEqual(result.match_kind, MatchKind.SIMILAR)
        self.assertEqual(len(fake.payloads), 4)

    def test_plausible_visual_candidate_is_linked_as_possible_not_verified(self) -> None:
        fake = FakeClient(
            [
                {
                    "matches": [
                        {
                            "productName": "White Ceramic Round Table Lamp",
                            "retailerName": "Lighting Store",
                            "productUrl": "https://lighting.example/white-round-lamp",
                            "matchKind": "similar",
                            "confidence": 0.78,
                            "evidence": "Round white base and tapered shade are plausible",
                        }
                    ]
                },
                {
                    "comparisons": [
                        {
                            "index": 0,
                            "verdict": "similar",
                            "confidence": 0.74,
                            "categoryMatch": "match",
                            "shapeMatch": "match",
                            "colorMatch": "match",
                            "materialMatch": "unknown",
                            "constructionMatch": "unknown",
                            "identityEvidence": False,
                            "contradictions": [],
                            "evidence": "Silhouette and color agree but details are soft",
                        }
                    ]
                },
            ]
        )
        metadata = ProductPageMetadata(
            url="https://lighting.example/white-round-lamp",
            title="White Ceramic Round Table Lamp",
            image_url="https://lighting.example/white-round-lamp.jpg",
        )
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"frame")
            detected = candidate(
                "lamp",
                name="white ceramic table lamp",
                category="lamp",
                brand=None,
                model=None,
            )
            detected.appearances[0] = Appearance(
                start_sec=1.0,
                evidence="White round lamp",
                bounding_box=BoundingBox(x=0.2, y=0.1, width=0.35, height=0.65),
                source_path=str(frame),
            )
            result = RetailerSearchService(
                client=fake,
                metadata_fetcher=lambda *args, **kwargs: metadata,
            ).enrich(detected)

        self.assertEqual(result.match_kind, MatchKind.POSSIBLE)
        self.assertEqual(result.product_url, metadata.url)
        self.assertAlmostEqual(result.match_confidence, 0.748)
        self.assertLess(result.match_confidence, 0.82)

    def test_visual_contradiction_rejects_high_confidence_search_result(self) -> None:
        fake = FakeClient(
            [
                {
                    "matches": [
                        {
                            "productName": "Bright Red Ceramic Table Lamp",
                            "retailerName": "Lighting Store",
                            "productUrl": "https://lighting.example/red-lamp",
                            "matchKind": "similar",
                            "confidence": 0.98,
                            "evidence": "Same broad category",
                        }
                    ]
                },
                {
                    "comparisons": [
                        {
                            "index": 0,
                            "verdict": "similar",
                            "confidence": 0.99,
                            "categoryMatch": "match",
                            "shapeMatch": "match",
                            "colorMatch": "mismatch",
                            "materialMatch": "match",
                            "constructionMatch": "unknown",
                            "identityEvidence": False,
                            "contradictions": [
                                "Target is pale green; retailer product is bright red"
                            ],
                            "evidence": "Dominant color contradicts the target",
                        }
                    ]
                },
            ]
        )
        metadata = ProductPageMetadata(
            url="https://lighting.example/red-lamp",
            title="Bright Red Ceramic Table Lamp",
            image_url="https://lighting.example/red-lamp.jpg",
        )
        with tempfile.TemporaryDirectory() as directory:
            frame = Path(directory) / "frame.jpg"
            frame.write_bytes(b"frame")
            detected = candidate(
                "lamp",
                name="pale green ceramic table lamp",
                category="lamp",
                brand=None,
                model=None,
            )
            detected.color = "pale green"
            detected.material = "ceramic"
            detected.appearances[0] = Appearance(
                start_sec=7.0,
                evidence="Pale green ceramic lamp with white shade",
                bounding_box=BoundingBox(
                    x=0.4, y=0.1, width=0.25, height=0.6
                ),
                source_path=str(frame),
            )
            service = RetailerSearchService(
                client=fake,
                metadata_fetcher=lambda *args, **kwargs: metadata,
            )
            result = service.enrich(detected)

        self.assertEqual(result.match_kind, MatchKind.POSSIBLE)
        self.assertIsNone(result.product_url)


if __name__ == "__main__":
    unittest.main()
