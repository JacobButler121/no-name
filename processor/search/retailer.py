from __future__ import annotations

import base64
import mimetypes
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from processor.ai.client import OpenAIResponsesClient
from processor.models import (
    AnalysisError,
    MatchKind,
    ProductCandidate,
    ProductFinding,
    RetailMatch,
)

from .validation import ProductPageMetadata, validate_product_url


EventCallback = Callable[[str, dict[str, Any]], None]
MetadataFetcher = Callable[..., ProductPageMetadata | None]


SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "productName": {"type": "string"},
                    "retailerName": {"type": "string"},
                    "productUrl": {"type": "string"},
                    "matchKind": {"type": "string", "enum": ["exact", "similar"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string"},
                },
                "required": ["productName", "retailerName", "productUrl", "matchKind", "confidence", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["matches"],
    "additionalProperties": False,
}


VISUAL_MATCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "comparisons": {
            "type": "array",
            "maxItems": 2,
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0, "maximum": 1},
                    "verdict": {"type": "string", "enum": ["exact", "similar", "reject"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "evidence": {"type": "string"},
                },
                "required": ["index", "verdict", "confidence", "evidence"],
                "additionalProperties": False,
            },
        }
    },
    "required": ["comparisons"],
    "additionalProperties": False,
}


SEARCH_INSTRUCTIONS = """You find purchasable product pages for objects detected in video.
Search the current web. Prefer the manufacturer's official product page, then established
retailers. Return direct public HTTPS product-detail pages, never search-result pages,
homepages, social posts, URL shorteners, marketplace search pages, or affiliate redirects.
An exact match requires corroborated brand AND model/variant evidence. If the visual
evidence lacks an exact model, return clearly labeled similar products instead. Do not
invent URLs, prices, availability, brand, or model. Return an empty matches array when
there is not enough evidence for a defensible result. When identity evidence is weak,
return at most one visually similar item whose shape, proportions, color, material, and
construction agree with the observations. Category alone is never enough."""


VISUAL_MATCH_INSTRUCTIONS = """You are the final product-match verifier. Compare the
target object in the first, padded video crop with each numbered retailer product image.
Ignore any remaining surrounding room and compare only the target object.
An exact verdict requires the same distinctive construction, proportions, materials,
hardware, colorway, and identity evidence. Similar requires multiple concrete visual
attributes in common. A shared category or color alone is insufficient. Reject when the
object is too small, obscured, generic, or materially different. Be conservative."""


_TOKEN_NOISE = {
    "lamp", "lamps", "light", "lighting", "table", "desk", "floor", "wall",
    "ceiling", "pendant", "sconce", "chandelier", "product", "item", "with",
    "and", "the", "for", "inch", "inches", "modern", "style",
}


def _tokens(*values: str | None) -> set[str]:
    return {
        token
        for value in values
        if value
        for token in re.findall(r"[a-z0-9]+", value.casefold())
        if len(token) >= 3 and token not in _TOKEN_NOISE
    }


def _frame_data_url(path_value: str) -> str | None:
    path = Path(path_value)
    if not path.is_file():
        return None
    mime_type = mimetypes.guess_type(path.name)[0] or "image/jpeg"
    return f"data:{mime_type};base64,{base64.b64encode(path.read_bytes()).decode('ascii')}"


def _cropped_frame_data_url(path_value: str, bounding_box: Any) -> str | None:
    """Return a padded crop of the detected object, falling back to the frame.

    The crop is written inside the temporary job directory, so normal job cleanup
    removes it along with the sampled frames.
    """

    if bounding_box is None:
        return _frame_data_url(path_value)
    source = Path(path_value)
    if not source.is_file():
        return None
    box = bounding_box.to_dict()
    padding = 0.15
    x = max(0.0, box["x"] - box["width"] * padding)
    y = max(0.0, box["y"] - box["height"] * padding)
    width = min(1.0 - x, box["width"] * (1 + padding * 2))
    height = min(1.0 - y, box["height"] * (1 + padding * 2))
    if width <= 0.01 or height <= 0.01:
        return _frame_data_url(path_value)

    crop_dir = source.parent / "crops"
    crop_name = (
        f"{source.stem}-{round(x * 10000)}-{round(y * 10000)}-"
        f"{round(width * 10000)}-{round(height * 10000)}.jpg"
    )
    target = crop_dir / crop_name
    if not target.is_file():
        ffmpeg = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
        if not ffmpeg:
            return _frame_data_url(path_value)
        try:
            crop_dir.mkdir(parents=True, exist_ok=True)
            subprocess.run(
                [
                    ffmpeg,
                    "-hide_banner",
                    "-loglevel",
                    "error",
                    "-y",
                    "-i",
                    str(source),
                    "-vf",
                    f"crop=iw*{width:.6f}:ih*{height:.6f}:iw*{x:.6f}:ih*{y:.6f}",
                    "-frames:v",
                    "1",
                    "-q:v",
                    "2",
                    str(target),
                ],
                check=True,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                timeout=20,
            )
        except (OSError, subprocess.SubprocessError):
            target.unlink(missing_ok=True)
            return _frame_data_url(path_value)
    return _frame_data_url(str(target))


