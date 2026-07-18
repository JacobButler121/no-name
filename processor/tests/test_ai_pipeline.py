from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from processor.ai.client import OpenAIResponsesClient
from processor.ai.dedupe import deduplicate_candidates
from processor.ai.pipeline import ProductAnalysisPipeline, _select_candidates
from processor.models import (
    AnalysisConfigurationError,
    Appearance,
    FrameManifest,
    MatchKind,
    ProductCandidate,
)
from processor.search import RetailerSearchService
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
    def __init__(self, responses: list[dict]):
        self.responses = list(responses)
        self.payloads: list[dict] = []

    def create_json(self, payload: dict) -> dict:
        self.payloads.append(payload)
        return self.responses.pop(0)


class ModelContractTests(unittest.TestCase):
    def test_manifest_accepts_media_worker_camel_case_shape(self) -> None:
        manifest = FrameManifest.from_dict(
            {
                "version": 1,
                "durationSec": 20.5,
                "searchFocus": "Find all the lamps",
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
                "captionSegments": [
                    {"startSec": 1.0, "endSec": 3.0, "text": "These headphones are black."}
                ],
                "frames": [{"timestampSec": 2.0, "path": str(image), "thumbnailUrl": "/thumb.jpg"}],
            }
            second_image = Path(directory) / "frame-two.jpg"
            second_image.write_bytes(b"also-not-decoded-locally")
            manifest["frames"].append(
                {"timestampSec": 7.0, "path": str(second_image), "thumbnailUrl": "/thumb-two.jpg"}
            )
            results = ProductAnalysisPipeline(client=fake).analyze(
                manifest, event_callback=lambda event, payload: events.append(event)
            )
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].appearances[0].start_sec, 7.0)
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
                source_path=str(frame),
            )
            service = RetailerSearchService(
                client=fake,
                metadata_fetcher=lambda *args, **kwargs: metadata,
            )
            result = service.enrich(detected)

        self.assertEqual(result.match_kind, MatchKind.SIMILAR)
        self.assertEqual(result.confidence, 0.86)
        self.assertEqual(result.product_url, metadata.url)
        self.assertEqual(len(fake.payloads), 2)


if __name__ == "__main__":
    unittest.main()
