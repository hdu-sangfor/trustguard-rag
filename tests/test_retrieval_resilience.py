from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.indexing.opensearch_backfill import backfill_ready_documents
from app.core.retrieval.keyword_retriever import KeywordRetriever
from app.core.retrieval.reranker import RerankError
from app.core.retrieval.search import HybridSearch, SearchUnavailableError
from app.domain import EffectiveSearchMode, SearchStatus
from app.settings import Settings
from app.core.retrieval.vector_retriever import VectorRetriever
from app.settings import get_settings
from app.stores import opensearch_store, qdrant_store


@pytest.fixture
def search_settings(monkeypatch):
    monkeypatch.setenv("RAG_QDRANT_MOCK", "true")
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "true")
    monkeypatch.setenv("RAG_RERANK_PROVIDER", "none")
    get_settings.cache_clear()
    yield get_settings()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_vector_retriever_uses_current_qdrant_query_api(monkeypatch) -> None:
    embedder = SimpleNamespace(embed_query=AsyncMock(return_value=[1.0, 0.0]))
    point = SimpleNamespace(
        id="chunk-1",
        score=0.9,
        payload={
            "chunk_text": "内容",
            "document_id": "doc-1",
            "chunk_index": 0,
            "metadata": {"category": "security"},
        },
    )
    client = SimpleNamespace(
        query_points=AsyncMock(return_value=SimpleNamespace(points=[point]))
    )
    monkeypatch.setenv("RAG_QDRANT_MOCK", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)

    results = await VectorRetriever(embedder).retrieve("查询", top_k=5)

    assert results[0]["chunk_id"] == "chunk-1"
    assert results[0]["text"] == "内容"
    client.query_points.assert_awaited_once_with(
        collection_name="rag_chunks",
        query=[1.0, 0.0],
        limit=5,
        with_payload=True,
        query_filter=None,
    )
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_vector_retriever_hydrates_text_missing_from_old_payload(monkeypatch) -> None:
    embedder = SimpleNamespace(embed_query=AsyncMock(return_value=[1.0, 0.0]))
    point = SimpleNamespace(
        id="chunk-1",
        score=0.9,
        payload={"document_id": "doc-1", "chunk_index": 0},
    )
    client = SimpleNamespace(
        query_points=AsyncMock(return_value=SimpleNamespace(points=[point]))
    )
    chunk_store = SimpleNamespace(
        get_many=AsyncMock(return_value=[SimpleNamespace(id="chunk-1", text="历史内容")])
    )
    monkeypatch.setenv("RAG_QDRANT_MOCK", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(qdrant_store, "get_client", lambda: client)
    monkeypatch.setattr(
        "app.core.retrieval.vector_retriever.get_chunk_store", lambda: chunk_store
    )

    results = await VectorRetriever(embedder).retrieve("查询", top_k=5)

    assert results[0]["text"] == "历史内容"
    chunk_store.get_many.assert_awaited_once_with(["chunk-1"])
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_keyword_retriever_recreates_and_backfills_missing_index(monkeypatch) -> None:
    indices = SimpleNamespace(
        exists=AsyncMock(return_value=False),
        create=AsyncMock(return_value={"acknowledged": True}),
    )
    client = SimpleNamespace(
        indices=indices,
        search=AsyncMock(return_value={"hits": {"hits": []}}),
    )
    backfill = AsyncMock()
    monkeypatch.setenv("RAG_SEARCH_OPENSEARCH_MOCK", "false")
    get_settings.cache_clear()
    monkeypatch.setattr(opensearch_store, "get_client", lambda: client)
    monkeypatch.setattr(
        "app.core.indexing.opensearch_backfill.backfill_ready_documents", backfill
    )

    retriever = KeywordRetriever()
    assert await retriever.retrieve("查询", top_k=5) == []

    indices.create.assert_awaited_once()
    backfill.assert_awaited_once_with(retriever=retriever, ensure_index=False)
    client.search.assert_awaited_once()
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_backfill_indexes_all_active_chunks_idempotently() -> None:
    document = SimpleNamespace(
        id="doc-1",
        source_uri="upload://source",
        original_filename="report.pdf",
    )
    rows = [
        SimpleNamespace(
            id="chunk-1",
            document_id="doc-1",
            chunk_index=0,
            text="有效内容",
            page_no=1,
            metadata_json={"tag": "security"},
            status="active",
        ),
        SimpleNamespace(
            id="chunk-deleted",
            document_id="doc-1",
            chunk_index=1,
            text="已删除内容",
            page_no=2,
            metadata_json=None,
            status="deleted",
        ),
    ]
    document_store = SimpleNamespace(list_by_status=AsyncMock(return_value=[document]))
    chunk_store = SimpleNamespace(list_for_document=AsyncMock(return_value=rows))

    class _Retriever:
        def __init__(self) -> None:
            self.ensure_index = AsyncMock(return_value=False)
            self.batches = []

        async def index_chunks_bulk(self, chunks):
            self.batches.append(chunks)

    retriever = _Retriever()

    result = await backfill_ready_documents(
        retriever=retriever,
        document_store=document_store,
        chunk_store=chunk_store,
    )

    assert result.documents == 1
    assert result.chunks == 1
    assert retriever.batches[0][0]["chunk_id"] == "chunk-1"
    assert retriever.batches[0][0]["body"]["source_uri"] == "upload://source"


@pytest.mark.asyncio
async def test_hybrid_search_fails_when_all_enabled_backends_fail(search_settings) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(retrieve=AsyncMock(side_effect=RuntimeError("vector")))
    engine._keyword = SimpleNamespace(retrieve=AsyncMock(side_effect=RuntimeError("keyword")))

    with pytest.raises(SearchUnavailableError):
        await engine.search("查询", enable_rerank=False)


@pytest.mark.asyncio
async def test_hybrid_search_reports_partial_degradation(search_settings) -> None:
    engine = HybridSearch()
    engine._documents = SimpleNamespace(ready_ids=AsyncMock(return_value={"doc-1"}))
    engine._vector = SimpleNamespace(retrieve=AsyncMock(side_effect=RuntimeError("vector")))
    engine._keyword = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "chunk-1",
                    "text": "内容",
                    "score": 1.0,
                    "document_id": "doc-1",
                }
            ]
        )
    )

    result = await engine.search("查询", enable_rerank=False)

    assert result["total"] == 1
    assert result["degraded_components"] == ["vector"]
    assert result["search_status"] is SearchStatus.DEGRADED
    assert result["effective_mode"] is EffectiveSearchMode.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_hybrid_search_fails_when_partial_degradation_has_no_result(
    search_settings,
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(side_effect=RuntimeError("vector"))
    )
    engine._keyword = SimpleNamespace(retrieve=AsyncMock(return_value=[]))

    with pytest.raises(SearchUnavailableError, match="no reliable result"):
        await engine.search("查询", enable_rerank=False)


