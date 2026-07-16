"""搜索 HTTP API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException

from app.core.retrieval.search import SearchUnavailableError, get_hybrid_search
from app.schemas.search import SearchRequest, SearchResponse

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(request: SearchRequest) -> SearchResponse:
    if not request.enable_vector and not request.enable_keyword:
        raise HTTPException(status_code=400, detail="At least one of enable_vector/enable_keyword must be True")

    engine = get_hybrid_search()
    try:
        result = await engine.search(
            query=request.query,
            top_k=request.top_k,
            vector_top_k=request.vector_top_k,
            keyword_top_k=request.keyword_top_k,
            fusion_method=request.fusion_method,
            vector_weight=request.vector_weight,
            keyword_weight=request.keyword_weight,
            enable_rerank=request.enable_rerank,
            enable_vector=request.enable_vector,
            enable_keyword=request.enable_keyword,
            filters=request.filters,
        )
    except SearchUnavailableError as e:
        raise HTTPException(status_code=503, detail=str(e)) from e

    return SearchResponse(
        query=request.query,
        results=result["results"],
        total=result["total"],
        fusion_method=result["fusion_method"],
        retrieval_time_ms=result["retrieval_time_ms"],
        components=result["components"],
        degraded_components=result["degraded_components"],
    )
