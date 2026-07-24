"""搜索 HTTP API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.core.retrieval.request_context import resolve_search_execution
from app.core.retrieval.search import SearchUnavailableError, get_hybrid_search
from app.schemas.search import SearchRequest, SearchResponse

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(request: SearchRequest, http_request: Request) -> SearchResponse:
    try:
        context = await resolve_search_execution(request)
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    except ValueError as error:
        raise HTTPException(status_code=400, detail=str(error)) from error

    engine = get_hybrid_search()
    try:
        result = await engine.search(**context.search_kwargs)
    except SearchUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return SearchResponse(
        request_id=getattr(http_request.state, "request_id", ""),
        query=request.query,
        knowledge_base_id=context.knowledge_base.id,
        content_revision=context.knowledge_base.content_revision,
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
