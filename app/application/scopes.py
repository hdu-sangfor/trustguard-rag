"""解析并约束逻辑知识 Scope 到物理知识库的映射。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.domain import RetrievalMode
from app.schemas.knowledge import KnowledgeScope, ScopeDefinition
from app.settings import get_settings


class ScopeRegistry:
    def __init__(self, definitions: Mapping[str, ScopeDefinition]) -> None:
        self._definitions = dict(definitions)

    @classmethod
    def from_json(cls, value: str) -> ScopeRegistry:
        try:
            payload = json.loads(value or "{}")
        except json.JSONDecodeError as error:
            raise ValueError("RAG_MCP_SCOPE_MAPPING_JSON must contain valid JSON") from error
        if not isinstance(payload, dict):
            raise ValueError("RAG_MCP_SCOPE_MAPPING_JSON must be a JSON object")

        definitions: dict[str, ScopeDefinition] = {}
        for raw_scope, raw_definition in payload.items():
            try:
                scope = KnowledgeScope(str(raw_scope)).value
            except ValueError as error:
                raise ValueError(f"Unsupported MCP knowledge scope: {raw_scope}") from error
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
    """Resolve scope mapping and merge the penetration Experience KB when enabled."""
    from app.stores.experience_store import (
        PENETRATION_EXPERIENCE_KB_ID,
        ensure_penetration_experience_knowledge_base,
    )

    settings = get_settings()
    active = registry or ScopeRegistry.from_json(settings.mcp_scope_mapping_json)
    scope_value = str(scope)
    definition = active.get(scope_value)

    if settings.experience_enabled and scope_value == KnowledgeScope.PENETRATION.value:
        kb = await ensure_penetration_experience_knowledge_base()
        experience_kb_id = kb.id or PENETRATION_EXPERIENCE_KB_ID
        if definition is None:
            return ScopeDefinition(
                knowledge_base_ids=[experience_kb_id],
                default_mode=RetrievalMode.AUTO,
                allowed_workflow_types=["penetration"],
            )
        ids = list(definition.knowledge_base_ids)
        if experience_kb_id not in ids:
            ids.append(experience_kb_id)
        workflows = list(definition.allowed_workflow_types)
        if "penetration" not in workflows:
            workflows.append("penetration")
        return definition.model_copy(
            update={
                "knowledge_base_ids": ids,
                "allowed_workflow_types": workflows,
            }
        )

    if definition is None:
        raise LookupError(f"Unknown or unconfigured knowledge scope: {scope}")
    return definition
