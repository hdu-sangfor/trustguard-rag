"""搜索 API 请求和响应模型。"""
from __future__ import annotations

from typing import Any

from pydantic import BaseModel, Field

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
    source: SourceInfo
    metadata: dict[str, Any] | None = None


class SearchRequest(BaseModel):
    """混合检索请求体。"""
    query: str = Field(description="搜索查询文本", min_length=1)
    top_k: int | None = Field(default=None, ge=1, le=100, description="返回结果数量，为空则使用默认配置")
    vector_top_k: int | None = Field(default=None, ge=1, le=200, description="向量检索召回上限")
    keyword_top_k: int | None = Field(default=None, ge=1, le=200, description="关键词检索召回上限")
    fusion_method: str | None = Field(default=None, pattern="^(rrf|weighted_score)$", description="融合策略")
    vector_weight: float | None = Field(default=None, ge=0.0, le=1.0, description="向量检索权重")
    keyword_weight: float | None = Field(default=None, ge=0.0, le=1.0, description="关键词检索权重")
    enable_rerank: bool = Field(default=True, description="是否启用重排序")
    enable_vector: bool = Field(default=True, description="是否启用向量检索")
    enable_keyword: bool = Field(default=True, description="是否启用关键词检索")
    filters: dict[str, Any] | None = Field(default=None, description="元数据过滤条件")


class SearchResponse(BaseModel):
    """混合检索响应体。"""
    query: str
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
