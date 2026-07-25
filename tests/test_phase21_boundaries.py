"""Phase 2.1：内部检索服务身份与共享应用服务边界。"""

from __future__ import annotations

import json
from typing import Any

import httpx
import pytest

from app.core.embedding.profiles import get_embedding_profile
from app.main import create_app
from app.mcp_server.backend import BackendError, RestRagBackend
from app.settings import Settings, get_settings
from app.stores.knowledge_base_store import KnowledgeBaseStore


class _SearchEngine:
    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    async def search(self, **kwargs: Any) -> dict[str, Any]:
        self.calls.append(kwargs)
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


@pytest.mark.asyncio
async def test_internal_search_requires_auth_and_matches_public_semantics(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    knowledge_base = await KnowledgeBaseStore().create(
        name="Phase 2.1 内部搜索",
        profile=get_embedding_profile("configured"),
    )
    engine = _SearchEngine()
    monkeypatch.setattr(
        "app.application.knowledge.get_hybrid_search",
        lambda: engine,
    )
    payload = {
        "query": "验证共享检索语义",
        "knowledge_base_id": knowledge_base.id,
        "enable_vector": False,
    }
    request_headers = {"X-Request-ID": "req-phase21-search"}

    public = await client.post(
        "/v1/search",
        headers=request_headers,
        json=payload,
    )
    assert public.status_code == 200

    get_settings.cache_clear()
    unconfigured = await client.post(
        "/v1/internal/knowledge/search",
        headers=request_headers,
        json=payload,
    )
    assert unconfigured.status_code == 503
    assert unconfigured.json()["code"] == "RAG_UNAVAILABLE"

    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "phase21-service-secret")
    get_settings.cache_clear()
    unauthorized = await client.post(
        "/v1/internal/knowledge/search",
        headers={**request_headers, "Authorization": "Bearer wrong"},
        json=payload,
    )
    assert unauthorized.status_code == 401
    assert unauthorized.json()["code"] == "AUTH_REQUIRED"

    internal = await client.post(
        "/v1/internal/knowledge/search",
        headers={
            **request_headers,
            "Authorization": "Bearer phase21-service-secret",
        },
        json=payload,
    )
    assert internal.status_code == 200
    assert internal.json() == public.json()
    assert len(engine.calls) == 2
    assert all(
        call["knowledge_base_id"] == knowledge_base.id for call in engine.calls
    )


@pytest.mark.asyncio
async def test_internal_scope_search_runs_federation_in_application_service(
    client: httpx.AsyncClient,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    first = await KnowledgeBaseStore().create(
        name="Phase 2.2 Scope A",
        profile=get_embedding_profile("configured"),
    )
    second = await KnowledgeBaseStore().create(
        name="Phase 2.2 Scope B",
        profile=get_embedding_profile("configured"),
    )
    engine = _SearchEngine()
    monkeypatch.setattr(
        "app.application.knowledge.get_hybrid_search",
        lambda: engine,
    )
    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "phase22-service-secret")
    monkeypatch.setenv(
        "RAG_MCP_SCOPE_MAPPING_JSON",
        json.dumps({"compliance": [first.id, second.id]}),
    )
    get_settings.cache_clear()

    response = await client.post(
        "/v1/internal/knowledge/search-scope",
        headers={
            "Authorization": "Bearer phase22-service-secret",
            "X-Request-ID": "req-phase22-scope",
        },
        json={
            "schema_version": "trustguard-knowledge-search-request-v1",
            "query": "password=secret-value 合规要求",
            "scope": "compliance",
            "limit": 3,
        },
    )
    public_response = await client.post(
        "/v1/search/scope",
        headers={"X-Request-ID": "req-phase22-public-scope"},
        json={
            "schema_version": "trustguard-knowledge-search-request-v1",
            "query": "password=secret-value 合规要求",
            "scope": "compliance",
            "limit": 3,
        },
    )

    assert response.status_code == 200
    assert public_response.status_code == 200
    assert response.json()["schema_version"] == "trustguard-knowledge-search-v1"
    assert response.json()["request_id"] == "req-phase22-scope"
    assert response.json()["scope"] == "compliance"
    assert public_response.json()["scope"] == response.json()["scope"]
    assert public_response.json()["hits"] == response.json()["hits"]
    assert len(engine.calls) == 4
    assert {call["knowledge_base_id"] for call in engine.calls} == {
        first.id,
        second.id,
    }
    assert all("secret-value" not in call["query"] for call in engine.calls)


