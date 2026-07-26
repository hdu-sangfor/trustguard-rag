"""增量同步游标存储。

对照 LangChain SQLRecordManager 的 key/timestamp 账本语义：本仓库只存
sync 水位（cursor_key → cursor_value），文档级 content_hash 仍在 documents 表。
"""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from app.stores.db import get_engine
from app.stores.models import IngestCursorRow


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


class CursorStore:
    async def get(self, cursor_key: str) -> str | None:
        """读取同步水位；不存在时返回 None。"""
        async with AsyncSession(get_engine()) as session:
            row = await session.get(IngestCursorRow, cursor_key)
            return None if row is None else row.cursor_value

    async def set(self, cursor_key: str, cursor_value: str) -> IngestCursorRow:
        """写入或更新同步水位。"""
        if not cursor_key or len(cursor_key) > 64:
            raise ValueError("cursor_key must be 1..64 characters")
        if not cursor_value or len(cursor_value) > 256:
            raise ValueError("cursor_value must be 1..256 characters")
        async with AsyncSession(get_engine()) as session:
            row = await session.get(IngestCursorRow, cursor_key)
            if row is None:
                row = IngestCursorRow(
                    cursor_key=cursor_key,
                    cursor_value=cursor_value,
                    updated_at=_utcnow(),
                )
                session.add(row)
            else:
                row.cursor_value = cursor_value
                row.updated_at = _utcnow()
            await session.commit()
            await session.refresh(row)
            return row

    async def list_keys(self, *, prefix: str | None = None) -> list[str]:
        """列出游标键，可选按前缀过滤。"""
        async with AsyncSession(get_engine()) as session:
            query = select(IngestCursorRow.cursor_key)
            if prefix:
                query = query.where(IngestCursorRow.cursor_key.startswith(prefix))
            result = await session.execute(query.order_by(IngestCursorRow.cursor_key))
            return list(result.scalars().all())


def get_cursor_store() -> CursorStore:
    return CursorStore()
