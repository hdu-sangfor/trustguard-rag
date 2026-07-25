"""搜索 HTTP API。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.application.access import KnowledgeAccessContext
from app.application.knowledge import (
    KnowledgeSearchError,
    get_knowledge_application_service,
)
from app.schemas.search import SearchRequest, SearchResponse
from app.security.service_auth import require_gateway_service

router = APIRouter(prefix="/v1/search", tags=["search"])


@router.post("", response_model=SearchResponse)
async def search(
    request: SearchRequest,
    http_request: Request,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
) -> SearchResponse:
    try:
        return await get_knowledge_application_service().search(
            request,
            request_id=getattr(http_request.state, "request_id", ""),
            access_context=access_context,
        )
    except KnowledgeSearchError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error
