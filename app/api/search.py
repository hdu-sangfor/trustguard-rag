"""搜索 HTTP API。"""
from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.application.access import KnowledgeAccessContext
from app.application.knowledge import (
    KnowledgeSearchError,
    get_knowledge_application_service,
)
from app.schemas.knowledge import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.search import SearchRequest, SearchResponse
from app.security.service_auth import require_gateway_service
from app.stores.experience_store import (
    PENETRATION_EXPERIENCE_KB_ID,
    PENETRATION_EXPERIENCE_KB_NAME,
)
from app.stores.knowledge_base_store import get_knowledge_base_store

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
    knowledge_base = await get_knowledge_base_store().get(request.knowledge_base_id)
    if knowledge_base is not None and (
        knowledge_base.id == PENETRATION_EXPERIENCE_KB_ID
        or knowledge_base.name == PENETRATION_EXPERIENCE_KB_NAME
    ):
        raise HTTPException(
            status_code=403,
            detail="Experience knowledge must be queried through an authorized scope",
        )
    try:
        return await get_knowledge_application_service().search(
            request,
            request_id=getattr(http_request.state, "request_id", ""),
            access_context=access_context,
        )
    except KnowledgeSearchError as error:
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


@router.post("/scope", response_model=KnowledgeSearchResponse)
async def search_scope(
    request: KnowledgeSearchRequest,
    http_request: Request,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_gateway_service),
    ],
) -> KnowledgeSearchResponse:
    """供 Agent Gateway、普通 REST 调用方和评测复用联邦 Scope Search。"""
    try:
        return await get_knowledge_application_service().search_scope(
            request,
            request_id=getattr(http_request.state, "request_id", ""),
            access_context=access_context,
        )
    except KnowledgeSearchError as error:
        raise HTTPException(
            status_code=error.status_code,
            detail={
                "code": error.code,
                "message": str(error),
                "retryable": error.retryable,
            },
        ) from error
