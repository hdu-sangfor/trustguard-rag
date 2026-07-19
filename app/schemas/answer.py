"""RAG 回答 API 的请求与响应模型。"""

from __future__ import annotations

from pydantic import BaseModel, Field

from app.domain import AnswerStatus, EffectiveSearchMode, SearchStatus
from app.schemas.search import SearchRequest


class AnswerRequest(SearchRequest):
    """单轮回答请求；检索选项与搜索接口保持一致。"""


class AnswerCitation(BaseModel):
    """答案实际引用的一条检索证据。"""

    citation_id: int = Field(ge=1)
    chunk_id: str
    document_id: str
    source_uri: str
    original_filename: str | None = None
    chunk_index: int
    page_no: int | None = None
    excerpt: str


class GenerationUsage(BaseModel):
    """上游 LLM 返回的 Token 用量。"""

    prompt_tokens: int = Field(ge=0)
    completion_tokens: int = Field(ge=0)
    total_tokens: int = Field(ge=0)


class AnswerResponse(BaseModel):
    """带来源引用和检索诊断信息的回答。"""

    query: str
    status: AnswerStatus
    answer: str
    citations: list[AnswerCitation]
    search_status: SearchStatus
    effective_mode: EffectiveSearchMode
    degraded_components: list[str] = Field(default_factory=list)
    retrieved_count: int = Field(ge=0)
    context_chunk_count: int = Field(ge=0)
    context_token_count: int = Field(ge=0)
    retrieval_time_ms: float = Field(ge=0)
    generation_time_ms: float = Field(ge=0)
    total_time_ms: float = Field(ge=0)
    model: str | None = None
    usage: GenerationUsage | None = None
