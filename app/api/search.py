"""搜索 HTTP API。"""
from __future__ import annotations

from fastapi import APIRouter, HTTPException, Request

from app.application.knowledge import (
    KnowledgeSearchError,
    get_knowledge_application_service,
)
from app.schemas.search import SearchRequest, SearchResponse

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(request: SearchRequest, http_request: Request) -> SearchResponse:
    try:
        return await get_knowledge_application_service().search(
            request,
            request_id=getattr(http_request.state, "request_id", ""),
        )
    except KnowledgeSearchError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
