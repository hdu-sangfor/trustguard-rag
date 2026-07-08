from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response, status

from app.schemas.api import DependencyStatus, HealthResponse
from app.settings import get_settings
from app.stores import (
    db,
    local_storage,
    minio_store,
    opensearch_store,
    qdrant_store,
    rabbitmq,
    redis_cache,
)

router = APIRouter(tags=["health"])

_CHECKS = {
    "mysql": db.check,
    "qdrant": qdrant_store.check,
    "opensearch": opensearch_store.check,
    "redis": redis_cache.check,
    "rabbitmq": rabbitmq.check,
    "minio": minio_store.check,
    "local_storage": local_storage.check,
}


def _ingest_required() -> tuple[str, ...]:
    s = get_settings()
    required: list[str] = ["mysql"]
    if s.minio_enabled:
        required.append("minio")
    else:
        required.append("local_storage")
    if not s.qdrant_mock:
        required.append("qdrant")
    return tuple(required)


def _ingest_reported() -> tuple[str, ...]:
    return _ingest_required() + ("qdrant",)


@router.get("/health/live", summary="存活探针")
async def live() -> dict[str, str]:
    return {"status": "alive"}


async def _gather() -> dict[str, DependencyStatus]:
    s = get_settings()
    if s.rag_mode == "ingest":
        names = list(_ingest_reported())
    else:
        names = list(_CHECKS)
    results = await asyncio.gather(
        *(_CHECKS[n]() for n in names), return_exceptions=True
    )
    out: dict[str, DependencyStatus] = {}
    for name, res in zip(names, results):
        if isinstance(res, Exception):
            out[name] = DependencyStatus(status="down", detail=str(res))
        else:
            out[name] = res
    return out


def _overall(deps: dict[str, DependencyStatus]) -> str:
    s = get_settings()
    if s.rag_mode == "ingest":
        for name in _ingest_required():
            dep = deps.get(name)
            if dep is None or dep.status == "down":
                return "degraded"
        return "ok"
    return "degraded" if any(d.status == "down" for d in deps.values()) else "ok"


def _build(deps: dict[str, DependencyStatus]) -> HealthResponse:
    s = get_settings()
    return HealthResponse(
        status=_overall(deps),
        service=s.app_name,
        version=s.app_version,
        env=s.app_env,
        dependencies=deps,
    )


@router.get("/health", response_model=HealthResponse, summary="详细健康检查（含依赖状态）")
async def health() -> HealthResponse:
    return _build(await _gather())


@router.get("/health/ready", response_model=HealthResponse, summary="就绪探针")
async def ready(response: Response) -> HealthResponse:
    result = _build(await _gather())
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
