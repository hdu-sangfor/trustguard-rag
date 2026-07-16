"""双引擎统一过滤契约测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from pydantic import ValidationError
from qdrant_client.models import FieldCondition, MatchValue

from app.core.indexing.qdrant_indexer import QdrantIndexer
from app.core.retrieval.filters import build_opensearch_filters, build_qdrant_filter
from app.core.retrieval.keyword_retriever import PseudoKeywordRetriever
from app.schemas.search import SearchFilters
from app.settings import get_settings


def test_filter_contract_rejects_legacy_doc_id() -> None:
    with pytest.raises(ValidationError, match="doc_id"):
        SearchFilters.model_validate({"doc_id": "doc-1"})


def test_filter_contract_builds_equivalent_engine_fields() -> None:
    filters = {
        "document_id": "doc-1",
        "page_no": 2,
        "metadata": {"category": "security"},
    }

    qdrant_filter = build_qdrant_filter(filters)
    assert qdrant_filter is not None
    assert qdrant_filter.must == [
        FieldCondition(key="document_id", match=MatchValue(value="doc-1")),
        FieldCondition(key="page_no", match=MatchValue(value=2)),
        FieldCondition(key="metadata.category", match=MatchValue(value="security")),
    ]
    assert build_opensearch_filters(filters) == [
        {"term": {"document_id": "doc-1"}},
        {"term": {"page_no": 2}},
        {"term": {"metadata.category": "security"}},
    ]


@pytest.mark.asyncio
async def test_pseudo_keyword_retriever_uses_nested_metadata_contract() -> None:
    retriever = PseudoKeywordRetriever()
    await retriever.index_chunk(
        chunk_id="chunk-1",
        text="网络安全事件",
        document_id="doc-1",
        chunk_index=0,
        source_uri="upload://one",
        original_filename="one.pdf",
        page_no=1,
        metadata={"category": "security"},
    )

    results = await retriever.retrieve(
        "安全",
        filters={"document_id": "doc-1", "metadata": {"category": "security"}},
    )

    assert [item["chunk_id"] for item in results] == ["chunk-1"]


@pytest.mark.asyncio
async def test_qdrant_payload_uses_canonical_fields(monkeypatch) -> None:
    monkeypatch.setenv("RAG_EMBEDDING_DIM", "2")
    monkeypatch.setenv("RAG_QDRANT_MOCK", "false")
    get_settings.cache_clear()
    client = SimpleNamespace(
        get_collections=AsyncMock(return_value=SimpleNamespace(collections=[])),
        create_collection=AsyncMock(),
        create_payload_index=AsyncMock(),
        upsert=AsyncMock(),
    )
    monkeypatch.setattr("app.stores.qdrant_store.get_client", lambda: client)

    await QdrantIndexer().upsert_chunks(
        document_id="doc-1",
        chunks=[
            {
                "id": "11111111-1111-1111-1111-111111111111",
                "text": "内容",
                "chunk_index": 0,
                "page_no": 1,
                "metadata": {"category": "security"},
            }
        ],
        vectors=[[1.0, 0.0]],
        source_uri="upload://one",
        original_filename="one.pdf",
    )

    point = client.upsert.await_args.kwargs["points"][0]
    assert point.payload["document_id"] == "doc-1"
    assert "doc_id" not in point.payload
    assert point.payload["metadata"] == {"category": "security"}
    get_settings.cache_clear()


@pytest.mark.asyncio
async def test_search_api_rejects_unknown_filter_field(client) -> None:
    response = await client.post(
        "/v1/search",
        json={"query": "安全", "filters": {"doc_id": "doc-1"}},
    )

    assert response.status_code == 422
