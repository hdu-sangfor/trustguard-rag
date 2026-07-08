"""对象存储健康检查：MinIO 启用时检查 bucket；未启用时返回 disabled。"""
from __future__ import annotations

import asyncio
import time

from app.schemas.api import DependencyStatus
from app.settings import get_settings
from app.stores.minio_client import ensure_bucket, get_minio_client


def _check_sync() -> DependencyStatus:
    s = get_settings()
    client = get_minio_client()
    ensure_bucket()
    client.bucket_exists(s.minio_bucket)
    return DependencyStatus(status="up")


async def check() -> DependencyStatus:
    s = get_settings()
    if not s.minio_enabled:
        return DependencyStatus(status="disabled", detail="minio backend disabled")

    t0 = time.perf_counter()
    try:
        result = await asyncio.to_thread(_check_sync)
        result.latency_ms = round((time.perf_counter() - t0) * 1000, 1)
        return result
    except Exception as e:  # noqa: BLE001
        return DependencyStatus(status="down", detail=str(e))
