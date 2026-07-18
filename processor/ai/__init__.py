"""Live multimodal product detection and deterministic candidate grouping."""

from .dedupe import deduplicate_candidates
from .pipeline import ProductAnalysisPipeline, analyze_and_enrich, analyze_manifest, process_job

__all__ = [
    "ProductAnalysisPipeline",
    "analyze_and_enrich",
    "analyze_manifest",
    "deduplicate_candidates",
    "process_job",
]
