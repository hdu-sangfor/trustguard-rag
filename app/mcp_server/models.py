"""兼容导出；共享知识契约已移至 schemas 层。"""

from app.schemas.knowledge import (
    KnowledgeCoverage,
    KnowledgeEffectiveness,
    KnowledgeHit,
    KnowledgeResource,
    KnowledgeScope,
    KnowledgeSearchFilters,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
    KnowledgeSourceType,
    KnowledgeVisibility,
    McpQueryPlan,
    McpQueryPlanSource,
    ScopeDefinition,
)

__all__ = [
    "KnowledgeCoverage",
    "KnowledgeEffectiveness",
    "KnowledgeHit",
    "KnowledgeResource",
    "KnowledgeScope",
    "KnowledgeSearchFilters",
    "KnowledgeSearchRequest",
    "KnowledgeSearchResponse",
    "KnowledgeSourceType",
    "KnowledgeVisibility",
    "McpQueryPlan",
    "McpQueryPlanSource",
    "ScopeDefinition",
]
