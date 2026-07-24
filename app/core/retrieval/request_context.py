"""Search 和 Answer 共用的知识库范围与检索参数解析。"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from app.core.embedding.profiles import get_embedding_profile
from app.schemas.search import SearchRequest
from app.stores.knowledge_base_store import get_knowledge_base_store
from app.stores.models import KnowledgeBaseRow


@dataclass(frozen=True)
class SearchExecutionContext:
    """已解析并强制绑定知识库范围的一次检索执行上下文。"""

    knowledge_base: KnowledgeBaseRow
    search_kwargs: dict[str, Any]


async def resolve_search_execution(
    request: SearchRequest,
) -> SearchExecutionContext:
    """统一解析知识库、嵌入配置、置信阈值和检索过滤条件。"""
    if not request.enable_vector and not request.enable_keyword:
        raise ValueError(
            "At least one of enable_vector/enable_keyword must be True"
        )

    knowledge_base = await get_knowledge_base_store().resolve(
        request.knowledge_base_id
    )
    profile = (
        get_embedding_profile(knowledge_base.embedding_profile)
        if request.enable_vector
        else None
    )
    filters = (
        request.filters.model_dump(exclude_none=True)
        if request.filters is not None
        else {}
    )
    filters["knowledge_base_id"] = knowledge_base.id

    return SearchExecutionContext(
        knowledge_base=knowledge_base,
        search_kwargs={
            "query": request.query,
            "knowledge_base_id": knowledge_base.id,
            "top_k": request.top_k,
            "vector_top_k": request.vector_top_k,
            "keyword_top_k": request.keyword_top_k,
            "max_chunks_per_document": request.max_chunks_per_document,
            "fusion_method": request.fusion_method,
            "vector_weight": request.vector_weight,
            "keyword_weight": request.keyword_weight,
            "enable_rerank": request.enable_rerank,
            "enable_vector": request.enable_vector,
            "enable_keyword": request.enable_keyword,
            "filters": filters,
            "embedding_profile": knowledge_base.embedding_profile,
            "enable_abstention": request.enable_abstention,
            "allow_keyword_fallback": request.allow_keyword_fallback,
            "min_vector_score": (
                request.min_vector_score
                if request.min_vector_score is not None
                else profile.retrieval_min_score
                if profile is not None
                else None
            ),
            "require_exact_entity_match": request.require_exact_entity_match,
            "component_max_retries": request.component_max_retries,
        },
    )
