"""Database-backed logical Knowledge Scope policy and bindings."""

from __future__ import annotations

from dataclasses import dataclass

from sqlalchemy import delete, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.domain import RetrievalMode
from app.schemas.knowledge import KnowledgeScope, ScopeDefinition
from app.schemas.knowledge_scope import KnowledgeScopeResponse, KnowledgeScopeUpdate
from app.stores.db import get_engine
from app.stores.models import (
    KnowledgeBaseRow,
    KnowledgeScopeBindingRow,
    KnowledgeScopeRow,
)

MANUAL_BINDING = "manual"
EXPERIENCE_BINDING = "experience"


@dataclass(frozen=True)
class StoredScopeDefinition:
    definition: ScopeDefinition
    system_knowledge_base_ids: tuple[str, ...]


@dataclass(frozen=True)
class ScopePolicyDefaults:
    default_mode: RetrievalMode
    per_knowledge_base_limit: int
    allowed_content_types: tuple[str, ...]
    allowed_workflow_types: tuple[str, ...]


class KnowledgeScopeStore:
    async def get(
        self,
        scope: str | KnowledgeScope,
        *,
        include_experience: bool = True,
    ) -> StoredScopeDefinition | None:
        scope_value = _scope_value(scope)
        async with AsyncSession(get_engine()) as session:
            row = await session.get(KnowledgeScopeRow, scope_value)
            if row is None:
                return None
            result = await session.execute(
                select(KnowledgeScopeBindingRow)
                .where(KnowledgeScopeBindingRow.scope == scope_value)
                .order_by(
                    KnowledgeScopeBindingRow.position,
                    KnowledgeScopeBindingRow.knowledge_base_id,
                )
            )
            bindings = list(result.scalars())
        if not include_experience:
            bindings = [
                binding
                for binding in bindings
                if binding.binding_type != EXPERIENCE_BINDING
            ]
        knowledge_base_ids = [binding.knowledge_base_id for binding in bindings]
        if not knowledge_base_ids:
            return None
        return StoredScopeDefinition(
            definition=ScopeDefinition(
                knowledge_base_ids=knowledge_base_ids,
                default_mode=RetrievalMode(row.default_mode),
                per_knowledge_base_limit=row.per_knowledge_base_limit,
                allowed_content_types=list(row.allowed_content_types or []),
                allowed_workflow_types=list(row.allowed_workflow_types or []),
            ),
            system_knowledge_base_ids=tuple(
                binding.knowledge_base_id
                for binding in bindings
                if binding.binding_type != MANUAL_BINDING
            ),
        )

    async def list(self, *, include_experience: bool = True) -> list[KnowledgeScopeResponse]:
        async with AsyncSession(get_engine()) as session:
            rows = list(
                (
                    await session.execute(
                        select(KnowledgeScopeRow).order_by(KnowledgeScopeRow.scope)
                    )
                ).scalars()
            )
            bindings = list(
                (
                    await session.execute(
                        select(KnowledgeScopeBindingRow).order_by(
                            KnowledgeScopeBindingRow.scope,
                            KnowledgeScopeBindingRow.position,
                            KnowledgeScopeBindingRow.knowledge_base_id,
                        )
                    )
                ).scalars()
            )
        by_scope: dict[str, list[KnowledgeScopeBindingRow]] = {}
        for binding in bindings:
            if not include_experience and binding.binding_type == EXPERIENCE_BINDING:
                continue
            by_scope.setdefault(binding.scope, []).append(binding)
        return [
            _response(row, by_scope.get(row.scope, []))
            for row in rows
            if by_scope.get(row.scope)
        ]

    async def replace_manual(
        self,
        scope: str | KnowledgeScope,
        request: KnowledgeScopeUpdate,
    ) -> KnowledgeScopeResponse:
        scope_value = _scope_value(scope)
        async with AsyncSession(get_engine()) as session, session.begin():
            kb_rows = list(
                (
                    await session.execute(
                        select(KnowledgeBaseRow).where(
                            KnowledgeBaseRow.id.in_(request.knowledge_base_ids)
                        )
                    )
                ).scalars()
            )
            found = {row.id for row in kb_rows}
            missing = [item for item in request.knowledge_base_ids if item not in found]
            if missing:
                raise LookupError(
                    "Knowledge base not found: " + ", ".join(missing)
                )

            existing_bindings = list(
                (
                    await session.execute(
                        select(KnowledgeScopeBindingRow).where(
                            KnowledgeScopeBindingRow.scope == scope_value,
                            KnowledgeScopeBindingRow.binding_type != MANUAL_BINDING,
                        )
                    )
                ).scalars()
            )
            system_ids = {binding.knowledge_base_id for binding in existing_bindings}
            if system_ids.intersection(request.knowledge_base_ids):
                raise ValueError(
                    "System-managed knowledge base bindings must not be configured manually"
                )

            row = await session.get(KnowledgeScopeRow, scope_value)
            if row is None:
                row = KnowledgeScopeRow(scope=scope_value)
                session.add(row)
            row.default_mode = request.default_mode.value
            row.per_knowledge_base_limit = request.per_knowledge_base_limit
            row.allowed_content_types = list(request.allowed_content_types)
            row.allowed_workflow_types = list(request.allowed_workflow_types)
            await session.execute(
                delete(KnowledgeScopeBindingRow).where(
                    KnowledgeScopeBindingRow.scope == scope_value,
                    KnowledgeScopeBindingRow.binding_type == MANUAL_BINDING,
                )
            )
            for position, knowledge_base_id in enumerate(request.knowledge_base_ids):
                session.add(
                    KnowledgeScopeBindingRow(
                        scope=scope_value,
                        knowledge_base_id=knowledge_base_id,
                        position=position,
                        binding_type=MANUAL_BINDING,
                    )
                )

        response = await self.get_response(scope_value)
        if response is None:  # pragma: no cover - transaction guarantees a row
            raise RuntimeError("Knowledge scope disappeared after update")
        return response

    async def clear_manual(
        self,
        scope: str | KnowledgeScope,
    ) -> KnowledgeScopeResponse | None:
        scope_value = _scope_value(scope)
        async with AsyncSession(get_engine()) as session, session.begin():
            row = await session.get(KnowledgeScopeRow, scope_value)
            if row is None:
                return None
            await session.execute(
                delete(KnowledgeScopeBindingRow).where(
                    KnowledgeScopeBindingRow.scope == scope_value,
                    KnowledgeScopeBindingRow.binding_type == MANUAL_BINDING,
                )
            )
            remaining = list(
                (
                    await session.execute(
                        select(KnowledgeScopeBindingRow).where(
                            KnowledgeScopeBindingRow.scope == scope_value,
                            KnowledgeScopeBindingRow.binding_type != MANUAL_BINDING,
                        )
                    )
                ).scalars()
            )
            if not remaining:
                await session.delete(row)
                return None
            defaults = _system_defaults(scope_value)
            row.default_mode = defaults.default_mode.value
            row.per_knowledge_base_limit = defaults.per_knowledge_base_limit
            row.allowed_content_types = list(defaults.allowed_content_types)
            row.allowed_workflow_types = list(defaults.allowed_workflow_types)
        return await self.get_response(scope_value)

    async def ensure_system_binding(
        self,
        scope: str | KnowledgeScope,
        knowledge_base_id: str,
        *,
        binding_type: str,
    ) -> None:
        if binding_type == MANUAL_BINDING:
            raise ValueError("System binding type cannot be manual")
        scope_value = _scope_value(scope)
        try:
            async with AsyncSession(get_engine()) as session, session.begin():
                row = await session.get(KnowledgeScopeRow, scope_value)
                if row is None:
                    defaults = _system_defaults(scope_value)
                    row = KnowledgeScopeRow(
                        scope=scope_value,
                        default_mode=defaults.default_mode.value,
                        per_knowledge_base_limit=defaults.per_knowledge_base_limit,
                        allowed_content_types=list(defaults.allowed_content_types),
                        allowed_workflow_types=list(defaults.allowed_workflow_types),
                    )
                    session.add(row)
                    await session.flush()
                key = {"scope": scope_value, "knowledge_base_id": knowledge_base_id}
                binding = await session.get(KnowledgeScopeBindingRow, key)
                if binding is None:
                    session.add(
                        KnowledgeScopeBindingRow(
                            scope=scope_value,
                            knowledge_base_id=knowledge_base_id,
                            position=10_000,
                            binding_type=binding_type,
                        )
                    )
                elif binding.binding_type != binding_type:
                    binding.binding_type = binding_type
                    binding.position = 10_000
        except IntegrityError:
            # Concurrent first use may race to insert the same scope/binding.
            # Accept the winner only after verifying that the desired row exists.
            async with AsyncSession(get_engine()) as session:
                binding = await session.get(
                    KnowledgeScopeBindingRow,
                    {"scope": scope_value, "knowledge_base_id": knowledge_base_id},
                )
            if binding is None or binding.binding_type != binding_type:
                raise

    async def get_response(
        self,
        scope: str | KnowledgeScope,
        *,
        include_experience: bool = True,
    ) -> KnowledgeScopeResponse | None:
        scope_value = _scope_value(scope)
        async with AsyncSession(get_engine()) as session:
            row = await session.get(KnowledgeScopeRow, scope_value)
            if row is None:
                return None
            result = await session.execute(
                select(KnowledgeScopeBindingRow)
                .where(KnowledgeScopeBindingRow.scope == scope_value)
                .order_by(
                    KnowledgeScopeBindingRow.position,
                    KnowledgeScopeBindingRow.knowledge_base_id,
                )
            )
            bindings = list(result.scalars())
        if not include_experience:
            bindings = [
                item for item in bindings if item.binding_type != EXPERIENCE_BINDING
            ]
        if not bindings:
            return None
        return _response(row, bindings)


