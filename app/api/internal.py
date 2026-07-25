"""仅供 MCP Gateway 等受信服务调用的内部 REST 接口。"""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.application.access import (
    KnowledgeAccessContext,
    KnowledgeAccessDenied,
    KnowledgePermission,
)
from app.application.knowledge import (
    KnowledgeSearchError,
    get_knowledge_application_service,
)
from app.schemas.internal import InternalChunkResponse
from app.schemas.knowledge import KnowledgeSearchRequest, KnowledgeSearchResponse
from app.schemas.search import SearchRequest, SearchResponse
from app.security.service_auth import require_internal_service
from app.stores.chunk_store import get_chunk_store

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
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


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
        raise HTTPException(status_code=error.status_code, detail=str(error)) from error


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
        access_context.require(
            KnowledgePermission.RESOURCE_READ,
            knowledge_base_id=knowledge_base_id,
        )
    except KnowledgeAccessDenied as error:
        raise HTTPException(status_code=403, detail=str(error)) from error
    row = await get_chunk_store().get_scoped_active(
        knowledge_base_id=knowledge_base_id,
        chunk_id=chunk_id,
    )
    if row is None:
        # 跨库访问与真实不存在统一返回 404，避免枚举 Chunk 归属。
        raise HTTPException(status_code=404, detail="Chunk not found")
    chunk, document, knowledge_base = row
    return InternalChunkResponse(
        request_id=request.state.request_id,
        knowledge_base_id=knowledge_base.id,
        content_revision=knowledge_base.content_revision,
        chunk_id=chunk.id,
        document_id=document.id,
        text=chunk.text,
        title=document.title,
        filename=document.original_filename,
        page_no=chunk.page_no,
        source_uri=document.source_uri,
        source_type=document.source_type,
        metadata=chunk.metadata_json or {},
    )
