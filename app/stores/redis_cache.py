"""Redis 客户端 + 健康检查。缓存 / 限流 / 任务心跳，见 §5.1。"""
from __future__ import annotations

import time

import redis.asyncio as aioredis

from app.schemas.api import DependencyStatus
from app.settings import get_settings

_client: aioredis.Redis | None = None


def get_client() -> "aioredis.Redis":
    """创建或复用进程级 Redis 客户端。"""
    global _client
    if _client is None:
        s = get_settings()
        _client = aioredis.from_url(s.redis_url, encoding="utf-8", decode_responses=True)
    return _client


async def check() -> DependencyStatus:
    """向 Redis 发送 ping；连接池失效时重置客户端。"""
    t0 = time.perf_counter()
    try:
        await get_client().ping()
        return DependencyStatus(status="up", latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        # 依赖重启后连接池可能失效；丢弃旧连接池，下次自动重连
        await close()
        return DependencyStatus(status="down", detail=str(e))


async def close() -> None:
    """在关闭应用或健康检查失败时关闭共享 Redis 客户端。"""
    global _client
    if _client is not None:
        await _client.aclose()
        _client = None
