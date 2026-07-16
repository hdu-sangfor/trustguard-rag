"""Idempotent RabbitMQ command handlers."""

from __future__ import annotations

import asyncio
from contextlib import suppress
from collections.abc import Awaitable
from typing import TypeVar

from app.core.ingest.compensator import get_compensator
from app.core.ingest.errors import MAX_ATTEMPTS_EXCEEDED
from app.core.ingest.pipeline import get_ingest_pipeline
from app.domain import (
    INGEST_CLAIMABLE_STATUSES,
    RESOLVE_CLAIMABLE_STATUSES,
    ClaimOutcome,
    CleanupAction,
    DocumentStatus,
    IngestJobStatus,
    PipelineResult,
)
from app.stores.blob_store import get_blob_store
from app.stores.document_store import DocumentStore
from app.settings import get_settings
from app.stores.job_store import JobLease, JobStore, LeaseLostError
from app.stores.outbox_store import OutboxStore
from app.workers.messages import (
    CLEANUP_DOCUMENT,
    INGEST_DOCUMENT,
    RESOLVE_CONFLICT,
    CommandMessage,
)


class RetryableCommandError(RuntimeError):
    """A command should be delayed and delivered again."""


class BusyCommandError(RuntimeError):
    """Another live Worker currently owns this command."""


_T = TypeVar("_T")


async def _claim_job(
    job_id: str, allowed_statuses: tuple[IngestJobStatus, ...]
) -> JobLease | None:
    jobs = JobStore()
    result = await jobs.claim(job_id, allowed_statuses=allowed_statuses)
    if result == ClaimOutcome.BUSY:
        raise BusyCommandError(f"job {job_id} is already running")
    if result == ClaimOutcome.TERMINAL:
        job = await jobs.get(job_id)
        if job and job.error_code == MAX_ATTEMPTS_EXCEEDED:
            get_blob_store().delete_job_staging(job_id)
            documents = DocumentStore()
            for document_id in {job.document_id, job.pending_document_id} - {None}:
                doc = await documents.get(document_id)
                if doc and doc.status == DocumentStatus.FAILED:
                    await OutboxStore().add(
                        event_type=CLEANUP_DOCUMENT,
                        aggregate_id=document_id,
                        payload={
                            "document_id": document_id,
                            "action": CleanupAction.ROLLBACK,
                        },
                    )
        return None
    return result


async def _with_heartbeat(lease: JobLease, operation: Awaitable[_T]) -> _T:
    """Cancel in-flight work promptly when deletion or another claimant revokes its lease."""
    lost = asyncio.Event()
    work = asyncio.create_task(operation)

    async def renew() -> None:
        interval = get_settings().worker_heartbeat_seconds
        while True:
            await asyncio.sleep(interval)
            if not await JobStore().heartbeat(lease.job_id, lease.token):
                lost.set()
                work.cancel()
                return

    heartbeat = asyncio.create_task(renew())
    try:
        return await work
    except asyncio.CancelledError:
        if lost.is_set():
            raise LeaseLostError(f"lease lost for job {lease.job_id}") from None
        raise
    finally:
        heartbeat.cancel()
        with suppress(asyncio.CancelledError):
            await heartbeat


async def _handle_ingest(command: CommandMessage) -> None:
    job_id = str(command.payload.get("job_id") or command.aggregate_id)
    lease = await _claim_job(job_id, INGEST_CLAIMABLE_STATUSES)
    if not lease:
        return
    try:
        result = await _with_heartbeat(
            lease,
            get_ingest_pipeline().run(job_id, lease_token=lease.token),
        )
    except LeaseLostError:
        return
    if result == PipelineResult.RETRYING:
        raise RetryableCommandError(f"ingest job {job_id} needs retry")


async def _handle_resolve(command: CommandMessage) -> None:
    job_id = str(command.payload.get("job_id") or command.aggregate_id)
    keep_document_id = str(command.payload["keep_document_id"])
    lease = await _claim_job(job_id, RESOLVE_CLAIMABLE_STATUSES)
    if not lease:
        return
    try:
        result = await _with_heartbeat(
            lease,
            get_ingest_pipeline().resolve_conflict(
                job_id, keep_document_id, lease_token=lease.token
            ),
        )
    except LeaseLostError:
        return
    if result == PipelineResult.RETRYING:
        raise RetryableCommandError(f"conflict job {job_id} needs retry")


async def _handle_cleanup(command: CommandMessage) -> None:
    document_id = str(command.payload.get("document_id") or command.aggregate_id)
    action = CleanupAction(command.payload.get("action") or CleanupAction.DELETE)
    doc = await DocumentStore().get(document_id)
    if not doc:
        return
    compensator = get_compensator()
    if action == CleanupAction.DELETE:
        if doc.status != DocumentStatus.DELETING:
            return
        await compensator.delete_document(document_id)
    elif action == CleanupAction.SUPERSEDE:
        if doc.status != DocumentStatus.SUPERSEDING:
            return
        await compensator.supersede_document(document_id)
    elif action == CleanupAction.ROLLBACK:
        if doc.status != DocumentStatus.FAILED:
            return
        if not await compensator.rollback_document(document_id):
            raise RetryableCommandError(f"rollback remains incomplete for {document_id}")
    else:
        raise ValueError(f"unsupported cleanup action: {action}")


async def dispatch_command(command: CommandMessage) -> None:
    if command.event_type == INGEST_DOCUMENT:
        await _handle_ingest(command)
    elif command.event_type == RESOLVE_CONFLICT:
        await _handle_resolve(command)
    elif command.event_type == CLEANUP_DOCUMENT:
        await _handle_cleanup(command)
    else:
        raise ValueError(f"unsupported command type: {command.event_type}")
