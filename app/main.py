"""FastAPI 入口：注册路由、管理依赖客户端生命周期。

见 doc/rag-platform-implementation-plan.md §15 M0（项目骨架）。
启动：uvicorn app.main:app --host 0.0.0.0 --port 18200
"""
from __future__ import annotations

import asyncio
import logging
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import FileResponse
from fastapi.staticfiles import StaticFiles

from app.api import documents, health, ingest, knowledge_bases, ocr_review, search, sources
from app.core.indexing.opensearch_backfill import backfill_ready_documents
from app.settings import get_settings
from app.stores import db, opensearch_store, qdrant_store, redis_cache
from app.stores.outbox_store import ensure_outbox_schema
from app.stores.knowledge_base_migration import (
    backfill_qdrant_knowledge_base_payloads,
    ensure_knowledge_base_schema,
    migrate_legacy_knowledge_bases,
)
from app.stores.models import Base
from app.stores.db import get_engine

logger = logging.getLogger(__name__)


async def run_opensearch_backfill() -> None:
    """后台回填历史索引，避免文档较多时长期阻塞 API 启动。"""
    try:
        result = await backfill_ready_documents()
        logger.info(
            "OpenSearch backfill complete: documents=%s chunks=%s",
            result.documents,
            result.chunks,
        )
    except asyncio.CancelledError:
        logger.info("OpenSearch startup backfill cancelled during shutdown")
        raise
    except Exception:  # noqa: BLE001
        logger.warning("OpenSearch startup backfill failed", exc_info=True)


async def ensure_ocr_schema() -> None:
    """确保 OCR 区域表存在（开发/SQLite 与增量部署）。"""
    try:
        engine = get_engine()
        async with engine.begin() as conn:
            await conn.run_sync(Base.metadata.create_all)
    except Exception:  # noqa: BLE001
        logger.warning("ensure_ocr_schema failed", exc_info=True)


@asynccontextmanager
async def lifespan(app: FastAPI):
    """启动时配置日志，关闭时释放共享客户端。"""
    s = get_settings()
    logging.basicConfig(level=s.log_level)
    logger.info("starting %s v%s (env=%s) on :%s", s.app_name, s.app_version, s.app_env, s.api_port)
    await ensure_outbox_schema()
    await ensure_ocr_schema()
    try:
        await ensure_knowledge_base_schema()
        migrated = await migrate_legacy_knowledge_bases()
        await backfill_qdrant_knowledge_base_payloads()
        if migrated:
            logger.info("knowledge base migration assigned %s legacy documents", migrated)
    except Exception:  # noqa: BLE001
        logger.warning("knowledge base migration failed", exc_info=True)
    backfill_task: asyncio.Task[None] | None = None
    if not s.search_opensearch_mock and s.opensearch_backfill_on_startup:
        backfill_task = asyncio.create_task(
            run_opensearch_backfill(),
            name="opensearch-startup-backfill",
        )
    yield
    if backfill_task is not None and not backfill_task.done():
        backfill_task.cancel()
        try:
            await backfill_task
        except asyncio.CancelledError:
            pass
    # 优雅关闭各连接
    for closer in (db.close, qdrant_store.close, opensearch_store.close, redis_cache.close):
        try:
            await closer()
        except Exception:  # noqa: BLE001
            logger.warning("error closing client", exc_info=True)


def create_app() -> FastAPI:
    """构建 FastAPI 应用并注册所有 HTTP 路由。"""
    s = get_settings()
    app = FastAPI(
        title=s.app_name,
        version=s.app_version,
        description="TrustGuard 独立 RAG 知识库：入库（ingest）与检索。",
        lifespan=lifespan,
    )
    
    app.include_router(health.router)
    app.include_router(ingest.router)
    app.include_router(knowledge_bases.router)
    app.include_router(documents.router)
    app.include_router(sources.router)
    app.include_router(search.router)
    app.include_router(ocr_review.router)

    frontend_dir = Path(__file__).resolve().parent.parent / "frontend"
    app.mount("/assets", StaticFiles(directory=frontend_dir / "assets"), name="assets")

    @app.get("/", include_in_schema=False)
    async def root() -> FileResponse:
        """将服务根路径重定向到交互式 API 文档。"""
        response = FileResponse(frontend_dir / "index.html")
        response.headers["Cache-Control"] = "no-store"
        return response

    return app


app = create_app()
