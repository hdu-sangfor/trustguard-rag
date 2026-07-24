"""搜索 API 请求和响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import EffectiveSearchMode, SearchStatus


class SourceInfo(BaseModel):
    """搜索结果来源文档的元数据。"""
    document_id: str
    source_uri: str
    original_filename: str | None = None
    chunk_index: int
    page_no: int | None = None


class SearchResult(BaseModel):
    """单条检索结果，包含得分和元数据。"""
    chunk_id: str
    text: str
    score: float
    vector_score: float | None = None
    keyword_score: float | None = None
    rerank_score: float | None = None
    entity_id: str | None = None
    entity_type: str | None = None
    entity_ids: list[str] = Field(default_factory=list)
    entity_types: list[str] = Field(default_factory=list)
    title: str | None = None
    aliases: list[str] = Field(default_factory=list)
    exact_entity_match: str | None = Field(
        default=None,
        pattern="^(primary|related)$",
    )
    source: SourceInfo
    metadata: dict[str, Any] | None = None


FilterScalar = str | int | float | bool


class SearchFilters(BaseModel):
    """两个召回引擎共同支持的过滤条件。"""

    model_config = ConfigDict(extra="forbid")

    document_id: str | None = Field(default=None, min_length=1, max_length=36)
    source_uri: str | None = Field(default=None, min_length=1, max_length=2048)
    original_filename: str | None = Field(default=None, min_length=1, max_length=512)
    chunk_index: int | None = Field(default=None, ge=0)
    page_no: int | None = Field(default=None, ge=1)
    metadata: dict[str, FilterScalar] | None = None

    @field_validator("metadata")
    @classmethod
    def validate_metadata_keys(
        cls, value: dict[str, FilterScalar] | None
    ) -> dict[str, FilterScalar] | None:
        """限制元数据键，避免生成含歧义的嵌套字段路径。"""
        if value is None:
            return None
        for key in value:
            if not key or len(key) > 64 or not key.replace("_", "").replace("-", "").isalnum():
                raise ValueError("metadata keys may contain only letters, numbers, '_' and '-'")
        return value


class SearchRequest(BaseModel):
    """混合检索请求体。"""

    model_config = ConfigDict(extra="forbid")

    query: str = Field(description="搜索查询文本", min_length=1)
    knowledge_base_id: str = Field(
        min_length=1,
        max_length=36,
        description="本次检索唯一允许访问的知识库 ID",
    )
    top_k: int | None = Field(default=None, ge=1, le=100, description="返回结果数量，为空则使用默认配置")
    vector_top_k: int | None = Field(default=None, ge=1, le=200, description="向量检索召回上限")
    keyword_top_k: int | None = Field(default=None, ge=1, le=200, description="关键词检索召回上限")
    max_chunks_per_document: int = Field(
        default=1,
        ge=1,
        le=10,
        description="最终候选中每篇文档最多保留的分块数，默认为 1",
    )
    fusion_method: str | None = Field(default=None, pattern="^(rrf|weighted_score)$", description="融合策略")
    vector_weight: float | None = Field(default=None, ge=0.0, le=1.0, description="向量检索权重")
    keyword_weight: float | None = Field(default=None, ge=0.0, le=1.0, description="关键词检索权重")
    enable_rerank: bool = Field(default=True, description="是否启用重排序")
    enable_vector: bool = Field(default=True, description="是否启用向量检索")
    enable_keyword: bool = Field(default=True, description="是否启用关键词检索")
    enable_abstention: bool = Field(
        default=True,
        description="启用精确实体缺失和低向量置信度拒答",
    )
    min_vector_score: float | None = Field(
        default=None,
        ge=-1.0,
        le=1.0,
        description="最低向量相似度；为空时使用所选 embedding profile 的校准值",
    )
    require_exact_entity_match: bool = Field(
        default=True,
        description="查询含 CVE/CWE/CAPEC 时，没有任何精确实体命中则返回空结果",
    )
    component_max_retries: int | None = Field(
        default=None,
        ge=0,
        le=5,
        description="向量或关键词组件临时失败后的额外重试次数",
    )
    filters: SearchFilters | None = Field(default=None, description="双引擎统一过滤条件")

    @field_validator("knowledge_base_id")
    @classmethod
    def validate_knowledge_base_id(cls, value: str) -> str:
        value = value.strip()
        if not value:
            raise ValueError("knowledge_base_id cannot be blank")
        return value


class SearchResponse(BaseModel):
    """混合检索响应体。"""
    query: str
    knowledge_base_id: str
    search_status: SearchStatus
    effective_mode: EffectiveSearchMode
    results: list[SearchResult]
    total: int
    fusion_method: str
    retrieval_time_ms: float
    components: dict[str, int] = Field(default_factory=dict, description="各检索引擎召回数量")
    degraded_components: list[str] = Field(
        default_factory=list,
        description="本次请求中发生故障并已降级的召回引擎",
    )
    query_entities: list[str] = Field(
        default_factory=list,
        description="从查询中识别并用于精确路由的 CVE/CWE/CAPEC 编号",
    )
    max_chunks_per_document: int = 1
    deduplicated_chunks: int = Field(
        default=0,
        description="文档级去重阶段移除的重复分块数量",
    )
    abstained: bool = Field(default=False, description="是否因低置信度或精确实体缺失拒答")
    abstention_reason: str | None = Field(
        default=None,
        description="拒答原因：no_exact_entity_match 或 low_vector_score",
    )
    min_vector_score: float | None = None
    component_attempts: dict[str, int] = Field(
        default_factory=dict,
        description="各检索组件本次实际调用次数",
    )
    recovered_components: list[str] = Field(
        default_factory=list,
        description="首次失败但在组件内部重试后恢复的检索组件",
    )
