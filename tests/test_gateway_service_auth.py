"""Phase 2.1：Gateway 与 MCP 服务身份不能互相替代。"""

from __future__ import annotations

import json

import httpx
import pytest

from app.application.access import (
    KnowledgeAccessDenied,
    KnowledgePermission,
    gateway_access_context,
    mcp_access_context,
)
from app.core.embedding.profiles import get_embedding_profile
from app.settings import get_settings
from app.stores.knowledge_base_store import KnowledgeBaseStore


@pytest.mark.asyncio
async def test_gateway_identity_protects_business_rest_but_not_health(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_GATEWAY_AUTH_ENABLED", "true")
    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "gateway-service-secret")
    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "mcp-service-secret")
    get_settings.cache_clear()

    health = await client.get("/health")
    missing = await client.get("/v1/knowledge-bases")
    wrong_identity = await client.get(
        "/v1/knowledge-bases",
        headers={"Authorization": "Bearer mcp-service-secret"},
    )
    authorized = await client.get(
        "/v1/knowledge-bases",
        headers={"Authorization": "Bearer gateway-service-secret"},
    )

    assert health.status_code == 200
    assert missing.status_code == 401
    assert wrong_identity.status_code == 401
    assert authorized.status_code == 200


@pytest.mark.asyncio
async def test_public_search_cannot_bypass_gateway_identity(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await KnowledgeBaseStore().create(
        name="Gateway 鉴权搜索",
        profile=get_embedding_profile("configured"),
    )

    class _Search:
        async def search(self, **_kwargs):
            return {
                "search_status": "ok",
                "effective_mode": "keyword_only",
                "results": [],
                "total": 0,
                "fusion_method": "rrf",
                "retrieval_time_ms": 1.0,
                "components": {"vector": 0, "keyword": 0},
                "degraded_components": [],
            }

    monkeypatch.setattr(
        "app.application.knowledge.get_hybrid_search",
        lambda: _Search(),
    )
    monkeypatch.setenv("RAG_GATEWAY_AUTH_ENABLED", "true")
    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "gateway-service-secret")
    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "mcp-service-secret")
    monkeypatch.setenv(
        "RAG_MCP_SCOPE_MAPPING_JSON",
        json.dumps({"compliance": {"knowledge_base_ids": [knowledge_base.id]}}),
    )
    get_settings.cache_clear()
    payload = {
        "query": "不能绕过 Gateway",
        "knowledge_base_id": knowledge_base.id,
        "enable_vector": False,
    }

    missing = await client.post("/v1/search", json=payload)
    mcp_identity = await client.post(
        "/v1/search",
        headers={"Authorization": "Bearer mcp-service-secret"},
        json=payload,
    )
    gateway_identity = await client.post(
        "/v1/search",
        headers={"Authorization": "Bearer gateway-service-secret"},
        json=payload,
    )
    scope_payload = {
        "schema_version": "trustguard-knowledge-search-request-v1",
        "query": "不能绕过 Gateway",
        "scope": "compliance",
    }
    mcp_scope_identity = await client.post(
        "/v1/search/scope",
        headers={"Authorization": "Bearer mcp-service-secret"},
        json=scope_payload,
    )
    gateway_scope_identity = await client.post(
        "/v1/search/scope",
        headers={"Authorization": "Bearer gateway-service-secret"},
        json=scope_payload,
    )

    assert missing.status_code == 401
    assert mcp_identity.status_code == 401
    assert gateway_identity.status_code == 200
    assert mcp_scope_identity.status_code == 401
    assert gateway_scope_identity.status_code == 200


@pytest.mark.asyncio
async def test_gateway_and_mcp_internal_tokens_are_not_interchangeable(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("RAG_GATEWAY_AUTH_ENABLED", "true")
    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "gateway-service-secret")
    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "mcp-service-secret")
    get_settings.cache_clear()
    resource_path = "/v1/internal/knowledge/resources/krf1.invalid?scope=compliance"
    scope_search_path = "/v1/internal/knowledge/search-scope"
    scope_payload = {
        "schema_version": "trustguard-knowledge-search-request-v1",
        "query": "安全要求",
        "scope": "compliance",
    }

    gateway_on_resource = await client.get(
        resource_path,
        headers={"Authorization": "Bearer gateway-service-secret"},
    )
    mcp_on_resource = await client.get(
        resource_path,
        headers={"Authorization": "Bearer mcp-service-secret"},
    )
    gateway_on_scope_search = await client.post(
        scope_search_path,
        headers={"Authorization": "Bearer gateway-service-secret"},
        json=scope_payload,
    )
    mcp_on_scope_search = await client.post(
        scope_search_path,
        headers={"Authorization": "Bearer mcp-service-secret"},
        json=scope_payload,
    )
    cross_workspace_scope_search = await client.post(
        scope_search_path,
        headers={
            "Authorization": "Bearer mcp-service-secret",
            "X-TrustGuard-Workspace-ID": "other-workspace",
        },
        json=scope_payload,
    )

    assert gateway_on_resource.status_code == 401
    assert mcp_on_resource.status_code == 404
    assert gateway_on_scope_search.status_code == 401
    assert mcp_on_scope_search.status_code == 404
    assert cross_workspace_scope_search.status_code == 403


def test_access_contexts_expose_only_required_permissions() -> None:
    gateway = gateway_access_context(
        service_id="gateway",
        workspace_id="default",
    )
    mcp = mcp_access_context(
        service_id="mcp",
        workspace_id="default",
    )

    gateway.require(KnowledgePermission.SEARCH, knowledge_base_id="kb-a")
    gateway.require(KnowledgePermission.ANSWER, knowledge_base_id="kb-a")
    mcp.require(KnowledgePermission.SEARCH, knowledge_base_id="kb-a")
    mcp.require(KnowledgePermission.RESOURCE_READ, knowledge_base_id="kb-a")

    with pytest.raises(KnowledgeAccessDenied):
        gateway.require(
            KnowledgePermission.RESOURCE_READ,
            knowledge_base_id="kb-a",
        )
    with pytest.raises(KnowledgeAccessDenied):
        mcp.require(KnowledgePermission.MANAGE, knowledge_base_id="kb-a")
