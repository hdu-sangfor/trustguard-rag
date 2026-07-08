"""Qdrant vector indexing for chunks."""
from __future__ import annotations

from typing import Any
from uuid import UUID

from qdrant_client.models import Distance, PointStruct, VectorParams

from app.core.ingest.errors import INDEX_FAILED, IngestError
from app.core.indexing.qdrant_mock import MockQdrantIndexer
from app.settings import get_settings
from app.stores import qdrant_store


class QdrantIndexer:
    def __init__(self) -> None:
        self._settings = get_settings()
        self._collection = f"{self._settings.qdrant_collection_prefix}chunks"

    async def ensure_collection(self) -> None:
        client = qdrant_store.get_client()
        collections = await client.get_collections()
        names = {c.name for c in collections.collections}
        if self._collection not in names:
            await client.create_collection(
                collection_name=self._collection,
                vectors_config=VectorParams(
                    size=self._settings.embedding_dim,
                    distance=Distance.COSINE,
                ),
            )

    async def upsert_chunks(
        self,
        *,
        document_id: str,
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        source_uri: str,
        original_filename: str | None,
    ) -> None:
        if len(chunks) != len(vectors):
            raise IngestError(INDEX_FAILED, "Chunk/vector count mismatch")
        try:
            await self.ensure_collection()
            points = []
            for chunk, vector in zip(chunks, vectors):
                point_id = chunk["id"]
                payload = {
                    "doc_id": document_id,
                    "chunk_index": chunk["chunk_index"],
                    "page_no": chunk.get("page_no"),
                    "source_uri": source_uri,
                    "original_filename": original_filename,
                }
                points.append(
                    PointStruct(id=_to_point_id(point_id), vector=vector, payload=payload)
                )
            client = qdrant_store.get_client()
            await client.upsert(collection_name=self._collection, points=points)
        except IngestError:
            raise
        except Exception as e:
            raise IngestError(INDEX_FAILED, str(e)) from e

    async def delete_points(self, point_ids: list[str]) -> None:
        if not point_ids:
            return
        client = qdrant_store.get_client()
        await client.delete(
            collection_name=self._collection,
            points_selector=[_to_point_id(pid) for pid in point_ids],
        )


def _to_point_id(value: str) -> str:
    return str(UUID(value))


def get_qdrant_indexer() -> QdrantIndexer | MockQdrantIndexer:
    if get_settings().qdrant_mock:
        return MockQdrantIndexer()
    return QdrantIndexer()
