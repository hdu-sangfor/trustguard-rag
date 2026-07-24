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


class QueryIntent(StrEnum):
    AUTO = "auto"
    FOCUSED = "focused"
    COMPREHENSIVE = "comprehensive"
    ENUMERATION = "enumeration"


class QueryPlanSource(StrEnum):
    EXPLICIT = "explicit"
    HEURISTIC = "heuristic"
    LLM = "llm"


class CoverageStatus(StrEnum):
    COMPLETE = "complete"
    PARTIAL = "partial"
    NOT_APPLICABLE = "not_applicable"
    UNKNOWN = "unknown"

