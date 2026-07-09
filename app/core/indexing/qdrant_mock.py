"""Qdrant 模拟索引器；未启用向量库时执行空操作。"""
from __future__ import annotations

from typing import Any


class MockQdrantIndexer:
    """所有索引操作都不访问 Qdrant，并视为成功。"""

    async def ensure_collection(self) -> None:
        """在模拟模式下假定向量集合已经存在。"""
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
        """只校验分块和向量数量，不写入向量库。"""
        if len(chunks) != len(vectors):
            from app.core.ingest.errors import INDEX_FAILED, IngestError

            raise IngestError(INDEX_FAILED, "Chunk/vector count mismatch")
        return None

    async def delete_points(self, point_ids: list[str]) -> None:
        """接受向量点删除请求，但不访问 Qdrant。"""
        return None
