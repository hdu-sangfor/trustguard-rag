"""Database-backed Knowledge Scope configuration tests."""

from __future__ import annotations

import httpx
import pytest

from app.application.scopes import resolve_scope_definition
from app.settings import get_settings
from app.stores.experience_store import PENETRATION_EXPERIENCE_KB_ID


async def _create_knowledge_base(client: httpx.AsyncClient, name: str) -> str:
    response = await client.post(
        "/v1/knowledge-bases",
        json={"name": name, "embedding_profile": "configured"},
    )
    assert response.status_code == 201, response.text
    return str(response.json()["id"])


@pytest.mark.asyncio
async def test_scope_configuration_is_persisted_and_resolved(
    client: httpx.AsyncClient,
) -> None:
    first = await _create_knowledge_base(client, "compliance-primary")
    second = await _create_knowledge_base(client, "compliance-secondary")

    saved = await client.put(
        "/v1/knowledge-scopes/compliance",
        json={
            "knowledge_base_ids": [first, second],
            "default_mode": "comprehensive",
            "per_knowledge_base_limit": 30,
            "allowed_content_types": ["legal_article"],
            "allowed_workflow_types": ["compliance"],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["knowledge_base_ids"] == [first, second]
    assert saved.json()["system_knowledge_base_ids"] == []

    loaded = await client.get("/v1/knowledge-scopes/compliance")
    assert loaded.status_code == 200
    assert loaded.json() == saved.json()
    definition = await resolve_scope_definition("compliance")
    assert definition.knowledge_base_ids == [first, second]
    assert definition.per_knowledge_base_limit == 30
    assert definition.allowed_content_types == ["legal_article"]


@pytest.mark.asyncio
async def test_scope_configuration_rejects_unknown_knowledge_base(
    client: httpx.AsyncClient,
) -> None:
    response = await client.put(
        "/v1/knowledge-scopes/product-docs",
        json={"knowledge_base_ids": ["missing-kb"]},
    )
    assert response.status_code == 404
    assert "missing-kb" in response.text


@pytest.mark.asyncio
async def test_penetration_experience_binding_is_system_managed(
    client: httpx.AsyncClient,
) -> None:
    regular = await _create_knowledge_base(client, "penetration-guides")
    initial = await client.get("/v1/knowledge-scopes/penetration")
    assert initial.status_code == 200, initial.text
    assert initial.json()["knowledge_base_ids"] == [PENETRATION_EXPERIENCE_KB_ID]
    assert initial.json()["system_knowledge_base_ids"] == [
        PENETRATION_EXPERIENCE_KB_ID
    ]

    saved = await client.put(
        "/v1/knowledge-scopes/penetration",
        json={
            "knowledge_base_ids": [regular],
            "default_mode": "comprehensive",
            "allowed_workflow_types": [],
        },
    )
    assert saved.status_code == 200, saved.text
    assert saved.json()["knowledge_base_ids"] == [regular, PENETRATION_EXPERIENCE_KB_ID]

    duplicate_system_binding = await client.put(
        "/v1/knowledge-scopes/penetration",
        json={"knowledge_base_ids": [PENETRATION_EXPERIENCE_KB_ID]},
    )
    assert duplicate_system_binding.status_code == 409

    cleared = await client.delete("/v1/knowledge-scopes/penetration")
    assert cleared.status_code == 200
    assert cleared.json()["knowledge_base_ids"] == [PENETRATION_EXPERIENCE_KB_ID]
    assert cleared.json()["allowed_workflow_types"] == ["penetration"]


@pytest.mark.asyncio
async def test_scope_routes_use_gateway_authentication(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_GATEWAY_AUTH_ENABLED", "true")
    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "gateway-token")
    get_settings.cache_clear()

    missing = await client.get("/v1/knowledge-scopes")
    assert missing.status_code == 401
    allowed = await client.get(
        "/v1/knowledge-scopes",
        headers={"Authorization": "Bearer gateway-token"},
    )
    assert allowed.status_code == 200
    get_settings.cache_clear()