def _scope_value(scope: str | KnowledgeScope) -> str:
    return KnowledgeScope(str(scope)).value


def _system_defaults(scope: str) -> ScopePolicyDefaults:
    workflows = [scope] if scope in {
        KnowledgeScope.PENETRATION.value,
        KnowledgeScope.ALERT_TRIAGE.value,
    } else []
    return ScopePolicyDefaults(
        default_mode=RetrievalMode.AUTO,
        per_knowledge_base_limit=20,
        allowed_content_types=(),
        allowed_workflow_types=tuple(workflows),
    )


def _response(
    row: KnowledgeScopeRow,
    bindings: list[KnowledgeScopeBindingRow],
) -> KnowledgeScopeResponse:
    return KnowledgeScopeResponse(
        scope=KnowledgeScope(row.scope),
        knowledge_base_ids=[binding.knowledge_base_id for binding in bindings],
        system_knowledge_base_ids=[
            binding.knowledge_base_id
            for binding in bindings
            if binding.binding_type != MANUAL_BINDING
        ],
        default_mode=RetrievalMode(row.default_mode),
        per_knowledge_base_limit=row.per_knowledge_base_limit,
        allowed_content_types=list(row.allowed_content_types or []),
        allowed_workflow_types=list(row.allowed_workflow_types or []),
        created_at=row.created_at,
        updated_at=row.updated_at,
    )


_store: KnowledgeScopeStore | None = None


def get_knowledge_scope_store() -> KnowledgeScopeStore:
    global _store
    if _store is None:
        _store = KnowledgeScopeStore()
    return _store
