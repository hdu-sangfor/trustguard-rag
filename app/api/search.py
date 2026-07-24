"""搜索 HTTP API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.embedding.profiles import get_embedding_profile
from app.core.retrieval.search import SearchUnavailableError, get_hybrid_search
from app.schemas.search import SearchRequest, SearchResponse
from app.stores.knowledge_base_store import get_knowledge_base_store

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    if not request.enable_vector and not request.enable_keyword:
        raise HTTPException(status_code=400, detail="At least one of enable_vector/enable_keyword must be True")
    kb_store = get_knowledge_base_store()
    try:
        knowledge_base = await kb_store.resolve(request.knowledge_base_id)
        embedding_profile = knowledge_base.embedding_profile
        profile = (
            get_embedding_profile(embedding_profile)
            if request.enable_vector
            else None
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    filters = request.filters.model_dump(exclude_none=True) if request.filters else {}
    filters["knowledge_base_id"] = knowledge_base.id

    engine = get_hybrid_search()
    try:
        result = await engine.search(
            query=request.query,
            knowledge_base_id=knowledge_base.id,
            top_k=request.top_k,
            vector_top_k=request.vector_top_k,
            keyword_top_k=request.keyword_top_k,
            max_chunks_per_document=request.max_chunks_per_document,
            fusion_method=request.fusion_method,
            vector_weight=request.vector_weight,
            keyword_weight=request.keyword_weight,
            enable_rerank=request.enable_rerank,
            enable_vector=request.enable_vector,
            enable_keyword=request.enable_keyword,
            filters=filters,
            embedding_profile=embedding_profile,
            enable_abstention=request.enable_abstention,
            min_vector_score=(
                request.min_vector_score
                if request.min_vector_score is not None
                else profile.retrieval_min_score if profile is not None else None
            ),
            require_exact_entity_match=request.require_exact_entity_match,
            component_max_retries=request.component_max_retries,
        )
    except SearchUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return SearchResponse(
        query=request.query,
        knowledge_base_id=knowledge_base.id,
        search_status=result["search_status"],
        effective_mode=result["effective_mode"],
        results=result["results"],
        total=result["total"],
        fusion_method=result["fusion_method"],
        retrieval_time_ms=result["retrieval_time_ms"],
        components=result["components"],
        degraded_components=result["degraded_components"],
        query_entities=result.get("query_entities", []),
        max_chunks_per_document=result.get("max_chunks_per_document", 1),
        deduplicated_chunks=result.get("deduplicated_chunks", 0),
        abstained=result.get("abstained", False),
        abstention_reason=result.get("abstention_reason"),
        min_vector_score=result.get("min_vector_score"),
        component_attempts=result.get("component_attempts", {}),
        recovered_components=result.get("recovered_components", []),
    )
