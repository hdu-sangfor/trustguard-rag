"""本地存储健康检查。"""
from __future__ import annotations

import time
from pathlib import Path

from app.schemas.api import DependencyStatus
from app.settings import get_settings


async def check() -> DependencyStatus:
    """通过创建并删除小型暂存探针文件验证本地存储。"""
    t0 = time.perf_counter()
    try:
        s = get_settings()
        root = Path(s.local_storage_dir)
        staging = Path(s.staging_dir)
        root.mkdir(parents=True, exist_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        probe = staging / ".health_probe"
        probe.write_text("ok", encoding="utf-8")
        probe.unlink(missing_ok=True)
        return DependencyStatus(status="up", latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        return DependencyStatus(status="down", detail=str(e))
