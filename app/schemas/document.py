"""文档 API 数据结构。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class DocumentResponse(BaseModel):
    id: str
    source_type: str
    source_uri: str
    content_hash: str
    status: str
    mime_type: str | None = None
    original_filename: str | None = None
    doc_version: int
    blob_path: str | None = None
    metadata: dict[str, Any] | None = None
    created_at: datetime | None = None
    updated_at: datetime | None = None


class ChunkResponse(BaseModel):
    id: str
    chunk_index: int
    text: str
    token_count: int
    page_no: int | None = None
    metadata: dict[str, Any] | None = None


class ArtifactsResponse(BaseModel):
    document_id: str
    files: list[str]
    blob_path: str | None = None
