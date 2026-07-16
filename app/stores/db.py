"""MySQL 连接（SQLAlchemy 异步）和健康检查。

元数据 / 文档 / 分块 / 任务存储的底座，见 §8.1。
"""
from __future__ import annotations

import time

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncEngine, create_async_engine

from app.schemas.api import DependencyStatus
from app.settings import get_settings

_engine: AsyncEngine | None = None


def get_engine() -> AsyncEngine:
    """创建或复用进程级异步 SQLAlchemy 引擎。"""
    global _engine
    if _engine is None:
        s = get_settings()
        _engine = create_async_engine(s.mysql_dsn, pool_pre_ping=True, pool_recycle=1800)
    return _engine


async def check() -> DependencyStatus:
    """通过轻量 SELECT 探针验证 MySQL 连通性。"""
    t0 = time.perf_counter()
    try:
        async with get_engine().connect() as conn:
            await conn.execute(text("SELECT 1"))
        return DependencyStatus(status="up", latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        return DependencyStatus(status="down", detail=str(e))


async def close() -> None:
    """在应用关闭时释放共享 SQLAlchemy 引擎。"""
    global _engine
    if _engine is not None:
        await _engine.dispose()
        _engine = None