@pytest.mark.asyncio
async def test_mcp_rest_backend_uses_authenticated_internal_search() -> None:
    seen: dict[str, Any] = {}

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("Authorization")
        seen["request_id"] = request.headers.get("X-Request-ID")
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json={"content_revision": 3, "results": []})

    backend = RestRagBackend(
        base_url="http://rag.test",
        internal_service_token="phase21-service-secret",
        timeout_seconds=1.0,
    )
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://rag.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await backend.search(
            knowledge_base_id="kb-a",
            request_id="req-mcp-internal-search",
            payload={"query": "安全要求"},
        )
    finally:
        await backend.aclose()

    assert result == {"content_revision": 3, "results": []}
    assert seen == {
        "path": "/v1/internal/knowledge/search",
        "authorization": "Bearer phase21-service-secret",
        "request_id": "req-mcp-internal-search",
        "json": {"query": "安全要求", "knowledge_base_id": "kb-a"},
    }


@pytest.mark.asyncio
async def test_mcp_rest_backend_delegates_one_authenticated_scope_search() -> None:
    seen: dict[str, Any] = {}
    response_payload = {
        "schema_version": "trustguard-knowledge-search-v1",
        "request_id": "req-scope-search",
        "scope": "compliance",
        "status": "ok",
        "content_revision": "revision",
        "hits": [],
        "query_plan": {"intent": "auto", "source": "heuristic"},
        "coverage": {"status": "not_applicable", "warning": None},
        "degraded_components": [],
        "latency_ms": 1.0,
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["authorization"] = request.headers.get("Authorization")
        seen["request_id"] = request.headers.get("X-Request-ID")
        seen["workspace_id"] = request.headers.get("X-TrustGuard-Workspace-ID")
        seen["workflow_types"] = request.headers.get(
            "X-TrustGuard-Workflow-Types"
        )
        seen["json"] = json.loads(request.content)
        return httpx.Response(200, json=response_payload)

    backend = RestRagBackend(
        base_url="http://rag.test",
        internal_service_token="phase21-service-secret",
        timeout_seconds=1.0,
    )
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://rag.test",
        transport=httpx.MockTransport(handler),
    )
    request_payload = {
        "schema_version": "trustguard-knowledge-search-request-v1",
        "query": "安全要求",
        "scope": "compliance",
        "mode": "auto",
        "limit": 5,
        "rewrite": False,
        "filters": {"content_types": [], "source_types": []},
    }
    try:
        result = await backend.search_scope(
            request_id="req-scope-search",
            payload=request_payload,
            workspace_id="default",
            allowed_workflow_types=frozenset({"penetration"}),
        )
    finally:
        await backend.aclose()

    assert result == response_payload
    assert seen == {
        "path": "/v1/internal/knowledge/search-scope",
        "authorization": "Bearer phase21-service-secret",
        "request_id": "req-scope-search",
        "workspace_id": "default",
        "workflow_types": "penetration",
        "json": request_payload,
    }


@pytest.mark.asyncio
async def test_mcp_rest_backend_maps_missing_scope_to_stable_error() -> None:
    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            404,
            json={
                "request_id": "req-missing-scope",
                "code": "RESOURCE_NOT_FOUND",
                "message": "The requested knowledge scope is not configured",
                "retryable": False,
                "detail": "The requested knowledge scope is not configured",
            },
        )

    backend = RestRagBackend(
        base_url="http://rag.test",
        internal_service_token="phase21-service-secret",
        timeout_seconds=1.0,
    )
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://rag.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        with pytest.raises(BackendError) as captured:
            await backend.search_scope(
                request_id="req-missing-scope",
                payload={
                    "schema_version": "trustguard-knowledge-search-request-v1",
                    "query": "安全要求",
                    "scope": "compliance",
                },
            )
    finally:
        await backend.aclose()

    assert captured.value.code == "UNKNOWN_SCOPE"
    assert captured.value.status_code == 404
    assert captured.value.retryable is False


