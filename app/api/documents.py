"""Document query HTTP API."""
from __future__ import annotations

import mimetypes
from pathlib import PurePosixPath

from fastapi import APIRouter, HTTPException, Response

from app.schemas.document import ArtifactsResponse, ChunkResponse, DocumentResponse
from app.stores.blob_store import get_blob_store
from app.stores.chunk_store import get_chunk_store
from app.stores.document_store import get_document_store

router = APIRouter(prefix="/v1/documents", tags=["documents"])


def _document_response(doc) -> DocumentResponse:
    return DocumentResponse(
        id=doc.id,
        source_type=doc.source_type,
        source_uri=doc.source_uri,
        content_hash=doc.content_hash,
        status=doc.status,
        mime_type=doc.mime_type,
        original_filename=doc.original_filename,
        doc_version=doc.doc_version,
        blob_path=doc.blob_path,
        metadata=doc.metadata_json,
        created_at=doc.created_at,
        updated_at=doc.updated_at,
    )


def _chunk_response(chunk) -> ChunkResponse:
    return ChunkResponse(
        id=chunk.id,
        chunk_index=chunk.chunk_index,
        text=chunk.text,
        token_count=chunk.token_count,
        page_no=chunk.page_no,
        metadata=chunk.metadata_json,
    )


def _safe_artifact_name(filename: str) -> str:
    path = PurePosixPath(filename)
    if path.name != filename or filename in {"", ".", ".."}:
        raise HTTPException(status_code=400, detail="Invalid artifact filename")
    return filename


async def _get_document_or_404(document_id: str):
    doc = await get_document_store().get(document_id)
    if not doc:
        raise HTTPException(status_code=404, detail="Document not found")
    return doc


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(document_id: str) -> DocumentResponse:
    doc = await _get_document_or_404(document_id)
    return _document_response(doc)


@router.get("/{document_id}/chunks", response_model=list[ChunkResponse])
async def list_document_chunks(document_id: str) -> list[ChunkResponse]:
    await _get_document_or_404(document_id)
    chunks = await get_chunk_store().list_for_document(document_id)
    return [_chunk_response(chunk) for chunk in chunks]


@router.get("/{document_id}/artifacts", response_model=ArtifactsResponse)
async def list_document_artifacts(document_id: str) -> ArtifactsResponse:
    doc = await _get_document_or_404(document_id)
    files = get_blob_store().list_artifacts(document_id, doc.doc_version)
    return ArtifactsResponse(document_id=document_id, files=files, blob_path=doc.blob_path)


@router.get("/{document_id}/artifacts/{filename}")
async def download_document_artifact(document_id: str, filename: str) -> Response:
    filename = _safe_artifact_name(filename)
    doc = await _get_document_or_404(document_id)
    blob_path = doc.blob_path or f"artifacts/{document_id}/v{doc.doc_version}"
    relative_path = f"{blob_path.rstrip('/')}/{filename}"
    blobs = get_blob_store()
    if not blobs.exists(relative_path):
        raise HTTPException(status_code=404, detail="Artifact not found")
    media_type = mimetypes.guess_type(filename)[0] or "application/octet-stream"
    return Response(content=blobs.read(relative_path), media_type=media_type)
