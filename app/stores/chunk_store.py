"""分块元数据存储。"""

from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores.db import get_engine
from app.stores.models import ChunkRow


class ChunkStore:
    @staticmethod
    def _row_from_chunk(chunk: dict[str, Any]) -> ChunkRow:
        """把流水线分块字典转换为数据库行。"""
        return ChunkRow(
            id=chunk.get("id") or str(uuid4()),
            document_id=chunk["document_id"],
            chunk_index=chunk["chunk_index"],
            text=chunk["text"],
            token_count=chunk.get("token_count", 0),
            page_no=chunk.get("page_no"),
            embedding_model=chunk.get("embedding_model"),
            embedding_dim=chunk.get("embedding_dim"),
            qdrant_point_id=chunk.get("qdrant_point_id"),
            metadata_json=chunk.get("metadata"),
            status=chunk.get("status", "active"),
        )

    async def create_many(self, chunks: list[dict[str, Any]]) -> list[ChunkRow]:
        """插入单个文档的分块记录，并返回刷新后的对象关系映射对象。"""
        rows = [self._row_from_chunk(chunk) for chunk in chunks]
        async with AsyncSession(get_engine()) as session:
            session.add_all(rows)
            await session.commit()
            for row in rows:
                await session.refresh(row)
        return rows

    async def replace_for_documents(
        self,
        chunks_by_document: dict[str, list[dict[str, Any]]],
    ) -> None:
        """在同一事务中用新分块替换一组文档的全部旧分块。"""
        if not chunks_by_document:
            return
        document_ids = list(chunks_by_document)
        rows = [
            self._row_from_chunk(chunk)
            for chunks in chunks_by_document.values()
            for chunk in chunks
        ]
        async with AsyncSession(get_engine()) as session:
            await session.execute(delete(ChunkRow).where(ChunkRow.document_id.in_(document_ids)))
            session.add_all(rows)
            await session.commit()

    async def list_for_document(self, document_id: str) -> list[ChunkRow]:
        """按分块序号返回文档的所有分块。"""
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ChunkRow)
                .where(ChunkRow.document_id == document_id)
                .order_by(ChunkRow.chunk_index)
            )
            return list(result.scalars().all())

    async def get_many(self, chunk_ids: list[str]) -> list[ChunkRow]:
        """批量加载指定分块，用于修复旧向量载荷中缺失的文本字段。"""
        if not chunk_ids:
            return []
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(select(ChunkRow).where(ChunkRow.id.in_(chunk_ids)))
            return list(result.scalars().all())

    async def delete_for_document(self, document_id: str) -> list[str]:
        """删除文档分块，并返回需要清理的向量点 ID。"""
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ChunkRow.qdrant_point_id).where(ChunkRow.document_id == document_id)
            )
            point_ids = [pid for pid in result.scalars().all() if pid]
            await session.execute(delete(ChunkRow).where(ChunkRow.document_id == document_id))
            await session.commit()
        return point_ids

    async def point_ids_for_document(self, document_id: str) -> list[str]:
        """返回文档关联的向量点 ID，不修改任何数据。"""
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(ChunkRow.qdrant_point_id).where(ChunkRow.document_id == document_id)
            )
            return [point_id for point_id in result.scalars().all() if point_id]

    async def update_embedding_configuration(
        self,
        chunk_ids: list[str],
        *,
        model: str,
        dimension: int,
        provider: str,
    ) -> None:
        """在索引重建后同步分块的嵌入配置和元数据。"""
        if not chunk_ids:
            return
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(select(ChunkRow).where(ChunkRow.id.in_(chunk_ids)))
            for row in result.scalars().all():
                row.embedding_model = model
                row.embedding_dim = dimension
                row.metadata_json = {
                    **(row.metadata_json or {}),
                    "embedding_provider": provider,
                }
            await session.commit()


def get_chunk_store() -> ChunkStore:
    """创建绑定共享数据库引擎的分块存储。"""
    return ChunkStore()
