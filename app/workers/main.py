"""Standalone RabbitMQ Worker process."""

from __future__ import annotations

import asyncio
import logging

from app.settings import get_settings
from app.stores import db, opensearch_store, qdrant_store, redis_cache
from app.stores.document_store import DocumentStore
from app.stores.outbox_store import ensure_outbox_schema
from app.workers.consumer import run_consumers
from app.workers.publisher import run_outbox_publisher
from app.workers.recovery import recover_once, run_recovery_loop

logger = logging.getLogger(__name__)


async def run() -> None:
    settings = get_settings()
    logging.basicConfig(level=settings.log_level)
    await ensure_outbox_schema()
    recovered = await DocumentStore().enqueue_pending_cleanups()
    logger.info("queued %s pending Saga cleanup command(s)", len(recovered))
    await recover_once()
    try:
        await asyncio.gather(
            run_outbox_publisher(),
            run_consumers(),
            run_recovery_loop(),
        )
    finally:
        for closer in (db.close, qdrant_store.close, opensearch_store.close, redis_cache.close):
            try:
                await closer()
            except Exception:  # noqa: BLE001
                logger.warning("error closing Worker dependency", exc_info=True)


if __name__ == "__main__":
    asyncio.run(run())
