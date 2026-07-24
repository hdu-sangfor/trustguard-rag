"""入库 API 数据结构。"""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field

from app.domain import IngestJobStatus, IngestStep


class IngestJobCreateResponse(BaseModel):
    job_id: str
    status: IngestJobStatus
    knowledge_base_id: str
    embedding_profile: str = "configured"
    embedding_model: str | None = None
    embedding_dim: int | None = None


class IngestJobResponse(BaseModel):
    id: str
    source_type: str
    status: IngestJobStatus
    current_step: IngestStep | None = None
    document_id: str | None = None
    pending_document_id: str | None = None
    conflict_candidates: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    attempt: int = 0
    max_attempts: int = 3
    step_logs: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None
    knowledge_base_id: str | None = None
    embedding_profile: str = "configured"
    embedding_model: str | None = None
    embedding_dim: int | None = None


class ConflictResolveRequest(BaseModel):
    keep_document_id: str
