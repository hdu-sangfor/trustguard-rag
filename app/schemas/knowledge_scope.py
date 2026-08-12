"""Knowledge Scope administration contracts."""

from __future__ import annotations

from datetime import datetime

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain import RetrievalMode
from app.schemas.knowledge import KnowledgeScope


class KnowledgeScopeUpdate(BaseModel):
    """Replace the manually managed policy and bindings for one Scope."""

    model_config = ConfigDict(extra="forbid")

    knowledge_base_ids: list[str] = Field(min_length=1, max_length=16)
    default_mode: RetrievalMode = RetrievalMode.AUTO
    per_knowledge_base_limit: int = Field(default=20, ge=1, le=100)
    allowed_content_types: list[str] = Field(default_factory=list, max_length=100)
    allowed_workflow_types: list[str] = Field(default_factory=list, max_length=32)

    @field_validator("knowledge_base_ids")
    @classmethod
    def normalize_knowledge_base_ids(cls, values: list[str]) -> list[str]:
        normalized = list(dict.fromkeys(value.strip() for value in values if value.strip()))
        if not normalized:
            raise ValueError("knowledge_base_ids cannot be empty")
        return normalized

    @field_validator("allowed_content_types", "allowed_workflow_types")
    @classmethod
    def normalize_allowlists(cls, values: list[str]) -> list[str]:
        return list(dict.fromkeys(value.strip() for value in values if value.strip()))


class KnowledgeScopeResponse(BaseModel):
    scope: KnowledgeScope
    knowledge_base_ids: list[str]
    system_knowledge_base_ids: list[str] = Field(default_factory=list)
    default_mode: RetrievalMode
    per_knowledge_base_limit: int
    allowed_content_types: list[str]
    allowed_workflow_types: list[str]
    created_at: datetime | None = None
    updated_at: datetime | None = None


class KnowledgeScopeListResponse(BaseModel):
    items: list[KnowledgeScopeResponse]
    total: int
