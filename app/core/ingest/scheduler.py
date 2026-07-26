"""增量同步定时调度（对照 flexible-graphrag / ragversion cron 外壳）。"""

from __future__ import annotations

import logging
from typing import Any

from app.schemas.sync import SyncDirectory, SyncRequest
from app.settings import get_settings

logger = logging.getLogger(__name__)

_scheduler: Any | None = None


async def _run_scheduled_sync() -> None:
    """按配置触发一次目录 sync。"""
    from app.core.ingest.sync import get_sync_runner

    settings = get_settings()
    cleanup = settings.sync_schedule_cleanup.strip().lower()
    req = SyncRequest(
        knowledge_base_id=settings.sync_schedule_knowledge_base_id,
        conflict_policy=settings.sync_schedule_conflict_policy,  # type: ignore[arg-type]
        cleanup=cleanup if cleanup in {"none", "full"} else "none",  # type: ignore[arg-type]
        source_uri_prefix=settings.sync_schedule_source_uri_prefix or None,
        cursor_key=settings.sync_schedule_cursor_key or None,
        directory=SyncDirectory(
            root_key=settings.sync_schedule_root_key,
            relative_path=settings.sync_schedule_relative_path,
            glob=settings.sync_schedule_glob,
            source_uri_template=settings.sync_schedule_source_uri_template,
        ),
        wait=True,
        wait_timeout_sec=600.0,
    )
    try:
        result = await get_sync_runner().run(req)
        logger.info(
            "scheduled sync complete sync_id=%s added=%s skipped=%s updated=%s deleted=%s failed=%s",
            result.sync_id,
            result.added,
            result.skipped,
            result.updated,
            result.deleted,
            result.failed,
        )
    except Exception:  # noqa: BLE001
        logger.exception("scheduled sync failed")


def start_sync_scheduler() -> None:
    """在 lifespan 启动时挂载 APScheduler（默认关闭）。"""
    global _scheduler
    settings = get_settings()
    if not settings.sync_schedule_enabled:
        return
    try:
        from apscheduler.schedulers.asyncio import AsyncIOScheduler
        from apscheduler.triggers.cron import CronTrigger
    except ImportError:
        logger.warning("apscheduler not installed; sync schedule disabled")
        return
    if _scheduler is not None:
        return
    scheduler = AsyncIOScheduler()
    trigger = CronTrigger.from_crontab(settings.sync_schedule_cron)
    scheduler.add_job(
        _run_scheduled_sync,
        trigger=trigger,
        id="rag-incremental-sync",
        replace_existing=True,
        max_instances=1,
        coalesce=True,
    )
    scheduler.start()
    _scheduler = scheduler
    logger.info(
        "sync scheduler started cron=%s root_key=%s",
        settings.sync_schedule_cron,
        settings.sync_schedule_root_key,
    )


def shutdown_sync_scheduler() -> None:
    """关闭定时同步调度器。"""
    global _scheduler
    if _scheduler is None:
        return
    try:
        _scheduler.shutdown(wait=False)
    except Exception:  # noqa: BLE001
        logger.warning("sync scheduler shutdown failed", exc_info=True)
    _scheduler = None
