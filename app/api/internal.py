"""仅供 MCP Gateway 等受信服务调用的内部 REST 接口。"""

from __future__ import annotations

import secrets
from typing import Annotated

from fastapi import APIRouter, Depends, Header, HTTPException, Request

from app.schemas.internal import InternalChunkResponse
from app.settings import get_settings
from app.stores.chunk_store import get_chunk_store

router = APIRouter(prefix="/v1/internal", tags=["internal"])


async def require_internal_service(
    authorization: Annotated[str | None, Header()] = None,
) -> None:
    """使用独立 Bearer Token 保护管理面与前端不需要访问的内部接口。"""
    expected = get_settings().internal_service_token
    if not expected:
        raise HTTPException(
            status_code=503,
            detail="Internal service authentication is not configured",
        )
    scheme, separator, credential = (authorization or "").partition(" ")
    if (
        not separator
        or scheme.casefold() != "bearer"
        or not secrets.compare_digest(credential, expected)
    ):
        raise HTTPException(
            status_code=401,
            detail="Invalid internal service credentials",
            headers={"WWW-Authenticate": "Bearer"},
        )


@router.get(
    "/knowledge-bases/{knowledge_base_id}/chunks/{chunk_id}",
    response_model=InternalChunkResponse,
    dependencies=[Depends(require_internal_service)],
)
async def get_scoped_chunk(
    knowledge_base_id: str,
    chunk_id: str,
    request: Request,
) -> InternalChunkResponse:
    """按知识库和 Chunk 双重约束读取已发布内容。"""
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
