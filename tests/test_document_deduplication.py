"""融合结果的文档级去重测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest

from app.core.retrieval.search import HybridSearch, _deduplicate_by_document


def test_document_deduplication_keeps_highest_ranked_chunk_stably() -> None:
    items = [
        {"chunk_id": "doc-1-best", "document_id": "doc-1", "score": 0.9},
        {"chunk_id": "doc-2-best", "document_id": "doc-2", "score": 0.8},
        {"chunk_id": "doc-1-second", "document_id": "doc-1", "score": 0.7},
        {"chunk_id": "orphan-1", "document_id": None, "score": 0.6},
        {"chunk_id": "orphan-2", "document_id": None, "score": 0.5},
    ]

    deduplicated, removed = _deduplicate_by_document(
        items,
        max_chunks_per_document=1,
    )

    assert [item["chunk_id"] for item in deduplicated] == [
        "doc-1-best",
        "doc-2-best",
        "orphan-1",
        "orphan-2",
    ]
    assert removed == 1


def test_document_deduplication_supports_configurable_chunk_limit() -> None:
    items = [
        {"chunk_id": "first", "document_id": "doc-1"},
        {"chunk_id": "second", "document_id": "doc-1"},
        {"chunk_id": "third", "document_id": "doc-1"},
    ]

    deduplicated, removed = _deduplicate_by_document(
        items,
        max_chunks_per_document=2,
    )

    assert [item["chunk_id"] for item in deduplicated] == ["first", "second"]
    assert removed == 1


@pytest.mark.asyncio
async def test_hybrid_search_deduplicates_before_rerank(tmp_storage) -> None:
    captured_candidates = []

    async def rerank(_query, candidates, _top_k):
        captured_candidates.extend(candidates)
        return candidates

    engine = HybridSearch()
    engine._documents = SimpleNamespace(
        ready_ids=AsyncMock(return_value={"doc-1", "doc-2"})
    )
    engine._vector = SimpleNamespace(
        retrieve=AsyncMock(
            return_value=[
                {
                    "chunk_id": "doc-1-best",
                    "document_id": "doc-1",
                    "text": "第一篇文档最佳分块",
                    "score": 0.95,
                },
                {
                    "chunk_id": "doc-1-second",
                    "document_id": "doc-1",
                    "text": "第一篇文档第二分块",
                    "score": 0.90,
                },
                {
                    "chunk_id": "doc-2-best",
                    "document_id": "doc-2",
                    "text": "第二篇文档最佳分块",
                    "score": 0.80,
                },
            ]
        )
    )
    engine._reranker = SimpleNamespace(rerank=rerank)

    result = await engine.search(
        "安全问题",
        knowledge_base_id="kb-1",
        enable_keyword=False,
        enable_rerank=True,
        max_chunks_per_document=1,
    )

    assert [item["chunk_id"] for item in captured_candidates] == [
        "doc-1-best",
        "doc-2-best",
    ]
    assert [
        item["source"]["document_id"] for item in result["results"]
    ] == ["doc-1", "doc-2"]
    assert result["deduplicated_chunks"] == 1
    assert result["max_chunks_per_document"] == 1


@pytest.mark.asyncio
async def test_search_api_validates_document_chunk_limit(client) -> None:
    knowledge_bases = await client.get("/v1/knowledge-bases")
    knowledge_base_id = knowledge_bases.json()["items"][0]["id"]

    invalid = await client.post(
        "/v1/search",
        json={
            "query": "安全",
            "knowledge_base_id": knowledge_base_id,
            "max_chunks_per_document": 11,
        },
    )
    assert invalid.status_code == 422

    valid = await client.post(
        "/v1/search",
        json={
            "query": "安全",
            "knowledge_base_id": knowledge_base_id,
            "max_chunks_per_document": 2,
            "enable_vector": False,
            "enable_keyword": True,
            "enable_rerank": False,
        },
    )
    assert valid.status_code == 200
    assert valid.json()["max_chunks_per_document"] == 2
