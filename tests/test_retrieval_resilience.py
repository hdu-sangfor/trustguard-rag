from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError

from app.core.embedding.client import EmbeddingError
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
    monkeypatch.setenv("RAG_SEARCH_COMPONENT_RETRY_BACKOFF_SECONDS", "0")
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
        await engine.search("查询", knowledge_base_id="kb-test", enable_rerank=False)


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

    result = await engine.search(
        "查询", knowledge_base_id="kb-test", enable_rerank=False
    )

    assert result["total"] == 1
    assert result["degraded_components"] == ["vector"]
    assert result["search_status"] is SearchStatus.DEGRADED
    assert result["effective_mode"] is EffectiveSearchMode.KEYWORD_ONLY


@pytest.mark.asyncio
async def test_hybrid_search_recovers_vector_component_with_retry(
    search_settings,
) -> None:
    vector = SimpleNamespace(
        retrieve=AsyncMock(
            side_effect=[
                RuntimeError("temporary vector failure"),
                [
                    {
                        "chunk_id": "vector-1",
                        "text": "向量结果",
                        "score": 0.9,
                        "document_id": "doc-1",
                    }
                ],
            ]
        )
    )
    engine = HybridSearch()
    engine._vector = vector
    engine._keyword = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(return_value={"doc-1"})
    )

    result = await engine.search(
        "查询",
        knowledge_base_id="kb-test",
        enable_rerank=False,
        component_max_retries=2,
    )

    assert result["search_status"] is SearchStatus.OK
    assert result["degraded_components"] == []
    assert result["component_attempts"]["vector"] == 2
    assert result["recovered_components"] == ["vector"]
    assert vector.retrieve.await_count == 2


@pytest.mark.asyncio
async def test_hybrid_search_does_not_retry_permanent_embedding_error(
    search_settings,
) -> None:
    vector = SimpleNamespace(
        retrieve=AsyncMock(
            side_effect=EmbeddingError("quota exhausted", retryable=False)
        )
    )
    engine = HybridSearch()
    engine._vector = vector
    engine._keyword = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "keyword-1",
                    "text": "关键词结果",
                    "score": 1.0,
                    "document_id": "doc-1",
                }
            ]
        )
    )
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(return_value={"doc-1"})
    )

    result = await engine.search(
        "查询",
        knowledge_base_id="kb-test",
        enable_rerank=False,
        enable_abstention=False,
        component_max_retries=2,
    )

    assert result["search_status"] is SearchStatus.DEGRADED
    assert result["component_attempts"]["vector"] == 1
    assert vector.retrieve.await_count == 1


@pytest.mark.asyncio
async def test_exact_identifier_without_exact_match_abstains(
    search_settings,
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "noise",
                    "text": "其他漏洞",
                    "score": 0.99,
                    "document_id": "doc-noise",
                    "entity_id": "CVE-2026-99999",
                    "entity_ids": ["CVE-2026-99999"],
                }
            ]
        )
    )
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(return_value={"doc-noise"})
    )

    result = await engine.search(
        "CVE-2099-9001 是什么？",
        knowledge_base_id="kb-test",
        enable_keyword=False,
        enable_rerank=False,
        min_vector_score=0.5,
    )

    assert result["results"] == []
    assert result["abstained"] is True
    assert result["abstention_reason"] == "no_exact_entity_match"


@pytest.mark.asyncio
async def test_low_vector_score_abstains_for_semantic_query(
    search_settings,
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "weak",
                    "text": "低相关内容",
                    "score": 0.49,
                    "document_id": "doc-weak",
                }
            ]
        )
    )
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(return_value={"doc-weak"})
    )

    result = await engine.search(
        "虚构产品的量子隧道漏洞",
        knowledge_base_id="kb-test",
        enable_keyword=False,
        enable_rerank=False,
        min_vector_score=0.52,
    )

    assert result["results"] == []
    assert result["abstained"] is True
    assert result["abstention_reason"] == "low_vector_score"
    assert result["min_vector_score"] == 0.52


