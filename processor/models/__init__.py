"""Shared data contracts for the Spotted processing pipeline."""

from .products import (
    AnalysisConfigurationError,
    AnalysisError,
    Appearance,
    BoundingBox,
    CaptionSegment,
    FrameManifest,
    FrameSample,
    MatchKind,
    ProductCandidate,
    ProductFinding,
    RetailMatch,
)

__all__ = [
    "AnalysisConfigurationError",
    "AnalysisError",
    "Appearance",
    "BoundingBox",
    "CaptionSegment",
    "FrameManifest",
    "FrameSample",
    "MatchKind",
    "ProductCandidate",
    "ProductFinding",
    "RetailMatch",
]
