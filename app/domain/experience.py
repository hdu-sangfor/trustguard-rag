"""Experience lifecycle and index status domain values."""

from __future__ import annotations

from enum import StrEnum


class ExperienceStatus(StrEnum):
    CANDIDATE = "candidate"
    PENDING = "pending"
    PROVEN = "proven"
    DEPRECATED = "deprecated"
    ARCHIVED = "archived"


class ExperienceIndexStatus(StrEnum):
    NOT_INDEXED = "not_indexed"
    INDEXED = "indexed"
    INDEX_PENDING = "index_pending"


# Forward lifecycle order (skip-forward allowed within this prefix).
_FORWARD_ORDER = (
    ExperienceStatus.CANDIDATE,
    ExperienceStatus.PENDING,
    ExperienceStatus.PROVEN,
)


def is_status_transition_allowed(
    current: ExperienceStatus,
    target: ExperienceStatus,
) -> bool:
    """Slice A: skip-forward along candidate→pending→proven; downgrade via deprecated/archived."""
    if current == target:
        return False
    if current == ExperienceStatus.ARCHIVED:
        return False
    if target == ExperienceStatus.DEPRECATED:
        return current in {
            ExperienceStatus.CANDIDATE,
            ExperienceStatus.PENDING,
            ExperienceStatus.PROVEN,
        }
    if target == ExperienceStatus.ARCHIVED:
        return current == ExperienceStatus.DEPRECATED
    if current in _FORWARD_ORDER and target in _FORWARD_ORDER:
        return _FORWARD_ORDER.index(target) > _FORWARD_ORDER.index(current)
    return False
