"""Retail product discovery backed by OpenAI web search."""

from .retailer import RetailerSearchService, enrich_candidates
from .validation import ProductPageMetadata, validate_product_url

__all__ = ["ProductPageMetadata", "RetailerSearchService", "enrich_candidates", "validate_product_url"]
