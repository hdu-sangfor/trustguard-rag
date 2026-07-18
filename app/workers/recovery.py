"""定期恢复 Worker 崩溃和入库 Saga 遗留的孤立状态。"""

from __future__ import annotations

import asyncio
import logging

from app.settings import get_settings
from app.stores.document_store import DocumentStore
from app.stores.job_store import JobStore

logger = logging.getLogger(__name__)


async def recover_once() -> tuple[int, int]:
    job_events = await JobStore().recover_expired_jobs()
    document_events = await DocumentStore().recover_orphan_publications()
    if job_events or document_events:
        logger.warning(
            "recovered expired_jobs=%s orphan_documents=%s",
            len(job_events),
            len(document_events),
        )
    return len(job_events), len(document_events)


async def run_recovery_loop() -> None:
    while True:
        try:
            await recover_once()
        except Exception:  # noqa: BLE001
            logger.warning("Worker recovery scan failed", exc_info=True)
        await asyncio.sleep(get_settings().worker_recovery_scan_seconds)
