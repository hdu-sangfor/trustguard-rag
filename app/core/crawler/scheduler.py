"""Database-backed periodic crawler source scheduler."""

from __future__ import annotations

import logging
from typing import Any

from app.application.crawler_sources import trigger_crawler_source
from app.settings import get_settings
from app.stores.crawler_source_store import CrawlerSourceStore

logger = logging.getLogger(__name__)
_scheduler: Any | None = None


async def run_crawler_schedule_once() -> int:
    settings = get_settings()
    store = CrawlerSourceStore()
    reconciled = await store.reconcile_versions(
        limit=settings.crawler_schedule_batch_size * 10
    )
    if reconciled:
        logger.info("reconciled crawler source versions: %s", reconciled)
    due = await store.claim_due(limit=settings.crawler_schedule_batch_size)
    created = 0
    for source in due:
        try:
            await trigger_crawler_source(source.id, trigger_type="schedule")
            created += 1
        except Exception:  # noqa: BLE001
            logger.exception("scheduled crawler source failed source_id=%s", source.id)
    return created


def start_crawler_scheduler() -> None:
    global _scheduler
    settings = get_settings()
    if not settings.crawler_enabled or not settings.crawler_schedule_enabled:
        return
    if _scheduler is not None:
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
    except ImportError:
        logger.warning("apscheduler not installed; crawler schedule disabled")
        return
    scheduler = AsyncIOScheduler()
    scheduler.add_job(
        run_crawler_schedule_once,
        "interval",
        seconds=settings.crawler_schedule_scan_seconds,
        id="rag-crawler-sources",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "crawler source scheduler started scan_seconds=%s",
        settings.crawler_schedule_scan_seconds,
    )


def shutdown_crawler_scheduler() -> None:
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        logger.warning("crawler scheduler shutdown failed", exc_info=True)
    _scheduler = None
