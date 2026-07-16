"""In-process command execution used only by deterministic tests."""

from __future__ import annotations

import logging

from app.settings import get_settings
from app.stores.outbox_store import OutboxEvent, OutboxStore
from app.workers.handlers import dispatch_command
from app.workers.messages import CommandMessage

logger = logging.getLogger(__name__)


async def dispatch_eager(event: OutboxEvent) -> None:
    """Execute after commit when explicitly enabled; production always uses RabbitMQ."""
    if not get_settings().worker_eager:
        return
    command = CommandMessage(
        event_id=event.id,
        event_type=event.event_type,
        aggregate_id=event.aggregate_id,
        payload=event.payload,
    )
    try:
        await dispatch_command(command)
    except Exception:  # noqa: BLE001
        logger.warning("eager command failed event_id=%s", event.id, exc_info=True)
        return
    await OutboxStore().mark_published(event.id)
