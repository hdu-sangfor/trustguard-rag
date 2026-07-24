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
    aggregate_revision,
)
from app.mcp_server.models import KnowledgeSearchRequest
from app.mcp_server.scopes import ScopeRegistry
from app.mcp_server.server import create_mcp_app
from app.settings import Settings

_CONTRACT_SCHEMAS = Path(__file__).parents[1] / "contracts" / "v1" / "schemas"


class _FakeBackend:
    def __init__(self) -> None:
        self.searches: dict[str, dict[str, Any] | BaseException] = {}
        self.revisions: dict[str, int | BaseException] = {}
        self.chunks: dict[tuple[str, str], dict[str, Any] | None | BaseException] = {}
        self.seen_payloads: list[dict[str, Any]] = []
        self.is_ready = True
        self.closed = False

    async def search(
        self,
        *,
        knowledge_base_id: str,
        request_id: str,
        payload: dict[str, Any],
    ) -> dict[str, Any]:
        self.seen_payloads.append(payload)
        value = self.searches[knowledge_base_id]
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_chunk(
        self,
        *,
        knowledge_base_id: str,
        chunk_id: str,
        request_id: str,
    ) -> dict[str, Any] | None:
        value = self.chunks.get((knowledge_base_id, chunk_id))
        if isinstance(value, BaseException):
            raise value
        return value

    async def get_content_revision(self, knowledge_base_id: str) -> int:
        value = self.revisions[knowledge_base_id]
        if isinstance(value, BaseException):
            raise value
        return value

    async def ready(self) -> bool:
        return self.is_ready

    async def aclose(self) -> None:
        self.closed = True


def _search_payload(
    *,
    revision: int,
    results: list[dict[str, Any]],
    status: str = "ok",
    degraded: list[str] | None = None,
) -> dict[str, Any]:
    return {
        "schema_version": "trustguard-search-v1",
        "request_id": "rest-request",
        "content_revision": revision,
        "search_status": status,
        "results": results,
        "query_plan": {"intent": "comprehensive", "source": "rule"},
        "coverage": {"status": "not_applicable", "warning": None},
        "degraded_components": degraded or [],
    }


def _result(
    chunk_id: str,
    *,
    document_id: str,
    text: str,
    content_type: str = "legal_article",
) -> dict[str, Any]:
    return {
        "chunk_id": chunk_id,
        "text": text,
        "score": 0.8,
        "title": "安全资料",
        "source": {
            "document_id": document_id,
            "source_uri": f"upload://{document_id}.pdf",
            "original_filename": f"{document_id}.pdf",
            "chunk_index": 0,
            "page_no": 1,
        },
        "metadata": {"content_type": content_type},
        "expanded": False,
    }


@pytest.mark.asyncio
async def test_gateway_federates_scopes_with_rrf_and_stable_revision() -> None:
    backend = _FakeBackend()
    backend.searches = {
        "kb-a": _search_payload(
            revision=2,
            results=[
                _result("shared", document_id="doc-a", text="A shared"),
                _result("a-only", document_id="doc-a", text="A only"),
            ],
        ),
        "kb-b": _search_payload(
            revision=7,
            results=[
                _result("b-only", document_id="doc-b", text="B only"),
                _result("shared", document_id="doc-b", text="B shared"),
            ],
        ),
    }
    gateway = KnowledgeGateway(
        backend=backend,
        scopes=ScopeRegistry.from_json(
            json.dumps(
                {
                    "compliance": {
                        "knowledge_base_ids": ["kb-a", "kb-b"],
                        "allowed_content_types": ["legal_article"],
                    }
                }
            )
        ),
    )

    response = await gateway.search(
        KnowledgeSearchRequest(
            schema_version="trustguard-knowledge-search-request-v1",
            query="password=hunter2 网络安全法要求",
            scope="compliance",
            mode="comprehensive",
            limit=3,
            filters={"content_types": ["legal_article"]},
        ),
        request_id="req-federated",
    )

    assert response.status == "ok"
    assert response.content_revision == aggregate_revision({"kb-a": 2, "kb-b": 7})
    assert [item.external_chunk_id for item in response.hits] == [
        "shared",
        "b-only",
        "a-only",
    ]
    assert response.hits[0].resource_uri.endswith(
        f"?revision={response.content_revision}"
    )
    assert all("hunter2" not in payload["query"] for payload in backend.seen_payloads)
    assert response.query_plan.source == "heuristic"
    _validate_contract("knowledge_search_response.schema.json", response.model_dump(mode="json"))


