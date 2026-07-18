"""Retail product discovery backed by visual retrieval and OpenAI verification."""

from .lens import GoogleLensSearchClient, LensCandidate
from .retailer import RetailerSearchService, enrich_candidates
from .validation import ProductPageMetadata, validate_product_url

__all__ = [
    "GoogleLensSearchClient",
    "LensCandidate",
    "ProductPageMetadata",
    "RetailerSearchService",
    "enrich_candidates",
    "validate_product_url",
]
