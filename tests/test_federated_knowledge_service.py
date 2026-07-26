"""应用层 Scope 联邦检索、RRF、过滤、脱敏和降级语义。"""

from __future__ import annotations

import hashlib
from typing import Any

import pytest

from app.application.access import (
    KnowledgeAccessContext,
    KnowledgeCallerType,
    KnowledgePermission,
    mcp_access_context,
)
from app.application.knowledge import (
    KnowledgeApplicationService,
    KnowledgeSearchError,
    ResolvedKnowledgeSource,
    aggregate_revision,
)
from app.application.scopes import ScopeRegistry
from app.schemas.knowledge import KnowledgeSearchRequest, KnowledgeSourceType
from app.schemas.search import SearchRequest, SearchResponse


class _FederatedKnowledgeService(KnowledgeApplicationService):
    def __init__(self) -> None:
        self.responses: dict[str, SearchResponse | BaseException] = {}
        self.revisions: dict[str, int | BaseException] = {}
        self.seen_requests: list[SearchRequest] = []
        self.source_revisions: dict[tuple[str, str], int] = {}
        self.content_hashes: dict[tuple[str, str], str] = {}
        self.source_types: dict[tuple[str, str], KnowledgeSourceType] = {}
        self.resolved_identity_batches: list[list[tuple[str, str]]] = []

    async def search(
        self,
        request: SearchRequest,
        *,
        request_id: str,
        access_context: KnowledgeAccessContext,
    ) -> SearchResponse:
        self.seen_requests.append(request)
        value = self.responses[request.knowledge_base_id]
        if isinstance(value, BaseException):
            raise value
        return value.model_copy(
            update={
                "request_id": request_id,
                "query": request.query,
                "knowledge_base_id": request.knowledge_base_id,
            }
        )

    async def _content_revision(self, knowledge_base_id: str) -> int:
        value = self.revisions[knowledge_base_id]
        if isinstance(value, BaseException):
            raise value
        return value

    async def _resolve_resource_sources(
        self,
        identities: list[tuple[str, str]],
    ) -> dict[tuple[str, str], ResolvedKnowledgeSource]:
        self.resolved_identity_batches.append(identities)
        requested = set(identities)
        resolved: dict[tuple[str, str], ResolvedKnowledgeSource] = {}
        for knowledge_base_id, value in self.responses.items():
            if not isinstance(value, SearchResponse):
                continue
            for item in value.results:
                identity = (knowledge_base_id, item.chunk_id)
                if identity not in requested:
                    continue
                metadata = item.metadata or {}
                resolved[identity] = ResolvedKnowledgeSource(
                    knowledge_base_id=knowledge_base_id,
                    content_revision=value.content_revision,
                    chunk_id=item.chunk_id,
                    chunk_index=item.source.chunk_index,
                    document_id=item.source.document_id,
                    source_revision=self.source_revisions.get(identity, 1),
                    content_hash=self.content_hashes.get(
                        identity,
                        hashlib.sha256(item.chunk_id.encode("utf-8")).hexdigest(),
                    ),
                    text=item.text,
                    title=item.title,
                    filename=item.source.original_filename,
                    page_no=item.source.page_no,
                    source_uri=item.source.source_uri,
                    source_type=self.source_types.get(
                        identity,
                        KnowledgeSourceType.DOCUMENT,
                    ),
                    metadata=metadata,
                )
        return resolved


def _result(
    chunk_id: str,
    *,
    document_id: str,
    text: str,
    content_type: str = "legal_article",
    metadata: dict[str, Any] | None = None,
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
        "metadata": {"content_type": content_type, **(metadata or {})},
        "expanded": False,
    }


def _search_response(
    knowledge_base_id: str,
    *,
    revision: int,
    results: list[dict[str, Any]],
    degraded: list[str] | None = None,
    coverage_status: str = "not_applicable",
) -> SearchResponse:
    return SearchResponse.model_validate(
        {
            "schema_version": "trustguard-search-v1",
            "request_id": "rest-request",
            "query": "placeholder",
            "knowledge_base_id": knowledge_base_id,
            "content_revision": revision,
            "search_status": "degraded" if degraded else "ok",
            "effective_mode": "keyword_only",
            "results": results,
            "total": len(results),
            "fusion_method": "rrf",
            "retrieval_time_ms": 1.0,
            "components": {"vector": 0, "keyword": len(results)},
            "degraded_components": degraded or [],
            "query_plan": {"intent": "comprehensive", "source": "rule"},
            "coverage": {"status": coverage_status, "warning": None},
        }
    )


def _scope_registry(*, allowed_content_types: bool = True) -> ScopeRegistry:
    allowed = ',"allowed_content_types":["legal_article"]' if allowed_content_types else ""
    return ScopeRegistry.from_json(
        f'{{"compliance":{{"knowledge_base_ids":["kb-a","kb-b"]{allowed}}}}}'
    )


def _request(**overrides: Any) -> KnowledgeSearchRequest:
    payload: dict[str, Any] = {
        "schema_version": "trustguard-knowledge-search-request-v1",
        "query": "网络安全法要求",
        "scope": "compliance",
        "mode": "comprehensive",
        "limit": 3,
    }
    payload.update(overrides)
    return KnowledgeSearchRequest.model_validate(payload)


