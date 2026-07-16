"""可靠 Worker、Outbox 和幂等命令测试。"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock
from uuid import uuid4

import pytest
from sqlalchemy import update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import (
    ClaimOutcome,
    DocumentStatus,
    IngestJobStatus,
    OutboxStatus,
)
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobLease, JobStore, LeaseLostError
from app.stores.models import DocumentRow, IngestJobRow
from app.stores.outbox_store import OutboxStore
from app.workers.handlers import dispatch_command
from app.workers.messages import CLEANUP_DOCUMENT, INGEST_DOCUMENT, CommandMessage


@pytest.mark.asyncio
async def test_ingest_job_and_outbox_are_committed_together(test_engine) -> None:
    job_id = str(uuid4())
    job, event = await JobStore().create_ingest_command(
        job_id=job_id,
        source_type="file",
        source="queued.pdf",
        options={"original_filename": "queued.pdf"},
    )

    persisted = await OutboxStore().get(event.id)
    assert job.id == job_id
    assert job.status is IngestJobStatus.QUEUED
    assert event.event_type == INGEST_DOCUMENT
    assert persisted is not None
    assert persisted.status is OutboxStatus.PENDING
    assert persisted.aggregate_id == job_id


@pytest.mark.asyncio
async def test_job_attempt_counts_claims_not_pipeline_steps(test_engine) -> None:
    job = await JobStore().create(source_type="file", source="attempt.pdf")

    lease = await JobStore().claim(job.id, allowed_statuses=("queued",))
    assert isinstance(lease, JobLease)
    await JobStore().mark_running(job.id, "extract", lease_token=lease.token)
    await JobStore().mark_running(job.id, "embed", lease_token=lease.token)

    persisted = await JobStore().get(job.id)
    assert persisted is not None
    assert persisted.attempt == 1
    assert persisted.current_step == "embed"


@pytest.mark.asyncio
async def test_retry_limit_fails_on_the_last_attempt_without_an_extra_delivery(test_engine) -> None:
    store = JobStore()
    job = await store.create(source_type="file", source="bounded.pdf")

    for attempt in range(1, 4):
        allowed = ("queued",) if attempt == 1 else ("ingest_retrying",)
        lease = await store.claim(job.id, allowed_statuses=allowed)
        assert isinstance(lease, JobLease)
        retrying = await store.mark_retrying(
            job.id,
            error_code="TEMPORARY",
            error_message="temporary failure",
            lease_token=lease.token,
        )
        assert retrying is (attempt < 3)

    persisted = await store.get(job.id)
    assert persisted is not None
    assert persisted.status == "failed"
    assert persisted.attempt == persisted.max_attempts == 3


@pytest.mark.asyncio
async def test_outbox_relay_leases_then_marks_event_published(test_engine) -> None:
    event = await OutboxStore().add(
        event_type=INGEST_DOCUMENT,
        aggregate_id="job-relay",
        payload={"job_id": "job-relay"},
    )

    claimed = await OutboxStore().claim_batch(limit=10)
    assert [item.id for item in claimed] == [event.id]
    assert (await OutboxStore().get(event.id)).status is OutboxStatus.PUBLISHING
    assert await OutboxStore().claim_batch(limit=10) == []

    await OutboxStore().mark_published(event.id)
    persisted = await OutboxStore().get(event.id)
    assert persisted.status is OutboxStatus.PUBLISHED
    assert persisted.published_at is not None


@pytest.mark.asyncio
async def test_conflict_resolution_resets_attempts_and_enqueues_command(test_engine) -> None:
    job = await JobStore().create(source_type="file", source="conflict.pdf")
    await JobStore().finish(
        job.id,
        "conflict",
        pending_document_id="pending-doc",
        conflict_candidates=["old-doc"],
    )
    lease = await JobStore().claim(job.id, allowed_statuses=("conflict",))
    assert isinstance(lease, JobLease)
    await JobStore().finish(
        job.id,
        "conflict",
        pending_document_id="pending-doc",
        conflict_candidates=["old-doc"],
    )

    resolving, event = await JobStore().request_resolution(job.id, "pending-doc")

    assert resolving.status == "resolving"
    assert resolving.attempt == 0
    assert event.event_type == "document.resolve"
    assert (await OutboxStore().get(event.id)).status == "pending"


@pytest.mark.asyncio
async def test_delete_state_and_cleanup_outbox_are_committed_together(test_engine) -> None:
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://delete.pdf",
        content_hash="a" * 64,
        status=DocumentStatus.READY,
    )

    event = await DocumentStore().request_delete(document.id)

    persisted_document = await DocumentStore().get(document.id)
    persisted_event = await OutboxStore().get(event.id)
    assert persisted_document is not None
    assert persisted_document.status == DocumentStatus.DELETING
    assert persisted_event is not None
    assert persisted_event.payload_json == {
        "document_id": document.id,
        "action": "delete",
    }


@pytest.mark.asyncio
async def test_stale_cleanup_command_does_not_delete_ready_document(
    test_engine, monkeypatch: pytest.MonkeyPatch
) -> None:
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://ready.pdf",
        content_hash="b" * 64,
        status=DocumentStatus.READY,
    )
    compensator = AsyncMock()
    monkeypatch.setattr("app.workers.handlers.get_compensator", lambda: compensator)
    command = CommandMessage(
        event_id=str(uuid4()),
        event_type=CLEANUP_DOCUMENT,
        aggregate_id=document.id,
        payload={"document_id": document.id, "action": "delete"},
    )

    await dispatch_command(command)

    compensator.delete_document.assert_not_awaited()
    assert (await DocumentStore().get(document.id)).status == DocumentStatus.READY


def test_command_message_round_trip() -> None:
    command = CommandMessage(
        event_id=str(uuid4()),
        event_type=INGEST_DOCUMENT,
        aggregate_id="job-1",
        payload={"job_id": "job-1"},
    )

    assert CommandMessage.from_bytes(command.to_bytes()) == command


def _past() -> datetime:
    return (datetime.now(timezone.utc) - timedelta(minutes=10)).replace(tzinfo=None)


@pytest.mark.asyncio
async def test_expired_worker_lease_is_recovered_with_outbox_command(test_engine) -> None:
    store = JobStore()
    job = await store.create(source_type="file", source="crashed.pdf")
    lease = await store.claim(job.id, allowed_statuses=("queued",), owner="worker-a")
    assert isinstance(lease, JobLease)
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://crashed.pdf",
        content_hash="c" * 64,
        status=DocumentStatus.INDEXING,
    )
    await store.set_document_id(job.id, document.id, lease_token=lease.token)
    async with AsyncSession(test_engine) as session:
        await session.execute(
            update(IngestJobRow)
            .where(IngestJobRow.id == job.id)
            .values(lease_expires_at=_past())
        )
        await session.commit()

    events = await store.recover_expired_jobs()

    recovered = await store.get(job.id)
    assert recovered is not None
    assert recovered.status == "ingest_retrying"
    assert recovered.lease_token is None
    assert len(events) == 1
    assert events[0].event_type == INGEST_DOCUMENT
    assert (await OutboxStore().get(events[0].id)).status == "pending"


@pytest.mark.asyncio
async def test_reclaimed_job_fences_the_old_worker(test_engine) -> None:
    store = JobStore()
    job = await store.create(source_type="file", source="fenced.pdf")
    old_lease = await store.claim(job.id, allowed_statuses=("queued",), owner="worker-a")
    assert isinstance(old_lease, JobLease)
    async with AsyncSession(test_engine) as session:
        await session.execute(
            update(IngestJobRow)
            .where(IngestJobRow.id == job.id)
            .values(lease_expires_at=_past())
        )
        await session.commit()
    new_lease = await store.claim(job.id, allowed_statuses=("queued",), owner="worker-b")
    assert isinstance(new_lease, JobLease)
    assert new_lease.token != old_lease.token

    with pytest.raises(LeaseLostError):
        await store.mark_running(job.id, "publish", lease_token=old_lease.token)
    assert await store.heartbeat(job.id, new_lease.token)


@pytest.mark.asyncio
async def test_delete_revokes_running_job_and_blocks_late_publish(test_engine) -> None:
    store = JobStore()
    job = await store.create(source_type="file", source="delete-running.pdf")
    lease = await store.claim(job.id, allowed_statuses=("queued",))
    assert isinstance(lease, JobLease)
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://delete-running.pdf",
        content_hash="d" * 64,
        status=DocumentStatus.INDEXING,
    )
    await store.set_document_id(job.id, document.id, lease_token=lease.token)

    await DocumentStore().request_delete(document.id)

    assert not await store.heartbeat(job.id, lease.token)
    with pytest.raises(LeaseLostError):
        await store.publish_document(job.id, document.id, lease_token=lease.token)
    persisted_job = await store.get(job.id)
    persisted_document = await DocumentStore().get(document.id)
    assert persisted_job is not None and persisted_job.status == "cancelled"
    assert persisted_document is not None
    assert persisted_document.status == DocumentStatus.DELETING


@pytest.mark.asyncio
async def test_stale_orphan_indexing_document_is_queued_for_rollback(test_engine) -> None:
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://orphan.pdf",
        content_hash="e" * 64,
        status=DocumentStatus.INDEXING,
    )
    async with AsyncSession(test_engine) as session:
        await session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document.id)
            .values(updated_at=_past())
        )
        await session.commit()

    events = await DocumentStore().recover_orphan_publications()

    persisted = await DocumentStore().get(document.id)
    assert persisted is not None and persisted.status == DocumentStatus.FAILED
    assert len(events) == 1
    assert events[0].payload == {"document_id": document.id, "action": "rollback"}


@pytest.mark.asyncio
async def test_conflict_document_is_not_mistaken_for_an_orphan(test_engine) -> None:
    document = await DocumentStore().create(
        source_type="file",
        source_uri="upload://waiting-conflict.pdf",
        content_hash="f" * 64,
        status=DocumentStatus.STAGING,
    )
    job = await JobStore().create(source_type="file", source="waiting-conflict.pdf")
    await JobStore().finish(
        job.id,
        IngestJobStatus.CONFLICT,
        pending_document_id=document.id,
        conflict_candidates=["existing-document"],
    )
    async with AsyncSession(test_engine) as session:
        await session.execute(
            update(DocumentRow)
            .where(DocumentRow.id == document.id)
            .values(updated_at=_past())
        )
        await session.commit()

    assert await DocumentStore().recover_orphan_publications() == []
    persisted = await DocumentStore().get(document.id)
    assert persisted is not None and persisted.status is DocumentStatus.STAGING


def test_claim_outcome_remains_wire_compatible() -> None:
    assert ClaimOutcome.BUSY == "busy"
    assert ClaimOutcome.TERMINAL == "terminal"
