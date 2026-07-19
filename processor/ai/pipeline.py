from __future__ import annotations

import base64
import json
import mimetypes
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from processor.models import (
    AnalysisError,
    Appearance,
    CaptionSegment,
    FrameManifest,
    FrameSample,
    ProductCandidate,
    ProductFinding,
)

from .client import OpenAIResponsesClient
from .dedupe import deduplicate_candidates


EventCallback = Callable[[str, dict[str, Any]], None]


DETECTION_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "candidates": {
            "type": "array",
            "items": {
                "type": "object",
                "properties": {
                    "name": {"type": "string"},
                    "category": {"type": "string"},
                    "brand": {"type": ["string", "null"]},
                    "model": {"type": ["string", "null"]},
                    "color": {"type": ["string", "null"]},
                    "material": {"type": ["string", "null"]},
                    "visualDescription": {"type": "string"},
                    "visibleText": {"type": "array", "items": {"type": "string"}},
                    "instanceKey": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "appearances": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "frameIndex": {"type": "integer", "minimum": 0, "maximum": 19},
                                "evidence": {"type": "string"},
                                "boundingBox": {
                                    "anyOf": [
                                        {
                                            "type": "object",
                                            "properties": {
                                                "x": {"type": "number", "minimum": 0, "maximum": 1},
                                                "y": {"type": "number", "minimum": 0, "maximum": 1},
                                                "width": {"type": "number", "minimum": 0, "maximum": 1},
                                                "height": {"type": "number", "minimum": 0, "maximum": 1},
                                            },
                                            "required": ["x", "y", "width", "height"],
                                            "additionalProperties": False,
                                        },
                                        {"type": "null"},
                                    ]
                                },
                            },
                            "required": ["frameIndex", "evidence", "boundingBox"],
                            "additionalProperties": False,
                        },
                    },
                },
                "required": [
                    "name",
                    "category",
                    "brand",
                    "model",
                    "color",
                    "material",
                    "visualDescription",
                    "visibleText",
                    "instanceKey",
                    "confidence",
                    "appearances",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["candidates"],
    "additionalProperties": False,
}


SYSTEM_PROMPT = """You are Spotted, a conservative visual product-detection engine.
Identify physical consumer goods a viewer could reasonably shop for, including watches
and jewelry; clothing such as shirts, jackets, dresses, and pants; shoes, bags, and other
wearable accessories; tools and workshop equipment; electronics, furniture, decor,
beauty, kitchenware, toys, packaged goods, and sports equipment. Consider centered
products, objects worn by people, objects being handled, and identifiable background
objects. Never identify people, body parts, scenery, architecture, text overlays, or
digital-only content as products.

Treat each visible garment, shoe pair, watch, accessory, or tool as its own shoppable
object when enough of it is visible to describe. For worn products, enclose the item—not
the person's full body—in the bounding box. Do not guess a clothing or accessory brand
from style alone. When a pair of shoes is clearly one matching product, return one
candidate rather than separate left and right shoes.

Only state a brand or model when supported by readable text, a distinctive design, or
the transcript. Use null rather than guessing. Use a useful generic name when the exact
SKU is unknown. Confidence measures identity certainty, not visual prominence. A
generic-but-certain category can have moderate confidence, but an exact SKU needs
specific evidence. Include normalized 0..1 bounding boxes where possible. For every
candidate, write a compact visualDescription of the observed object: silhouette,
proportions, geometry, finish, colors, materials, hardware, and distinctive details.
Describe only what is visible; do not turn this description into a guessed catalog name.

The same physical item across frames must use the same short instanceKey. Different
physical instances—even visually similar ones—must use different instanceKeys. Return
every frameIndex where it appears in this batch. frameIndex must be copied from the
label immediately before the corresponding image; never calculate or invent a timestamp.
When a scene contains a matching pair or set (for example two bedside lamps), return one
candidate per physical object, with a separate instanceKey and tight bounding box for
each. Never use plural evidence such as "both lamps" for a candidate whose box encloses
only one lamp. Symmetry and matching appearance do not make two objects one instance.
Evidence must briefly describe what is actually visible or spoken. Bounding boxes must
tightly enclose the named object rather than a person, wall, or whole room. Systematically
sweep every supplied frame and do not omit a clearly visible background object that
matches SEARCH FOCUS. Return exactly one candidate per physical object; never return
multiple possible brand or retailer identities for one object. Detection names describe
what is visible and must not look like guessed catalog listings. Do not invent shopping
links or prices."""


def _emit(callback: EventCallback | None, event_type: str, payload: dict[str, Any]) -> None:
    if callback:
        callback(event_type, payload)


def _image_input(frame: FrameSample) -> str:
    if frame.image_url:
        return frame.image_url
    path = Path(frame.path or "")
    if not path.is_file():
        raise AnalysisError(f"Frame file does not exist: {path}")
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(path.read_bytes()).decode("ascii")
    return f"data:{mime_type};base64,{encoded}"


