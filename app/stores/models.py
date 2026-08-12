"""入库相关 SQLAlchemy ORM 模型。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import (
    Boolean,
    DateTime,
    Enum as SqlEnum,
    Float,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    func,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain import DocumentStatus, IngestJobStatus, IngestStep, OutboxStatus, OcrRegionStatus
from app.domain.crawler import CrawlJobStatus


def _enum_values(enum_type: type[StrEnum]) -> list[str]:
    """让 SQLAlchemy 持久化枚举值而不是成员名。"""
    return [member.value for member in enum_type]


class Base(DeclarativeBase):
    pass


class KnowledgeBaseRow(Base):
    """知识库配置；模型在知识库级冻结，避免请求级向量空间混用。"""

    __tablename__ = "knowledge_bases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(128), unique=True)
    description: Mapped[str | None] = mapped_column(String(1024), nullable=True)
    embedding_profile: Mapped[str] = mapped_column(String(64))
    embedding_provider: Mapped[str] = mapped_column(String(32))
    embedding_api_driver: Mapped[str] = mapped_column(
        String(32), default="openai_compatible"
    )
    embedding_model: Mapped[str] = mapped_column(String(128))
    embedding_dim: Mapped[int] = mapped_column(Integer)
    content_revision: Mapped[int] = mapped_column(Integer, default=0)
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeScopeRow(Base):
    """Persisted policy for one logical knowledge scope."""

    __tablename__ = "knowledge_scopes"

    scope: Mapped[str] = mapped_column(String(64), primary_key=True)
    default_mode: Mapped[str] = mapped_column(String(32), default="auto")
    per_knowledge_base_limit: Mapped[int] = mapped_column(Integer, default=20)
    allowed_content_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    allowed_workflow_types: Mapped[list[str]] = mapped_column(JSON, default=list)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class KnowledgeScopeBindingRow(Base):
    """Ordered logical-scope to physical-knowledge-base binding."""

    __tablename__ = "knowledge_scope_bindings"
    __table_args__ = (
        Index("idx_knowledge_scope_binding_kb", "knowledge_base_id"),
    )

    scope: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("knowledge_scopes.scope", ondelete="CASCADE"),
        primary_key=True,
    )
    knowledge_base_id: Mapped[str] = mapped_column(
        String(36),
        ForeignKey("knowledge_bases.id", ondelete="CASCADE"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, default=0)
    binding_type: Mapped[str] = mapped_column(String(32), default="manual")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class MigrationStateRow(Base):
    """记录可重入后台迁移的状态，供 readiness 和运维排障使用。"""

    __tablename__ = "migration_states"

    name: Mapped[str] = mapped_column(String(128), primary_key=True)
    status: Mapped[str] = mapped_column(String(32))
    processed_count: Mapped[int] = mapped_column(Integer, default=0)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("idx_documents_knowledge_base", "knowledge_base_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_bases.id", ondelete="RESTRICT"),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32))
    source_uri: Mapped[str] = mapped_column(String(2048))
    content_hash: Mapped[str] = mapped_column(String(64))
    title: Mapped[str | None] = mapped_column(String(512), nullable=True)
    mime_type: Mapped[str | None] = mapped_column(String(128), nullable=True)
    original_filename: Mapped[str | None] = mapped_column(String(512), nullable=True)
    doc_version: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[DocumentStatus] = mapped_column(
        SqlEnum(
            DocumentStatus,
            values_callable=_enum_values,
            native_enum=False,
            length=32,
        )
    )
    blob_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class ChunkRow(Base):
    __tablename__ = "chunks"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36))
    chunk_index: Mapped[int] = mapped_column(Integer)
    text: Mapped[str] = mapped_column(Text)
    token_count: Mapped[int] = mapped_column(Integer, default=0)
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    embedding_model: Mapped[str | None] = mapped_column(String(64), nullable=True)
    embedding_dim: Mapped[int | None] = mapped_column(Integer, nullable=True)
    qdrant_point_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[str] = mapped_column(String(32), default="active")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class IngestJobRow(Base):
    __tablename__ = "ingest_jobs"
    __table_args__ = (
        Index("idx_jobs_lease", "status", "lease_expires_at"),
        Index("idx_jobs_knowledge_base_status", "knowledge_base_id", "status"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(
        String(36),
        ForeignKey("knowledge_bases.id", ondelete="SET NULL"),
        nullable=True,
    )
    source_type: Mapped[str] = mapped_column(String(32))
    source: Mapped[str] = mapped_column(Text)
    options_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    status: Mapped[IngestJobStatus] = mapped_column(
        SqlEnum(
            IngestJobStatus,
            values_callable=_enum_values,
            native_enum=False,
            length=32,
        )
    )
    current_step: Mapped[IngestStep | None] = mapped_column(
        SqlEnum(
            IngestStep,
            values_callable=_enum_values,
            native_enum=False,
            length=32,
        ),
        nullable=True,
    )
    document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    pending_document_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    conflict_candidates_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    error_code: Mapped[str | None] = mapped_column(String(64), nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    step_logs: Mapped[list[dict[str, Any]] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    lease_owner: Mapped[str | None] = mapped_column(String(128), nullable=True)
    lease_token: Mapped[str | None] = mapped_column(String(36), nullable=True)
    lease_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    heartbeat_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class CrawlJobRow(Base):
    """一次网络采集运行；单个运行可扇出多个文档入库任务。"""

    __tablename__ = "crawl_jobs"
    __table_args__ = (
        Index("idx_crawl_jobs_status_updated", "status", "updated_at"),
        Index("idx_crawl_jobs_knowledge_base", "knowledge_base_id", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base_id: Mapped[str] = mapped_column(String(36))
    status: Mapped[CrawlJobStatus] = mapped_column(
        SqlEnum(
            CrawlJobStatus,
            values_callable=_enum_values,
            native_enum=False,
            length=32,
        ),
        default=CrawlJobStatus.QUEUED,
    )
    config_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    progress_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    ingest_job_ids_json: Mapped[list[str] | None] = mapped_column(JSON, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=3)
    cancel_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    pause_requested: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class CrawlUrlRecordRow(Base):
    """知识库级 URL 去重与最近采集结果。"""

    __tablename__ = "crawl_url_records"

    knowledge_base_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    url_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    url: Mapped[str] = mapped_column(String(2048))
    content_hash: Mapped[str | None] = mapped_column(String(64), nullable=True)
    ingest_job_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    last_status: Mapped[str] = mapped_column(String(32), default="pending")
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    last_crawled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class OutboxEventRow(Base):
    """等待可靠发布到 RabbitMQ 的领域命令。"""

    __tablename__ = "outbox_events"
    __table_args__ = (
        Index("idx_outbox_dispatch", "status", "next_attempt_at", "created_at"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_type: Mapped[str] = mapped_column(String(64))
    aggregate_id: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    status: Mapped[OutboxStatus] = mapped_column(
        SqlEnum(
            OutboxStatus,
            values_callable=_enum_values,
            native_enum=False,
            length=32,
        ),
        default=OutboxStatus.PENDING,
    )
    attempt: Mapped[int] = mapped_column(Integer, default=0)
    max_attempts: Mapped[int] = mapped_column(Integer, default=20)
    next_attempt_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    locked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)
    last_error: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=False), nullable=True)


class IngestCursorRow(Base):
    """增量同步水位（对照 LangChain RecordManager 的 namespace/key 账本语义）。"""

    __tablename__ = "ingest_cursors"

    cursor_key: Mapped[str] = mapped_column(String(64), primary_key=True)
    cursor_value: Mapped[str] = mapped_column(String(256))
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class OcrRegionRow(Base):
    """PDF/图片 OCR 区域及人工复核状态。"""

    __tablename__ = "ocr_regions"
    __table_args__ = (Index("idx_ocr_document", "document_id", "status"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    document_id: Mapped[str] = mapped_column(String(36))
    page_no: Mapped[int | None] = mapped_column(Integer, nullable=True)
    bbox_json: Mapped[list[float] | None] = mapped_column(JSON, nullable=True)
    crop_blob_path: Mapped[str | None] = mapped_column(String(512), nullable=True)
    ocr_text: Mapped[str] = mapped_column(Text, default="")
    corrected_text: Mapped[str | None] = mapped_column(Text, nullable=True)
    status: Mapped[OcrRegionStatus] = mapped_column(
        SqlEnum(
            OcrRegionStatus,
            values_callable=_enum_values,
            native_enum=False,
            length=32,
        ),
        default=OcrRegionStatus.PENDING,
    )
    provider: Mapped[str | None] = mapped_column(String(64), nullable=True)
    confidence: Mapped[float | None] = mapped_column(Float, nullable=True)
    error_message: Mapped[str | None] = mapped_column(Text, nullable=True)
    metadata_json: Mapped[dict[str, Any] | None] = mapped_column(JSON, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class ExperienceItemRow(Base):
    """Audited Experience authority record (not a document ingest artifact)."""

    __tablename__ = "experience_items"
    __table_args__ = (
        Index(
            "uq_experience_source_external",
            "source_system",
            "external_id",
            unique=True,
        ),
        Index("idx_experience_status", "status"),
        Index("idx_experience_workflow", "workflow_type", "knowledge_scope"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    external_id: Mapped[str] = mapped_column(String(128))
    source_system: Mapped[str] = mapped_column(String(64))
    source_revision: Mapped[int] = mapped_column(Integer)
    knowledge_base_id: Mapped[str] = mapped_column(String(36))
    knowledge_scope: Mapped[str] = mapped_column(String(64))
    workflow_type: Mapped[str] = mapped_column(String(64))
    experience_type: Mapped[str] = mapped_column(String(64))
    workspace_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    visibility: Mapped[str] = mapped_column(String(32))
    status: Mapped[str] = mapped_column(String(32), default="candidate")
    index_status: Mapped[str] = mapped_column(String(32), default="not_indexed")
    conditions_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    action_summary: Mapped[str] = mapped_column(Text)
    outcome_summary: Mapped[str] = mapped_column(Text)
    skill_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    phase: Mapped[str | None] = mapped_column(String(64), nullable=True)
    source_task_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    evidence_refs_json: Mapped[list[dict[str, Any]] | None] = mapped_column(
        JSON, nullable=True
    )
    search_text: Mapped[str] = mapped_column(Text, default="")
    usage_count: Mapped[int] = mapped_column(Integer, default=0)
    success_count: Mapped[int] = mapped_column(Integer, default=0)
    failure_count: Mapped[int] = mapped_column(Integer, default=0)
    expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class ExperienceFeedbackEventRow(Base):
    """Idempotent feedback events; does not change Experience status in Slice A."""

    __tablename__ = "experience_feedback_events"
    __table_args__ = (
        Index("uq_experience_feedback_event_id", "event_id", unique=True),
        Index("idx_experience_feedback_item", "experience_id"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    event_id: Mapped[str] = mapped_column(String(128))
    experience_id: Mapped[str] = mapped_column(String(36))
    task_id: Mapped[str] = mapped_column(String(128))
    workflow_type: Mapped[str] = mapped_column(String(64))
    outcome: Mapped[str] = mapped_column(String(32))
    evidence_level: Mapped[str] = mapped_column(String(32))
    notes: Mapped[str | None] = mapped_column(Text, nullable=True)
    occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=False), nullable=True
    )
    actor_service_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class ExperienceStatusHistoryRow(Base):
    """Audited Experience status transitions."""

    __tablename__ = "experience_status_history"
    __table_args__ = (Index("idx_experience_status_history_item", "experience_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    experience_id: Mapped[str] = mapped_column(String(36))
    from_status: Mapped[str] = mapped_column(String(32))
    to_status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str | None] = mapped_column(String(512), nullable=True)
    actor_service_id: Mapped[str | None] = mapped_column(String(128), nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )


class ExperienceIdempotencyRow(Base):
    """Optional Idempotency-Key short-window dedupe for upsert/feedback."""

    __tablename__ = "experience_idempotency_keys"
    __table_args__ = (
        Index(
            "uq_experience_idempotency",
            "actor_service_id",
            "idempotency_key",
            unique=True,
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    actor_service_id: Mapped[str] = mapped_column(String(128))
    idempotency_key: Mapped[str] = mapped_column(String(128))
    operation: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[dict[str, Any]] = mapped_column(JSON)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=False))
