"""RAG 内部 Service-to-Service 接口契约。"""

from __future__ import annotations

from typing import Any, Literal

from pydantic import BaseModel


class InternalContentRevisionResponse(BaseModel):
    schema_version: Literal["trustguard-internal-content-revision-v1"] = (
        "trustguard-internal-content-revision-v1"
    )
    request_id: str
    knowledge_base_id: str
    content_revision: int


class InternalChunkResponse(BaseModel):
    schema_version: Literal["trustguard-internal-chunk-v1"] = "trustguard-internal-chunk-v1"
    request_id: str
    knowledge_base_id: str
    content_revision: int
    source_revision: int
    content_hash: str
    chunk_id: str
    document_id: str
    text: str
    title: str | None = None
    filename: str | None = None
    page_no: int | None = None
    source_uri: str
    source_type: str
    metadata: dict[str, Any]
