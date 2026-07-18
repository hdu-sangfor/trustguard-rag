"""事务性 Outbox 中继。"""

from __future__ import annotations

import asyncio
import logging

from app.settings import get_settings
from app.stores.outbox_store import OutboxStore
from app.stores.rabbitmq import connect, declare_topology, publish_command
from app.workers.messages import CommandMessage

logger = logging.getLogger(__name__)


async def run_outbox_publisher() -> None:
    settings = get_settings()
    store = OutboxStore()
    connection = await connect()
    try:
        channel = await connection.channel(publisher_confirms=True)
        await declare_topology(channel)
        while True:
            events = await store.claim_batch()
            if not events:
                await asyncio.sleep(settings.worker_outbox_poll_seconds)
                continue
            for event in events:
                command = CommandMessage(
                    event_id=event.id,
                    event_type=event.event_type,
                    aggregate_id=event.aggregate_id,
                    payload=event.payload,
                )
                try:
                    await publish_command(channel, command)
                    await store.mark_published(event.id)
                except Exception as exc:  # noqa: BLE001
                    logger.warning("outbox publish failed event_id=%s", event.id, exc_info=True)
                    await store.mark_failed(event.id, str(exc))
    finally:
        await connection.close()
