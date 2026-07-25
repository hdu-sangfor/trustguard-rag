"""REST 与 MCP 共享的知识 v1 契约和 Scope 配置模型。"""

from __future__ import annotations

from enum import StrEnum
from typing import Annotated, Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import CoverageStatus, RetrievalMode, SearchStatus


class KnowledgeScope(StrEnum):
    PENETRATION = "penetration"
    ALERT_TRIAGE = "alert-triage"
    COMPLIANCE = "compliance"
    PRODUCT_DOCS = "product-docs"
    THREAT_INTELLIGENCE = "threat-intelligence"
    RESPONSE_PLAYBOOKS = "response-playbooks"


class KnowledgeSourceType(StrEnum):
    DOCUMENT = "document"
    EXPERIENCE = "experience"
    PLAYBOOK = "playbook"


class KnowledgeVisibility(StrEnum):
    GLOBAL = "global"
    WORKSPACE = "workspace"


class KnowledgeEffectiveness(StrEnum):
    UNKNOWN = "unknown"
    PROMISING = "promising"
    EFFECTIVE = "effective"
    INEFFECTIVE = "ineffective"


class McpQueryPlanSource(StrEnum):
    EXPLICIT = "explicit"
    HEURISTIC = "heuristic"
    LLM = "llm"


class KnowledgeSearchFilters(BaseModel):
    model_config = ConfigDict(extra="forbid")

    content_types: list[
        Annotated[str, Field(min_length=1, max_length=64)]
    ] = Field(
        default_factory=list,
        max_length=20,
        json_schema_extra={"uniqueItems": True},
    )
    source_types: list[KnowledgeSourceType] = Field(
        default_factory=list,
        max_length=3,
        json_schema_extra={"uniqueItems": True},
    )

    @field_validator("content_types")
    @classmethod
    def validate_content_types(cls, values: list[str]) -> list[str]:
        result: list[str] = []
        for value in values:
            normalized = value.strip()
            if not 1 <= len(normalized) <= 64:
                raise ValueError("content_types entries must contain 1 to 64 characters")
            if normalized not in result:
                result.append(normalized)
        return result

    @field_validator("source_types")
    @classmethod
    def deduplicate_source_types(
        cls, values: list[KnowledgeSourceType]
    ) -> list[KnowledgeSourceType]:
        return list(dict.fromkeys(values))


class KnowledgeSearchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["trustguard-knowledge-search-request-v1"]
    query: str = Field(min_length=1, max_length=2000)
    scope: KnowledgeScope
    mode: RetrievalMode = RetrievalMode.AUTO
    limit: int = Field(default=5, ge=1, le=20)
    rewrite: bool = False
    filters: KnowledgeSearchFilters = Field(default_factory=KnowledgeSearchFilters)

    @field_validator("query")
    @classmethod
    def normalize_query(cls, value: str) -> str:
        normalized = " ".join(value.split())
        if not normalized:
            raise ValueError("query cannot be blank")
        return normalized


class KnowledgeHit(BaseModel):
    model_config = ConfigDict(extra="forbid")

    external_chunk_id: str = Field(min_length=1, max_length=128)
    resource_uri: str = Field(pattern=r"^trustguard-rag://")
    resource_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        pattern=r"^krf1\.",
    )
    source_revision: int | None = Field(default=None, ge=1)
    content_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    snippet: str = Field(max_length=4000)
    score: float
    title: str | None = Field(default=None, max_length=512)
    document_id: str | None = Field(default=None, max_length=128)
    filename: str | None = Field(default=None, max_length=512)
    page_no: int | None = Field(default=None, ge=1)
    source_uri: str | None = Field(default=None, max_length=2048)
    source_type: KnowledgeSourceType
    workflow_type: str | None = Field(default=None, max_length=64)
    effectiveness: KnowledgeEffectiveness | None = None
    visibility: KnowledgeVisibility
    expanded: bool


class McpQueryPlan(BaseModel):
    model_config = ConfigDict(extra="forbid")

    intent: RetrievalMode
    source: McpQueryPlanSource


class KnowledgeCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CoverageStatus
    warning: str | None = Field(max_length=1000)


class KnowledgeSearchResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["trustguard-knowledge-search-v1"]
    request_id: str = Field(min_length=1, max_length=128)
    scope: str = Field(min_length=1, max_length=64)
    status: SearchStatus
    content_revision: str = Field(min_length=1, max_length=128)
    hits: list[KnowledgeHit] = Field(max_length=20)
    query_plan: McpQueryPlan
    coverage: KnowledgeCoverage
    degraded_components: list[
        Literal["vector", "keyword", "rerank", "rewrite", "federation"]
    ] = Field(json_schema_extra={"uniqueItems": True})
    latency_ms: float = Field(ge=0)


class KnowledgeResource(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["trustguard-knowledge-resource-v1"]
    scope: str = Field(min_length=1, max_length=64)
    content_revision: str = Field(min_length=1, max_length=128)
    resource_ref: str | None = Field(
        default=None,
        min_length=1,
        max_length=2048,
        pattern=r"^krf1\.",
    )
    source_revision: int | None = Field(default=None, ge=1)
    content_hash: str | None = Field(
        default=None,
        pattern=r"^sha256:[a-f0-9]{64}$",
    )
    chunk_id: str = Field(min_length=1, max_length=128)
    document_id: str | None = Field(default=None, max_length=128)
    experience_id: str | None = Field(default=None, max_length=128)
    text: str = Field(min_length=1, max_length=32000)
    title: str | None = Field(default=None, max_length=512)
    filename: str | None = Field(default=None, max_length=512)
    page_no: int | None = Field(default=None, ge=1)
    source_uri: str | None = Field(default=None, max_length=2048)
    source_type: KnowledgeSourceType
    workflow_type: str | None = Field(default=None, max_length=64)
    effectiveness: KnowledgeEffectiveness | None = None
    visibility: KnowledgeVisibility
    metadata: dict[str, Any]


class ScopeDefinition(BaseModel):
    """逻辑 Scope 对一个或多个物理知识库的受控映射。"""

    model_config = ConfigDict(extra="forbid")

    knowledge_base_ids: list[str] = Field(min_length=1, max_length=16)
    default_mode: RetrievalMode = RetrievalMode.AUTO
    per_knowledge_base_limit: int = Field(default=20, ge=1, le=100)
    allowed_content_types: list[str] = Field(default_factory=list, max_length=100)
    allowed_workflow_types: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("knowledge_base_ids")
    @classmethod
    def normalize_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            raise ValueError("knowledge_base_ids cannot be empty")
        return normalized

    @field_validator("allowed_content_types", "allowed_workflow_types")
    @classmethod
    def normalize_allowlists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))