@pytest.mark.asyncio
async def test_mcp_rest_backend_reads_resource_ref_with_trusted_context() -> None:
    seen: dict[str, Any] = {}
    response_payload = {
        "schema_version": "trustguard-knowledge-resource-v1",
        "scope": "compliance",
        "content_revision": "3",
        "resource_ref": "krf1.opaque",
        "source_revision": 1,
        "content_hash": f"sha256:{'a' * 64}",
        "chunk_id": "chunk-a",
        "document_id": "doc-a",
        "experience_id": None,
        "text": "完整来源",
        "title": None,
        "filename": None,
        "page_no": None,
        "source_uri": "upload://doc-a",
        "source_type": "document",
        "workflow_type": None,
        "effectiveness": None,
        "visibility": "global",
        "metadata": {},
    }

    async def handler(request: httpx.Request) -> httpx.Response:
        seen["path"] = request.url.path
        seen["scope"] = request.url.params.get("scope")
        seen["workspace_id"] = request.headers.get("X-TrustGuard-Workspace-ID")
        seen["workflow_types"] = request.headers.get(
            "X-TrustGuard-Workflow-Types"
        )
        return httpx.Response(200, json=response_payload)

    backend = RestRagBackend(
        base_url="http://rag.test",
        internal_service_token="phase21-service-secret",
        timeout_seconds=1.0,
    )
    await backend._client.aclose()
    backend._client = httpx.AsyncClient(
        base_url="http://rag.test",
        transport=httpx.MockTransport(handler),
    )
    try:
        result = await backend.get_resource(
            scope="compliance",
            resource_ref="krf1.opaque",
            request_id="req-resource-ref",
            workspace_id="default",
            allowed_workflow_types=frozenset({"penetration"}),
        )
    finally:
        await backend.aclose()

    assert result == response_payload
    assert seen == {
        "path": "/v1/internal/knowledge/resources/krf1.opaque",
        "scope": "compliance",
        "workspace_id": "default",
        "workflow_types": "penetration",
    }


@pytest.mark.asyncio
async def test_mcp_rest_backend_fails_closed_without_internal_identity() -> None:
    backend = RestRagBackend(
        base_url="http://rag.test",
        internal_service_token=None,
        timeout_seconds=1.0,
    )
    try:
        with pytest.raises(BackendError) as captured:
            await backend.search(
                knowledge_base_id="kb-a",
                request_id="req-no-service-identity",
                payload={"query": "安全要求"},
            )
    finally:
        await backend.aclose()

    assert captured.value.code == "RAG_UNAVAILABLE"
    assert captured.value.retryable is False


def test_production_requires_internal_identity_and_mcp_auth(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.delenv("RAG_INTERNAL_SERVICE_TOKEN", raising=False)
    monkeypatch.setenv("RAG_APP_ENV", "prod")
    monkeypatch.setenv("RAG_QDRANT_MOCK", "false")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "false")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="RAG_INTERNAL_SERVICE_TOKEN"):
        create_app()

    monkeypatch.setenv("RAG_INTERNAL_SERVICE_TOKEN", "internal-service-token")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="RAG_GATEWAY_AUTH_ENABLED"):
        create_app()

    monkeypatch.setenv("RAG_GATEWAY_AUTH_ENABLED", "true")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="RAG_GATEWAY_SERVICE_TOKEN"):
        create_app()

    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "internal-service-token")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="must differ"):
        create_app()

    monkeypatch.setenv("RAG_GATEWAY_SERVICE_TOKEN", "gateway-service-token")
    get_settings.cache_clear()
    with pytest.raises(ValueError, match="RAG_RESOURCE_REF_SECRET"):
        create_app()

    monkeypatch.setenv(
        "RAG_RESOURCE_REF_SECRET",
        "production-resource-ref-secret-with-at-least-32-characters",
    )
    get_settings.cache_clear()
    create_app()

    monkeypatch.delenv("RAG_INTERNAL_SERVICE_TOKEN", raising=False)
    with pytest.raises(ValueError, match="RAG_INTERNAL_SERVICE_TOKEN"):
        Settings(
            _env_file=None,
            app_env="prod",
            mcp_enabled=True,
            mcp_auth_enabled=True,
            mcp_auth_issuer="https://auth.test",
            mcp_auth_jwks_url="https://auth.test/.well-known/jwks.json",
            qdrant_mock=False,
            search_opensearch_mock=False,
        )

    with pytest.raises(ValueError, match="RAG_MCP_AUTH_ENABLED"):
        Settings(
            _env_file=None,
            app_env="prod",
            internal_service_token="production-service-token",
            mcp_enabled=True,
            mcp_auth_enabled=False,
            qdrant_mock=False,
            search_opensearch_mock=False,
        )
    get_settings.cache_clear()
