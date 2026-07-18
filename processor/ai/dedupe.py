from __future__ import annotations

import hashlib
import re
from difflib import SequenceMatcher
from typing import Iterable

from processor.models import Appearance, ProductCandidate


_NOISE = {"a", "an", "the", "product", "item", "unknown", "generic"}
_CATEGORY_FAMILIES = {
    "lighting": {"lamp", "light", "lighting", "sconce", "chandelier", "pendant"},
    "headphones": {"headphone", "headphones", "headset", "earbuds"},
    "shoes": {"shoe", "shoes", "sneaker", "sneakers", "boot", "boots"},
    "seating": {"chair", "chairs", "stool", "sofa", "couch", "bench"},
}
_BATCH_PATTERN = re.compile(r"batch-(\d+)-candidate-")


def _normalized(value: str | None) -> str:
    if not value:
        return ""
    tokens = re.findall(r"[a-z0-9]+", value.lower())
    return " ".join(token for token in tokens if token not in _NOISE)


def _tokens(*values: str | None) -> set[str]:
    return set(_normalized(" ".join(value or "" for value in values)).split())


def _category_family(value: str | None) -> str:
    tokens = set(_normalized(value).split())
    for family, aliases in _CATEGORY_FAMILIES.items():
        if tokens & aliases:
            return family
    return _normalized(value)


def _batch_id(candidate: ProductCandidate) -> str | None:
    match = _BATCH_PATTERN.search(candidate.id)
    return match.group(1) if match else None


def _similarity(left: ProductCandidate, right: ProductCandidate) -> float:
    if _category_family(left.category) != _category_family(right.category):
        return 0.0
    left_brand, right_brand = _normalized(left.brand), _normalized(right.brand)
    left_model, right_model = _normalized(left.model), _normalized(right.model)
    if left_brand and right_brand and left_brand != right_brand:
        return 0.0
    if left_model and right_model and left_model != right_model:
        return 0.0
    if left_brand and right_brand and left_model and right_model:
        return 1.0
    if left.instance_key and right.instance_key:
        if _normalized(left.instance_key) == _normalized(right.instance_key):
            return 1.0
        # Instance keys are authoritative only inside one model request. The
        # model cannot coordinate key names across independently analyzed batches.
        left_batch, right_batch = _batch_id(left), _batch_id(right)
        if not left_batch or not right_batch or left_batch == right_batch:
            return 0.0
    left_tokens = _tokens(
        left.name, left.brand, left.model, left.color, left.material, left.visual_description
    )
    right_tokens = _tokens(
        right.name, right.brand, right.model, right.color, right.material, right.visual_description
    )
    union = left_tokens | right_tokens
    jaccard = len(left_tokens & right_tokens) / len(union) if union else 0.0
    name_ratio = SequenceMatcher(None, _normalized(left.name), _normalized(right.name)).ratio()
    identity_bonus = 0.12 if left_brand and left_brand == right_brand else 0.0
    return min(1.0, 0.55 * jaccard + 0.45 * name_ratio + identity_bonus)


def _merge_appearances(items: Iterable[Appearance], tolerance_sec: float = 0.75) -> list[Appearance]:
    ordered = sorted(items, key=lambda item: item.start_sec)
    result: list[Appearance] = []
    for item in ordered:
        if result and abs(result[-1].start_sec - item.start_sec) <= tolerance_sec:
            prior = result[-1]
            # Prefer the richer evidence and a real thumbnail/bounding box.
            result[-1] = Appearance(
                start_sec=min(prior.start_sec, item.start_sec),
                end_sec=max(value for value in (prior.end_sec, item.end_sec) if value is not None) if prior.end_sec is not None or item.end_sec is not None else None,
                thumbnail_url=prior.thumbnail_url or item.thumbnail_url,
                bounding_box=prior.bounding_box or item.bounding_box,
                evidence=max((prior.evidence, item.evidence), key=len),
                source_path=prior.source_path or item.source_path,
            )
        else:
            result.append(item)
    return result


def _stable_id(candidate: ProductCandidate) -> str:
    identity = "|".join(
        _normalized(value)
        for value in (candidate.category, candidate.brand, candidate.model, candidate.instance_key, candidate.name)
    )
    return f"product-{hashlib.sha1(identity.encode('utf-8')).hexdigest()[:12]}"


def _merge_into(target: ProductCandidate, source: ProductCandidate) -> None:
    if source.confidence > target.confidence:
        target.name = source.name
    target.confidence = max(target.confidence, source.confidence)
    target.brand = target.brand or source.brand
    target.model = target.model or source.model
    target.color = target.color or source.color
    target.material = target.material or source.material
    target.visual_description = target.visual_description or source.visual_description
    target.instance_key = target.instance_key or source.instance_key
    target.visible_text = sorted(set(target.visible_text + source.visible_text), key=str.casefold)
    target.appearances = _merge_appearances([*target.appearances, *source.appearances])


def deduplicate_candidates(
    candidates: Iterable[ProductCandidate], *, similarity_threshold: float = 0.72
) -> list[ProductCandidate]:
    """Merge repeat sightings while retaining genuinely distinct instances.

    An explicit model-generated ``instance_key`` is authoritative. Without one,
    brand/model identity or category-aware visual text similarity is used.
    """
    groups: list[ProductCandidate] = []
    for candidate in sorted(candidates, key=lambda item: min((a.start_sec for a in item.appearances), default=float("inf"))):
        best: ProductCandidate | None = None
        best_score = 0.0
        for existing in groups:
            score = _similarity(existing, candidate)
            if score >= similarity_threshold and score > best_score:
                best, best_score = existing, score
        if best is None:
            candidate.appearances = _merge_appearances(candidate.appearances)
            groups.append(candidate)
        else:
            _merge_into(best, candidate)
    for item in groups:
        item.id = _stable_id(item)
    return groups
