"""Resolve logical Knowledge Scopes from persisted policy."""

from __future__ import annotations

from collections.abc import Mapping

from app.domain import RetrievalMode
from app.schemas.knowledge import KnowledgeScope, ScopeDefinition
from app.settings import get_settings


class ScopeRegistry:
    """In-memory registry used only for isolated application-service tests."""

    def __init__(self, definitions: Mapping[str, ScopeDefinition]) -> None:
        self._definitions = dict(definitions)

    @classmethod
    def from_definitions(
        cls,
        payload: Mapping[str, ScopeDefinition | Mapping[str, object]],
    ) -> ScopeRegistry:
        definitions: dict[str, ScopeDefinition] = {}
        for raw_scope, raw_definition in payload.items():
            try:
                scope = KnowledgeScope(str(raw_scope)).value
            except ValueError as error:
                raise ValueError(f"Unsupported knowledge scope: {raw_scope}") from error
            definitions[scope] = ScopeDefinition.model_validate(raw_definition)
        return cls(definitions)

    def get(self, scope: str | KnowledgeScope) -> ScopeDefinition | None:
        return self._definitions.get(str(scope))

    def require(self, scope: str | KnowledgeScope) -> ScopeDefinition:
        definition = self.get(scope)
        if definition is None:
            raise LookupError(f"Unknown or unconfigured knowledge scope: {scope}")
        return definition

    @property
    def configured_scopes(self) -> tuple[str, ...]:
        return tuple(sorted(self._definitions))

    def __bool__(self) -> bool:
        return bool(self._definitions)


async def resolve_scope_definition(
    scope: str | KnowledgeScope,
    *,
    registry: ScopeRegistry | None = None,
) -> ScopeDefinition:
    """Resolve a Scope from the database and synchronize system-owned bindings."""
    from app.stores.experience_store import (
        PENETRATION_EXPERIENCE_KB_ID,
        ensure_penetration_experience_knowledge_base,
    )

    from app.stores.knowledge_scope_store import get_knowledge_scope_store

    settings = get_settings()
    scope_value = str(scope)
    definition = registry.get(scope_value) if registry is not None else None

    if settings.experience_enabled and scope_value == KnowledgeScope.PENETRATION.value:
        kb = await ensure_penetration_experience_knowledge_base()
        experience_kb_id = kb.id or PENETRATION_EXPERIENCE_KB_ID
        if registry is not None:
            if definition is None:
                return ScopeDefinition(
                    knowledge_base_ids=[experience_kb_id],
                    default_mode=RetrievalMode.AUTO,
                    allowed_workflow_types=["penetration"],
                )
            ids = list(definition.knowledge_base_ids)
            if experience_kb_id not in ids:
                ids.append(experience_kb_id)
            return definition.model_copy(update={"knowledge_base_ids": ids})

    if registry is None:
        stored = await get_knowledge_scope_store().get(
            scope_value,
            include_experience=settings.experience_enabled,
        )
        definition = stored.definition if stored is not None else None

    if definition is None:
        raise LookupError(f"Unknown or unconfigured knowledge scope: {scope}")
    return definition
