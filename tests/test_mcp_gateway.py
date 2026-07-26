"""Phase 2：只读 MCP Gateway 的联邦、资源、鉴权和协议测试。"""

from __future__ import annotations

import json
import time
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from jsonschema import Draft202012Validator
from mcp import ClientSession
from mcp.client.streamable_http import streamable_http_client
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from mcp.server.auth.provider import AccessToken

from app.mcp_server.auth import (
    JwtTokenVerifier,
    ScopeAuthorizationError,
    authorize_knowledge_scope,
)
from app.mcp_server.gateway import (
    KnowledgeGateway,
    KnowledgeGatewayError,
)
from app.application.knowledge import aggregate_revision
from app.application.scopes import ScopeRegistry
from app.schemas.knowledge import KnowledgeSearchRequest
from app.mcp_server.server import create_mcp_app
from app.settings import Settings

_CONTRACT_SCHEMAS = Path(__file__).parents[1] / "contracts" / "v1" / "schemas"


class _FakeBackend:
    def __init__(self) -> None:
        self.scope_search: dict[str, Any] | BaseException = RuntimeError(
            "scope search is not configured"
        )
        self.resources: dict[str, dict[str, Any] | BaseException] = {}
        self.seen_scope_payloads: list[dict[str, Any]] = []
        self.is_ready = True
        self.closed = False

    async def search_scope(
        self,
        *,
        request_id: str,
        payload: dict[str, Any],
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        self.seen_scope_payloads.append(payload)
        if isinstance(self.scope_search, BaseException):
            raise self.scope_search
        return self.scope_search

    async def read_resource(
        self,
        *,
        scope: str,
        resource_ref: str,
        request_id: str,
        workspace_id: str | None = None,
        allowed_workflow_types: frozenset[str] | None = None,
    ) -> dict[str, Any]:
        value = self.resources[resource_ref]
        if isinstance(value, BaseException):
            raise value
        return value

    async def ready(self) -> bool:
        return self.is_ready

    async def aclose(self) -> None:
        self.closed = True


def _knowledge_search_payload(
    *,
    request_id: str,
    revision: str,
    hits: list[dict[str, Any]],
    status: str = "ok",
    degraded: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "trustguard-knowledge-search-v1",
        "request_id": request_id,
        "scope": "compliance",
        "status": status,
        "content_revision": revision,
        "hits": hits,
        "query_plan": {"intent": "comprehensive", "source": "heuristic"},
        "coverage": {"status": "not_applicable", "warning": None},
        "degraded_components": degraded or [],
        "latency_ms": 1.0,
    }


def _knowledge_hit(
    chunk_id: str,
    *,
    revision: str,
    text: str,
) -> dict[str, Any]:
    resource_ref = f"krf1.{chunk_id}-opaque"
    return {
        "resource_uri": (f"trustguard-rag://compliance/resources/{resource_ref}"),
        "resource_ref": resource_ref,
        "source_revision": 1,
        "content_hash": f"sha256:{'a' * 64}",
        "snippet": text,
        "score": 0.5,
        "title": "安全资料",
        "document_id": "doc-1",
        "filename": "doc-1.pdf",
        "page_no": 1,
        "source_uri": "upload://doc-1.pdf",
        "source_type": "document",
        "workflow_type": None,
        "effectiveness": None,
        "visibility": "global",
        "expanded": False,
    }


def _knowledge_resource_payload(
    *,
    resource_ref: str,
    text: str,
) -> dict[str, Any]:
    return {
        "schema_version": "trustguard-knowledge-resource-v1",
        "scope": "compliance",
        "content_revision": "4",
        "resource_ref": resource_ref,
        "source_revision": 1,
        "content_hash": f"sha256:{'a' * 64}",
        "document_id": "doc-1",
        "experience_id": None,
        "text": text,
        "title": "安全资料",
        "filename": "doc-1.pdf",
        "page_no": 1,
        "source_uri": "upload://doc-1.pdf",
        "source_type": "document",
        "workflow_type": None,
        "effectiveness": None,
        "visibility": "global",
        "metadata": {},
    }


@pytest.mark.asyncio
async def test_gateway_delegates_scope_search_and_validates_contract() -> None:
    backend = _FakeBackend()
    revision = aggregate_revision({"kb-a": 2, "kb-b": 7})
    backend.scope_search = _knowledge_search_payload(
        request_id="req-delegated",
        revision=revision,
        hits=[_knowledge_hit("shared", revision=revision, text="shared")],
    )
    gateway = KnowledgeGateway(backend=backend)

    response = await gateway.search(
        KnowledgeSearchRequest(
            schema_version="trustguard-knowledge-search-request-v1",
            query="password=hunter2 网络安全法要求",
            scope="compliance",
            mode="comprehensive",
            limit=3,
            filters={"content_types": ["legal_article"]},
        ),
        request_id="req-delegated",
    )

    assert response.status == "ok"
    assert response.content_revision == revision
    assert [item.resource_ref for item in response.hits] == ["krf1.shared-opaque"]
    assert backend.seen_scope_payloads == [
        {
            "schema_version": "trustguard-knowledge-search-request-v1",
            "query": "password=hunter2 网络安全法要求",
            "scope": "compliance",
            "mode": "comprehensive",
            "limit": 3,
            "rewrite": False,
            "filters": {
                "content_types": ["legal_article"],
                "source_types": [],
            },
        }
    ]
    _validate_contract(
        "knowledge_search_response.schema.json",
        response.model_dump(mode="json"),
    )


@pytest.mark.asyncio
async def test_gateway_rejects_invalid_backend_scope_contract() -> None:
    backend = _FakeBackend()
    backend.scope_search = {"schema_version": "unexpected"}
    gateway = KnowledgeGateway(backend=backend)

    with pytest.raises(KnowledgeGatewayError) as captured:
        await gateway.search(
            KnowledgeSearchRequest(
                schema_version="trustguard-knowledge-search-request-v1",
                query="要求",
                scope="compliance",
            ),
            request_id="req-schema",
        )

    assert captured.value.code == "SCHEMA_MISMATCH"


@pytest.mark.asyncio
async def test_official_mcp_client_lists_and_calls_read_only_contract() -> None:
    backend = _FakeBackend()
    revision = aggregate_revision({"kb-a": 4})
    backend.scope_search = _knowledge_search_payload(
        request_id="req-mcp-client",
        revision=revision,
        hits=[_knowledge_hit("chunk-1", revision=revision, text="检索片段")],
    )
    backend.resources["krf1.chunk-1-opaque"] = _knowledge_resource_payload(
        resource_ref="krf1.chunk-1-opaque",
        text="完整检索片段",
    )
    settings = Settings(
        _env_file=None,
        mcp_enabled=True,
        mcp_scope_mapping_json=('{"compliance":{"knowledge_base_ids":["kb-a"]}}'),
        mcp_allowed_hosts="test",
    )
    app = create_mcp_app(settings, backend=backend)

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as http_client:
            async with streamable_http_client(
                "http://test/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()
                    assert [tool.name for tool in tools.tools] == ["knowledge_search"]
                    annotations = tools.tools[0].annotations
                    assert annotations is not None
                    assert annotations.readOnlyHint is True
                    assert annotations.destructiveHint is False
                    templates = await session.list_resource_templates()
                    assert len(templates.resourceTemplates) == 1

                    called = await session.call_tool(
                        "knowledge_search",
                        {
                            "schema_version": "trustguard-knowledge-search-request-v1",
                            "query": "有哪些要求",
                            "scope": "compliance",
                            "limit": 1,
                        },
                    )
                    assert called.isError is False
                    assert called.structuredContent is not None
                    assert called.structuredContent["schema_version"] == (
                        "trustguard-knowledge-search-v1"
                    )
                    assert len(called.content) == 1
                    uri = called.structuredContent["hits"][0]["resource_uri"]

                    resource = await session.read_resource(uri)
                    payload = json.loads(resource.contents[0].text)
                    assert payload["schema_version"] == ("trustguard-knowledge-resource-v1")
                    assert payload["text"] == "完整检索片段"

            live = await http_client.get("/health/live")
            ready = await http_client.get("/health/ready")
            metrics = await http_client.get("/metrics")
            assert live.status_code == 200
            assert ready.status_code == 200
            assert "trustguard_rag_mcp_requests_total" in metrics.text

    assert backend.closed is True


@pytest.mark.asyncio
async def test_default_mcp_hosts_allow_agent_compose_bridge() -> None:
    settings = Settings(
        _env_file=None,
        mcp_enabled=True,
        mcp_scope_mapping_json=('{"compliance":{"knowledge_base_ids":["kb-a"]}}'),
    )
    app = create_mcp_app(settings, backend=_FakeBackend())

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://host.docker.internal:18201",
        ) as http_client:
            async with streamable_http_client(
                "http://host.docker.internal:18201/mcp",
                http_client=http_client,
            ) as (read_stream, write_stream, _):
                async with ClientSession(read_stream, write_stream) as session:
                    await session.initialize()
                    tools = await session.list_tools()

    assert [tool.name for tool in tools.tools] == ["knowledge_search"]


@pytest.mark.asyncio
async def test_jwt_verifier_and_claim_based_knowledge_scope_authorization(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    private_key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
    token = jwt.encode(
        {
            "iss": "https://auth.test",
            "sub": "trustguard-agent",
            "aud": "trustguard-rag-mcp",
            "scope": "rag.search rag.resource.read",
            "knowledge_scopes": ["compliance"],
            "workspace_id": "default",
            "workflow_types": ["penetration"],
            "exp": int(time.time()) + 60,
        },
        private_key,
        algorithm="RS256",
        headers={"kid": "test-key"},
    )
    verifier = JwtTokenVerifier(
        issuer="https://auth.test",
        audience="trustguard-rag-mcp",
        jwks_url="https://auth.test/.well-known/jwks.json",
        algorithms=["RS256"],
    )
    monkeypatch.setattr(
        verifier._jwks,
        "get_signing_key_from_jwt",
        lambda _token: SimpleNamespace(key=private_key.public_key()),
    )

    access_token = await verifier.verify_token(token)
    assert access_token is not None
    assert access_token.client_id == "trustguard-agent"

    context_token = auth_context_var.set(AuthenticatedUser(access_token))
    try:
        authorization = authorize_knowledge_scope(
            "compliance",
            required_permission="rag.search",
            auth_enabled=True,
        )
        assert authorization.workspace_id == "default"
        assert authorization.allowed_workflow_types == frozenset({"penetration"})
        with pytest.raises(ScopeAuthorizationError):
            authorize_knowledge_scope(
                "penetration",
                required_permission="rag.search",
                auth_enabled=True,
            )
    finally:
        auth_context_var.reset(context_token)

    search_only_token = AccessToken(
        token="search-only",
        client_id="trustguard-agent",
        subject="trustguard-agent",
        scopes=["rag.search"],
        expires_at=int(time.time()) + 60,
        claims={
            "knowledge_scopes": ["compliance"],
            "workspace_id": "default",
            "workflow_types": ["penetration"],
        },
    )
    context_token = auth_context_var.set(AuthenticatedUser(search_only_token))
    try:
        authorize_knowledge_scope(
            "compliance",
            required_permission="rag.search",
            auth_enabled=True,
        )
        with pytest.raises(ScopeAuthorizationError):
            authorize_knowledge_scope(
                "compliance",
                required_permission="rag.resource.read",
                auth_enabled=True,
            )
    finally:
        auth_context_var.reset(context_token)


@pytest.mark.asyncio
async def test_mcp_http_boundary_authenticates_without_aggregating_operation_scopes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verify_token(_self, token: str) -> AccessToken | None:
        if token == "search-only":
            return AccessToken(
                token=token,
                client_id="agent",
                subject="agent",
                scopes=["rag.search"],
                expires_at=int(time.time()) + 60,
                claims={"knowledge_scopes": ["compliance"]},
            )
        return None

    monkeypatch.setattr(JwtTokenVerifier, "verify_token", verify_token)
    settings = Settings(
        _env_file=None,
        mcp_enabled=True,
        mcp_auth_enabled=True,
        mcp_auth_issuer="https://auth.test",
        mcp_auth_jwks_url="https://auth.test/.well-known/jwks.json",
        mcp_resource_server_url="http://test/mcp",
        mcp_scope_mapping_json=('{"compliance":{"knowledge_base_ids":["kb-a"]}}'),
        mcp_allowed_hosts="test",
    )
    app = create_mcp_app(settings, backend=_FakeBackend())
    initialize = {
        "jsonrpc": "2.0",
        "id": 1,
        "method": "initialize",
        "params": {
            "protocolVersion": "2025-11-25",
            "capabilities": {},
            "clientInfo": {"name": "test", "version": "1"},
        },
    }

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(
            transport=httpx.ASGITransport(app=app),
            base_url="http://test",
        ) as client:
            missing = await client.post("/mcp", json=initialize)
            search_only = await client.post(
                "/mcp",
                json=initialize,
                headers={
                    "Authorization": "Bearer search-only",
                    "Accept": "application/json, text/event-stream",
                },
            )

    assert missing.status_code == 401
    assert search_only.status_code == 200


def test_scope_mapping_rejects_unknown_alias_and_empty_mapping() -> None:
    with pytest.raises(ValueError, match="Unsupported MCP knowledge scope"):
        ScopeRegistry.from_json('{"arbitrary":{"knowledge_base_ids":["kb-a"]}}')
    with pytest.raises(ValueError, match="knowledge_base_ids"):
        ScopeRegistry.from_json('{"compliance":{"knowledge_base_ids":[]}}')
    with pytest.raises(ValueError, match="valid dictionary"):
        ScopeRegistry.from_json('{"compliance":["kb-a"]}')


def test_mcp_auth_configuration_requires_issuer_and_jwks() -> None:
    with pytest.raises(ValueError, match="RAG_MCP_AUTH_ISSUER"):
        Settings(_env_file=None, mcp_auth_enabled=True)


def _validate_contract(schema_name: str, payload: dict[str, Any]) -> None:
    schema = json.loads((_CONTRACT_SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)
