from __future__ import annotations

import base64
import json
import mimetypes
import os
import re
import shutil
import subprocess
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping

from processor.ai.client import OpenAIResponsesClient
from processor.models import (
    AnalysisError,
    Appearance,
    MatchKind,
    ProductCandidate,
    ProductFinding,
    RetailMatch,
)

from .validation import ProductPageMetadata, validate_product_url
from .lens import GoogleLensSearchClient, LensCandidate, LensSearchOutcome


EventCallback = Callable[[str, dict[str, Any]], None]
MetadataFetcher = Callable[..., ProductPageMetadata | None]


SEARCH_SCHEMA: dict[str, Any] = {
    "type": "object",
    "properties": {
        "matches": {
            "type": "array",
            "maxItems": 8,
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
            "maxItems": 8,
            "items": {
                "type": "object",
                "properties": {
                    "index": {"type": "integer", "minimum": 0, "maximum": 7},
                    "verdict": {"type": "string", "enum": ["exact", "similar", "reject"]},
                    "confidence": {"type": "number", "minimum": 0, "maximum": 1},
                    "categoryMatch": {"type": "string", "enum": ["match", "mismatch", "unknown"]},
                    "shapeMatch": {"type": "string", "enum": ["match", "mismatch", "unknown"]},
                    "colorMatch": {"type": "string", "enum": ["match", "mismatch", "unknown"]},
                    "materialMatch": {"type": "string", "enum": ["match", "mismatch", "unknown"]},
                    "constructionMatch": {"type": "string", "enum": ["match", "mismatch", "unknown"]},
                    "identityEvidence": {"type": "boolean"},
                    "contradictions": {"type": "array", "items": {"type": "string"}},
                    "evidence": {"type": "string"},
                },
                "required": [
                    "index",
                    "verdict",
                    "confidence",
                    "categoryMatch",
                    "shapeMatch",
                    "colorMatch",
                    "materialMatch",
                    "constructionMatch",
                    "identityEvidence",
                    "contradictions",
                    "evidence",
                ],
                "additionalProperties": False,
            },
        }
    },
    "required": ["comparisons"],
    "additionalProperties": False,
}


SEARCH_INSTRUCTIONS = """You are an image-first shopping candidate researcher. The user
supplies up to three crops of the SAME physical object from a video plus a structured
visual fingerprint. Inspect the images before searching. Use observed silhouette,
proportions, geometry, color, material, hardware, visible text, transcript clues, and
source context to formulate several precise web searches. Search the current web and
return a diverse candidate pool, not repeated variants of one weak guess.

Prefer the manufacturer's official product page, then established retailers. Return
direct public HTTPS product-detail pages, never search-result pages, homepages, social
posts, URL shorteners, marketplace search pages, or affiliate redirects. An exact match
requires corroborated brand AND model/variant evidence. Otherwise label the candidate
similar. Do not invent URLs, prices, availability, brand, or model. Category alone is
never enough. Return an empty matches array when no visually defensible candidates can
be found. Candidate confidence is only discovery confidence; final visual verification
happens separately."""


VISUAL_MATCH_INSTRUCTIONS = """You are the final product-match verifier. The first group
contains multiple video crops of ONE tracked physical object. Compare that object against
each numbered retailer image. Ignore people and remaining room context.

Judge category, silhouette/shape, color, material, and construction independently. Mark
an axis unknown when the video cannot support it; never force a match. A clear mismatch
in category, dominant color, material, silhouette, or construction is a hard
contradiction and requires reject. Similar requires category and shape agreement plus
at least one additional concrete attribute, with no hard contradiction. Exact requires
near-identical geometry and details plus corroborating logo, visible text, brand/model,
or unmistakable variant evidence. A shared category, generic shape, or color alone is
insufficient. Confidence is confidence that the RETAILER PRODUCT matches the tracked
object, not confidence that the object category was detected. Be conservative."""


POSSIBLE_VISUAL_CONFIDENCE = 0.68
VERIFIED_VISUAL_CONFIDENCE = 0.82


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


