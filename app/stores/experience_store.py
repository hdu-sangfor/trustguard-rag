"""Experience persistence helpers and schema bootstrap."""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding.profiles import get_embedding_profile
from app.domain.experience import ExperienceIndexStatus, ExperienceStatus
from app.settings import get_settings
from app.stores.db import get_engine
from app.stores.knowledge_base_store import KnowledgeBaseStore
from app.stores.models import (
    Base,
    ExperienceFeedbackEventRow,
    ExperienceIdempotencyRow,
    ExperienceItemRow,
    ExperienceStatusHistoryRow,
    KnowledgeBaseRow,
)

logger = logging.getLogger(__name__)

PENETRATION_EXPERIENCE_KB_NAME = "penetration-experience"
PENETRATION_EXPERIENCE_KB_ID = str(
    uuid5(NAMESPACE_URL, "trustguard:knowledge-base:penetration-experience")
)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


async def ensure_experience_schema() -> None:
    """Create experience tables (create_all is idempotent for missing tables)."""
    try:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:  # noqa: BLE001
        logger.warning("ensure_experience_schema failed", exc_info=True)
        raise


async def ensure_penetration_experience_knowledge_base() -> KnowledgeBaseRow:
    """Ensure the system KB used for penetration Experience indexing/search."""
    store = KnowledgeBaseStore()
    existing = await store.get(PENETRATION_EXPERIENCE_KB_ID)
    if existing is not None:
        return existing
    by_name = await store.get_by_name(PENETRATION_EXPERIENCE_KB_NAME)
    if by_name is not None:
        return by_name
    profile = get_embedding_profile("configured")
    try:
        return await store.create(
            name=PENETRATION_EXPERIENCE_KB_NAME,
            description="System knowledge base for penetration Experience entries.",
            profile=profile,
            knowledge_base_id=PENETRATION_EXPERIENCE_KB_ID,
            is_system=True,
        )
    except ValueError:
        row = await store.get_by_name(PENETRATION_EXPERIENCE_KB_NAME)
        if row is None:
            row = await store.get(PENETRATION_EXPERIENCE_KB_ID)
        if row is None:
            raise
        return row


class ExperienceStore:
    async def get(self, experience_id: str) -> ExperienceItemRow | None:
        async with AsyncSession(get_engine()) as session:
            return await session.get(ExperienceItemRow, experience_id)

    async def get_by_source(
        self, *, source_system: str, external_id: str
    ) -> ExperienceItemRow | None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ExperienceItemRow).where(
                    ExperienceItemRow.source_system == source_system,
                    ExperienceItemRow.external_id == external_id,
                )
            )
            return result.scalars().first()

    async def list_items(
        self,
        *,
        status: str | None = None,
        workflow_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> list[ExperienceItemRow]:
        async with AsyncSession(get_engine()) as session:
            stmt = select(ExperienceItemRow).order_by(ExperienceItemRow.updated_at.desc())
            if status:
                stmt = stmt.where(ExperienceItemRow.status == status)
            if workflow_type:
                stmt = stmt.where(ExperienceItemRow.workflow_type == workflow_type)
            stmt = stmt.offset(max(offset, 0)).limit(min(max(limit, 1), 200))
            result = await session.execute(stmt)
            return list(result.scalars().all())

    async def save_item(self, row: ExperienceItemRow) -> ExperienceItemRow:
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def update_item_fields(
        self, experience_id: str, values: dict[str, Any]
    ) -> ExperienceItemRow | None:
        async with AsyncSession(get_engine()) as session:
            row = await session.get(ExperienceItemRow, experience_id)
            if row is None:
                return None
            for key, value in values.items():
                setattr(row, key, value)
            row.updated_at = _utcnow()
            await session.commit()
            await session.refresh(row)
            return row

    async def add_status_history(
        self,
        *,
        experience_id: str,
        from_status: str,
        to_status: str,
        reason: str | None,
        actor_service_id: str | None,
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            session.add(
                ExperienceStatusHistoryRow(
                    id=str(uuid4()),
                    experience_id=experience_id,
                    from_status=from_status,
                    to_status=to_status,
                    reason=reason,
                    actor_service_id=actor_service_id,
                )
            )
            await session.commit()

    async def get_feedback_by_event_id(
        self, event_id: str
    ) -> ExperienceFeedbackEventRow | None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ExperienceFeedbackEventRow).where(
                    ExperienceFeedbackEventRow.event_id == event_id
                )
            )
            return result.scalars().first()

    async def add_feedback(
        self, row: ExperienceFeedbackEventRow
    ) -> ExperienceFeedbackEventRow:
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
            return row

    async def get_idempotency(
        self, *, actor_service_id: str, idempotency_key: str
    ) -> ExperienceIdempotencyRow | None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ExperienceIdempotencyRow).where(
                    ExperienceIdempotencyRow.actor_service_id == actor_service_id,
                    ExperienceIdempotencyRow.idempotency_key == idempotency_key,
                )
            )
            row = result.scalars().first()
            if row is None:
                return None
            if row.expires_at < _utcnow():
                await session.delete(row)
                await session.commit()
                return None
            return row

    async def put_idempotency(
        self,
        *,
        actor_service_id: str,
        idempotency_key: str,
        operation: str,
        response_json: dict[str, Any],
    ) -> None:
        settings = get_settings()
        expires = _utcnow()
        from datetime import timedelta

        expires = expires + timedelta(seconds=settings.experience_idempotency_ttl_seconds)
        async with AsyncSession(get_engine()) as session:
            session.add(
                ExperienceIdempotencyRow(
                    id=str(uuid4()),
                    actor_service_id=actor_service_id,
                    idempotency_key=idempotency_key,
                    operation=operation,
                    response_json=response_json,
                    expires_at=expires,
                )
            )
            await session.commit()


def build_search_text(
    *,
    conditions: dict[str, Any],
    action_summary: str,
    outcome_summary: str,
    experience_type: str,
) -> str:
    condition_bits = []
    for key, value in sorted(conditions.items()):
        condition_bits.append(f"{key}: {value}")
    conditions_block = "; ".join(condition_bits) if condition_bits else "(none)"
    return (
        f"Experience type: {experience_type}\n"
        f"Conditions: {conditions_block}\n"
        f"Action: {action_summary}\n"
        f"Outcome: {outcome_summary}"
    )


def default_new_item_status() -> str:
    return ExperienceStatus.CANDIDATE.value


def default_index_status() -> str:
    return ExperienceIndexStatus.NOT_INDEXED.value
