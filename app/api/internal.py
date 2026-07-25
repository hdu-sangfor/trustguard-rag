"""仅供 MCP Gateway 等受信服务调用的内部 REST 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request

from app.application.access import (
    KnowledgeAccessContext,
    KnowledgeAccessDenied,
    KnowledgePermission,
)
from app.application.knowledge import (
    KnowledgeSearchError,
    get_knowledge_application_service,
)
from app.schemas.internal import (
    InternalChunkResponse,
    InternalContentRevisionResponse,
)
from app.schemas.knowledge import (
    KnowledgeResource,
    KnowledgeSearchRequest,
    KnowledgeSearchResponse,
)
from app.schemas.search import SearchRequest, SearchResponse
from app.security.service_auth import require_internal_service
from app.stores.knowledge_base_store import get_knowledge_base_store

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


@router.post(
    "/knowledge/search",
    response_model=SearchResponse,
)
async def search_knowledge(
    payload: SearchRequest,
    request: Request,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_internal_service),
    ],
) -> SearchResponse:
    """使用服务身份执行与公开 Search 相同语义的知识库检索。"""
    try:
        return await get_knowledge_application_service().search(
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


@router.get(
    "/knowledge-bases/{knowledge_base_id}/content-revision",
    response_model=InternalContentRevisionResponse,
)
async def get_content_revision(
    knowledge_base_id: str,
    request: Request,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_internal_service),
    ],
) -> InternalContentRevisionResponse:
    """供旧 Resource URI 兼容校验使用的受保护版本读取。"""
    try:
        access_context.require(
            KnowledgePermission.RESOURCE_READ,
            knowledge_base_id=knowledge_base_id,
        )
    except KnowledgeAccessDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    knowledge_base = await get_knowledge_base_store().get(knowledge_base_id)
    if knowledge_base is None:
        raise HTTPException(status_code=404, detail="Knowledge base not found")
    return InternalContentRevisionResponse(
        request_id=request.state.request_id,
        knowledge_base_id=knowledge_base.id,
        content_revision=knowledge_base.content_revision,
    )


@router.get(
    "/knowledge-bases/{knowledge_base_id}/chunks/{chunk_id}",
    response_model=InternalChunkResponse,
)
async def get_scoped_chunk(
    knowledge_base_id: str,
    chunk_id: str,
    request: Request,
    access_context: Annotated[
        KnowledgeAccessContext,
        Depends(require_internal_service),
    ],
) -> InternalChunkResponse:
    """按知识库和 Chunk 双重约束读取已发布内容。"""
    try:
        source = await get_knowledge_application_service().resolve_scoped_source(
            knowledge_base_id=knowledge_base_id,
            chunk_id=chunk_id,
            access_context=access_context,
        )
    except KnowledgeSearchError as error:
        raise _http_error(error) from error
    if source is None:
        # 跨库访问与真实不存在统一返回 404，避免枚举 Chunk 归属。
        raise HTTPException(status_code=404, detail="Chunk not found")
    return InternalChunkResponse(
        request_id=request.state.request_id,
        knowledge_base_id=source.knowledge_base_id,
        content_revision=source.content_revision,
        source_revision=source.source_revision,
        content_hash=source.content_hash,
        chunk_id=source.chunk_id,
        document_id=source.document_id,
        text=source.text,
        title=source.title,
        filename=source.filename,
        page_no=source.page_no,
        source_uri=source.source_uri,
        source_type=source.source_type.value,
        metadata=source.metadata,
    )


def _http_error(error: KnowledgeSearchError) -> HTTPException:
    return HTTPException(
        status_code=error.status_code,
        detail={
            "code": error.code,
            "message": str(error),
            "retryable": error.retryable,
        },
    )
