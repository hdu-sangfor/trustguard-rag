"""持久化后台迁移状态，并将其暴露为健康检查依赖。"""

from __future__ import annotations

from sqlalchemy.ext.asyncio import AsyncSession

from app.schemas.api import DependencyStatus
from app.settings import get_settings
from app.stores.db import get_engine
from app.stores.models import MigrationStateRow

KNOWLEDGE_BASE_INDEX_BACKFILL = "knowledge_base_index_backfill"


async def set_migration_state(
    name: str,
    status: str,
    *,
    processed_count: int = 0,
    error_message: str | None = None,
) -> None:
    async with AsyncSession(get_engine()) as session:
        row = await session.get(MigrationStateRow, name)
        if row is None:
            row = MigrationStateRow(name=name, status=status)
            session.add(row)
        row.status = status
        row.processed_count = processed_count
        row.error_message = error_message
        await session.commit()


async def get_migration_state(name: str) -> MigrationStateRow | None:
    async with AsyncSession(get_engine()) as session:
        return await session.get(MigrationStateRow, name)


async def check_knowledge_base_index_backfill() -> DependencyStatus:
    if get_settings().qdrant_mock:
        return DependencyStatus(
            status="disabled",
            detail="qdrant mock mode does not require payload backfill",
        )
    row = await get_migration_state(KNOWLEDGE_BASE_INDEX_BACKFILL)
    if row is None:
        return DependencyStatus(status="down", detail="index backfill has not started")
    if row.status == "ready":
        return DependencyStatus(
            status="up",
            detail=f"processed {row.processed_count} documents",
        )
    detail = row.status
    if row.error_message:
        detail = f"{detail}: {row.error_message}"
    return DependencyStatus(status="down", detail=detail)