def _cropped_frame_path(path_value: str, bounding_box: Any) -> Path | None:
    """Return a padded crop of the detected object, falling back to the frame.

    The crop is written inside the temporary job directory, so normal job cleanup
    removes it along with the sampled frames.
    """

    if bounding_box is None:
        source = Path(path_value)
        return source if source.is_file() else None
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
        return source

    crop_dir = source.parent / "crops"
    crop_name = (
        f"{source.stem}-{round(x * 10000)}-{round(y * 10000)}-"
        f"{round(width * 10000)}-{round(height * 10000)}.jpg"
    )
    target = crop_dir / crop_name
    if not target.is_file():
        ffmpeg = os.getenv("FFMPEG_PATH") or shutil.which("ffmpeg")
        if not ffmpeg:
            return source
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
                    (
                        f"crop=iw*{width:.6f}:ih*{height:.6f}:iw*{x:.6f}:ih*{y:.6f},"
                        "scale=w='max(iw\\,512)':h=-2:flags=lanczos"
                    ),
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
            return source
    return target


def _cropped_frame_data_url(path_value: str, bounding_box: Any) -> str | None:
    path = _cropped_frame_path(path_value, bounding_box)
    return _frame_data_url(str(path)) if path else None


def _emit(callback: EventCallback | None, event_type: str, payload: dict[str, Any]) -> None:
    if callback:
        callback(event_type, payload)


def _is_inaccessible_image_error(exc: AnalysisError) -> bool:
    """Identify Responses API failures caused by a retailer blocking its image URL."""
    message = str(exc).casefold()
    return (
        "error while downloading file" in message
        and ("upstream status code: 403" in message or '"param": "url"' in message)
    )


