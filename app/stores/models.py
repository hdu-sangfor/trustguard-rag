"""入库相关 SQLAlchemy ORM 模型。"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum
from typing import Any

from sqlalchemy import Boolean, DateTime, Enum as SqlEnum, Float, Index, Integer, JSON, String, Text, func
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column

from app.domain import DocumentStatus, IngestJobStatus, IngestStep, OutboxStatus, OcrRegionStatus


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
    is_default: Mapped[bool] = mapped_column(Boolean, default=False)
    is_system: Mapped[bool] = mapped_column(Boolean, default=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now()
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=False), server_default=func.now(), onupdate=func.now()
    )


class DocumentRow(Base):
    __tablename__ = "documents"
    __table_args__ = (Index("idx_documents_knowledge_base", "knowledge_base_id"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    knowledge_base_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
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
    __table_args__ = (Index("idx_jobs_lease", "status", "lease_expires_at"),)

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
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