@pytest.mark.asyncio
async def test_hybrid_search_returns_true_empty_when_backends_are_healthy(
    search_settings,
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    engine._keyword = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    engine._documents = SimpleNamespace(ready_ids=AsyncMock(return_value=set()))

    result = await engine.search("查询", enable_rerank=False)

    assert result["total"] == 0
    assert result["search_status"] is SearchStatus.OK
    assert result["effective_mode"] is EffectiveSearchMode.HYBRID
    assert result["degraded_components"] == []


@pytest.mark.asyncio
async def test_hybrid_search_reports_rerank_degradation(search_settings) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "chunk-1",
                    "text": "召回内容",
                    "score": 0.9,
                    "document_id": "doc-1",
                }
            ]
        )
    )
    engine._documents = SimpleNamespace(ready_ids=AsyncMock(return_value={"doc-1"}))
    engine._reranker = SimpleNamespace(
        rerank=AsyncMock(side_effect=RerankError("reranker unavailable"))
    )

    result = await engine.search("查询", enable_keyword=False, enable_rerank=True)

    assert result["total"] == 1
    assert result["results"][0]["chunk_id"] == "chunk-1"
    assert result["search_status"] is SearchStatus.DEGRADED
    assert result["effective_mode"] is EffectiveSearchMode.VECTOR_ONLY
    assert result["degraded_components"] == ["rerank"]


@pytest.mark.asyncio
async def test_hybrid_search_filters_non_ready_documents(search_settings) -> None:
    engine = HybridSearch()
    engine._documents = SimpleNamespace(ready_ids=AsyncMock(return_value={"ready-doc"}))
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "ready-chunk",
                    "text": "可检索",
                    "score": 0.9,
                    "document_id": "ready-doc",
                },
                {
                    "chunk_id": "deleting-chunk",
                    "text": "不应返回",
                    "score": 0.8,
                    "document_id": "deleting-doc",
                },
            ]
        )
    )

    result = await engine.search(
        "查询",
        enable_keyword=False,
        enable_rerank=False,
    )

    assert [item["source"]["document_id"] for item in result["results"]] == ["ready-doc"]


def test_production_rejects_mock_retrieval_backends() -> None:
    with pytest.raises(ValidationError, match="Production cannot enable mock"):
        Settings(
            _env_file=None,
            app_env="prod",
            qdrant_mock=True,
            search_opensearch_mock=False,
        )

    with pytest.raises(ValidationError, match="Production cannot enable mock"):
        Settings(
            _env_file=None,
            app_env="prod",
            qdrant_mock=False,
            search_opensearch_mock=True,
        )


@pytest.mark.asyncio
async def test_search_api_returns_503_when_no_reliable_result(
    client, monkeypatch: pytest.MonkeyPatch
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(side_effect=RuntimeError("vector"))
    )
    engine._keyword = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    monkeypatch.setattr("app.api.search.get_hybrid_search", lambda: engine)

    response = await client.post(
        "/v1/search",
        json={"query": "查询", "enable_rerank": False},
    )

    assert response.status_code == 503
    assert "no reliable result" in response.json()["detail"]