@pytest.mark.asyncio
async def test_gateway_returns_degraded_results_when_one_knowledge_base_fails() -> None:
    backend = _FakeBackend()
    backend.searches = {
        "kb-a": _search_payload(
            revision=2,
            results=[_result("a-only", document_id="doc-a", text="A only")],
        ),
        "kb-b": RuntimeError("offline"),
    }
    backend.revisions = {"kb-b": 7}
    gateway = KnowledgeGateway(
        backend=backend,
        scopes=ScopeRegistry.from_json('{"compliance":["kb-a","kb-b"]}'),
    )

    response = await gateway.search(
        KnowledgeSearchRequest(
            schema_version="trustguard-knowledge-search-request-v1",
            query="要求",
            scope="compliance",
        ),
        request_id="req-degraded",
    )

    assert response.status == "degraded"
    assert response.degraded_components == ["federation"]
    assert response.coverage.status == "unknown"
    assert response.content_revision == aggregate_revision({"kb-a": 2, "kb-b": 7})
    assert [item.external_chunk_id for item in response.hits] == ["a-only"]


@pytest.mark.asyncio
async def test_resource_rejects_stale_revision_and_never_crosses_scope() -> None:
    backend = _FakeBackend()
    backend.revisions = {"kb-a": 3}
    backend.chunks = {
        ("kb-a", "chunk-1"): {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "text": "完整证据",
            "source_uri": "upload://evidence.pdf",
            "source_type": "upload",
            "metadata": {"article_no": "第十条"},
        }
    }
    gateway = KnowledgeGateway(
        backend=backend,
        scopes=ScopeRegistry.from_json('{"compliance":["kb-a"]}'),
    )

    with pytest.raises(KnowledgeGatewayError) as stale:
        await gateway.read_resource(
            scope="compliance",
            chunk_id="chunk-1",
            revision="old",
            request_id="req-stale",
        )
    assert stale.value.code == "RESOURCE_STALE"

    revision = aggregate_revision({"kb-a": 3})
    resource = await gateway.read_resource(
        scope="compliance",
        chunk_id="chunk-1",
        revision=revision,
        request_id="req-resource",
    )
    assert resource.text == "完整证据"
    assert resource.source_type == "document"
    assert resource.content_revision == revision
    _validate_contract("knowledge_resource.schema.json", resource.model_dump(mode="json"))

    with pytest.raises(KnowledgeGatewayError) as unknown:
        await gateway.read_resource(
            scope="penetration",
            chunk_id="chunk-1",
            revision=revision,
        )
    assert unknown.value.code == "UNKNOWN_SCOPE"


@pytest.mark.asyncio
async def test_official_mcp_client_lists_and_calls_read_only_contract() -> None:
    backend = _FakeBackend()
    backend.searches = {
        "kb-a": _search_payload(
            revision=4,
            results=[_result("chunk-1", document_id="doc-1", text="检索片段")],
        )
    }
    backend.revisions = {"kb-a": 4}
    backend.chunks = {
        ("kb-a", "chunk-1"): {
            "chunk_id": "chunk-1",
            "document_id": "doc-1",
            "text": "完整检索片段",
            "source_uri": "upload://doc-1.pdf",
            "source_type": "document",
            "metadata": {},
        }
    }
    settings = Settings(
        _env_file=None,
        mcp_enabled=True,
        mcp_scope_mapping_json='{"compliance":["kb-a"]}',
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
                    assert payload["schema_version"] == (
                        "trustguard-knowledge-resource-v1"
                    )
                    assert payload["text"] == "完整检索片段"

            live = await http_client.get("/health/live")
            ready = await http_client.get("/health/ready")
            metrics = await http_client.get("/metrics")
            assert live.status_code == 200
            assert ready.status_code == 200
            assert "trustguard_rag_mcp_requests_total" in metrics.text

    assert backend.closed is True


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
        authorize_knowledge_scope(
            "compliance",
            required_permission="rag.search",
            auth_enabled=True,
        )
        with pytest.raises(ScopeAuthorizationError):
            authorize_knowledge_scope(
                "penetration",
                required_permission="rag.search",
                auth_enabled=True,
            )
    finally:
        auth_context_var.reset(context_token)


@pytest.mark.asyncio
async def test_mcp_http_boundary_rejects_missing_or_under_scoped_tokens(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    async def verify_token(_self, token: str) -> AccessToken | None:
        if token == "under-scoped":
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
        mcp_scope_mapping_json='{"compliance":["kb-a"]}',
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
            under_scoped = await client.post(
                "/mcp",
                json=initialize,
                headers={"Authorization": "Bearer under-scoped"},
            )

    assert missing.status_code == 401
    assert under_scoped.status_code == 403


def test_scope_mapping_rejects_unknown_alias_and_empty_mapping() -> None:
    with pytest.raises(ValueError, match="Unsupported MCP knowledge scope"):
        ScopeRegistry.from_json('{"arbitrary":["kb-a"]}')
    with pytest.raises(ValueError, match="knowledge_base_ids"):
        ScopeRegistry.from_json('{"compliance":[]}')


def test_mcp_auth_configuration_requires_issuer_and_jwks() -> None:
    with pytest.raises(ValueError, match="RAG_MCP_AUTH_ISSUER"):
        Settings(_env_file=None, mcp_auth_enabled=True)


def _validate_contract(schema_name: str, payload: dict[str, Any]) -> None:
    schema = json.loads((_CONTRACT_SCHEMAS / schema_name).read_text(encoding="utf-8"))
    Draft202012Validator(schema).validate(payload)
