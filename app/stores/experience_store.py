"""Experience persistence helpers and schema bootstrap."""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any
from uuid import NAMESPACE_URL, uuid4, uuid5

from sqlalchemy import func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.core.embedding.profiles import get_embedding_profile
from app.domain.experience import (
    ExperienceIndexStatus,
    ExperienceStatus,
    is_status_transition_allowed,
)
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


@dataclass(frozen=True)
class IdempotencyReservation:
    row: ExperienceIdempotencyRow
    created: bool


@dataclass(frozen=True)
class FeedbackWriteResult:
    event: ExperienceFeedbackEventRow
    experience: ExperienceItemRow
    duplicated: bool


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
        row = existing
    else:
        by_name = await store.get_by_name(PENETRATION_EXPERIENCE_KB_NAME)
        if by_name is not None:
            row = by_name
        else:
            profile = get_embedding_profile("configured")
            try:
                row = await store.create(
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

    # The projection is a system-owned binding. Administrators may add regular
    # knowledge bases to the Scope, but cannot accidentally detach this one.
    from app.schemas.knowledge import KnowledgeScope
    from app.stores.knowledge_scope_store import (
        EXPERIENCE_BINDING,
        get_knowledge_scope_store,
    )

    await get_knowledge_scope_store().ensure_system_binding(
        KnowledgeScope.PENETRATION,
        row.id,
        binding_type=EXPERIENCE_BINDING,
    )
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
        workspace_id: str,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> tuple[list[ExperienceItemRow], int]:
        async with AsyncSession(get_engine()) as session:
            predicates = [
                or_(
                    ExperienceItemRow.visibility == "global",
                    (
                        (ExperienceItemRow.visibility == "workspace")
                        & (ExperienceItemRow.workspace_id == workspace_id)
                    ),
                )
            ]
            if status:
                predicates.append(ExperienceItemRow.status == status)
            if workflow_type:
                predicates.append(ExperienceItemRow.workflow_type == workflow_type)
            if allowed_workflow_types is not None:
                predicates.append(
                    ExperienceItemRow.workflow_type.in_(allowed_workflow_types)
                )
            stmt = (
                select(ExperienceItemRow)
                .where(*predicates)
                .order_by(ExperienceItemRow.updated_at.desc())
            )
            stmt = stmt.offset(max(offset, 0)).limit(min(max(limit, 1), 200))
            result = await session.execute(stmt)
            total = await session.scalar(
                select(func.count()).select_from(ExperienceItemRow).where(*predicates)
            )
            return list(result.scalars().all()), int(total or 0)

    async def create_item_if_absent(
        self, row: ExperienceItemRow
    ) -> ExperienceItemRow | None:
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return None
            await session.refresh(row)
            return row

    async def update_item_if_revision(
        self,
        experience_id: str,
        *,
        expected_revision: int,
        values: dict[str, Any],
    ) -> ExperienceItemRow | None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                update(ExperienceItemRow)
                .where(
                    ExperienceItemRow.id == experience_id,
                    ExperienceItemRow.source_revision == expected_revision,
                )
                .values(**values, updated_at=_utcnow())
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return await session.get(ExperienceItemRow, experience_id)

    async def update_index_status_if_state(
        self,
        experience_id: str,
        *,
        source_revision: int,
        authority_status: str,
        index_status: str,
    ) -> ExperienceItemRow | None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                update(ExperienceItemRow)
                .where(
                    ExperienceItemRow.id == experience_id,
                    ExperienceItemRow.source_revision == source_revision,
                    ExperienceItemRow.status == authority_status,
                )
                .values(index_status=index_status, updated_at=_utcnow())
            )
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return await session.get(ExperienceItemRow, experience_id)

    async def transition_status(
        self,
        *,
        experience_id: str,
        target: ExperienceStatus,
        reason: str | None,
        actor_service_id: str,
        workspace_id: str,
        allowed_workflow_types: frozenset[str] | None,
    ) -> tuple[ExperienceItemRow | None, ExperienceStatus | None, bool]:
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            async with session.begin():
                row = await session.get(
                    ExperienceItemRow,
                    experience_id,
                    with_for_update=True,
                )
                if row is None:
                    return None, None, False
                if not _row_is_visible_to(
                    row,
                    workspace_id=workspace_id,
                    allowed_workflow_types=allowed_workflow_types,
                ):
                    raise PermissionError("Experience is outside the caller context")
                current = ExperienceStatus(row.status)
                if not is_status_transition_allowed(current, target):
                    return row, current, False
                row.status = target.value
                if target == ExperienceStatus.PROVEN:
                    row.index_status = ExperienceIndexStatus.INDEX_PENDING.value
                elif current == ExperienceStatus.PROVEN:
                    row.index_status = ExperienceIndexStatus.INDEX_PENDING.value
                row.updated_at = _utcnow()
                session.add(
                    ExperienceStatusHistoryRow(
                        id=str(uuid4()),
                        experience_id=experience_id,
                        from_status=current.value,
                        to_status=target.value,
                        reason=reason,
                        actor_service_id=actor_service_id,
                    )
                )
            return row, current, True

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

    async def record_feedback(
        self,
        event: ExperienceFeedbackEventRow,
        *,
        workspace_id: str,
        allowed_workflow_types: frozenset[str] | None,
    ) -> FeedbackWriteResult:
        try:
            async with AsyncSession(get_engine(), expire_on_commit=False) as session:
                async with session.begin():
                    experience = await session.get(
                        ExperienceItemRow,
                        event.experience_id,
                        with_for_update=True,
                    )
                    if experience is None:
                        raise LookupError("Experience not found")
                    if not _row_is_visible_to(
                        experience,
                        workspace_id=workspace_id,
                        allowed_workflow_types=allowed_workflow_types,
                    ):
                        raise PermissionError("Experience is outside the caller context")
                    result = await session.execute(
                        select(ExperienceFeedbackEventRow).where(
                            ExperienceFeedbackEventRow.event_id == event.event_id
                        )
                    )
                    existing = result.scalars().first()
                    if existing is not None:
                        return FeedbackWriteResult(existing, experience, True)
                    session.add(event)
                    experience.usage_count += 1
                    if event.outcome == "success":
                        experience.success_count += 1
                    elif event.outcome == "failure":
                        experience.failure_count += 1
                    experience.updated_at = _utcnow()
                return FeedbackWriteResult(event, experience, False)
        except IntegrityError:
            # A concurrent request may win the globally unique event_id race.
            existing = await self.get_feedback_by_event_id(event.event_id)
            experience = await self.get(event.experience_id)
            if existing is None or experience is None:
                raise
            return FeedbackWriteResult(existing, experience, True)

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

    async def reserve_idempotency(
        self,
        *,
        actor_service_id: str,
        idempotency_key: str,
        operation: str,
        request_hash: str,
    ) -> IdempotencyReservation:
        existing = await self.get_idempotency(
            actor_service_id=actor_service_id,
            idempotency_key=idempotency_key,
        )
        if existing is not None:
            return IdempotencyReservation(existing, False)

        from datetime import timedelta

        row = ExperienceIdempotencyRow(
            id=str(uuid4()),
            actor_service_id=actor_service_id,
            idempotency_key=idempotency_key,
            operation=operation,
            response_json={"request_hash": request_hash, "pending": True},
            expires_at=_utcnow()
            + timedelta(seconds=get_settings().experience_idempotency_ttl_seconds),
        )
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                winner = await self.get_idempotency(
                    actor_service_id=actor_service_id,
                    idempotency_key=idempotency_key,
                )
                if winner is None:
                    raise
                return IdempotencyReservation(winner, False)
            await session.refresh(row)
            return IdempotencyReservation(row, True)

    async def finalize_idempotency(
        self,
        *,
        actor_service_id: str,
        idempotency_key: str,
        operation: str,
        request_hash: str,
        response_json: dict[str, Any],
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ExperienceIdempotencyRow)
                .where(
                    ExperienceIdempotencyRow.actor_service_id == actor_service_id,
                    ExperienceIdempotencyRow.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            row = result.scalars().first()
            if row is None:
                raise RuntimeError("Idempotency reservation disappeared")
            payload = row.response_json or {}
            if row.operation != operation or payload.get("request_hash") != request_hash:
                raise RuntimeError("Idempotency reservation changed")
            row.response_json = {
                "request_hash": request_hash,
                "response": response_json,
            }
            await session.commit()

    async def release_idempotency(
        self,
        *,
        actor_service_id: str,
        idempotency_key: str,
        operation: str,
        request_hash: str,
    ) -> None:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ExperienceIdempotencyRow)
                .where(
                    ExperienceIdempotencyRow.actor_service_id == actor_service_id,
                    ExperienceIdempotencyRow.idempotency_key == idempotency_key,
                )
                .with_for_update()
            )
            row = result.scalars().first()
            if row is None:
                return
            payload = row.response_json or {}
            if (
                row.operation == operation
                and payload.get("request_hash") == request_hash
                and payload.get("pending") is True
            ):
                await session.delete(row)
                await session.commit()

    async def list_index_pending(self, *, limit: int = 100) -> list[ExperienceItemRow]:
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ExperienceItemRow)
                .where(
                    ExperienceItemRow.index_status
                    == ExperienceIndexStatus.INDEX_PENDING.value
                )
                .order_by(ExperienceItemRow.updated_at.asc())
                .limit(min(max(limit, 1), 500))
            )
            return list(result.scalars().all())

    async def expire_proven(self, *, limit: int = 100) -> list[ExperienceItemRow]:
        now = _utcnow()
        async with AsyncSession(get_engine(), expire_on_commit=False) as session:
            async with session.begin():
                result = await session.execute(
                    select(ExperienceItemRow)
                    .where(
                        ExperienceItemRow.status == ExperienceStatus.PROVEN.value,
                        ExperienceItemRow.expires_at.is_not(None),
                        ExperienceItemRow.expires_at <= now,
                    )
                    .order_by(ExperienceItemRow.expires_at.asc())
                    .limit(min(max(limit, 1), 500))
                    .with_for_update()
                )
                rows = list(result.scalars().all())
                for row in rows:
                    row.status = ExperienceStatus.DEPRECATED.value
                    row.index_status = ExperienceIndexStatus.INDEX_PENDING.value
                    row.updated_at = now
                    session.add(
                        ExperienceStatusHistoryRow(
                            id=str(uuid4()),
                            experience_id=row.id,
                            from_status=ExperienceStatus.PROVEN.value,
                            to_status=ExperienceStatus.DEPRECATED.value,
                            reason="Experience expired",
                            actor_service_id="experience-expiry-worker",
                        )
                    )
            return rows


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


def is_experience_knowledge_base(*, knowledge_base_id: str, name: str) -> bool:
    return (
        knowledge_base_id == PENETRATION_EXPERIENCE_KB_ID
        or name == PENETRATION_EXPERIENCE_KB_NAME
    )


def _row_is_visible_to(
    row: ExperienceItemRow,
    *,
    workspace_id: str,
    allowed_workflow_types: frozenset[str] | None,
) -> bool:
    if row.visibility == "workspace" and row.workspace_id != workspace_id:
        return False
    if (
        allowed_workflow_types is not None
        and row.workflow_type not in allowed_workflow_types
    ):
        return False
    return True