@pytest.mark.asyncio
async def test_scope_search_fuses_multiple_knowledge_bases_and_redacts_query() -> None:
    service = _FederatedKnowledgeService()
    service.responses = {
        "kb-a": _search_response(
            "kb-a",
            revision=2,
            results=[
                _result("shared", document_id="doc-a", text="A shared"),
                _result("a-only", document_id="doc-a", text="A only"),
            ],
        ),
        "kb-b": _search_response(
            "kb-b",
            revision=7,
            results=[
                _result("b-only", document_id="doc-b", text="B only"),
                _result("shared", document_id="doc-b", text="B shared"),
            ],
        ),
    }

    response = await service.search_scope(
        _request(
            query="password=hunter2 Bearer abcdefghijklmnop 网络安全法要求",
            filters={"content_types": ["legal_article"]},
        ),
        request_id="req-federated",
        access_context=mcp_access_context(service_id="mcp", workspace_id="default"),
        scopes=_scope_registry(),
    )

    assert response.status == "ok"
    assert response.content_revision == aggregate_revision({"kb-a": 2, "kb-b": 7})
    assert [item.snippet for item in response.hits] == [
        "A shared",
        "B only",
        "A only",
    ]
    assert "/resources/krf1." in response.hits[0].resource_uri
    assert all(item.resource_ref for item in response.hits)
    assert all(item.source_revision == 1 for item in response.hits)
    assert all(item.content_hash.startswith("sha256:") for item in response.hits)
    assert all("hunter2" not in item.query for item in service.seen_requests)
    assert all("abcdefghijklmnop" not in item.query for item in service.seen_requests)
    assert response.query_plan.source == "heuristic"


@pytest.mark.asyncio
async def test_scope_search_returns_degraded_result_when_one_database_fails() -> None:
    service = _FederatedKnowledgeService()
    service.responses = {
        "kb-a": _search_response(
            "kb-a",
            revision=2,
            results=[_result("a-only", document_id="doc-a", text="A only")],
        ),
        "kb-b": RuntimeError("offline"),
    }
    service.revisions = {"kb-b": 7}

    response = await service.search_scope(
        _request(),
        request_id="req-degraded",
        access_context=mcp_access_context(service_id="mcp", workspace_id="default"),
        scopes=_scope_registry(allowed_content_types=False),
    )

    assert response.status == "degraded"
    assert response.degraded_components == ["federation"]
    assert response.coverage.status == "unknown"
    assert response.content_revision == aggregate_revision({"kb-a": 2, "kb-b": 7})
    assert [item.snippet for item in response.hits] == ["A only"]


@pytest.mark.asyncio
async def test_scope_search_rejects_disallowed_filter_before_search() -> None:
    service = _FederatedKnowledgeService()

    with pytest.raises(KnowledgeSearchError) as captured:
        await service.search_scope(
            _request(filters={"content_types": ["private_note"]}),
            request_id="req-filter",
            access_context=mcp_access_context(
                service_id="mcp",
                workspace_id="default",
            ),
            scopes=_scope_registry(),
        )

    assert captured.value.status_code == 400
    assert captured.value.code == "INVALID_ARGUMENT"
    assert service.seen_requests == []


@pytest.mark.asyncio
async def test_scope_search_fails_when_all_knowledge_bases_are_unavailable() -> None:
    service = _FederatedKnowledgeService()
    service.responses = {
        "kb-a": RuntimeError("offline-a"),
        "kb-b": RuntimeError("offline-b"),
    }

    with pytest.raises(KnowledgeSearchError) as captured:
        await service.search_scope(
            _request(),
            request_id="req-unavailable",
            access_context=mcp_access_context(
                service_id="mcp",
                workspace_id="default",
            ),
            scopes=_scope_registry(allowed_content_types=False),
        )

    assert captured.value.status_code == 503
    assert captured.value.code == "RAG_UNAVAILABLE"
    assert captured.value.retryable is True


@pytest.mark.asyncio
async def test_scope_search_checks_every_mapped_knowledge_base_permission() -> None:
    service = _FederatedKnowledgeService()
    restricted_context = KnowledgeAccessContext(
        caller_type=KnowledgeCallerType.MCP,
        service_id="restricted-mcp",
        workspace_id="default",
        permissions=frozenset({KnowledgePermission.SEARCH}),
        allowed_knowledge_base_ids=frozenset({"kb-a"}),
    )

    with pytest.raises(KnowledgeSearchError) as captured:
        await service.search_scope(
            _request(),
            request_id="req-forbidden",
            access_context=restricted_context,
            scopes=_scope_registry(allowed_content_types=False),
        )

    assert captured.value.status_code == 403
    assert captured.value.code == "AUTH_FORBIDDEN"
    assert service.seen_requests == []