class ProductAnalysisPipeline:
    def __init__(
        self,
        *,
        client: OpenAIResponsesClient | None = None,
        batch_size: int = 10,
        detail: str = "high",
        min_confidence: float = 0.7,
        focused_limit: int = 8,
        broad_limit: int = 8,
    ) -> None:
        if not 1 <= batch_size <= 20:
            raise ValueError("batch_size must be between 1 and 20")
        if detail not in {"low", "high", "auto", "original"}:
            raise ValueError("detail must be low, high, auto, or original")
        self.client = client or OpenAIResponsesClient()
        self.batch_size = batch_size
        self.detail = detail
        self.min_confidence = min_confidence
        self.focused_limit = focused_limit
        self.broad_limit = broad_limit

    def analyze(
        self,
        manifest: FrameManifest | Mapping[str, Any] | Sequence[Mapping[str, Any]],
        *,
        event_callback: EventCallback | None = None,
    ) -> list[ProductCandidate]:
        parsed = manifest if isinstance(manifest, FrameManifest) else FrameManifest.from_dict(manifest)
        candidates: list[ProductCandidate] = []
        total_batches = (len(parsed.frames) + self.batch_size - 1) // self.batch_size
        for batch_index, offset in enumerate(range(0, len(parsed.frames), self.batch_size), start=1):
            frames = parsed.frames[offset : offset + self.batch_size]
            _emit(
                event_callback,
                "analyzing_frame",
                {
                    "batch": batch_index,
                    "totalBatches": total_batches,
                    "frameCount": len(frames),
                    "startSec": frames[0].timestamp_sec,
                    "endSec": frames[-1].timestamp_sec,
                },
            )
            batch_candidates = self._analyze_batch(
                frames,
                parsed.caption_segments,
                parsed.search_focus,
                batch_index,
            )
            candidates.extend(batch_candidates)
            for item in batch_candidates:
                _emit(
                    event_callback,
                    "candidate_found",
                    {
                        "candidateId": item.id,
                        "name": item.name,
                        "category": item.category,
                        "confidence": item.confidence,
                        "appearances": [appearance.to_dict() for appearance in item.appearances],
                    },
                )
        merged = deduplicate_candidates(candidates)
        for candidate in merged:
            candidate.source_url = parsed.source_url
            candidate.source_title = parsed.source_title
            candidate.source_channel = parsed.source_channel
            candidate.source_platform = parsed.source_platform
            candidate.search_focus = parsed.search_focus
        selected = _select_candidates(
            merged,
            limit=self.focused_limit if parsed.search_focus else self.broad_limit,
            allow_single_frame=bool(parsed.search_focus),
        )
        _emit(
            event_callback,
            "merging_duplicates",
            {
                "candidateCount": len(candidates),
                "mergedCount": len(merged),
                "selectedCount": len(selected),
            },
        )
        return selected

    def _analyze_batch(
        self,
        frames: Sequence[FrameSample],
        caption_segments: Sequence[CaptionSegment],
        search_focus: str | None,
        batch_index: int,
    ) -> list[ProductCandidate]:
        local_captions = _captions_for_range(
            caption_segments,
            frames[0].timestamp_sec,
            frames[-1].timestamp_sec,
        )
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    "Analyze this ordered frame batch. Each FRAME_INDEX label maps exactly to the following image. "
                    "Return that frameIndex for every appearance; the server owns timestamps.\n"
                    + (
                        f"SEARCH FOCUS: {search_focus}. Return only physical products that match this request. "
                        "Treat plurals, synonyms, styles, and closely related subcategories as matches.\n"
                        if search_focus
                        else "SEARCH FOCUS: none. Return all defensible shoppable products.\n"
                    )
                    + (
                        f"Timestamp-aligned captions for only this frame window:\n{local_captions}"
                        if local_captions
                        else "No timestamp-aligned captions are available for this frame window."
                    )
                ),
            }
        ]
        for frame_index, frame in enumerate(frames):
            content.append(
                {
                    "type": "input_text",
                    "text": f"FRAME_INDEX={frame_index} timestampSec={frame.timestamp_sec:.3f}",
                }
            )
            content.append({"type": "input_image", "image_url": _image_input(frame), "detail": self.detail})
        payload = {
            "instructions": SYSTEM_PROMPT,
            "input": [{"role": "user", "content": content}],
            "reasoning": {"effort": "none"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "spotted_product_candidates",
                    "strict": True,
                    "schema": DETECTION_SCHEMA,
                }
            },
        }
        result = self.client.create_json(payload)
        raw_candidates = result.get("candidates")
        if not isinstance(raw_candidates, list):
            raise AnalysisError("Detection response is missing candidates")
        candidates: list[ProductCandidate] = []
        for index, raw in enumerate(raw_candidates, start=1):
            if not isinstance(raw, Mapping):
                raise AnalysisError("Detection candidate must be an object")
            normalized_appearances: list[dict[str, Any]] = []
            raw_appearances = raw.get("appearances")
            if isinstance(raw_appearances, list):
                for raw_appearance in raw_appearances:
                    if not isinstance(raw_appearance, Mapping):
                        continue
                    frame_index = raw_appearance.get("frameIndex")
                    if not isinstance(frame_index, int) or not 0 <= frame_index < len(frames):
                        continue
                    frame = frames[frame_index]
                    evidence = raw_appearance.get(
                        "evidence", "Visible in sampled frame"
                    )
                    for timestamp in (
                        frame.timestamp_sec,
                        *frame.similar_timestamps,
                    ):
                        normalized_appearances.append(
                            {
                                "startSec": timestamp,
                                "evidence": (
                                    evidence
                                    if timestamp == frame.timestamp_sec
                                    else f"{evidence}; repeated in a near-identical sampled scene"
                                ),
                                "boundingBox": raw_appearance.get("boundingBox"),
                                "thumbnailUrl": frame.thumbnail_url,
                                "sourcePath": frame.path,
                            }
                        )
            normalized_raw = dict(raw)
            normalized_raw["appearances"] = normalized_appearances
            candidate = ProductCandidate.from_dict(
                normalized_raw,
                fallback_id=f"batch-{batch_index}-candidate-{index}",
            )
            if candidate.confidence < self.min_confidence:
                continue
            if candidate.appearances:
                candidates.append(candidate)
        return candidates


