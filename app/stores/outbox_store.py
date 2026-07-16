"""Transactional Outbox persistence and relay leasing."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import inspect, or_, select, text, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import OutboxStatus
from app.settings import get_settings
from app.stores.db import get_engine
from app.stores.models import OutboxEventRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


@dataclass(frozen=True, slots=True)
class OutboxEvent:
    id: str
    event_type: str
    aggregate_id: str
    payload: dict[str, Any]


def event_from_row(row: OutboxEventRow) -> OutboxEvent:
    return OutboxEvent(
        id=row.id,
        event_type=row.event_type,
        aggregate_id=row.aggregate_id,
        payload=dict(row.payload_json or {}),
    )


def add_outbox_event(
    session: AsyncSession,
    *,
    event_type: str,
    aggregate_id: str,
    payload: dict[str, Any] | None = None,
    event_id: str | None = None,
) -> OutboxEventRow:
    """Add an event to an existing business transaction."""
    row = OutboxEventRow(
        id=event_id or str(uuid4()),
        event_type=event_type,
        aggregate_id=aggregate_id,
        payload_json=payload or {},
        status=OutboxStatus.PENDING,
    )
    session.add(row)
    return row


async def ensure_outbox_schema() -> None:
    """Create additive Worker schema for both fresh and existing databases."""
    async with get_engine().begin() as connection:
        is_mysql = connection.dialect.name == "mysql"
        if is_mysql:
            await connection.execute(text("SELECT GET_LOCK('trustguard_worker_schema', 30)"))
        try:
            await connection.run_sync(
                lambda sync_connection: OutboxEventRow.__table__.create(
                    sync_connection,
                    checkfirst=True,
                )
            )
            columns = await connection.run_sync(
                lambda sync_connection: {
                    column["name"]
                    for column in inspect(sync_connection).get_columns("ingest_jobs")
                }
            )
            definitions = {
                "lease_owner": "VARCHAR(128) NULL",
                "lease_token": "VARCHAR(36) NULL",
                "lease_expires_at": "DATETIME NULL",
                "heartbeat_at": "DATETIME NULL",
            }
            for name, definition in definitions.items():
                if name not in columns:
                    await connection.execute(
                        text(f"ALTER TABLE ingest_jobs ADD COLUMN {name} {definition}")
                    )
            indexes = await connection.run_sync(
                lambda sync_connection: {
                    index["name"]
                    for index in inspect(sync_connection).get_indexes("ingest_jobs")
                }
            )
            if "idx_jobs_lease" not in indexes:
                await connection.execute(
                    text(
                        "CREATE INDEX idx_jobs_lease "
                        "ON ingest_jobs (status, lease_expires_at)"
                    )
                )
        finally:
            if is_mysql:
                await connection.execute(text("SELECT RELEASE_LOCK('trustguard_worker_schema')"))


class OutboxStore:
    async def add(
        self,
        *,
        event_type: str,
        aggregate_id: str,
        payload: dict[str, Any] | None = None,
    ) -> OutboxEvent:
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            row = add_outbox_event(
                session,
                event_type=event_type,
                aggregate_id=aggregate_id,
                payload=payload,
            )
            await session.commit()
            return event_from_row(row)

    async def claim_batch(self, limit: int | None = None) -> list[OutboxEvent]:
        """Lease publishable rows; stale publishing leases are reclaimable."""
        settings = get_settings()
        now = _utcnow()
        stale_before = now - timedelta(seconds=settings.worker_outbox_lease_seconds)
        batch_size = limit or settings.worker_outbox_batch_size
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            result = await session.execute(
                select(OutboxEventRow)
                .where(
                    OutboxEventRow.attempt < OutboxEventRow.max_attempts,
                    or_(
                        OutboxEventRow.status == OutboxStatus.PENDING,
                        (
                            (OutboxEventRow.status == OutboxStatus.PUBLISHING)
                            & (OutboxEventRow.locked_at <= stale_before)
                        ),
                    ),
                    or_(
                        OutboxEventRow.next_attempt_at.is_(None),
                        OutboxEventRow.next_attempt_at <= now,
                    ),
                )
                .order_by(OutboxEventRow.created_at, OutboxEventRow.id)
                .limit(batch_size)
                .with_for_update(skip_locked=True)
            )
            rows = list(result.scalars().all())
            for row in rows:
                row.status = OutboxStatus.PUBLISHING
                row.locked_at = now
            await session.commit()
            return [event_from_row(row) for row in rows]

    async def mark_published(self, event_id: str) -> None:
        now = _utcnow()
        async with AsyncSession(get_engine()) as session:
            await session.execute(
                update(OutboxEventRow)
                .where(OutboxEventRow.id == event_id)
                .values(
                    status=OutboxStatus.PUBLISHED,
                    published_at=now,
                    locked_at=None,
                    last_error=None,
                )
            )
            await session.commit()

    async def mark_failed(self, event_id: str, error: str) -> None:
        """Release a failed publish with bounded exponential backoff."""
        async with AsyncSession(get_engine()) as session:
            row = await session.get(OutboxEventRow, event_id, with_for_update=True)
            if not row:
                return
            row.attempt += 1
            row.status = (
                OutboxStatus.DEAD
                if row.attempt >= row.max_attempts
                else OutboxStatus.PENDING
            )
            row.next_attempt_at = _utcnow() + timedelta(seconds=min(2 ** row.attempt, 60))
            row.locked_at = None
            row.last_error = error[:4000]
            await session.commit()

    async def get(self, event_id: str) -> OutboxEventRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(OutboxEventRow, event_id)


def get_outbox_store() -> OutboxStore:
    return OutboxStore()
