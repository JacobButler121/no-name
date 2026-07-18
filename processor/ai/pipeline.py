from __future__ import annotations

import base64
import json
import mimetypes
import re
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from processor.models import (
    AnalysisError,
    Appearance,
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
                    "visibleText": {"type": "array", "items": {"type": "string"}},
                    "instanceKey": {"type": ["string", "null"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "appearances": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "startSec": {"type": "number", "minimum": 0},
                                "endSec": {"type": ["number", "null"], "minimum": 0},
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
                            "required": ["startSec", "endSec", "evidence", "boundingBox"],
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
Identify physical consumer goods a viewer could reasonably shop for, including apparel,
accessories, electronics, furniture, decor, beauty, kitchenware, tools, toys, packaged
goods, and sports equipment. Consider centered products and identifiable background
objects. Never identify people, body parts, scenery, architecture, text overlays, or
digital-only content as products.

Only state a brand or model when supported by readable text, a distinctive design, or
the transcript. Use null rather than guessing. Use a useful generic name when the exact
SKU is unknown. Confidence measures identity certainty, not visual prominence. A
generic-but-certain category can have moderate confidence, but an exact SKU needs
specific evidence. Include normalized 0..1 bounding boxes where possible.

The same physical item across frames must use the same short instanceKey. Different
physical instances—even visually similar ones—must use different instanceKeys. Return
every appearance timestamp represented in this batch. Evidence must briefly describe
what is actually visible or spoken. Do not invent shopping links or prices."""


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


def _closest_frame(frames: Sequence[FrameSample], timestamp: float) -> FrameSample:
    return min(frames, key=lambda frame: abs(frame.timestamp_sec - timestamp))


class ProductAnalysisPipeline:
    def __init__(
        self,
        *,
        client: OpenAIResponsesClient | None = None,
        batch_size: int = 10,
        detail: str = "high",
    ) -> None:
        if not 1 <= batch_size <= 20:
            raise ValueError("batch_size must be between 1 and 20")
        if detail not in {"low", "high", "auto", "original"}:
            raise ValueError("detail must be low, high, auto, or original")
        self.client = client or OpenAIResponsesClient()
        self.batch_size = batch_size
        self.detail = detail

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
            batch_candidates = self._analyze_batch(frames, parsed.transcript, batch_index)
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
        _emit(event_callback, "merging_duplicates", {"candidateCount": len(candidates)})
        return deduplicate_candidates(candidates)

    def _analyze_batch(
        self, frames: Sequence[FrameSample], transcript: str | None, batch_index: int
    ) -> list[ProductCandidate]:
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    "Analyze this ordered frame batch. The timestamp written before each image is authoritative.\n"
                    + (f"Video transcript/captions (may span outside this batch):\n{transcript[:12000]}" if transcript else "No transcript is available.")
                ),
            }
        ]
        for frame in frames:
            content.append({"type": "input_text", "text": f"FRAME timestampSec={frame.timestamp_sec:.3f}"})
            content.append({"type": "input_image", "image_url": _image_input(frame), "detail": self.detail})
        payload = {
            "instructions": SYSTEM_PROMPT,
            "input": [{"role": "user", "content": content}],
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
            candidate = ProductCandidate.from_dict(raw, fallback_id=f"batch-{batch_index}-candidate-{index}")
            enriched: list[Appearance] = []
            for appearance in candidate.appearances:
                closest = _closest_frame(frames, appearance.start_sec)
                # Do not let a hallucinated timestamp escape far outside the supplied batch.
                if abs(closest.timestamp_sec - appearance.start_sec) > max(5.0, frames[-1].timestamp_sec - frames[0].timestamp_sec):
                    continue
                enriched.append(
                    Appearance(
                        start_sec=closest.timestamp_sec,
                        end_sec=appearance.end_sec,
                        thumbnail_url=appearance.thumbnail_url or closest.thumbnail_url,
                        bounding_box=appearance.bounding_box,
                        evidence=appearance.evidence,
                    )
                )
            candidate.appearances = enriched
            if candidate.appearances:
                candidates.append(candidate)
        return candidates


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


def _caption_text(paths: Sequence[str]) -> str | None:
    chunks: list[str] = []
    for raw_path in paths:
        path = Path(raw_path)
        if not path.is_file():
            continue
        text = path.read_text(encoding="utf-8", errors="replace")
        for line in text.splitlines():
            stripped = re.sub(r"<[^>]+>", "", line).strip()
            if (
                not stripped
                or stripped.upper() == "WEBVTT"
                or stripped.isdigit()
                or "-->" in stripped
                or stripped.startswith(("NOTE", "STYLE", "REGION"))
            ):
                continue
            chunks.append(stripped)
    compact = " ".join(dict.fromkeys(chunks))
    return compact[:12000] or None


def process_job(*, job_id: str, manifest_path: Path, store: Any) -> list[ProductFinding]:
    """Media-worker boundary: analyze a manifest, persist findings, and complete.

    ``store`` intentionally uses structural typing so the AI package does not
    import or couple itself to the storage implementation.
    """
    try:
        manifest_data = json.loads(Path(manifest_path).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise AnalysisError(f"Unable to read frame manifest: {exc}") from exc
    job = store.get(job_id)
    if not manifest_data.get("transcript"):
        captions = job.metadata.get("captions", []) if isinstance(job.metadata, Mapping) else []
        transcript = _caption_text([str(item) for item in captions])
        if transcript:
            manifest_data["transcript"] = transcript

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
