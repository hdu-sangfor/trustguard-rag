"""分块元数据存储。"""
from __future__ import annotations

from typing import Any
from uuid import uuid4

from sqlalchemy import delete, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores.db import get_engine
from app.stores.models import ChunkRow


class ChunkStore:
    async def create_many(self, chunks: list[dict[str, Any]]) -> list[ChunkRow]:
        """插入单个文档的分块行，并返回刷新后的 ORM 对象。"""
        rows = [
            ChunkRow(
                id=c.get("id") or str(uuid4()),
                document_id=c["document_id"],
                chunk_index=c["chunk_index"],
                text=c["text"],
                token_count=c.get("token_count", 0),
                page_no=c.get("page_no"),
                embedding_model=c.get("embedding_model"),
                embedding_dim=c.get("embedding_dim"),
                qdrant_point_id=c.get("qdrant_point_id"),
                metadata_json=c.get("metadata"),
                status=c.get("status", "active"),
            )
            for c in chunks
        ]
        async with AsyncSession(get_engine()) as session:
            session.add_all(rows)
            await session.commit()
            for row in rows:
                await session.refresh(row)
        return rows

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
        """批量加载指定分块，用于修复旧向量 payload 中缺失的文本字段。"""
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


def get_chunk_store() -> ChunkStore:
    """创建绑定共享数据库引擎的分块存储。"""
    return ChunkStore()
