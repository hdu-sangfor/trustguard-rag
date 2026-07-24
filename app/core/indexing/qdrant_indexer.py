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
    PayloadSchemaType,
    PointStruct,
    VectorParams,
)

from app.core.ingest.errors import INDEX_FAILED, IngestError
from app.core.embedding.profiles import EmbeddingProfile, collection_name
from app.core.indexing.qdrant_mock import MockQdrantIndexer
from app.core.retrieval.security_entities import build_security_entity_fields
from app.settings import Settings, get_settings
from app.stores import qdrant_store


class QdrantIndexer:
    def __init__(
        self,
        settings: Settings | None = None,
        *,
        profile: EmbeddingProfile | None = None,
    ) -> None:
        """读取向量配置并生成目标分块集合名。"""
        self._settings = settings or get_settings()
        self._profile = profile
        self._collection = (
            collection_name(profile, self._settings)
            if profile is not None
            else f"{self._settings.qdrant_collection_prefix}chunks"
        )
        self._collection_ready = False

    @property
    def collection_name(self) -> str:
        """返回当前使用的 Qdrant 集合名。"""
        return self._collection

    async def ensure_collection(self) -> None:
        """在尚未创建时初始化分块向量集合。"""
        if self._collection_ready:
            return
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
        for field_name, field_schema in (
            ("knowledge_base_id", PayloadSchemaType.KEYWORD),
            ("entity_id", PayloadSchemaType.KEYWORD),
            ("entity_type", PayloadSchemaType.KEYWORD),
            ("entity_ids", PayloadSchemaType.KEYWORD),
            ("entity_types", PayloadSchemaType.KEYWORD),
            ("aliases", PayloadSchemaType.KEYWORD),
            ("document_id", PayloadSchemaType.KEYWORD),
            ("source_uri", PayloadSchemaType.KEYWORD),
            ("original_filename", PayloadSchemaType.KEYWORD),
            ("chunk_index", PayloadSchemaType.INTEGER),
            ("page_no", PayloadSchemaType.INTEGER),
        ):
            await client.create_payload_index(
                collection_name=self._collection,
                field_name=field_name,
                field_schema=field_schema,
                wait=True,
            )
        self._collection_ready = True

    async def upsert_chunks(
        self,
        *,
        document_id: str,
        chunks: list[dict[str, Any]],
        vectors: list[list[float]],
        source_uri: str,
        original_filename: str | None,
    ) -> None:
        """写入分块向量及后续检索需要的载荷字段。"""
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
                security_fields = build_security_entity_fields(
                    text=chunk["text"],
                    original_filename=original_filename,
                    metadata=chunk.get("metadata"),
                )
                payload = {
                    "chunk_text": chunk["text"],
                    "knowledge_base_id": chunk.get("knowledge_base_id")
                    or (chunk.get("metadata") or {}).get("knowledge_base_id"),
                    "document_id": document_id,
                    "chunk_index": chunk["chunk_index"],
                    "page_no": chunk.get("page_no"),
                    "source_uri": source_uri,
                    "original_filename": original_filename,
                    **security_fields,
                    "metadata": chunk.get("metadata") or {},
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
        collections = await client.get_collections()
        for collection in self._delete_collections(collections.collections):
            await client.delete(
                collection_name=collection,
                points_selector=[_to_point_id(pid) for pid in point_ids],
            )

    async def delete_document(self, document_id: str) -> None:
        """按载荷中的文档 ID 删除全部向量，覆盖分块尚未落库的情况。"""
        client = qdrant_store.get_client()
        collections = await client.get_collections()
        for collection in self._delete_collections(collections.collections):
            await client.delete(
                collection_name=collection,
                points_selector=FilterSelector(
                    filter=Filter(
                        must=[
                            FieldCondition(
                                key="document_id",
                                match=MatchValue(value=document_id),
                            )
                        ]
                    )
                ),
            )

    def _delete_collections(self, collections: list[Any]) -> list[str]:
        names = {item.name for item in collections}
        if self._profile is not None:
            return [self._collection] if self._collection in names else []
        base = f"{self._settings.qdrant_collection_prefix}chunks"
        return sorted(name for name in names if name == base or name.startswith(f"{base}__"))


def _to_point_id(value: str) -> str:
    """将 UUID 字符串规范化为 Qdrant 向量点 ID。"""
    return str(UUID(value))


def get_qdrant_indexer(
    settings: Settings | None = None,
    *,
    profile: EmbeddingProfile | None = None,
) -> QdrantIndexer | MockQdrantIndexer:
    """根据配置选择真实或模拟 Qdrant 索引器。"""
    settings = settings or get_settings()
    if settings.qdrant_mock:
        return MockQdrantIndexer()
    return QdrantIndexer(settings, profile=profile)
