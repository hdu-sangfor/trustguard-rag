"""仅供确定性测试使用的进程内命令执行器。"""

from __future__ import annotations

import logging

from app.settings import get_settings
from app.stores.outbox_store import OutboxEvent, OutboxStore
from app.workers.handlers import dispatch_command
from app.workers.messages import CommandMessage

logger = logging.getLogger(__name__)


async def dispatch_eager(event: OutboxEvent) -> None:
    """显式启用时在事务提交后执行；生产环境始终使用 RabbitMQ。"""
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
