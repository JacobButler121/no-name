from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Mapping, Sequence


class AnalysisError(RuntimeError):
    """Base exception for product analysis failures."""


class AnalysisConfigurationError(AnalysisError):
    """Raised when a live AI operation is attempted without configuration."""


class MatchKind(str, Enum):
    EXACT = "exact"
    SIMILAR = "similar"
    POSSIBLE = "possible"


def _value(data: Mapping[str, Any], *names: str, default: Any = None) -> Any:
    for name in names:
        if name in data:
            return data[name]
    return default


def _bounded_float(value: Any, low: float, high: float, name: str) -> float:
    try:
        parsed = float(value)
    except (TypeError, ValueError) as exc:
        raise ValueError(f"{name} must be a number") from exc
    if not low <= parsed <= high:
        raise ValueError(f"{name} must be between {low} and {high}")
    return parsed


@dataclass(frozen=True)
class BoundingBox:
    x: float
    y: float
    width: float
    height: float

    def __post_init__(self) -> None:
        for name in ("x", "y", "width", "height"):
            _bounded_float(getattr(self, name), 0.0, 1.0, name)
        if self.x + self.width > 1.001 or self.y + self.height > 1.001:
            raise ValueError("bounding box must fit within normalized frame bounds")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | None) -> BoundingBox | None:
        if not data:
            return None
        return cls(x=float(data["x"]), y=float(data["y"]), width=float(data["width"]), height=float(data["height"]))

    def to_dict(self) -> dict[str, float]:
        return {"x": self.x, "y": self.y, "width": self.width, "height": self.height}


@dataclass(frozen=True)
class FrameSample:
    timestamp_sec: float
    path: str | None = None
    image_url: str | None = None
    thumbnail_url: str | None = None
    width: int | None = None
    height: int | None = None

    def __post_init__(self) -> None:
        if self.timestamp_sec < 0:
            raise ValueError("timestamp_sec cannot be negative")
        if not self.path and not self.image_url:
            raise ValueError("frame requires path or image_url")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> FrameSample:
        return cls(
            timestamp_sec=float(_value(data, "timestampSec", "timestamp_sec", "timestamp", default=0)),
            path=_value(data, "path", "imagePath", "image_path"),
            image_url=_value(data, "imageUrl", "image_url", "url"),
            thumbnail_url=_value(data, "thumbnailUrl", "thumbnail_url"),
            width=_value(data, "width"),
            height=_value(data, "height"),
        )

    @property
    def source(self) -> str:
        return self.image_url or self.path or ""

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"timestampSec": self.timestamp_sec}
        if self.path:
            payload["path"] = self.path
        if self.image_url:
            payload["imageUrl"] = self.image_url
        if self.thumbnail_url:
            payload["thumbnailUrl"] = self.thumbnail_url
        if self.width is not None:
            payload["width"] = self.width
        if self.height is not None:
            payload["height"] = self.height
        return payload


@dataclass(frozen=True)
class FrameManifest:
    frames: tuple[FrameSample, ...]
    transcript: str | None = None
    source_url: str | None = None
    duration_sec: float | None = None

    def __post_init__(self) -> None:
        if not self.frames:
            raise ValueError("frame manifest must contain at least one frame")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any] | Sequence[Mapping[str, Any]]) -> FrameManifest:
        if isinstance(data, Mapping):
            raw_frames = _value(data, "frames", "samples", default=[])
            transcript = _value(data, "transcript", "captions")
            source_url = _value(data, "sourceUrl", "source_url", "url")
            duration = _value(data, "durationSec", "duration_sec", "duration")
        else:
            raw_frames = data
            transcript = None
            source_url = None
            duration = None
        frames = tuple(sorted((FrameSample.from_dict(item) for item in raw_frames), key=lambda item: item.timestamp_sec))
        return cls(
            frames=frames,
            transcript=str(transcript) if transcript else None,
            source_url=str(source_url) if source_url else None,
            duration_sec=float(duration) if duration is not None else None,
        )


