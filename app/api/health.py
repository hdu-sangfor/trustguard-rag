from __future__ import annotations

import asyncio

from fastapi import APIRouter, Response, status

from app.schemas.api import DependencyStatus, HealthResponse
from app.settings import get_settings
from app.stores import (
    db,
    local_storage,
    mineru_store,
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
    "mineru": mineru_store.check,
}


def _ingest_required() -> tuple[str, ...]:
    """列出入库模式就绪检查必须健康的依赖。"""
    s = get_settings()
    required: list[str] = ["mysql"]
    if s.minio_enabled:
        required.append("minio")
    else:
        required.append("local_storage")
    if not s.qdrant_mock:
        required.append("qdrant")
    if not s.search_opensearch_mock:
        required.append("opensearch")
    # MinerU only required when PDF or DOCX is configured to use it.
    needs_mineru = (
        s.pdf_parser.strip().lower() == "mineru"
        or s.docx_parser.strip().lower() == "mineru"
    )
    if needs_mineru:
        required.append("mineru")
    return tuple(required)


def _ingest_reported() -> tuple[str, ...]:
    """列出入库模式会报告的依赖，包括可选的检索后端状态。"""
    return tuple(dict.fromkeys((*_ingest_required(), "qdrant", "opensearch", "rabbitmq")))


@router.get("/health/live", summary="存活探针")
async def live() -> dict[str, str]:
    """返回轻量存活状态，不访问外部依赖。"""
    return {"status": "alive"}


async def _gather() -> dict[str, DependencyStatus]:
    """按当前 RAG 模式执行对应的依赖检查。"""
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
    """将依赖状态汇总为对外的健康状态。"""
    s = get_settings()
    if s.rag_mode == "ingest":
        for name in _ingest_required():
            dep = deps.get(name)
            if dep is None or dep.status == "down":
                return "degraded"
        return "ok"
    return "degraded" if any(d.status == "down" for d in deps.values()) else "ok"


def _build(deps: dict[str, DependencyStatus]) -> HealthResponse:
    """组装包含服务元数据和依赖详情的健康检查响应。"""
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
    """返回详细健康信息，不改变 HTTP 状态码。"""
    return _build(await _gather())


@router.get("/health/ready", response_model=HealthResponse, summary="就绪探针")
async def ready(response: Response) -> HealthResponse:
    """返回就绪状态；必要依赖异常时返回 HTTP 503。"""
    result = _build(await _gather())
    if result.status != "ok":
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return result
