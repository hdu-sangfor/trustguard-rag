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
