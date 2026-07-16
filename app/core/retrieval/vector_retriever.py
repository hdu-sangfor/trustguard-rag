"""Qdrant 向量语义检索器。"""
from __future__ import annotations

from typing import Any

from qdrant_client.models import Filter, FieldCondition, MatchValue

from app.core.embedding.client import EmbeddingClient
from app.settings import get_settings
from app.stores import qdrant_store
from app.stores.chunk_store import get_chunk_store


class VectorRetriever:
    """基于 Qdrant 稠密向量检索，返回带余弦相似度的结果。"""

    def __init__(self, embedder: EmbeddingClient | None = None) -> None:
        self._settings = get_settings()
        self._embedder = embedder or EmbeddingClient()
        self._collection = f"{self._settings.qdrant_collection_prefix}chunks"

    @property
    def collection_name(self) -> str:
        return self._collection

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        top_k = top_k or self._settings.search_vector_top_k
        query_vector = await self._embedder.embed_query(query)

        qdrant_filter = _build_qdrant_filter(filters)
        client = qdrant_store.get_client()

        response = await client.query_points(
            collection_name=self._collection,
            query=query_vector,
            limit=top_k,
            with_payload=True,
            query_filter=qdrant_filter,
        )
        results = response.points

        missing_text_ids = [
            str(point.id)
            for point in results
            if not point.payload or not point.payload.get("chunk_text")
        ]
        fallback_text = {
            row.id: row.text for row in await get_chunk_store().get_many(missing_text_ids)
        }

        return [
            {
                "chunk_id": r.id,
                "text": (
                    r.payload.get("chunk_text") if r.payload else None
                ) or fallback_text.get(str(r.id), ""),
                "score": float(r.score),
                "doc_id": r.payload.get("doc_id") if r.payload else None,
                "chunk_index": r.payload.get("chunk_index") if r.payload else None,
                "page_no": r.payload.get("page_no") if r.payload else None,
                "source_uri": r.payload.get("source_uri") if r.payload else None,
                "original_filename": r.payload.get("original_filename") if r.payload else None,
                "metadata": r.payload,
            }
            for r in results
        ]


class MockVectorRetriever:
    """mock 模式下返回空结果。"""

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return []


def _build_qdrant_filter(filters: dict[str, Any] | None) -> Filter | None:
    if not filters:
        return None
    conditions = []
    for key, value in filters.items():
        conditions.append(FieldCondition(key=key, match=MatchValue(value=value)))
    return Filter(must=conditions) if conditions else None


def get_vector_retriever() -> VectorRetriever | MockVectorRetriever:
    if get_settings().qdrant_mock:
        return MockVectorRetriever()
    return VectorRetriever()
