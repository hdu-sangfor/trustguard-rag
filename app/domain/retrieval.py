"""检索编排结果的稳定领域值。"""

from __future__ import annotations

from enum import StrEnum


class SearchStatus(StrEnum):
    OK = "ok"
    DEGRADED = "degraded"


class EffectiveSearchMode(StrEnum):
    HYBRID = "hybrid"
    VECTOR_ONLY = "vector_only"
    KEYWORD_ONLY = "keyword_only"


class RetrievalComponent(StrEnum):
    VECTOR = "vector"
    KEYWORD = "keyword"
    RERANK = "rerank"

