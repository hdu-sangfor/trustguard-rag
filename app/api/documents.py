"""文档查询 HTTP API。"""

from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Query, Response, status

from app.domain import DocumentStatus
from app.schemas.document import (
    ArtifactsResponse,
    ChunkResponse,
    DocumentListResponse,
    DocumentResponse,
    DocumentUpdateRequest,
)
from app.stores.blob_store import get_blob_store
from app.stores.chunk_store import get_chunk_store
from app.stores.document_store import get_document_store
from app.workers.eager import dispatch_eager

router = APIRouter(prefix="/v1/documents", tags=["documents"])

_DELETABLE_DOCUMENT_STATUSES = frozenset(
    {
        DocumentStatus.READY,
        DocumentStatus.FAILED,
        DocumentStatus.DELETING,
        DocumentStatus.SUPERSEDED,
    }
)


def _document_response(doc) -> DocumentResponse:
    """将文档行映射为公开的文档响应结构。"""
    return DocumentResponse(
        id=doc.id,
        knowledge_base_id=doc.knowledge_base_id,
        source_type=doc.source_type,
        source_uri=doc.source_uri,
        content_hash=doc.content_hash,
        status=doc.status,
        title=doc.title,
        mime_type=doc.mime_type,
        original_filename=doc.original_filename,
        doc_version=doc.doc_version,
        blob_path=doc.blob_path,
        metadata=doc.metadata_json,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _chunk_response(chunk) -> ChunkResponse:
    """将分块行映射为公开的分块响应结构。"""
    return ChunkResponse(
        id=chunk.id,
        chunk_index=chunk.chunk_index,
        text=chunk.text,
        token_count=chunk.token_count,
        page_no=chunk.page_no,
        metadata=chunk.metadata_json,
    )


def _safe_artifact_name(filename: str) -> str:
    """读取产物文件前拒绝路径穿越和空文件名。"""
    path = PurePosixPath(filename)
    if path.name != filename or filename in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid artifact filename")
    return filename


async def _get_document_or_404(document_id: str):
    """加载文档行，不存在时抛出 API 层 404 错误。"""
    doc = await get_document_store().get(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("", response_model=DocumentListResponse)
async def list_documents(
    offset: int = Query(default=0, ge=0),
    limit: int = Query(default=20, ge=1, le=100),
    status_filter: DocumentStatus | None = Query(default=None, alias="status"),
    query: str | None = Query(default=None, alias="q", max_length=512),
    knowledge_base_id: str | None = Query(default=None, max_length=36),
) -> DocumentListResponse:
    """分页列出知识库文档，支持状态和关键词筛选。"""
    rows, total = await get_document_store().list(
        offset=offset,
        limit=limit,
        status=status_filter,
        query=query.strip() if query and query.strip() else None,
        knowledge_base_id=knowledge_base_id,
    )
    return DocumentListResponse(
        items=[_document_response(row) for row in rows],
        total=total,
        offset=offset,
        limit=limit,
    )


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(document_id: str) -> DocumentResponse:
    """返回单个已入库文档的元数据。"""
    doc = await _get_document_or_404(document_id)
    return _document_response(doc)


@router.patch("/{document_id}", response_model=DocumentResponse)
async def update_document(document_id: str, request: DocumentUpdateRequest) -> DocumentResponse:
    """更新文档标题、原始文件名或业务元数据。"""
    await _get_document_or_404(document_id)
    supplied = request.model_fields_set
    if not supplied:
        raise HTTPException(status_code=400, detail="At least one editable field is required")

    values = request.model_dump(exclude_unset=True)
    for key in ("title", "original_filename"):
        value = values.get(key)
        if isinstance(value, str):
            value = value.strip()
            if not value:
                raise HTTPException(status_code=422, detail=f"{key} cannot be blank")
            values[key] = value
    if "metadata" in values:
        values["metadata_json"] = values.pop("metadata")

    doc = await get_document_store().update(document_id, values)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return _document_response(doc)


@router.delete("/{document_id}", status_code=status.HTTP_202_ACCEPTED)
async def delete_document(document_id: str) -> Response:
    """将文档标记为删除中，并将幂等的双索引清理命令加入队列。"""
    doc = await _get_document_or_404(document_id)
    if doc.status not in _DELETABLE_DOCUMENT_STATUSES:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=f"Document cannot be deleted while status is {doc.status}",
        )
    try:
        event = await get_document_store().request_delete(document_id)
    except LookupError:
        raise HTTPException(status_code=404, detail="Document not found")
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    await dispatch_eager(event)
    return Response(status_code=status.HTTP_202_ACCEPTED)


@router.get("/{document_id}/chunks", response_model=list[ChunkResponse])
async def list_document_chunks(document_id: str) -> list[ChunkResponse]:
    """按原始分块序号返回文档的所有分块。"""
    await _get_document_or_404(document_id)
    chunks = await get_chunk_store().list_for_document(document_id)
    return [_chunk_response(chunk) for chunk in chunks]


@router.get("/{document_id}/artifacts", response_model=ArtifactsResponse)
async def list_document_artifacts(document_id: str) -> ArtifactsResponse:
    """列出文档产物包中已提交的文件。"""
    doc = await _get_document_or_404(document_id)
    files = get_blob_store().list_artifacts(document_id, doc.doc_version)
    return ArtifactsResponse(document_id=document_id, files=files, blob_path=doc.blob_path)


@router.get("/{document_id}/artifacts/{filename}")
async def download_document_artifact(document_id: str, filename: str) -> Response:
    """校验文档和文件名后返回单个产物文件。"""
    filename = _safe_artifact_name(filename)
    doc = await _get_document_or_404(document_id)
    blob_path = doc.blob_path or f"artifacts/{document_id}/v{doc.doc_version}"
    relative_path = f"{blob_path.rstrip('/')}/{filename}"
    blobs = get_blob_store()
    if not blobs.exists(relative_path):
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(content=blobs.read(relative_path), media_type=media_type)
