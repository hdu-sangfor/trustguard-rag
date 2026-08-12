"""Experience authority application service (Slice A)."""

from __future__ import annotations

import hashlib
import json
import logging
from datetime import datetime, timezone
from typing import Any, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from app.core.indexing.experience_indexer import get_experience_indexer
from app.domain.experience import ExperienceIndexStatus, ExperienceStatus
from app.schemas.experience import (
    ExperienceEvidenceRef,
    ExperienceFeedbackRequest,
    ExperienceFeedbackResponse,
    ExperienceListResponse,
    ExperienceResponse,
    ExperienceStatusPatchRequest,
    ExperienceUpsertRequest,
)
from app.application.access import KnowledgeAccessContext
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
_ResponseModel = TypeVar("_ResponseModel", bound=BaseModel)


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


def _normalize_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is not None:
        return value.astimezone(timezone.utc).replace(tzinfo=None)
    return value


def _content_fingerprint(payload: ExperienceUpsertRequest) -> dict[str, Any]:
    expires_at = _normalize_datetime(payload.expires_at)
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
        "expires_at": expires_at.isoformat() if expires_at else None,
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


def _request_hash(operation: str, identity: str, payload: BaseModel) -> str:
    material = json.dumps(
        {
            "operation": operation,
            "identity": identity,
            "payload": payload.model_dump(mode="json"),
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(material.encode("utf-8")).hexdigest()


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


def _ensure_access_to_values(
    *,
    visibility: str,
    workspace_id: str | None,
    workflow_type: str,
    access: KnowledgeAccessContext,
) -> None:
    if visibility == "workspace" and workspace_id != access.workspace_id:
        raise ExperienceError(
            "Experience workspace is outside the caller context",
            status_code=403,
            code="EXPERIENCE_FORBIDDEN",
        )
    if (
        access.allowed_workflow_types is not None
        and workflow_type not in access.allowed_workflow_types
    ):
        raise ExperienceError(
            "Experience workflow is outside the caller context",
            status_code=403,
            code="EXPERIENCE_FORBIDDEN",
        )


def _ensure_row_access(
    row: ExperienceItemRow,
    access: KnowledgeAccessContext,
) -> None:
    _ensure_access_to_values(
        visibility=row.visibility,
        workspace_id=row.workspace_id,
        workflow_type=row.workflow_type,
        access=access,
    )


def _feedback_matches(
    event: ExperienceFeedbackEventRow,
    payload: ExperienceFeedbackRequest,
) -> bool:
    return (
        event.experience_id == payload.experience_id
        and event.task_id == payload.task_id
        and event.workflow_type == payload.workflow_type
        and event.outcome == payload.outcome
        and event.evidence_level == payload.evidence_level
        and event.notes == payload.notes
        and event.occurred_at == _normalize_datetime(payload.occurred_at)
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
        access: KnowledgeAccessContext,
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
        _ensure_access_to_values(
            visibility=payload.visibility,
            workspace_id=payload.workspace_id,
            workflow_type=payload.workflow_type,
            access=access,
        )

        operation = "upsert"
        request_hash = _request_hash(operation, external_id, payload)
        cached = await self._reserve_idempotency(
            access=access,
            idempotency_key=idempotency_key,
            operation=operation,
            request_hash=request_hash,
            response_type=ExperienceResponse,
        )
        if cached is not None:
            return cached
        try:
            response = await self._upsert_authority(payload, access=access)
            await self._finalize_idempotency(
                access=access,
                idempotency_key=idempotency_key,
                operation=operation,
                request_hash=request_hash,
                response=response,
            )
            return response
        except Exception:
            await self._release_idempotency(
                access=access,
                idempotency_key=idempotency_key,
                operation=operation,
                request_hash=request_hash,
            )
            raise

    async def _upsert_authority(
        self,
        payload: ExperienceUpsertRequest,
        *,
        access: KnowledgeAccessContext,
    ) -> ExperienceResponse:
        kb = await ensure_penetration_experience_knowledge_base()
        for _ in range(8):
            existing = await self._store.get_by_source(
                source_system=payload.source_system,
                external_id=payload.external_id,
            )
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
                    evidence_refs_json=[
                        ref.model_dump() for ref in payload.evidence_refs
                    ],
                    search_text=build_search_text(
                        conditions=payload.conditions,
                        action_summary=payload.action_summary,
                        outcome_summary=payload.outcome_summary,
                        experience_type=payload.experience_type,
                    ),
                    expires_at=_normalize_datetime(payload.expires_at),
                )
                created = await self._store.create_item_if_absent(row)
                if created is not None:
                    return _to_response(created)
                continue

            _ensure_row_access(existing, access)
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
                if (
                    existing.status == ExperienceStatus.PROVEN.value
                    and existing.index_status
                    == ExperienceIndexStatus.INDEX_PENDING.value
                ):
                    existing = await self._reindex_proven(existing)
                return _to_response(existing)

            values: dict[str, Any] = {
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
                "evidence_refs_json": [
                    ref.model_dump() for ref in payload.evidence_refs
                ],
                "search_text": build_search_text(
                    conditions=payload.conditions,
                    action_summary=payload.action_summary,
                    outcome_summary=payload.outcome_summary,
                    experience_type=payload.experience_type,
                ),
                "expires_at": _normalize_datetime(payload.expires_at),
            }
            if existing.status == ExperienceStatus.PROVEN.value:
                values["index_status"] = ExperienceIndexStatus.INDEX_PENDING.value
            updated = await self._store.update_item_if_revision(
                existing.id,
                expected_revision=existing.source_revision,
                values=values,
            )
            if updated is None:
                continue
            if updated.status == ExperienceStatus.PROVEN.value:
                updated = await self._reindex_proven(updated)
            return _to_response(updated)

        raise ExperienceError(
            "Experience was modified concurrently; retry the request",
            status_code=409,
            code="EXPERIENCE_CONCURRENT_UPDATE",
            retryable=True,
        )

    async def get(
        self,
        experience_id: str,
        *,
        access: KnowledgeAccessContext,
    ) -> ExperienceResponse:
        row = await self._store.get(experience_id)
        if row is None:
            raise ExperienceError(
                "Experience not found",
                status_code=404,
                code="RESOURCE_NOT_FOUND",
            )
        _ensure_row_access(row, access)
        return _to_response(row)

    async def list_experiences(
        self,
        *,
        access: KnowledgeAccessContext,
        status: str | None = None,
        workflow_type: str | None = None,
        limit: int = 50,
        offset: int = 0,
    ) -> ExperienceListResponse:
        rows, total = await self._store.list_items(
            status=status,
            workflow_type=workflow_type,
            limit=limit,
            offset=offset,
            workspace_id=access.workspace_id,
            allowed_workflow_types=access.allowed_workflow_types,
        )
        return ExperienceListResponse(
            items=[_to_response(row) for row in rows],
            total=total,
        )

    async def patch_status(
        self,
        experience_id: str,
        payload: ExperienceStatusPatchRequest,
        *,
        access: KnowledgeAccessContext,
    ) -> ExperienceResponse:
        try:
            updated, current, changed = await self._store.transition_status(
                experience_id=experience_id,
                target=payload.status,
                reason=payload.reason,
                actor_service_id=access.service_id,
                workspace_id=access.workspace_id,
                allowed_workflow_types=access.allowed_workflow_types,
            )
        except PermissionError as error:
            raise ExperienceError(
                str(error),
                status_code=403,
                code="EXPERIENCE_FORBIDDEN",
            ) from error
        if updated is None or current is None:
            raise ExperienceError(
                "Experience not found",
                status_code=404,
                code="RESOURCE_NOT_FOUND",
            )
        if not changed:
            raise ExperienceError(
                f"Transition {current.value} -> {payload.status.value} is not allowed",
                status_code=409,
                code="EXPERIENCE_CONFLICT",
            )

        if payload.status == ExperienceStatus.PROVEN:
            updated = await self._reindex_proven(updated)
        elif (
            current == ExperienceStatus.PROVEN
            or updated.index_status == ExperienceIndexStatus.INDEX_PENDING.value
        ):
            updated = await self._remove_projection(updated)
        return _to_response(updated)

    async def feedback(
        self,
        experience_id: str,
        payload: ExperienceFeedbackRequest,
        *,
        access: KnowledgeAccessContext,
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
        _ensure_row_access(row, access)
        if payload.workflow_type != row.workflow_type:
            raise ExperienceError(
                "Feedback workflow_type must match the Experience",
                status_code=409,
                code="EXPERIENCE_CONFLICT",
            )

        operation = "feedback"
        request_hash = _request_hash(operation, experience_id, payload)
        cached = await self._reserve_idempotency(
            access=access,
            idempotency_key=idempotency_key,
            operation=operation,
            request_hash=request_hash,
            response_type=ExperienceFeedbackResponse,
        )
        if cached is not None:
            return cached
        try:
            event = ExperienceFeedbackEventRow(
                id=str(uuid4()),
                event_id=payload.event_id,
                experience_id=experience_id,
                task_id=payload.task_id,
                workflow_type=payload.workflow_type,
                outcome=payload.outcome,
                evidence_level=payload.evidence_level,
                notes=payload.notes,
                occurred_at=_normalize_datetime(payload.occurred_at),
                actor_service_id=access.service_id,
            )
            try:
                result = await self._store.record_feedback(
                    event,
                    workspace_id=access.workspace_id,
                    allowed_workflow_types=access.allowed_workflow_types,
                )
            except LookupError as error:
                raise ExperienceError(
                    "Experience not found",
                    status_code=404,
                    code="RESOURCE_NOT_FOUND",
                ) from error
            except PermissionError as error:
                raise ExperienceError(
                    str(error),
                    status_code=403,
                    code="EXPERIENCE_FORBIDDEN",
                ) from error
            if result.duplicated and not _feedback_matches(result.event, payload):
                raise ExperienceError(
                    "event_id already exists with different feedback content",
                    status_code=409,
                    code="EXPERIENCE_CONFLICT",
                )
            response = ExperienceFeedbackResponse(
                id=result.event.id,
                event_id=result.event.event_id,
                experience_id=result.event.experience_id,
                task_id=result.event.task_id,
                workflow_type=result.event.workflow_type,
                outcome=result.event.outcome,
                evidence_level=result.event.evidence_level,
                notes=result.event.notes,
                occurred_at=result.event.occurred_at,
                duplicated=result.duplicated,
                experience_status=ExperienceStatus(result.experience.status),
            )
            await self._finalize_idempotency(
                access=access,
                idempotency_key=idempotency_key,
                operation=operation,
                request_hash=request_hash,
                response=response,
            )
            return response
        except Exception:
            await self._release_idempotency(
                access=access,
                idempotency_key=idempotency_key,
                operation=operation,
                request_hash=request_hash,
            )
            raise

    async def recover_pending_indexes(self, *, limit: int = 100) -> int:
        recovered = 0
        await self._store.expire_proven(limit=limit)
        for row in await self._store.list_index_pending(limit=limit):
            if row.status == ExperienceStatus.PROVEN.value:
                updated = await self._reindex_proven(row)
            else:
                updated = await self._remove_projection(row)
            if updated.index_status != ExperienceIndexStatus.INDEX_PENDING.value:
                recovered += 1
        return recovered

    async def _reindex_proven(self, row: ExperienceItemRow) -> ExperienceItemRow:
        current = row
        for _ in range(5):
            latest = await self._store.get(current.id)
            if latest is None:
                raise RuntimeError("Experience disappeared during indexing")
            if latest.status != ExperienceStatus.PROVEN.value:
                return await self._remove_projection(latest)
            current = latest
            try:
                await self._indexer.upsert_proven(current)
            except Exception:  # noqa: BLE001
                logger.exception("proven indexing failed for %s", current.id)
                pending = await self._store.update_index_status_if_state(
                    current.id,
                    source_revision=current.source_revision,
                    authority_status=ExperienceStatus.PROVEN.value,
                    index_status=ExperienceIndexStatus.INDEX_PENDING.value,
                )
                return pending or (await self._store.get(current.id)) or current
            indexed = await self._store.update_index_status_if_state(
                current.id,
                source_revision=current.source_revision,
                authority_status=ExperienceStatus.PROVEN.value,
                index_status=ExperienceIndexStatus.INDEXED.value,
            )
            if indexed is not None:
                return indexed
        latest = await self._store.get(current.id)
        return latest or current

    async def _remove_projection(self, row: ExperienceItemRow) -> ExperienceItemRow:
        try:
            removed = await self._indexer.delete_proven(row.id)
        except Exception:  # noqa: BLE001
            logger.exception("experience index removal failed for %s", row.id)
            removed = False
        target = (
            ExperienceIndexStatus.NOT_INDEXED.value
            if removed
            else ExperienceIndexStatus.INDEX_PENDING.value
        )
        updated = await self._store.update_index_status_if_state(
            row.id,
            source_revision=row.source_revision,
            authority_status=row.status,
            index_status=target,
        )
        return updated or (await self._store.get(row.id)) or row

    async def _reserve_idempotency(
        self,
        *,
        access: KnowledgeAccessContext,
        idempotency_key: str | None,
        operation: str,
        request_hash: str,
        response_type: type[_ResponseModel],
    ) -> _ResponseModel | None:
        if not idempotency_key:
            return None
        reservation = await self._store.reserve_idempotency(
            actor_service_id=access.service_id,
            idempotency_key=idempotency_key,
            operation=operation,
            request_hash=request_hash,
        )
        if reservation.created:
            return None
        payload = reservation.row.response_json or {}
        if (
            reservation.row.operation != operation
            or payload.get("request_hash") != request_hash
        ):
            raise ExperienceError(
                "Idempotency-Key was already used for a different request",
                status_code=409,
                code="IDEMPOTENCY_CONFLICT",
            )
        if "response" in payload:
            return response_type.model_validate(payload["response"])
        raise ExperienceError(
            "A request with this Idempotency-Key is still in progress",
            status_code=409,
            code="IDEMPOTENCY_IN_PROGRESS",
            retryable=True,
        )

    async def _finalize_idempotency(
        self,
        *,
        access: KnowledgeAccessContext,
        idempotency_key: str | None,
        operation: str,
        request_hash: str,
        response: BaseModel,
    ) -> None:
        if not idempotency_key:
            return
        await self._store.finalize_idempotency(
            actor_service_id=access.service_id,
            idempotency_key=idempotency_key,
            operation=operation,
            request_hash=request_hash,
            response_json=json.loads(response.model_dump_json()),
        )

    async def _release_idempotency(
        self,
        *,
        access: KnowledgeAccessContext,
        idempotency_key: str | None,
        operation: str,
        request_hash: str,
    ) -> None:
        if not idempotency_key:
            return
        try:
            await self._store.release_idempotency(
                actor_service_id=access.service_id,
                idempotency_key=idempotency_key,
                operation=operation,
                request_hash=request_hash,
            )
        except Exception:  # noqa: BLE001
            logger.warning("failed to release idempotency reservation", exc_info=True)


def get_experience_service() -> ExperienceService:
    return ExperienceService()