def _emit(callback: EventCallback | None, event_type: str, payload: dict[str, Any]) -> None:
    if callback:
        callback(event_type, payload)


class RetailerSearchService:
    def __init__(
        self,
        *,
        client: OpenAIResponsesClient | None = None,
        metadata_fetcher: MetadataFetcher = validate_product_url,
    ) -> None:
        self.client = client or OpenAIResponsesClient()
        self.metadata_fetcher = metadata_fetcher

    def search(self, candidate: ProductCandidate) -> list[RetailMatch]:
        evidence = "; ".join(
            appearance.evidence for appearance in candidate.appearances[:5] if appearance.evidence
        )
        query_context = {
            "detectedName": candidate.name,
            "category": candidate.category,
            "brand": candidate.brand,
            "model": candidate.model,
            "color": candidate.color,
            "material": candidate.material,
            "visualDescription": candidate.visual_description,
            "visibleText": candidate.visible_text,
            "visualEvidence": evidence,
            "detectionConfidence": candidate.confidence,
        }
        payload = {
            "instructions": SEARCH_INSTRUCTIONS,
            "input": (
                "Search for up to two defensible product-page matches for this detected item. "
                f"Detection evidence: {query_context}"
            ),
            "tools": [{"type": "web_search", "search_context_size": "medium"}],
            "tool_choice": "required",
            "reasoning": {"effort": "none"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "spotted_retail_matches",
                    "strict": True,
                    "schema": SEARCH_SCHEMA,
                }
            },
        }
        result = self.client.create_json(payload)
        raw_matches = result.get("matches")
        if not isinstance(raw_matches, list):
            raise AnalysisError("Retail search response is missing matches")
        verified: list[RetailMatch] = []
        for raw in raw_matches[:2]:
            if not isinstance(raw, Mapping):
                continue
            url = str(raw.get("productUrl", "")).strip()
            expected_terms = [
                value
                for value in (candidate.brand, candidate.model, candidate.name, str(raw.get("productName", "")))
                if value
            ]
            metadata = self.metadata_fetcher(url, expected_terms=expected_terms)
            if metadata is None:
                continue
            requested_kind = MatchKind(str(raw.get("matchKind", "similar")))
            # An exact SKU claim is never permitted without both brand and model evidence.
            match_kind = requested_kind if candidate.brand and candidate.model else MatchKind.SIMILAR
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
            if confidence < 0.75:
                continue
            page_tokens = _tokens(metadata.title, str(raw.get("productName", "")))
            identity_tokens = _tokens(candidate.brand, candidate.model)
            observed_tokens = _tokens(
                candidate.name,
                candidate.color,
                candidate.material,
                candidate.visual_description,
                *candidate.visible_text,
            )
            if match_kind is MatchKind.EXACT and (
                confidence < 0.85 or not identity_tokens or not identity_tokens.issubset(page_tokens)
            ):
                match_kind = MatchKind.SIMILAR
            if match_kind is MatchKind.SIMILAR:
                overlap = observed_tokens & page_tokens
                if not observed_tokens or not overlap:
                    continue
            verified.append(
                RetailMatch(
                    product_name=str(raw.get("productName") or metadata.title),
                    retailer_name=str(raw.get("retailerName") or "Retailer"),
                    product_url=metadata.url,
                    match_kind=match_kind,
                    confidence=confidence,
                    evidence=str(raw.get("evidence", "Verified product page")),
                    image_url=metadata.image_url,
                    price=metadata.price,
                )
            )
        ranked = sorted(verified, key=lambda item: item.confidence, reverse=True)
        return self._visually_verify(candidate, ranked[:2])

    def _visually_verify(
        self, candidate: ProductCandidate, matches: list[RetailMatch]
    ) -> list[RetailMatch]:
        if not matches:
            return []
        appearance = max(
            (item for item in candidate.appearances if item.source_path),
            key=lambda item: (
                item.bounding_box.width * item.bounding_box.height
                if item.bounding_box
                else 0.0
            ),
            default=None,
        )
        frame_url = (
            _cropped_frame_data_url(
                appearance.source_path, appearance.bounding_box
            )
            if appearance and appearance.source_path
            else None
        )
        comparable = [match for match in matches if match.image_url]
        if not frame_url or not comparable:
            # Strong text identity can still support an exact SKU. Similar-style
            # shopping results must pass the image-to-image comparison.
            return [match for match in matches if match.match_kind is MatchKind.EXACT]

        box = appearance.bounding_box.to_dict() if appearance and appearance.bounding_box else None
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    f"Target detection: {candidate.name}; category={candidate.category}; "
                    f"color={candidate.color}; material={candidate.material}; "
                    f"visualDescription={candidate.visual_description}; boundingBox={box}; "
                    f"evidence={appearance.evidence if appearance else ''}. "
                    "The supplied video image is a padded crop centered on the target object."
                ),
            },
            {"type": "input_text", "text": "VIDEO CROP (target object):"},
            {"type": "input_image", "image_url": frame_url, "detail": "high"},
        ]
        for index, match in enumerate(comparable):
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": f"RETAILER IMAGE {index}: {match.product_name}",
                    },
                    {"type": "input_image", "image_url": match.image_url, "detail": "high"},
                ]
            )
        payload = {
            "instructions": VISUAL_MATCH_INSTRUCTIONS,
            "input": [{"role": "user", "content": content}],
            "reasoning": {"effort": "none"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "spotted_visual_match_verification",
                    "strict": True,
                    "schema": VISUAL_MATCH_SCHEMA,
                }
            },
        }
        result = self.client.create_json(payload)
        raw_comparisons = result.get("comparisons")
        if not isinstance(raw_comparisons, list):
            return []
        comparisons = {
            int(item["index"]): item
            for item in raw_comparisons
            if isinstance(item, Mapping) and isinstance(item.get("index"), int)
        }
        accepted: list[RetailMatch] = []
        for index, match in enumerate(comparable):
            comparison = comparisons.get(index)
            if not comparison:
                continue
            visual_confidence = max(
                0.0, min(1.0, float(comparison.get("confidence", 0)))
            )
            verdict = str(comparison.get("verdict", "reject"))
            if verdict == "reject" or visual_confidence < 0.75:
                continue
            match_kind = (
                MatchKind.EXACT
                if verdict == "exact" and match.match_kind is MatchKind.EXACT
                else MatchKind.SIMILAR
            )
            accepted.append(
                RetailMatch(
                    product_name=match.product_name,
                    retailer_name=match.retailer_name,
                    product_url=match.product_url,
                    match_kind=match_kind,
                    confidence=min(match.confidence, visual_confidence),
                    evidence=f"{match.evidence}; visual check: {comparison.get('evidence', '')}",
                    image_url=match.image_url,
                    price=match.price,
                )
            )
        return sorted(accepted, key=lambda item: item.confidence, reverse=True)

    def enrich(
        self, candidate: ProductCandidate, *, event_callback: EventCallback | None = None
    ) -> ProductFinding:
        _emit(
            event_callback,
            "searching_retailers",
            {"candidateId": candidate.id, "name": candidate.name},
        )
        matches = self.search(candidate)
        primary = matches[0] if matches else None
        match_kind = primary.match_kind if primary else MatchKind.POSSIBLE
        finding = ProductFinding(
            id=candidate.id,
            name=primary.product_name if primary else candidate.name,
            category=candidate.category,
            match_kind=match_kind,
            confidence=min(candidate.confidence, primary.confidence) if primary else candidate.confidence,
            appearances=candidate.appearances,
            detection_confidence=candidate.confidence,
            match_confidence=primary.confidence if primary else None,
            brand=candidate.brand,
            model=candidate.model,
            retailer_name=primary.retailer_name if primary else None,
            product_url=primary.product_url if primary else None,
            image_url=primary.image_url if primary else None,
            price=primary.price if primary else None,
            alternatives=matches[1:],
        )
        _emit(event_callback, "product_ready", finding.to_dict())
        return finding

    def enrich_all(
        self,
        candidates: Iterable[ProductCandidate],
        *,
        event_callback: EventCallback | None = None,
    ) -> list[ProductFinding]:
        return [
            self.enrich(candidate, event_callback=event_callback)
            for candidate in candidates
            if candidate.confidence >= 0.7
        ]


def enrich_candidates(
    candidates: Iterable[ProductCandidate],
    *,
    event_callback: EventCallback | None = None,
    client: OpenAIResponsesClient | None = None,
) -> list[ProductFinding]:
    """Convenience integration hook that returns frontend-ready findings."""
    return RetailerSearchService(client=client).enrich_all(candidates, event_callback=event_callback)
