from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.indexing.opensearch_backfill import backfill_ready_documents
from app.core.retrieval.keyword_retriever import KeywordRetriever
from app.core.retrieval.search import HybridSearch, SearchUnavailableError
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
        payload={"chunk_text": "内容", "doc_id": "doc-1", "chunk_index": 0},
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
        payload={"doc_id": "doc-1", "chunk_index": 0},
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
async def test_keyword_retriever_recreates_missing_index_without_backfill(monkeypatch) -> None:
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
    backfill.assert_not_awaited()
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
                    "doc_id": "ready-doc",
                },
                {
                    "chunk_id": "deleting-chunk",
                    "text": "不应返回",
                    "score": 0.8,
                    "doc_id": "deleting-doc",
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
