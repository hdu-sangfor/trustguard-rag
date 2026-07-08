"""RabbitMQ 连接 + 健康检查。异步任务队列 rag.*，见 §9。

M0 仅做连通性探测；连接池与消费者在 M1 的 worker 中实现。
"""
from __future__ import annotations

import time

import aio_pika

from app.schemas.api import DependencyStatus
from app.settings import get_settings


async def check() -> DependencyStatus:
    t0 = time.perf_counter()
    conn = None
    try:
        s = get_settings()
        conn = await aio_pika.connect_robust(
            s.rabbitmq_url, timeout=s.health_check_timeout_seconds
        )
        return DependencyStatus(status="up", latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        return DependencyStatus(status="down", detail=str(e))
    finally:
        if conn is not None:
            await conn.close()
