"""仅供 MCP Gateway 等受信服务调用的内部 REST 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.application.access import KnowledgeAccessContext
from app.application.knowledge import (
    KnowledgeSearchError,
    get_knowledge_application_service,
)
from app.schemas.knowledge import (
    KnowledgeResource,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.security.service_auth import require_internal_service

router = APIRouter(prefix="/v1/internal", tags=["internal"])


@router.post(
    "/knowledge/search-scope",
    response_model=KnowledgeSearchResponse,
)
async def search_knowledge_scope(
    payload: KnowledgeSearchRequest,
    request: Request,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_internal_service),
    ],
) -> KnowledgeSearchResponse:
    """由 RAG 应用层解析 Scope 并完成联邦检索与结果融合。"""
    try:
        return await get_knowledge_application_service().search_scope(
            payload,
            request_id=request.state.request_id,
            access_context=access_context,
        )
    except KnowledgeSearchError as error:
        raise _http_error(error) from error


@router.get(
    "/knowledge/resources/{resource_ref}",
    response_model=KnowledgeResource,
)
async def get_knowledge_resource(
    resource_ref: str,
    request: Request,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_internal_service),
    ],
    scope: str = Query(min_length=1, max_length=64),
) -> KnowledgeResource:
    """解析不透明 Resource Ref，并直接读取唯一来源。"""
    try:
        return await get_knowledge_application_service().read_resource(
            scope=scope,
            resource_ref=resource_ref,
            request_id=request.state.request_id,
            access_context=access_context,
        )
    except KnowledgeSearchError as error:
        raise _http_error(error) from error


def _http_error(error: KnowledgeSearchError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        },
    )
