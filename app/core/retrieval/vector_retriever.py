"""Qdrant 向量语义检索器。"""
from __future__ import annotations

from typing import Any

from app.core.embedding.client import EmbeddingClient
from app.core.embedding.profiles import (
    collection_name,
    get_embedding_profile,
    profile_settings,
)
from app.core.retrieval.filters import build_qdrant_filter
from app.settings import get_settings
from app.stores import qdrant_store
from app.stores.chunk_store import get_chunk_store


class VectorRetriever:
    """基于 Qdrant 稠密向量检索，返回带余弦相似度的结果。"""

    def __init__(
        self,
        embedder: EmbeddingClient | None = None,
        *,
        profile_id: str | None = None,
    ) -> None:
        base_settings = get_settings()
        profile = get_embedding_profile(profile_id, base_settings)
        self._settings = profile_settings(profile, base_settings)
        self._embedder = embedder or EmbeddingClient(self._settings)
        self._collection = collection_name(profile, self._settings)

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

        qdrant_filter = build_qdrant_filter(filters)
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
                "document_id": r.payload.get("document_id") if r.payload else None,
                "knowledge_base_id": (
                    r.payload.get("knowledge_base_id") if r.payload else None
                ),
                "chunk_index": r.payload.get("chunk_index") if r.payload else None,
                "page_no": r.payload.get("page_no") if r.payload else None,
                "source_uri": r.payload.get("source_uri") if r.payload else None,
                "original_filename": r.payload.get("original_filename") if r.payload else None,
                "entity_id": r.payload.get("entity_id") if r.payload else None,
                "entity_type": r.payload.get("entity_type") if r.payload else None,
                "entity_ids": (r.payload.get("entity_ids") or []) if r.payload else [],
                "entity_types": (
                    r.payload.get("entity_types") or []
                ) if r.payload else [],
                "title": r.payload.get("title") if r.payload else None,
                "aliases": (r.payload.get("aliases") or []) if r.payload else [],
                "metadata": r.payload.get("metadata") if r.payload else None,
            }
            for r in results
        ]


class MockVectorRetriever:
    """模拟模式下返回空结果。"""

    async def retrieve(
        self,
        query: str,
        top_k: int | None = None,
        filters: dict[str, Any] | None = None,
    ) -> list[dict[str, Any]]:
        return []
def get_vector_retriever(
    profile_id: str | None = None,
) -> VectorRetriever | MockVectorRetriever:
    if get_settings().qdrant_mock:
        return MockVectorRetriever()
    return VectorRetriever(profile_id=profile_id)
