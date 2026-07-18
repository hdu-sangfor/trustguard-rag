"""Qdrant 客户端 + 健康检查，见 §8.2。"""
from __future__ import annotations

import time

from qdrant_client import AsyncQdrantClient

from app.schemas.api import DependencyStatus
from app.settings import get_settings

_client: AsyncQdrantClient | None = None


def get_client() -> AsyncQdrantClient:
    """创建或复用进程级 Qdrant 异步客户端。"""
    global _client
    if _client is None:
        s = get_settings()
        _client = AsyncQdrantClient(
            url=s.qdrant_url,
            api_key=s.qdrant_api_key,
            check_compatibility=False,
        )
    return _client


async def check() -> DependencyStatus:
    """非模拟模式下检查 Qdrant 可用性。"""
    s = get_settings()
    if s.qdrant_mock:
        return DependencyStatus(status="disabled", detail="qdrant mock mode (no real index)")

    t0 = time.perf_counter()
    try:
        await get_client().get_collections()
        return DependencyStatus(status="up", latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        # 依赖重启后旧连接可能失效（Docker 端口代理回 502）；丢弃旧连接，下次自动重连
        await close()
        return DependencyStatus(status="down", detail=str(e))


async def close() -> None:
    """在关闭应用或健康检查失败时关闭共享 Qdrant 客户端。"""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
