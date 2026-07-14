"""面向分块的 Qdrant 向量索引。"""

from __future__ import annotations

from typing import Any
from uuid import UUID

from qdrant_client.models import (
    Distance,
    FieldCondition,
    Filter,
    FilterSelector,
    MatchValue,
    PointStruct,
    VectorParams,
)

from app.core.ingest.errors import INDEX_FAILED, IngestError
from app.core.indexing.qdrant_mock import MockQdrantIndexer
from app.settings import get_settings
from app.stores import qdrant_store


class QdrantIndexer:
    def __init__(self) -> None:
        """读取向量配置并生成目标分块集合名。"""
        self._settings = get_settings()
        self._collection = f"{self._settings.qdrant_collection_prefix}chunks"

    async def ensure_collection(self) -> None:
        """在尚未创建时初始化分块向量集合。"""
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
        """写入分块向量及后续检索需要的 payload 字段。"""
        if len(chunks) != len(vectors):
            raise IngestError(INDEX_FAILED, "Chunk/vector count mismatch")
        for idx, vector in enumerate(vectors):
            if len(vector) != self._settings.embedding_dim:
                raise IngestError(
                    INDEX_FAILED,
                    f"Vector dimension mismatch at index {idx}: "
                    f"expected {self._settings.embedding_dim}, got {len(vector)}",
                )
        try:
            await self.ensure_collection()
            points = []
            for chunk, vector in zip(chunks, vectors):
                point_id = chunk["id"]
                payload = {
                    "chunk_text": chunk["text"],
                    "doc_id": document_id,
                    "chunk_index": chunk["chunk_index"],
                    "page_no": chunk.get("page_no"),
                    "source_uri": source_uri,
                    "original_filename": original_filename,
                    "embedding_model": self._settings.embedding_model,
                    "embedding_dim": self._settings.embedding_dim,
                    "embedding_provider": self._settings.embedding_provider,
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
        """按已保存的分块或向量 ID 删除向量点。"""
        if not point_ids:
            return
        client = qdrant_store.get_client()
        await client.delete(
            collection_name=self._collection,
            points_selector=[_to_point_id(pid) for pid in point_ids],
        )

    async def delete_document(self, document_id: str) -> None:
        """按 payload 中的文档 ID 删除全部向量，覆盖 chunks 尚未落库的情况。"""
        client = qdrant_store.get_client()
        collections = await client.get_collections()
        if self._collection not in {item.name for item in collections.collections}:
            return
        await client.delete(
            collection_name=self._collection,
            points_selector=FilterSelector(
                filter=Filter(
                    must=[
                        FieldCondition(
                            key="doc_id",
                            match=MatchValue(value=document_id),
                        )
                    ]
                )
            ),
        )


def _to_point_id(value: str) -> str:
    """将 UUID 字符串规范化为 Qdrant point ID。"""
    return str(UUID(value))


def get_qdrant_indexer() -> QdrantIndexer | MockQdrantIndexer:
    """根据配置选择真实或模拟 Qdrant 索引器。"""
    if get_settings().qdrant_mock:
        return MockQdrantIndexer()
    return QdrantIndexer()