@dataclass(frozen=True)
class Appearance:
    start_sec: float
    evidence: str
    end_sec: float | None = None
    thumbnail_url: str | None = None
    bounding_box: BoundingBox | None = None

    def __post_init__(self) -> None:
        if self.start_sec < 0:
            raise ValueError("start_sec cannot be negative")
        if self.end_sec is not None and self.end_sec < self.start_sec:
            raise ValueError("end_sec cannot precede start_sec")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any]) -> Appearance:
        end = _value(data, "endSec", "end_sec")
        return cls(
            start_sec=float(_value(data, "startSec", "start_sec", "timestampSec", "timestamp_sec", default=0)),
            end_sec=float(end) if end is not None else None,
            thumbnail_url=_value(data, "thumbnailUrl", "thumbnail_url"),
            bounding_box=BoundingBox.from_dict(_value(data, "boundingBox", "bounding_box")),
            evidence=str(_value(data, "evidence", default="Visible in frame")),
        )

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {"startSec": self.start_sec, "evidence": self.evidence}
        if self.end_sec is not None:
            payload["endSec"] = self.end_sec
        if self.thumbnail_url:
            payload["thumbnailUrl"] = self.thumbnail_url
        if self.bounding_box:
            payload["boundingBox"] = self.bounding_box.to_dict()
        return payload


@dataclass
class ProductCandidate:
    id: str
    name: str
    category: str
    confidence: float
    appearances: list[Appearance] = field(default_factory=list)
    brand: str | None = None
    model: str | None = None
    color: str | None = None
    material: str | None = None
    visible_text: list[str] = field(default_factory=list)
    instance_key: str | None = None

    def __post_init__(self) -> None:
        self.confidence = _bounded_float(self.confidence, 0.0, 1.0, "confidence")
        if not self.name.strip() or not self.category.strip():
            raise ValueError("candidate name and category are required")

    @classmethod
    def from_dict(cls, data: Mapping[str, Any], *, fallback_id: str) -> ProductCandidate:
        raw_appearances = _value(data, "appearances", default=[])
        if not raw_appearances and _value(data, "timestampSec", "timestamp_sec") is not None:
            raw_appearances = [data]
        return cls(
            id=str(_value(data, "id", default=fallback_id)),
            name=str(_value(data, "name", "productName", "product_name", default="")).strip(),
            category=str(_value(data, "category", default="other")).strip().lower(),
            confidence=float(_value(data, "confidence", default=0)),
            appearances=[Appearance.from_dict(item) for item in raw_appearances],
            brand=_clean_optional(_value(data, "brand")),
            model=_clean_optional(_value(data, "model")),
            color=_clean_optional(_value(data, "color")),
            material=_clean_optional(_value(data, "material")),
            visible_text=[str(item).strip() for item in _value(data, "visibleText", "visible_text", default=[]) if str(item).strip()],
            instance_key=_clean_optional(_value(data, "instanceKey", "instance_key")),
        )


@dataclass(frozen=True)
class RetailMatch:
    product_name: str
    retailer_name: str
    product_url: str
    match_kind: MatchKind
    confidence: float
    evidence: str
    image_url: str | None = None
    price: str | None = None

    def __post_init__(self) -> None:
        _bounded_float(self.confidence, 0.0, 1.0, "confidence")


@dataclass
class ProductFinding:
    id: str
    name: str
    category: str
    match_kind: MatchKind
    confidence: float
    appearances: list[Appearance]
    brand: str | None = None
    model: str | None = None
    retailer_name: str | None = None
    product_url: str | None = None
    image_url: str | None = None
    price: str | None = None
    alternatives: list[RetailMatch] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        payload: dict[str, Any] = {
            "id": self.id,
            "name": self.name,
            "category": self.category,
            "matchKind": self.match_kind.value,
            "confidence": round(self.confidence, 4),
            "appearances": [item.to_dict() for item in sorted(self.appearances, key=lambda item: item.start_sec)],
        }
        optional = {
            "brand": self.brand,
            "model": self.model,
            "retailerName": self.retailer_name,
            "productUrl": self.product_url,
            "imageUrl": self.image_url,
            "price": self.price,
        }
        payload.update({key: value for key, value in optional.items() if value is not None})
        if self.alternatives:
            payload["alternatives"] = [
                {
                    "productName": item.product_name,
                    "retailerName": item.retailer_name,
                    "productUrl": item.product_url,
                    "matchKind": item.match_kind.value,
                    "confidence": item.confidence,
                    "evidence": item.evidence,
                    **({"imageUrl": item.image_url} if item.image_url else {}),
                    **({"price": item.price} if item.price else {}),
                }
                for item in self.alternatives
            ]
        return payload


def _clean_optional(value: Any) -> str | None:
    if value is None:
        return None
    parsed = str(value).strip()
    return parsed or None