@pytest.mark.asyncio
async def test_hybrid_confidence_gate_keeps_keyword_only_candidates_with_vector_anchor(
    search_settings,
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "vector-anchor",
                    "text": "可信向量结果",
                    "score": 0.80,
                    "document_id": "doc-vector",
                },
                {
                    "chunk_id": "weak-vector",
                    "text": "低分向量结果",
                    "score": 0.40,
                    "document_id": "doc-weak",
                },
            ]
        )
    )
    engine._keyword = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "keyword-only",
                    "text": "仅关键词召回",
                    "score": 12.0,
                    "document_id": "doc-keyword",
                },
                {
                    "chunk_id": "weak-vector",
                    "text": "低分向量但有关键词证据",
                    "score": 8.0,
                    "document_id": "doc-weak",
                },
            ]
        )
    )
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(
            return_value={"doc-vector", "doc-keyword", "doc-weak"}
        )
    )

    result = await engine.search(
        "混合检索",
        knowledge_base_id="kb-test",
        enable_rerank=False,
        min_vector_score=0.52,
    )

    by_id = {item["chunk_id"]: item for item in result["results"]}
    assert set(by_id) == {"vector-anchor", "keyword-only", "weak-vector"}
    assert by_id["keyword-only"]["vector_score"] is None
    assert by_id["keyword-only"]["keyword_score"] == 12.0
    assert result["abstained"] is False


@pytest.mark.asyncio
async def test_hybrid_confidence_gate_abstains_without_vector_anchor(
    search_settings,
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "weak-vector",
                    "text": "低分向量结果",
                    "score": 0.40,
                    "document_id": "doc-weak",
                }
            ]
        )
    )
    engine._keyword = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "keyword-only",
                    "text": "关键词噪声",
                    "score": 20.0,
                    "document_id": "doc-keyword",
                }
            ]
        )
    )
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(return_value={"doc-weak", "doc-keyword"})
    )

    result = await engine.search(
        "超出知识库范围的问题",
        knowledge_base_id="kb-test",
        enable_rerank=False,
        min_vector_score=0.52,
    )

    assert result["results"] == []
    assert result["abstained"] is True
    assert result["abstention_reason"] == "low_vector_score"


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
        await engine.search("查询", knowledge_base_id="kb-test", enable_rerank=False)


@pytest.mark.asyncio
async def test_hybrid_search_returns_true_empty_when_backends_are_healthy(
    search_settings,
) -> None:
    engine = HybridSearch()
    engine._vector = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    engine._keyword = SimpleNamespace(retrieve=AsyncMock(return_value=[]))
    engine._documents = SimpleNamespace(ready_ids=AsyncMock(return_value=set()))

    result = await engine.search(
        "查询", knowledge_base_id="kb-test", enable_rerank=False
    )

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

    result = await engine.search(
        "查询",
        knowledge_base_id="kb-test",
        enable_keyword=False,
        enable_rerank=True,
    )

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
        knowledge_base_id="kb-test",
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


def test_search_component_retry_settings_are_bounded() -> None:
    with pytest.raises(ValidationError, match="MAX_RETRIES"):
        Settings(_env_file=None, search_component_max_retries=6)
    with pytest.raises(ValidationError, match="BACKOFF_SECONDS"):
        Settings(
            _env_file=None,
            search_component_retry_backoff_seconds=-0.1,
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
    knowledge_bases = await client.get("/v1/knowledge-bases")
    knowledge_base_id = knowledge_bases.json()["items"][0]["id"]

    response = await client.post(
        "/v1/search",
        json={
            "query": "查询",
            "knowledge_base_id": knowledge_base_id,
            "enable_rerank": False,
        },
    )

    assert response.status_code == 503
    assert "no reliable result" in response.json()["detail"]
