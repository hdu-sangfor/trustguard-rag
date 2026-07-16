"""入库任务存储。"""

from __future__ import annotations

import os
import socket
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.settings import get_settings
from app.core.ingest.errors import (
    MAX_ATTEMPTS_EXCEEDED,
    WORKER_LEASE_EXPIRED,
)
from app.domain import (
    ClaimOutcome,
    CleanupAction,
    DocumentStatus,
    IngestJobStatus,
    IngestStep,
)
from app.stores.db import get_engine
from app.stores.models import DocumentRow, IngestJobRow
from app.stores.outbox_store import OutboxEvent, add_outbox_event, event_from_row
from app.workers.messages import CLEANUP_DOCUMENT, INGEST_DOCUMENT, RESOLVE_CONFLICT


def _utcnow() -> datetime:
    """返回适用于 MySQL datetime 字段的无时区 UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class LeaseLostError(RuntimeError):
    """The caller no longer owns the right to mutate a running job."""


@dataclass(frozen=True, slots=True)
class JobLease:
    job_id: str
    token: str
    owner: str
    expires_at: datetime


ClaimResult = JobLease | ClaimOutcome


def _worker_owner() -> str:
    return f"{socket.gethostname()}:{os.getpid()}"


class JobStore:
    async def create(
        self,
        *,
        source_type: str,
        source: str,
        options: dict[str, Any] | None = None,
        status: IngestJobStatus | str = IngestJobStatus.QUEUED,
        job_id: str | None = None,
    ) -> IngestJobRow:
        """创建 queued 状态的入库任务行，并初始化选项和日志。"""
        row = IngestJobRow(
            id=job_id or str(uuid4()),
            source_type=source_type,
            source=source,
            options_json=options,
            status=IngestJobStatus(status),
            step_logs=[],
        )
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def create_ingest_command(
        self,
        *,
        job_id: str,
        source_type: str,
        source: str,
        options: dict[str, Any] | None = None,
    ) -> tuple[IngestJobRow, OutboxEvent]:
        """Atomically create an ingest job and its dispatch event."""
        row = IngestJobRow(
            id=job_id,
            source_type=source_type,
            source=source,
            options_json=options,
            status=IngestJobStatus.QUEUED,
            step_logs=[],
        )
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            session.add(row)
            event_row = add_outbox_event(
                session,
                event_type=INGEST_DOCUMENT,
                aggregate_id=job_id,
                payload={"job_id": job_id},
            )
            await session.commit()
            return row, event_from_row(event_row)

    async def get(self, job_id: str) -> IngestJobRow | None:
        """按主键加载单个入库任务。"""
        async with AsyncSession(get_engine()) as session:
            return await session.get(IngestJobRow, job_id)

    async def mark_running(
        self,
        job_id: str,
        step: IngestStep | str,
        *,
        lease_token: str | None = None,
    ) -> None:
        """记录流水线步骤并刷新 Worker 租约，不增加任务 attempt。"""
        async with AsyncSession(get_engine()) as session:
            job = await session.get(IngestJobRow, job_id)
            if not job:
                return
            if lease_token is not None and (
                job.status != IngestJobStatus.RUNNING or job.lease_token != lease_token
            ):
                raise LeaseLostError(f"lease lost for job {job_id}")
            now = _utcnow()
            logs = list(job.step_logs or [])
            logs.append({"step": step, "status": "started", "at": now.isoformat()})
            job.status = IngestJobStatus.RUNNING
            job.current_step = IngestStep(step)
            if job.started_at is None:
                job.started_at = now
            if lease_token is not None:
                job.heartbeat_at = now
                job.lease_expires_at = now + timedelta(
                    seconds=get_settings().worker_job_lease_seconds
                )
            job.step_logs = logs
            await session.commit()

    async def claim(
        self,
        job_id: str,
        *,
        allowed_statuses: tuple[IngestJobStatus | str, ...],
        owner: str | None = None,
    ) -> ClaimResult:
        """Atomically claim one job; expired running leases may be reclaimed."""
        now = _utcnow()
        settings = get_settings()
        async with AsyncSession(get_engine()) as session:
            job = await session.get(IngestJobRow, job_id, with_for_update=True)
            if not job:
                return ClaimOutcome.TERMINAL
            normalized_statuses = tuple(IngestJobStatus(value) for value in allowed_statuses)
            reclaimable = job.status == IngestJobStatus.RUNNING and (
                job.lease_expires_at is None or job.lease_expires_at <= now
            )
            if job.status not in normalized_statuses and not reclaimable:
                return (
                    ClaimOutcome.BUSY
                    if job.status == IngestJobStatus.RUNNING
                    else ClaimOutcome.TERMINAL
                )
            if (job.attempt or 0) >= (job.max_attempts or 3):
                job.status = IngestJobStatus.FAILED
                job.current_step = IngestStep.FAILED
                job.error_code = MAX_ATTEMPTS_EXCEEDED
                job.error_message = "Worker retry limit exceeded"
                job.finished_at = now
                self._clear_lease(job)
                for document_id in {job.document_id, job.pending_document_id} - {None}:
                    doc = await session.get(DocumentRow, document_id)
                    if doc and doc.status in {
                        DocumentStatus.STAGING,
                        DocumentStatus.INDEXING,
                    }:
                        doc.status = DocumentStatus.FAILED
                        doc.updated_at = now
                        add_outbox_event(
                            session,
                            event_type=CLEANUP_DOCUMENT,
                            aggregate_id=doc.id,
                            payload={
                                "document_id": doc.id,
                                "action": CleanupAction.ROLLBACK,
                            },
                        )
                await session.commit()
                return ClaimOutcome.TERMINAL
            job.status = IngestJobStatus.RUNNING
            job.attempt = (job.attempt or 0) + 1
            if job.started_at is None:
                job.started_at = now
            job.finished_at = None
            lease_owner = owner or _worker_owner()
            lease_token = str(uuid4())
            lease_expires_at = now + timedelta(seconds=settings.worker_job_lease_seconds)
            job.lease_owner = lease_owner
            job.lease_token = lease_token
            job.heartbeat_at = now
            job.lease_expires_at = lease_expires_at
            await session.commit()
            return JobLease(
                job_id=job_id,
                token=lease_token,
                owner=lease_owner,
                expires_at=lease_expires_at,
            )

    async def heartbeat(self, job_id: str, lease_token: str) -> bool:
        """Renew a lease only while the caller still owns the running job."""
        now = _utcnow()
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                update(IngestJobRow)
                .where(
                    IngestJobRow.id == job_id,
                    IngestJobRow.status == IngestJobStatus.RUNNING,
                    IngestJobRow.lease_token == lease_token,
                )
                .values(
                    heartbeat_at=now,
                    lease_expires_at=now
                    + timedelta(seconds=get_settings().worker_job_lease_seconds),
                )
            )
            await session.commit()
            return bool(result.rowcount)

    async def request_resolution(
        self,
        job_id: str,
        keep_document_id: str,
    ) -> tuple[IngestJobRow, OutboxEvent]:
        """Atomically persist a conflict decision and enqueue its command."""
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            job = await session.get(IngestJobRow, job_id, with_for_update=True)
            if not job or job.status != IngestJobStatus.CONFLICT:
                raise ValueError("Job is not in conflict state")
            pending_id = job.pending_document_id
            candidates = list(job.conflict_candidates_json or [])
            if keep_document_id != pending_id and keep_document_id not in candidates:
                raise ValueError("keep_document_id not in conflict set")
            options = dict(job.options_json or {})
            options["keep_document_id"] = keep_document_id
            job.options_json = options
            job.status = IngestJobStatus.RESOLVING
            job.current_step = IngestStep.QUEUED
            job.attempt = 0
            job.finished_at = None
            job.error_code = None
            job.error_message = None
            event_row = add_outbox_event(
                session,
                event_type=RESOLVE_CONFLICT,
                aggregate_id=job_id,
                payload={"job_id": job_id, "keep_document_id": keep_document_id},
            )
            await session.commit()
            return job, event_from_row(event_row)

    async def append_step_log(
        self,
        job_id: str,
        step: IngestStep | str,
        status: str,
        detail: str | None = None,
    ) -> None:
        """追加步骤日志，不改变任务状态。"""
        async with AsyncSession(get_engine()) as session:
            job = await session.get(IngestJobRow, job_id)
            if not job:
                return
            logs = list(job.step_logs or [])
            entry: dict[str, Any] = {
                "step": IngestStep(step),
                "status": status,
                "at": _utcnow().isoformat(),
            }
            if detail:
                entry["detail"] = detail
            logs.append(entry)
            job.step_logs = logs
            await session.commit()

    async def set_document_id(
        self,
        job_id: str,
        document_id: str | None,
        *,
        lease_token: str | None = None,
    ) -> None:
        """Persist the current Saga document so retries can clean partial work."""
        async with AsyncSession(get_engine()) as session:
            query = update(IngestJobRow).where(IngestJobRow.id == job_id)
            if lease_token is not None:
                query = query.where(
                    IngestJobRow.status == IngestJobStatus.RUNNING,
                    IngestJobRow.lease_token == lease_token,
                )
            result = await session.execute(query.values(document_id=document_id))
            if lease_token is not None and not result.rowcount:
                await session.rollback()
                raise LeaseLostError(f"lease lost for job {job_id}")
            await session.commit()

    async def mark_retrying(
        self,
        job_id: str,
        *,
        error_code: str,
        error_message: str,
        status: IngestJobStatus | str = IngestJobStatus.INGEST_RETRYING,
        lease_token: str | None = None,
    ) -> bool:
        """Release a claimed job, or fail it immediately at the attempt limit."""
        async with AsyncSession(get_engine()) as session:
            job = await session.get(IngestJobRow, job_id, with_for_update=True)
            if not job or job.status != IngestJobStatus.RUNNING:
                return False
            if lease_token is not None and job.lease_token != lease_token:
                raise LeaseLostError(f"lease lost for job {job_id}")
            job.error_code = error_code
            job.error_message = error_message
            if (job.attempt or 0) >= (job.max_attempts or 3):
                job.status = IngestJobStatus.FAILED
                job.current_step = IngestStep.FAILED
                job.finished_at = _utcnow()
                self._clear_lease(job)
                await session.commit()
                return False
            job.status = IngestJobStatus(status)
            job.current_step = IngestStep.RETRY_WAIT
            job.finished_at = None
            self._clear_lease(job)
            await session.commit()
            return True

    async def finish(
        self,
        job_id: str,
        status: IngestJobStatus | str,
        *,
        document_id: str | None = None,
        pending_document_id: str | None = None,
        conflict_candidates: list[str] | None = None,
        error_code: str | None = None,
        error_message: str | None = None,
        lease_token: str | None = None,
    ) -> None:
        """设置终态或冲突状态，并保存最终任务元数据。"""
        values: dict[str, Any] = {
            "status": IngestJobStatus(status),
            "finished_at": _utcnow(),
            "error_code": error_code,
            "error_message": error_message,
            "lease_owner": None,
            "lease_token": None,
            "lease_expires_at": None,
            "heartbeat_at": None,
        }
        if document_id is not None:
            values["document_id"] = document_id
        if pending_document_id is not None:
            values["pending_document_id"] = pending_document_id
        if conflict_candidates is not None:
            values["conflict_candidates_json"] = conflict_candidates
        async with AsyncSession(get_engine()) as session:
            query = update(IngestJobRow).where(IngestJobRow.id == job_id)
            if lease_token is not None:
                query = query.where(
                    IngestJobRow.status == IngestJobStatus.RUNNING,
                    IngestJobRow.lease_token == lease_token,
                )
            result = await session.execute(query.values(**values))
            if lease_token is not None and not result.rowcount:
                await session.rollback()
                raise LeaseLostError(f"lease lost for job {job_id}")
            await session.commit()

    async def publish_document(
        self, job_id: str, document_id: str, *, lease_token: str
    ) -> None:
        """Atomically publish a document and finish only for the current lease owner."""
        now = _utcnow()
        async with AsyncSession(get_engine()) as session:
            job = await session.get(IngestJobRow, job_id, with_for_update=True)
            if (
                not job
                or job.status != IngestJobStatus.RUNNING
                or job.lease_token != lease_token
            ):
                raise LeaseLostError(f"lease lost for job {job_id}")
            doc = await session.get(DocumentRow, document_id, with_for_update=True)
            if not doc or doc.status != DocumentStatus.INDEXING:
                raise LeaseLostError(
                    f"document {document_id} is no longer publishable"
                )
            doc.status = DocumentStatus.READY
            doc.updated_at = now
            job.status = IngestJobStatus.SUCCEEDED
            job.document_id = document_id
            job.finished_at = now
            job.error_code = None
            job.error_message = None
            self._clear_lease(job)
            await session.commit()

    async def recover_expired_jobs(self) -> list[OutboxEvent]:
        """Release crashed Worker leases and atomically enqueue resumable commands."""
        now = _utcnow()
        events: list[OutboxEvent] = []
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            result = await session.execute(
                select(IngestJobRow)
                .where(
                    IngestJobRow.status == IngestJobStatus.RUNNING,
                    (
                        IngestJobRow.lease_expires_at.is_(None)
                        | (IngestJobRow.lease_expires_at <= now)
                    ),
                )
                .with_for_update(skip_locked=True)
            )
            for job in result.scalars():
                logs = list(job.step_logs or [])
                logs.append(
                    {
                        "step": job.current_step or "unknown",
                        "status": "lease_expired",
                        "at": now.isoformat(),
                    }
                )
                job.step_logs = logs
                self._clear_lease(job)
                if (job.attempt or 0) >= (job.max_attempts or 3):
                    job.status = IngestJobStatus.FAILED
                    job.current_step = IngestStep.FAILED
                    job.error_code = MAX_ATTEMPTS_EXCEEDED
                    job.error_message = "Worker crashed and retry limit was exceeded"
                    job.finished_at = now
                    if job.document_id:
                        doc = await session.get(DocumentRow, job.document_id)
                        if doc and doc.status in {
                            DocumentStatus.STAGING,
                            DocumentStatus.INDEXING,
                        }:
                            doc.status = DocumentStatus.FAILED
                            doc.updated_at = now
                            event_row = add_outbox_event(
                                session,
                                event_type=CLEANUP_DOCUMENT,
                                aggregate_id=doc.id,
                                payload={
                                    "document_id": doc.id,
                                    "action": CleanupAction.ROLLBACK,
                                },
                            )
                            events.append(event_from_row(event_row))
                    continue

                keep_document_id = (job.options_json or {}).get("keep_document_id")
                resolving = bool(keep_document_id and job.pending_document_id)
                job.status = (
                    IngestJobStatus.RESOLVE_RETRYING
                    if resolving
                    else IngestJobStatus.INGEST_RETRYING
                )
                job.current_step = IngestStep.RETRY_WAIT
                job.error_code = WORKER_LEASE_EXPIRED
                job.error_message = "Previous Worker stopped before completing the task"
                event_type = RESOLVE_CONFLICT if resolving else INGEST_DOCUMENT
                payload: dict[str, Any] = {"job_id": job.id}
                if resolving:
                    payload["keep_document_id"] = keep_document_id
                event_row = add_outbox_event(
                    session,
                    event_type=event_type,
                    aggregate_id=job.id,
                    payload=payload,
                )
                events.append(event_from_row(event_row))
            await session.commit()
        return events

    @staticmethod
    def _clear_lease(job: IngestJobRow) -> None:
        job.lease_owner = None
        job.lease_token = None
        job.lease_expires_at = None
        job.heartbeat_at = None

    async def clear_document_references(self, document_id: str) -> None:
        """清空任务中的文档外键式引用，并从冲突候选中移除该文档。"""
        async with AsyncSession(get_engine()) as session:
            await session.execute(
                update(IngestJobRow)
                .where(IngestJobRow.document_id == document_id)
                .values(document_id=None)
            )
            await session.execute(
                update(IngestJobRow)
                .where(IngestJobRow.pending_document_id == document_id)
                .values(pending_document_id=None)
            )
            result = await session.execute(
                select(IngestJobRow).where(IngestJobRow.conflict_candidates_json.is_not(None))
            )
            for job in result.scalars():
                candidates = list(job.conflict_candidates_json or [])
                filtered = [candidate for candidate in candidates if candidate != document_id]
                if filtered != candidates:
                    job.conflict_candidates_json = filtered
            await session.commit()


def get_job_store() -> JobStore:
    """创建绑定共享数据库引擎的任务存储。"""
    return JobStore()
