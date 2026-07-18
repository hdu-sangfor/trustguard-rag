"""MinerU dependency health check."""

from __future__ import annotations

import httpx

from app.schemas.api import DependencyStatus
from app.settings import get_settings


async def check() -> DependencyStatus:
    settings = get_settings()
    url = f"{settings.mineru_base_url.rstrip('/')}/health"
    try:
        async with httpx.AsyncClient(timeout=min(settings.mineru_timeout_seconds, 5.0)) as client:
            response = await client.get(url)
            response.raise_for_status()
        return DependencyStatus(status="up", detail=f"MinerU reachable at {url}")
    except Exception as exc:  # noqa: BLE001
        return DependencyStatus(status="down", detail=f"MinerU unavailable: {exc}")
