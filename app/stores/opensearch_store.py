"""OpenSearch 客户端 + 健康检查，见 §8.3。"""
from __future__ import annotations

import time

from opensearchpy import AsyncOpenSearch

from app.schemas.api import DependencyStatus
from app.settings import get_settings

_client: AsyncOpenSearch | None = None


def get_client() -> AsyncOpenSearch:
    """创建或复用进程级 OpenSearch 异步客户端。"""
    global _client
    if _client is None:
        s = get_settings()
        http_auth = (s.opensearch_user, s.opensearch_password) if s.opensearch_user else None
        _client = AsyncOpenSearch(
            hosts=[{"host": s.opensearch_host, "port": s.opensearch_port}],
            http_auth=http_auth,
            use_ssl=s.opensearch_use_ssl,
            verify_certs=s.opensearch_verify_certs,
            ssl_show_warn=False,
        )
    return _client


async def check() -> DependencyStatus:
    """向 OpenSearch 发送 ping；连接失效时重置客户端。"""
    s = get_settings()
    if s.search_opensearch_mock:
        return DependencyStatus(
            status="disabled",
            detail="opensearch mock mode (in-memory keyword index)",
        )

    t0 = time.perf_counter()
    try:
        ok = await get_client().ping()
        if not ok:
            await close()
            return DependencyStatus(status="down", detail="ping returned False")
        index_name = f"{s.opensearch_index_prefix}chunks"
        if not await get_client().indices.exists(index=index_name):
            return DependencyStatus(status="down", detail=f"missing index: {index_name}")
        return DependencyStatus(status="up", latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        # 依赖重启后旧连接可能失效；丢弃旧连接，下次自动重连
        await close()
        return DependencyStatus(status="down", detail=str(e))


async def close() -> None:
    """在关闭应用或健康检查失败时关闭共享 OpenSearch 客户端。"""
    global _client
    if _client is not None:
        await _client.close()
        _client = None