def _captions_for_range(
    segments: Sequence[CaptionSegment], start_sec: float, end_sec: float
) -> str:
    relevant = [
        segment.text
        for segment in segments
        if segment.end_sec >= start_sec - 3 and segment.start_sec <= end_sec + 3
    ]
    return " ".join(dict.fromkeys(relevant))[:4_000]


def _select_candidates(
    candidates: Sequence[ProductCandidate],
    *,
    limit: int,
    allow_single_frame: bool = False,
) -> list[ProductCandidate]:
    """Prefer defensible physical-object tracks over one-frame guesses.

    A candidate must either recur in at least two sampled frames or carry real
    identifying evidence. This intentionally favors precision for shopping:
    missing an ambiguous background object is better than presenting a random
    retailer result as though it were the object in the video.
    """

    def has_identity(candidate: ProductCandidate) -> bool:
        return bool(
            (candidate.brand and candidate.model)
            or any(len(value.strip()) >= 3 for value in candidate.visible_text)
        )

    defensible = [
        candidate
        for candidate in candidates
        if len({appearance.start_sec for appearance in candidate.appearances}) >= 2
        or has_identity(candidate)
        or (
            allow_single_frame
            and candidate.confidence >= 0.82
            and any(
                appearance.bounding_box is not None
                for appearance in candidate.appearances
            )
        )
    ]

    def score(candidate: ProductCandidate) -> tuple[float, int, float]:
        sightings = len({appearance.start_sec for appearance in candidate.appearances})
        identity_bonus = 0.2 if candidate.brand and candidate.model else 0.08 if candidate.visible_text else 0.0
        return (
            candidate.confidence + min(sightings, 5) * 0.05 + identity_bonus,
            sightings,
            candidate.confidence,
        )

    return sorted(defensible, key=score, reverse=True)[: max(1, limit)]


def analyze_manifest(
    manifest: FrameManifest | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    event_callback: EventCallback | None = None,
    client: OpenAIResponsesClient | None = None,
) -> list[ProductCandidate]:
    """Convenience hook for the media worker after it writes ``manifest.json``."""
    return ProductAnalysisPipeline(client=client).analyze(manifest, event_callback=event_callback)


def analyze_and_enrich(
    manifest: FrameManifest | Mapping[str, Any] | Sequence[Mapping[str, Any]],
    *,
    event_callback: EventCallback | None = None,
    client: OpenAIResponsesClient | None = None,
) -> list[ProductFinding]:
    """Run the complete live AI pipeline and return frontend-ready findings."""
    # Local import keeps detection independently usable and avoids a module cycle.
    from processor.search import RetailerSearchService

    shared_client = client or OpenAIResponsesClient()
    candidates = ProductAnalysisPipeline(client=shared_client).analyze(
        manifest, event_callback=event_callback
    )
    return RetailerSearchService(client=shared_client).enrich_all(
        candidates, event_callback=event_callback
    )


def process_job(*, job_id: str, manifest_path: Path, store: Any) -> list[ProductFinding]:
    """Media-worker boundary: analyze a manifest, persist findings, and complete.

    ``store`` intentionally uses structural typing so the AI package does not
    import or couple itself to the storage implementation.
    """
    try:
        manifest_data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"Unable to read frame manifest: {exc}") from exc
    def emit(event_type: str, payload: dict[str, Any]) -> None:
        store.emit(job_id, event_type, **payload)

    findings = analyze_and_enrich(manifest_data, event_callback=emit)
    serialized = [finding.to_dict() for finding in findings]
    store.update(job_id, findings=serialized, error=None)
    store.emit(
        job_id,
        "completed",
        message="Product analysis completed",
        frameCount=len(manifest_data.get("frames", [])),
        findingCount=len(serialized),
        findings=serialized,
    )
    return findings
