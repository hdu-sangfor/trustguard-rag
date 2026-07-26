"""解析并约束逻辑知识 Scope 到物理知识库的映射。"""

from __future__ import annotations

import json
from collections.abc import Mapping

from app.schemas.knowledge import KnowledgeScope, ScopeDefinition


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
