"""文档元数据存储。"""
from __future__ import annotations

from datetime import datetime, timezone
from typing import Any
from uuid import uuid4

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores.db import get_engine
from app.stores.models import DocumentRow


def _utcnow() -> datetime:
    """返回适用于 MySQL datetime 字段的无时区 UTC 时间。"""
    return datetime.now(timezone.utc).replace(tzinfo=None)


class DocumentStore:
    async def create(
        self,
        *,
        source_type: str,
        source_uri: str,
        content_hash: str,
        status: str = "staging",
        title: str | None = None,
        mime_type: str | None = None,
        original_filename: str | None = None,
        doc_version: int = 1,
        blob_path: str | None = None,
        metadata: dict[str, Any] | None = None,
        document_id: str | None = None,
    ) -> DocumentRow:
        """创建文档元数据行，可使用调用方提供的 ID。"""
        row = DocumentRow(
            id=document_id or str(uuid4()),
            source_type=source_type,
            source_uri=source_uri,
            content_hash=content_hash,
            title=title,
            mime_type=mime_type,
            original_filename=original_filename,
            doc_version=doc_version,
            status=status,
            blob_path=blob_path,
            metadata_json=metadata,
        )
        async with AsyncSession(get_engine()) as session:
            session.add(row)
            await session.commit()
            await session.refresh(row)
        return row

    async def get(self, document_id: str) -> DocumentRow | None:
        """按主键加载单个文档。"""
        async with AsyncSession(get_engine()) as session:
            return await session.get(DocumentRow, document_id)

    async def find_by_source(
        self, source_type: str, source_uri: str, content_hash: str
    ) -> DocumentRow | None:
        """按来源标识和内容哈希查找完全匹配的文档。"""
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(DocumentRow).where(
                    DocumentRow.source_type == source_type,
                    DocumentRow.source_uri == source_uri,
                    DocumentRow.content_hash == content_hash,
                )
            )
            return result.scalar_one_or_none()

    async def find_ready_by_filename(self, original_filename: str) -> list[DocumentRow]:
        """查找原始文件名相同的已发布文档。"""
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(
                select(DocumentRow).where(
                    DocumentRow.original_filename == original_filename,
                    DocumentRow.status == "ready",
                )
            )
            return list(result.scalars().all())

    async def find_ready_by_source_uri(
        self, source_uri: str, exclude_hash: str | None = None
    ) -> list[DocumentRow]:
        """查找来源 URI 相同的已发布文档，可排除指定哈希。"""
        async with AsyncSession(get_engine()) as session:
            q = select(DocumentRow).where(
                DocumentRow.source_uri == source_uri,
                DocumentRow.status == "ready",
            )
            if exclude_hash:
                q = q.where(DocumentRow.content_hash != exclude_hash)
            result = await session.execute(q)
            return list(result.scalars().all())

    async def update_status(
        self,
        document_id: str,
        status: str,
        *,
        blob_path: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> None:
        """更新文档状态和可选的发布元数据。"""
        values: dict[str, Any] = {"status": status, "updated_at": _utcnow()}
        if blob_path is not None:
            values["blob_path"] = blob_path
        if metadata is not None:
            values["metadata_json"] = metadata
        async with AsyncSession(get_engine()) as session:
            await session.execute(
                update(DocumentRow).where(DocumentRow.id == document_id).values(**values)
            )
            await session.commit()

    async def list_by_status(self, status: str) -> list[DocumentRow]:
        """列出处于指定生命周期状态的文档。"""
        async with AsyncSession(get_engine()) as session:
            result = await session.execute(select(DocumentRow).where(DocumentRow.status == status))
            return list(result.scalars().all())


def get_document_store() -> DocumentStore:
    """创建绑定共享数据库引擎的文档存储。"""
    return DocumentStore()