class RetailerSearchService:
    def __init__(
        self,
        *,
        client: OpenAIResponsesClient | None = None,
        metadata_fetcher: MetadataFetcher = validate_product_url,
        candidate_limit: int = 8,
        target_crop_limit: int = 3,
        match_model: str | None = None,
        image_detail: str | None = None,
        lens_client: GoogleLensSearchClient | None = None,
    ) -> None:
        if not 1 <= candidate_limit <= 8:
            raise ValueError("candidate_limit must be between 1 and 8")
        if not 1 <= target_crop_limit <= 3:
            raise ValueError("target_crop_limit must be between 1 and 3")
        self.client = client or OpenAIResponsesClient()
        self.metadata_fetcher = metadata_fetcher
        self.candidate_limit = candidate_limit
        self.target_crop_limit = target_crop_limit
        self.match_model = match_model or os.getenv(
            "SPOTTED_MATCH_MODEL", "gpt-5.6-terra"
        )
        self.image_detail = image_detail or os.getenv(
            "SPOTTED_MATCH_IMAGE_DETAIL", "original"
        )
        self.lens_client = lens_client or GoogleLensSearchClient()
        if self.image_detail not in {"high", "original"}:
            raise ValueError("image_detail must be high or original")

    def _target_crops(
        self, candidate: ProductCandidate
    ) -> list[tuple[Appearance, str]]:
        """Return a few high-signal, non-duplicate views of one tracked object."""
        ranked = sorted(
            (
                item
                for item in candidate.appearances
                if item.source_path
                and (
                    item.bounding_box is not None
                    or (candidate.brand and candidate.model)
                )
            ),
            key=lambda item: (
                item.bounding_box.width * item.bounding_box.height
                if item.bounding_box
                else 0.0,
                len(item.evidence),
            ),
            reverse=True,
        )
        crops: list[tuple[Appearance, str]] = []
        seen: set[tuple[str, str]] = set()
        for appearance in ranked:
            box_key = (
                json.dumps(appearance.bounding_box.to_dict(), sort_keys=True)
                if appearance.bounding_box
                else "full-frame"
            )
            key = (appearance.source_path or "", box_key)
            if key in seen:
                continue
            seen.add(key)
            image = _cropped_frame_data_url(
                appearance.source_path or "", appearance.bounding_box
            )
            if image:
                crops.append((appearance, image))
            if len(crops) >= self.target_crop_limit:
                break
        return crops

    def _lens_candidates(
        self,
        candidate: ProductCandidate,
        target_crops: list[tuple[Appearance, str]],
        diagnostics: dict[str, Any],
    ) -> list[LensCandidate]:
        """Run one reverse-image lookup for the strongest crop, never per frame."""
        if not target_crops:
            diagnostics["reasons"].append({
                "stage": "crop_selection",
                "status": "empty",
                "code": "no_target_crops",
                "message": "No usable target crop was available for reverse-image search.",
                "retryable": False,
            })
            return []
        search_crop_diagnostic = getattr(
            self.lens_client, "search_crop_with_diagnostics", None
        )
        if not self.lens_client.enabled and not callable(search_crop_diagnostic):
            diagnostics["reasons"].append({
                "stage": "lens_configuration",
                "status": "disabled",
                "code": "lens_disabled",
                "message": "Google Lens retrieval is not fully configured.",
                "retryable": False,
            })
            return []
        appearance = target_crops[0][0]
        crop_path = _cropped_frame_path(
            appearance.source_path or "", appearance.bounding_box
        )
        if not crop_path:
            diagnostics["reasons"].append({
                "stage": "crop_selection",
                "status": "error",
                "code": "crop_generation_failed",
                "message": "The selected target crop could not be generated.",
                "retryable": False,
            })
            return []
        query = " ".join(
            value
            for value in (
                candidate.brand,
                candidate.model,
                candidate.color,
                candidate.material,
                candidate.name,
                candidate.source_channel,
                candidate.source_title,
                candidate.search_focus,
            )
            if value
        )
        if callable(search_crop_diagnostic):
            relay_outcome = search_crop_diagnostic(
                crop_path,
                query=query,
                limit=self.candidate_limit,
            )
            if isinstance(relay_outcome, LensSearchOutcome):
                diagnostics["reasons"].extend(
                    item.to_dict() for item in relay_outcome.diagnostics
                )
                if relay_outcome.candidates:
                    return list(relay_outcome.candidates)
                # A successful empty Lens response should not be repeated using
                # the same image. Transport/API failures may use the legacy
                # public job URL as a best-effort fallback.
                diagnostic_codes = {
                    item.code for item in relay_outcome.diagnostics
                }
                if relay_outcome.status == "empty" or "missing_api_key" in diagnostic_codes:
                    return []
        else:
            search_crop = getattr(self.lens_client, "search_crop", None)
            if callable(search_crop):
                relay_results = search_crop(
                    crop_path,
                    query=query,
                    limit=self.candidate_limit,
                )
                if relay_results or getattr(self.lens_client, "relay_enabled", False):
                    return relay_results

        if crop_path.parent.name == "crops":
            public_url = self.lens_client.public_crop_url(
                appearance.thumbnail_url, crop_path.name
            )
        else:
            public_url = self.lens_client.public_frame_url(
                appearance.thumbnail_url
            )
        if not public_url:
            diagnostics["reasons"].append({
                "stage": "public_image_url",
                "status": "error",
                "code": "public_url_unavailable",
                "message": "No public crop URL was available for Google Lens.",
                "retryable": False,
            })
            return []
        search_diagnostic = getattr(self.lens_client, "search_with_diagnostics", None)
        if callable(search_diagnostic):
            outcome = search_diagnostic(
                public_url, query=query, limit=self.candidate_limit
            )
            if isinstance(outcome, LensSearchOutcome):
                diagnostics["reasons"].extend(
                    item.to_dict() for item in outcome.diagnostics
                )
                return list(outcome.candidates)
        return self.lens_client.search(public_url, query=query, limit=self.candidate_limit)

    def search(
        self,
        candidate: ProductCandidate,
        *,
        diagnostics: dict[str, Any] | None = None,
    ) -> list[RetailMatch]:
        report = diagnostics if diagnostics is not None else {}
        report.update({
            "candidateId": candidate.id,
            "counts": {
                "targetCrops": 0,
                "lensCandidates": 0,
                "webCandidates": 0,
                "validRetailerPages": 0,
                "visualCandidates": 0,
                "possibleMatches": 0,
                "verifiedMatches": 0,
            },
            "reasons": [],
        })
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
            "sourceContext": {
                "title": candidate.source_title,
                "channel": candidate.source_channel,
                "platform": candidate.source_platform,
                "url": candidate.source_url,
                "searchFocus": candidate.search_focus,
            },
        }
        target_crops = self._target_crops(candidate)
        report["counts"]["targetCrops"] = len(target_crops)
        lens_candidates = self._lens_candidates(candidate, target_crops, report)
        report["counts"]["lensCandidates"] = len(lens_candidates)
        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    f"Find up to {self.candidate_limit} distinct, defensible product-page candidates. "
                    "Inspect all target crops before searching. Preserve visual contradictions for the final verifier.\n"
                    f"TARGET FINGERPRINT: {json.dumps(query_context, ensure_ascii=True, sort_keys=True)}\n"
                    "Google Lens candidates are visual retrieval leads, not verified matches. "
                    "Check their product pages and reject accessories, category neighbors, and visual contradictions.\n"
                    f"LENS CANDIDATES: {json.dumps([item.to_prompt_dict() for item in lens_candidates], ensure_ascii=True)}"
                ),
            }
        ]
        for index, (appearance, image) in enumerate(target_crops, start=1):
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": f"TARGET CROP {index} at {appearance.start_sec:.3f}s:",
                    },
                    {
                        "type": "input_image",
                        "image_url": image,
                        "detail": self.image_detail,
                    },
                ]
            )
        payload = {
            "model": self.match_model,
            "instructions": SEARCH_INSTRUCTIONS,
            "input": [{"role": "user", "content": content}],
            "tools": [{"type": "web_search", "search_context_size": "medium"}],
            "tool_choice": "required",
            "reasoning": {"effort": "low"},
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
        raw_items = [
            raw
            for raw in raw_matches[: self.candidate_limit]
            if isinstance(raw, Mapping)
        ]
        report["counts"]["webCandidates"] = len(raw_items)
        if not raw_items:
            report["reasons"].append({
                "stage": "web_search",
                "status": "empty",
                "code": "no_web_candidates",
                "message": "Shopping web search returned no candidate product pages.",
                "retryable": False,
            })
        expected_terms = [
            value
            for value in (
                candidate.brand,
                candidate.model,
                candidate.name,
                candidate.category,
            )
            if value
        ]

        def fetch_metadata(raw: Mapping[str, Any]) -> ProductPageMetadata | None:
            try:
                return self.metadata_fetcher(
                    str(raw.get("productUrl", "")).strip(),
                    expected_terms=expected_terms,
                )
            except (OSError, ValueError, TypeError):
                return None

        if raw_items:
            with ThreadPoolExecutor(
                max_workers=min(4, len(raw_items)),
                thread_name_prefix="spotted-retailer-check",
            ) as executor:
                metadata_items = list(executor.map(fetch_metadata, raw_items))
        else:
            metadata_items = []
        verified: list[RetailMatch] = []
        valid_page_count = 0
        invalid_page_count = 0
        low_confidence_count = 0
        for raw, metadata in zip(raw_items, metadata_items, strict=True):
            if metadata is None:
                invalid_page_count += 1
                continue
            valid_page_count += 1
            requested_kind = MatchKind(str(raw.get("matchKind", "similar")))
            # An exact SKU claim is never permitted without both brand and model evidence.
            match_kind = requested_kind if candidate.brand and candidate.model else MatchKind.SIMILAR
            confidence = max(0.0, min(1.0, float(raw.get("confidence", 0))))
            if confidence < 0.5:
                low_confidence_count += 1
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
            if match_kind is MatchKind.SIMILAR and not metadata.image_url:
                overlap = observed_tokens & page_tokens
                if not observed_tokens or not overlap:
                    invalid_page_count += 1
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
        report["counts"]["validRetailerPages"] = valid_page_count
        if invalid_page_count:
            report["reasons"].append({
                "stage": "retailer_validation",
                "status": "rejected",
                "code": "invalid_retailer_pages",
                "message": f"{invalid_page_count} candidate page(s) failed retailer-page validation.",
                "retryable": False,
            })
        if low_confidence_count:
            report["reasons"].append({
                "stage": "candidate_filtering",
                "status": "rejected",
                "code": "low_discovery_confidence",
                "message": f"{low_confidence_count} candidate(s) had insufficient discovery confidence.",
                "retryable": False,
            })
        ranked = sorted(verified, key=lambda item: item.confidence, reverse=True)
        report["counts"]["visualCandidates"] = sum(
            bool(item.image_url) for item in ranked[: self.candidate_limit]
        )
        accepted = self._visually_verify(
            candidate,
            ranked[: self.candidate_limit],
            target_crops=target_crops,
        )
        report["counts"]["possibleMatches"] = sum(
            item.match_kind is MatchKind.POSSIBLE for item in accepted
        )
        report["counts"]["verifiedMatches"] = sum(
            item.match_kind is not MatchKind.POSSIBLE for item in accepted
        )
        if verified and not accepted:
            report["reasons"].append({
                "stage": "visual_verification",
                "status": "rejected",
                "code": "no_visual_match",
                "message": "No validated retailer candidate passed visual verification.",
                "retryable": False,
            })
        return accepted

    def _visually_verify(
        self,
        candidate: ProductCandidate,
        matches: list[RetailMatch],
        *,
        target_crops: list[tuple[Appearance, str]] | None = None,
    ) -> list[RetailMatch]:
        if not matches:
            return []
        crops = target_crops if target_crops is not None else self._target_crops(candidate)
        comparable = [match for match in matches if match.image_url]
        if not crops or not comparable:
            # Strong text identity can still support an exact SKU. Similar-style
            # shopping results must pass the image-to-image comparison.
            return [match for match in matches if match.match_kind is MatchKind.EXACT]

        content: list[dict[str, Any]] = [
            {
                "type": "input_text",
                "text": (
                    f"Target detection: {candidate.name}; category={candidate.category}; "
                    f"color={candidate.color}; material={candidate.material}; "
                    f"brand={candidate.brand}; model={candidate.model}; "
                    f"visibleText={candidate.visible_text}; "
                    f"visualDescription={candidate.visual_description}. "
                    "All TARGET CROP images show the same tracked physical object."
                ),
            },
        ]
        for index, (appearance, image) in enumerate(crops, start=1):
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": f"TARGET CROP {index} at {appearance.start_sec:.3f}s:",
                    },
                    {
                        "type": "input_image",
                        "image_url": image,
                        "detail": self.image_detail,
                    },
                ]
            )
        for index, match in enumerate(comparable):
            content.extend(
                [
                    {
                        "type": "input_text",
                        "text": f"RETAILER IMAGE {index}: {match.product_name}",
                    },
                    {
                        "type": "input_image",
                        "image_url": match.image_url,
                        "detail": "high",
                    },
                ]
            )
        payload = {
            "model": self.match_model,
            "instructions": VISUAL_MATCH_INSTRUCTIONS,
            "input": [{"role": "user", "content": content}],
            "reasoning": {"effort": "low"},
            "text": {
                "format": {
                    "type": "json_schema",
                    "name": "spotted_visual_match_verification",
                    "strict": True,
                    "schema": VISUAL_MATCH_SCHEMA,
                }
            },
        }
        try:
            result = self.client.create_json(payload)
        except AnalysisError as exc:
            if not _is_inaccessible_image_error(exc):
                raise
            # A single hotlink-protected merchant image makes the Responses API
            # reject the whole multimodal request. Retry candidates separately so
            # only the inaccessible image is discarded, not the entire scan.
            if len(comparable) == 1:
                return []
            accepted: list[RetailMatch] = []
            for match in comparable:
                accepted.extend(
                    self._visually_verify(
                        candidate,
                        [match],
                        target_crops=crops,
                    )
                )
            return sorted(accepted, key=lambda item: item.confidence, reverse=True)
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
            axes = {
                name: str(comparison.get(name, "unknown"))
                for name in (
                    "categoryMatch",
                    "shapeMatch",
                    "colorMatch",
                    "materialMatch",
                    "constructionMatch",
                )
            }
            contradictions = comparison.get("contradictions")
            hard_mismatch = any(value == "mismatch" for value in axes.values())
            match_count = sum(value == "match" for value in axes.values())
            similar_supported = (
                axes["categoryMatch"] == "match"
                and axes["shapeMatch"] == "match"
                and match_count >= 3
            )
            possible_supported = (
                axes["categoryMatch"] == "match"
                and axes["shapeMatch"] == "match"
            )
            combined_confidence = min(
                0.99, 0.8 * visual_confidence + 0.2 * match.confidence
            )
            if (
                verdict == "reject"
                or visual_confidence < POSSIBLE_VISUAL_CONFIDENCE
                or combined_confidence < POSSIBLE_VISUAL_CONFIDENCE
                or hard_mismatch
                or not possible_supported
            ):
                continue
            verified_supported = (
                visual_confidence >= VERIFIED_VISUAL_CONFIDENCE
                and similar_supported
            )
            exact_supported = (
                verdict == "exact"
                and match.match_kind is MatchKind.EXACT
                and bool(comparison.get("identityEvidence"))
                and visual_confidence >= 0.92
                and match_count == len(axes)
            )
            match_kind = (
                MatchKind.EXACT
                if exact_supported
                else MatchKind.SIMILAR
                if verified_supported
                else MatchKind.POSSIBLE
            )
            final_confidence = (
                min(VERIFIED_VISUAL_CONFIDENCE - 0.001, combined_confidence)
                if match_kind is MatchKind.POSSIBLE
                else combined_confidence
            )
            contradiction_note = (
                f"; contradictions reviewed: {', '.join(str(item) for item in contradictions)}"
                if isinstance(contradictions, list) and contradictions
                else ""
            )
            accepted.append(
                RetailMatch(
                    product_name=match.product_name,
                    retailer_name=match.retailer_name,
                    product_url=match.product_url,
                    match_kind=match_kind,
                    confidence=final_confidence,
                    evidence=(
                        f"{match.evidence}; visual check: {comparison.get('evidence', '')}"
                        f"{contradiction_note}"
                    ),
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
        diagnostics: dict[str, Any] = {}
        matches = self.search(candidate, diagnostics=diagnostics)
        _emit(event_callback, "retailer_search_diagnostics", diagnostics)
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
