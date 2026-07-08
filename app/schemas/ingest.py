"""Ingest API schemas."""
from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, Field


class IngestJobCreateResponse(BaseModel):
    job_id: str
    status: str


class IngestJobResponse(BaseModel):
    id: str
    source_type: str
    status: str
    current_step: str | None = None
    document_id: str | None = None
    pending_document_id: str | None = None
    conflict_candidates: list[str] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None
    step_logs: list[dict[str, Any]] = Field(default_factory=list)
    created_at: datetime | None = None
    started_at: datetime | None = None
    finished_at: datetime | None = None


class ConflictResolveRequest(BaseModel):
    keep_document_id: str
