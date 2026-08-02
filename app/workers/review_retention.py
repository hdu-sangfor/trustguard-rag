"""Expire retained content for Agent-rejected crawler review items."""

from __future__ import annotations

import asyncio
import logging

from app.settings import get_settings
from app.stores.blob_store import get_blob_store
from app.stores.crawler_store import CrawlerStore

logger = logging.getLogger(__name__)


async def cleanup_rejected_review_content_once() -> int:
    store = CrawlerStore()
    claims = await store.claim_expired_review_content()
    cleaned = 0
    blobs = get_blob_store()
    for claim in claims:
        try:
            blobs.delete_job_staging(claim.item_id)
        except Exception:  # noqa: BLE001
            await store.release_review_content_cleanup(claim)
            logger.warning(
                "failed to expire retained review content for %s",
                claim.item_id,
                exc_info=True,
            )
            continue
        if await store.finalize_review_content_cleanup(claim):
            cleaned += 1
    if cleaned:
        logger.info("expired %s retained Agent review item(s)", cleaned)
    return cleaned


async def run_review_content_cleanup_loop() -> None:
    while True:
        await asyncio.sleep(get_settings().crawler_review_cleanup_scan_seconds)
        try:
            await cleanup_rejected_review_content_once()
        except Exception:  # noqa: BLE001
            logger.warning("Agent review retention cleanup failed", exc_info=True)
