"""Qdrant 文档级向量删除测试。"""

from __future__ import annotations

from types import SimpleNamespace
from unittest.mock import AsyncMock

import pytest
from qdrant_client.models import FieldCondition, FilterSelector, MatchValue

from app.core.indexing.qdrant_indexer import QdrantIndexer


@pytest.mark.asyncio
async def test_delete_document_uses_document_id_payload_filter(monkeypatch) -> None:
    client = AsyncMock()
    client.get_collections.return_value = SimpleNamespace(
        collections=[SimpleNamespace(name="rag_chunks")]
    )
    monkeypatch.setattr("app.stores.qdrant_store.get_client", lambda: client)

    await QdrantIndexer().delete_document("doc-1")

    selector = client.delete.call_args.kwargs["points_selector"]
    assert isinstance(selector, FilterSelector)
    assert selector.filter.must == [
        FieldCondition(key="document_id", match=MatchValue(value="doc-1"))
    ]
