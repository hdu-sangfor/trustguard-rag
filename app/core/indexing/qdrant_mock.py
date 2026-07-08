"""Qdrant mock indexer — no-op when vector store is not in use."""
from __future__ import annotations

from typing import Any


class MockQdrantIndexer:
    """All index operations succeed without contacting Qdrant."""

    async def ensure_collection(self) -> None:
        return None

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
            from app.core.ingest.errors import INDEX_FAILED, IngestError

            raise IngestError(INDEX_FAILED, "Chunk/vector count mismatch")
        return None

    async def delete_points(self, point_ids: list[str]) -> None:
        return None
