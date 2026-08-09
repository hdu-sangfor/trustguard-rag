"""Pydantic models for Experience REST APIs (contracts/v1 aligned)."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.experience import ExperienceIndexStatus, ExperienceStatus


class ExperienceEvidenceRef(BaseModel):
    model_config = ConfigDict(extra="forbid")

    type: Literal["task_chunk", "artifact", "trace"]
    ref: str = Field(min_length=1, max_length=512)


class ExperienceUpsertRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["trustguard-experience-upsert-v1"]
    external_id: str = Field(min_length=1, max_length=128)
    source_system: str = Field(min_length=1, max_length=64)
    source_revision: int = Field(ge=1)
    knowledge_scope: Literal["penetration", "alert-triage"]
    workflow_type: Literal["penetration", "alert-triage"]
    experience_type: str = Field(min_length=1, max_length=64)
    workspace_id: str | None = Field(default=None, min_length=1, max_length=128)
    visibility: Literal["global", "workspace"]
    conditions: dict[str, Any] = Field(default_factory=dict)
    action_summary: str = Field(min_length=1, max_length=8000)
    outcome_summary: str = Field(min_length=1, max_length=8000)
    skill_id: str | None = Field(default=None, max_length=128)
    phase: str | None = Field(default=None, max_length=64)
    source_task_id: str | None = Field(default=None, max_length=128)
    evidence_refs: list[ExperienceEvidenceRef] = Field(default_factory=list, max_length=20)
    expires_at: datetime | None = None

    @field_validator("conditions")
    @classmethod
    def limit_conditions(cls, value: dict[str, Any]) -> dict[str, Any]:
        if len(value) > 50:
            raise ValueError("conditions may have at most 50 properties")
        return value

    @model_validator(mode="after")
    def validate_visibility_and_scope(self) -> ExperienceUpsertRequest:
        if self.visibility == "workspace" and not self.workspace_id:
            raise ValueError("workspace_id is required when visibility=workspace")
        if self.visibility == "global" and self.workspace_id is not None:
            raise ValueError("workspace_id must be null when visibility=global")
        if self.workflow_type != self.knowledge_scope:
            raise ValueError("workflow_type and knowledge_scope must match")
        return self


class ExperienceFeedbackRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    schema_version: Literal["trustguard-experience-feedback-v1"]
    event_id: str = Field(min_length=1, max_length=128)
    experience_id: str = Field(min_length=1, max_length=128)
    task_id: str = Field(min_length=1, max_length=128)
    workflow_type: Literal["penetration", "alert-triage"]
    outcome: Literal["success", "failure", "neutral"]
    evidence_level: Literal["reported", "observed", "verified"]
    notes: str | None = Field(default=None, max_length=2000)
    occurred_at: datetime | None = None


class ExperienceStatusPatchRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ExperienceStatus
    reason: str | None = Field(default=None, max_length=512)


class ExperienceResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    external_id: str
    source_system: str
    source_revision: int
    knowledge_base_id: str
    knowledge_scope: str
    workflow_type: str
    experience_type: str
    workspace_id: str | None
    visibility: str
    status: ExperienceStatus
    index_status: ExperienceIndexStatus
    conditions: dict[str, Any]
    action_summary: str
    outcome_summary: str
    skill_id: str | None
    phase: str | None
    source_task_id: str | None
    evidence_refs: list[ExperienceEvidenceRef]
    usage_count: int
    success_count: int
    failure_count: int
    expires_at: datetime | None
    created_at: datetime
    updated_at: datetime


class ExperienceListResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    items: list[ExperienceResponse]
    total: int


class ExperienceFeedbackResponse(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    event_id: str
    experience_id: str
    task_id: str
    workflow_type: str
    outcome: str
    evidence_level: str
    notes: str | None
    occurred_at: datetime | None
    duplicated: bool = False
    experience_status: ExperienceStatus
