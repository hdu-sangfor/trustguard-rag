"""Experience authority application service (Slice A)."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from app.core.indexing.experience_indexer import get_experience_indexer
from app.domain.experience import (
    ExperienceIndexStatus,
    ExperienceStatus,
    is_status_transition_allowed,
)
from app.schemas.experience import (
    ExperienceEvidenceRef,
    ExperienceFeedbackRequest,
    ExperienceFeedbackResponse,
    ExperienceListResponse,
    ExperienceResponse,
    ExperienceStatusPatchRequest,
    ExperienceUpsertRequest,
)
from app.security.experience_auth import ExperienceAccessContext
from app.stores.experience_store import (
    ExperienceStore,
    build_search_text,
    default_index_status,
    default_new_item_status,
    ensure_penetration_experience_knowledge_base,
)
from app.stores.models import ExperienceFeedbackEventRow, ExperienceItemRow

logger = logging.getLogger(__name__)

ENABLED_SCOPES = frozenset({"penetration"})


class ExperienceError(Exception):
    def __init__(
        self,
        message: str,
        *,
        status_code: int,
        code: str,
        retryable: bool = False,
    ) -> None:
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.retryable = retryable


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _normalize_expires(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _content_fingerprint(payload: ExperienceUpsertRequest) -> dict[str, Any]:
    return {
        "knowledge_scope": payload.knowledge_scope,
        "workflow_type": payload.workflow_type,
        "experience_type": payload.experience_type,
        "workspace_id": payload.workspace_id,
        "visibility": payload.visibility,
        "conditions": payload.conditions,
        "action_summary": payload.action_summary,
        "outcome_summary": payload.outcome_summary,
        "skill_id": payload.skill_id,
        "phase": payload.phase,
        "source_task_id": payload.source_task_id,
        "evidence_refs": [ref.model_dump() for ref in payload.evidence_refs],
        "expires_at": payload.expires_at.isoformat() if payload.expires_at else None,
    }


def _row_fingerprint(row: ExperienceItemRow) -> dict[str, Any]:
    return {
        "knowledge_scope": row.knowledge_scope,
        "workflow_type": row.workflow_type,
        "experience_type": row.experience_type,
        "workspace_id": row.workspace_id,
        "visibility": row.visibility,
        "conditions": row.conditions_json or {},
        "action_summary": row.action_summary,
        "outcome_summary": row.outcome_summary,
        "skill_id": row.skill_id,
        "phase": row.phase,
        "source_task_id": row.source_task_id,
        "evidence_refs": row.evidence_refs_json or [],
        "expires_at": row.expires_at.isoformat() if row.expires_at else None,
    }


def _to_response(row: ExperienceItemRow) -> ExperienceResponse:
    refs = [
        ExperienceEvidenceRef.model_validate(item)
        for item in (row.evidence_refs_json or [])
    ]
    return ExperienceResponse(
        id=row.id,
        external_id=row.external_id,
        source_system=row.source_system,
        source_revision=row.source_revision,
        knowledge_base_id=row.knowledge_base_id,
        knowledge_scope=row.knowledge_scope,
        workflow_type=row.workflow_type,
        experience_type=row.experience_type,
        workspace_id=row.workspace_id,
        visibility=row.visibility,
        status=ExperienceStatus(row.status),
        index_status=ExperienceIndexStatus(row.index_status),
        conditions=row.conditions_json or {},
        action_summary=row.action_summary,
        outcome_summary=row.outcome_summary,
        skill_id=row.skill_id,
        phase=row.phase,
        source_task_id=row.source_task_id,
        evidence_refs=refs,
        usage_count=row.usage_count,
        success_count=row.success_count,
        failure_count=row.failure_count,
        expires_at=row.expires_at,
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


class ExperienceService:
    def __init__(self, store: ExperienceStore | None = None) -> None:
        self._store = store or ExperienceStore()
        self._indexer = get_experience_indexer()

    async def upsert(
        self,
        external_id: str,
        payload: ExperienceUpsertRequest,
        *,
        access: ExperienceAccessContext,
        idempotency_key: str | None = None,
    ) -> ExperienceResponse:
        if payload.external_id != external_id:
            raise ExperienceError(
                "Path external_id must match body external_id",
                status_code=422,
                code="EXPERIENCE_INVALID",
            )
        if payload.knowledge_scope not in ENABLED_SCOPES:
            raise ExperienceError(
                f"Experience knowledge_scope '{payload.knowledge_scope}' is not enabled",
                status_code=422,
                code="EXPERIENCE_SCOPE_NOT_ENABLED",
            )

        if idempotency_key:
            cached = await self._store.get_idempotency(
                actor_service_id=access.service_id,
                idempotency_key=idempotency_key,
            )
            if cached is not None and cached.operation == "upsert":
                return ExperienceResponse.model_validate(cached.response_json)

        existing = await self._store.get_by_source(
            source_system=payload.source_system,
            external_id=payload.external_id,
        )
        kb = await ensure_penetration_experience_knowledge_base()

        if existing is None:
            row = ExperienceItemRow(
                id=str(uuid4()),
                external_id=payload.external_id,
                source_system=payload.source_system,
                source_revision=payload.source_revision,
                knowledge_base_id=kb.id,
                knowledge_scope=payload.knowledge_scope,
                workflow_type=payload.workflow_type,
                experience_type=payload.experience_type,
                workspace_id=payload.workspace_id,
                visibility=payload.visibility,
                status=default_new_item_status(),
                index_status=default_index_status(),
                conditions_json=payload.conditions,
                action_summary=payload.action_summary,
                outcome_summary=payload.outcome_summary,
                skill_id=payload.skill_id,
                phase=payload.phase,
                source_task_id=payload.source_task_id,
                evidence_refs_json=[ref.model_dump() for ref in payload.evidence_refs],
                search_text=build_search_text(
                    conditions=payload.conditions,
                    action_summary=payload.action_summary,
                    outcome_summary=payload.outcome_summary,
                    experience_type=payload.experience_type,
                ),
                expires_at=_normalize_expires(payload.expires_at),
            )
            row = await self._store.save_item(row)
            response = _to_response(row)
            await self._maybe_cache_idempotency(
                access, idempotency_key, "upsert", response
            )
            return response

        if payload.source_revision < existing.source_revision:
            raise ExperienceError(
                "Stale source_revision cannot overwrite a newer Experience",
                status_code=409,
                code="EXPERIENCE_STALE_REVISION",
            )

        if payload.source_revision == existing.source_revision:
            if _content_fingerprint(payload) != _row_fingerprint(existing):
                raise ExperienceError(
                    "source_revision matches but payload conflicts with stored content",
                    status_code=409,
                    code="EXPERIENCE_CONFLICT",
                )
            response = _to_response(existing)
            await self._maybe_cache_idempotency(
                access, idempotency_key, "upsert", response
            )
            return response

        search_text = build_search_text(
            conditions=payload.conditions,
            action_summary=payload.action_summary,
            outcome_summary=payload.outcome_summary,
            experience_type=payload.experience_type,
        )
        updated = await self._store.update_item_fields(
            existing.id,
            {
                "source_revision": payload.source_revision,
                "knowledge_base_id": kb.id,
                "knowledge_scope": payload.knowledge_scope,
                "workflow_type": payload.workflow_type,
                "experience_type": payload.experience_type,
                "workspace_id": payload.workspace_id,
                "visibility": payload.visibility,
                "conditions_json": payload.conditions,
                "action_summary": payload.action_summary,
                "outcome_summary": payload.outcome_summary,
                "skill_id": payload.skill_id,
                "phase": payload.phase,
                "source_task_id": payload.source_task_id,
                "evidence_refs_json": [ref.model_dump() for ref in payload.evidence_refs],
                "search_text": search_text,
                "expires_at": _normalize_expires(payload.expires_at),
            },
        )
        assert updated is not None
        if updated.status == ExperienceStatus.PROVEN.value:
            updated = await self._reindex_proven(updated)
        response = _to_response(updated)
        await self._maybe_cache_idempotency(access, idempotency_key, "upsert", response)
        return response

    async def get(self, experience_id: str) -> ExperienceResponse:
        row = await self._store.get(experience_id)
        if row is None:
            raise ExperienceError(
                "Experience not found",
                status_code=404,
                code="RESOURCE_NOT_FOUND",
            )
        return _to_response(row)

    async def list_experiences(
        self,
        *,
        status: str | None = None,
        workflow_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ExperienceListResponse:
        rows = await self._store.list_items(
            status=status,
            workflow_type=workflow_type,
            limit=limit,
            offset=offset,
        )
        items = [_to_response(row) for row in rows]
        return ExperienceListResponse(items=items, total=len(items))

    async def patch_status(
        self,
        experience_id: str,
        payload: ExperienceStatusPatchRequest,
        *,
        access: ExperienceAccessContext,
    ) -> ExperienceResponse:
        row = await self._store.get(experience_id)
        if row is None:
            raise ExperienceError(
                "Experience not found",
                status_code=404,
                code="RESOURCE_NOT_FOUND",
            )
        current = ExperienceStatus(row.status)
        target = payload.status
        if not is_status_transition_allowed(current, target):
            raise ExperienceError(
                f"Transition {current.value} -> {target.value} is not allowed",
                status_code=409,
                code="EXPERIENCE_CONFLICT",
            )

        was_proven = current == ExperienceStatus.PROVEN
        will_be_proven = target == ExperienceStatus.PROVEN
        values: dict[str, Any] = {"status": target.value}
        if will_be_proven and not was_proven:
            values["index_status"] = ExperienceIndexStatus.INDEX_PENDING.value
        if was_proven and not will_be_proven:
            values["index_status"] = ExperienceIndexStatus.NOT_INDEXED.value

        updated = await self._store.update_item_fields(experience_id, values)
        assert updated is not None
        await self._store.add_status_history(
            experience_id=experience_id,
            from_status=current.value,
            to_status=target.value,
            reason=payload.reason,
            actor_service_id=access.service_id,
        )

        if will_be_proven:
            updated = await self._reindex_proven(updated)
        elif was_proven and not will_be_proven:
            await self._indexer.delete_proven(experience_id)

        return _to_response(updated)

    async def feedback(
        self,
        experience_id: str,
        payload: ExperienceFeedbackRequest,
        *,
        access: ExperienceAccessContext,
        idempotency_key: str | None = None,
    ) -> ExperienceFeedbackResponse:
        if payload.experience_id != experience_id:
            raise ExperienceError(
                "Path experience_id must match body experience_id",
                status_code=422,
                code="EXPERIENCE_INVALID",
            )
        row = await self._store.get(experience_id)
        if row is None:
            raise ExperienceError(
                "Experience not found",
                status_code=404,
                code="RESOURCE_NOT_FOUND",
            )

        if idempotency_key:
            cached = await self._store.get_idempotency(
                actor_service_id=access.service_id,
                idempotency_key=idempotency_key,
            )
            if cached is not None and cached.operation == "feedback":
                return ExperienceFeedbackResponse.model_validate(cached.response_json)

        existing_event = await self._store.get_feedback_by_event_id(payload.event_id)
        if existing_event is not None:
            response = ExperienceFeedbackResponse(
                id=existing_event.id,
                event_id=existing_event.event_id,
                experience_id=existing_event.experience_id,
                task_id=existing_event.task_id,
                workflow_type=existing_event.workflow_type,
                outcome=existing_event.outcome,
                evidence_level=existing_event.evidence_level,
                notes=existing_event.notes,
                occurred_at=existing_event.occurred_at,
                duplicated=True,
                experience_status=ExperienceStatus(row.status),
            )
            await self._maybe_cache_idempotency(
                access, idempotency_key, "feedback", response
            )
            return response

        event = ExperienceFeedbackEventRow(
            id=str(uuid4()),
            event_id=payload.event_id,
            experience_id=experience_id,
            task_id=payload.task_id,
            workflow_type=payload.workflow_type,
            outcome=payload.outcome,
            evidence_level=payload.evidence_level,
            notes=payload.notes,
            occurred_at=_normalize_expires(payload.occurred_at),
            actor_service_id=access.service_id,
        )
        event = await self._store.add_feedback(event)

        counters: dict[str, Any] = {"usage_count": row.usage_count + 1}
        if payload.outcome == "success":
            counters["success_count"] = row.success_count + 1
        elif payload.outcome == "failure":
            counters["failure_count"] = row.failure_count + 1
        await self._store.update_item_fields(experience_id, counters)
        refreshed = await self._store.get(experience_id)
        assert refreshed is not None

        response = ExperienceFeedbackResponse(
            id=event.id,
            event_id=event.event_id,
            experience_id=event.experience_id,
            task_id=event.task_id,
            workflow_type=event.workflow_type,
            outcome=event.outcome,
            evidence_level=event.evidence_level,
            notes=event.notes,
            occurred_at=event.occurred_at,
            duplicated=False,
            experience_status=ExperienceStatus(refreshed.status),
        )
        await self._maybe_cache_idempotency(
            access, idempotency_key, "feedback", response
        )
        return response

    async def _reindex_proven(self, row: ExperienceItemRow) -> ExperienceItemRow:
        try:
            await self._indexer.upsert_proven(row)
        except Exception as error:  # noqa: BLE001
            logger.exception("proven indexing failed for %s", row.id)
            updated = await self._store.update_item_fields(
                row.id,
                {"index_status": ExperienceIndexStatus.INDEX_PENDING.value},
            )
            assert updated is not None
            # Keep status proven but not searchable until compensated.
            _ = error
            return updated
        updated = await self._store.update_item_fields(
            row.id,
            {"index_status": ExperienceIndexStatus.INDEXED.value},
        )
        assert updated is not None
        return updated

    async def _maybe_cache_idempotency(
        self,
        access: ExperienceAccessContext,
        idempotency_key: str | None,
        operation: str,
        response: ExperienceResponse | ExperienceFeedbackResponse,
    ) -> None:
        if not idempotency_key:
            return
        await self._store.put_idempotency(
            actor_service_id=access.service_id,
            idempotency_key=idempotency_key,
            operation=operation,
            response_json=json.loads(response.model_dump_json()),
        )


def get_experience_service() -> ExperienceService:
    return ExperienceService()