@pytest.mark.asyncio
async def test_resource_ref_reads_one_source_and_ignores_unrelated_revision() -> None:
    service = _FederatedKnowledgeService()
    service.responses = {
        "kb-a": _search_response(
            "kb-a",
            revision=2,
            results=[_result("a-only", document_id="doc-a", text="A only")],
        ),
        "kb-b": _search_response(
            "kb-b",
            revision=7,
            results=[_result("b-only", document_id="doc-b", text="B only")],
        ),
    }
    scopes = _scope_registry(allowed_content_types=False)
    context = mcp_access_context(service_id="mcp", workspace_id="default")
    response = await service.search_scope(
        _request(limit=2),
        request_id="req-resource-ref",
        access_context=context,
        scopes=scopes,
    )
    hit = next(item for item in response.hits if item.snippet == "A only")

    service.responses["kb-b"] = service.responses["kb-b"].model_copy(
        update={"content_revision": 99}
    )
    service.resolved_identity_batches.clear()
    resource = await service.read_resource(
        scope="compliance",
        resource_ref=hit.resource_ref,
        request_id="req-resource-read",
        access_context=context,
        scopes=scopes,
    )

    assert resource.text == "A only"
    assert resource.source_revision == 1
    assert resource.content_hash == hit.content_hash
    assert service.resolved_identity_batches == [[("kb-a", "a-only")]]


@pytest.mark.asyncio
async def test_resource_ref_becomes_stale_only_when_bound_source_changes() -> None:
    service = _FederatedKnowledgeService()
    service.responses = {
        "kb-a": _search_response(
            "kb-a",
            revision=2,
            results=[_result("a-only", document_id="doc-a", text="A only")],
        ),
        "kb-b": _search_response("kb-b", revision=7, results=[]),
    }
    scopes = _scope_registry(allowed_content_types=False)
    context = mcp_access_context(service_id="mcp", workspace_id="default")
    response = await service.search_scope(
        _request(limit=1),
        request_id="req-stale-source",
        access_context=context,
        scopes=scopes,
    )
    resource_ref = response.hits[0].resource_ref
    service.source_revisions[("kb-a", "a-only")] = 2

    with pytest.raises(KnowledgeSearchError) as captured:
        await service.read_resource(
            scope="compliance",
            resource_ref=resource_ref,
            request_id="req-stale-read",
            access_context=context,
            scopes=scopes,
        )

    assert captured.value.code == "RESOURCE_STALE"
    assert captured.value.status_code == 409


@pytest.mark.asyncio
async def test_federation_uses_physical_identity_for_same_chunk_id() -> None:
    service = _FederatedKnowledgeService()
    service.responses = {
        "kb-a": _search_response(
            "kb-a",
            revision=2,
            results=[_result("collision", document_id="doc-a", text="A collision")],
        ),
        "kb-b": _search_response(
            "kb-b",
            revision=7,
            results=[_result("collision", document_id="doc-b", text="B collision")],
        ),
    }
    service.content_hashes = {
        ("kb-a", "collision"): "a" * 64,
        ("kb-b", "collision"): "b" * 64,
    }

    response = await service.search_scope(
        _request(limit=2),
        request_id="req-collision",
        access_context=mcp_access_context(
            service_id="mcp",
            workspace_id="default",
        ),
        scopes=_scope_registry(allowed_content_types=False),
    )

    assert [item.snippet for item in response.hits] == ["A collision", "B collision"]
    assert response.hits[0].resource_ref != response.hits[1].resource_ref
    assert response.hits[0].content_hash != response.hits[1].content_hash


@pytest.mark.asyncio
async def test_scope_search_enforces_workspace_and_workflow_visibility() -> None:
    service = _FederatedKnowledgeService()
    service.responses = {
        "kb-a": _search_response(
            "kb-a",
            revision=2,
            results=[
                _result(
                    "workspace-experience",
                    document_id="doc-a",
                    text="Workspace experience",
                    metadata={
                        "visibility": "workspace",
                        "workspace_id": "default",
                        "workflow_type": "penetration",
                    },
                )
            ],
        ),
        "kb-b": _search_response(
            "kb-b",
            revision=7,
            results=[
                _result(
                    "other-workspace",
                    document_id="doc-b",
                    text="Other workspace",
                    metadata={
                        "visibility": "workspace",
                        "workspace_id": "other",
                    },
                )
            ],
        ),
    }
    service.source_types[("kb-a", "workspace-experience")] = KnowledgeSourceType.EXPERIENCE
    scopes = ScopeRegistry.from_json(
        '{"compliance":{"knowledge_base_ids":["kb-a","kb-b"],'
        '"allowed_workflow_types":["penetration"]}}'
    )

    allowed = await service.search_scope(
        _request(limit=2),
        request_id="req-workspace-allowed",
        access_context=mcp_access_context(
            service_id="mcp",
            workspace_id="default",
            allowed_workflow_types=frozenset({"penetration"}),
        ),
        scopes=scopes,
    )
    denied = await service.search_scope(
        _request(limit=2),
        request_id="req-workspace-denied",
        access_context=mcp_access_context(
            service_id="mcp",
            workspace_id="default",
            allowed_workflow_types=frozenset({"alert-triage"}),
        ),
        scopes=scopes,
    )

    assert [item.snippet for item in allowed.hits] == ["Workspace experience"]
    assert denied.hits == []
