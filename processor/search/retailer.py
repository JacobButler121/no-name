from __future__ import annotations

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
            "maxItems": 3,
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


SEARCH_INSTRUCTIONS = """You find purchasable product pages for objects detected in video.
Search the current web. Prefer the manufacturer's official product page, then established
retailers. Return direct public HTTPS product-detail pages, never search-result pages,
homepages, social posts, URL shorteners, marketplace search pages, or affiliate redirects.
An exact match requires corroborated brand AND model/variant evidence. If the visual
evidence lacks an exact model, return clearly labeled similar products instead. Do not
invent URLs, prices, availability, brand, or model. Return an empty matches array when
there is not enough evidence for a defensible result."""


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
            "visibleText": candidate.visible_text,
            "visualEvidence": evidence,
            "detectionConfidence": candidate.confidence,
        }
        payload = {
            "instructions": SEARCH_INSTRUCTIONS,
            "input": (
                "Search for up to three defensible product-page matches for this detected item. "
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
        for raw in raw_matches[:3]:
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
            if confidence < 0.5:
                continue
            if match_kind is MatchKind.EXACT and confidence < 0.8:
                match_kind = MatchKind.SIMILAR
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
        return sorted(verified, key=lambda item: item.confidence, reverse=True)

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
            if candidate.confidence >= 0.5
        ]


def enrich_candidates(
    candidates: Iterable[ProductCandidate],
    *,
    event_callback: EventCallback | None = None,
    client: OpenAIResponsesClient | None = None,
) -> list[ProductFinding]:
    """Convenience integration hook that returns frontend-ready findings."""
    return RetailerSearchService(client=client).enrich_all(candidates, event_callback=event_callback)
