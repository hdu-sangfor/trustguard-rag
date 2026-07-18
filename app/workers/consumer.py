"""支持延迟重试和死信处理的 RabbitMQ 命令消费者。"""

from __future__ import annotations

import asyncio
import logging

from aio_pika import DeliveryMode, Message
from aio_pika.abc import AbstractIncomingMessage

from app.settings import get_settings
from app.stores.rabbitmq import DEAD_QUEUE, connect, declare_topology, publish_command
from app.workers.handlers import BusyCommandError, dispatch_command
from app.workers.messages import ROUTING_KEYS, CommandMessage

logger = logging.getLogger(__name__)


async def run_consumers() -> None:
    settings = get_settings()
    connection = await connect()
    channel = await connection.channel(publisher_confirms=True)
    await channel.set_qos(prefetch_count=settings.rabbitmq_prefetch_count)
    await declare_topology(channel)

    async def on_message(message: AbstractIncomingMessage) -> None:
        try:
            command = CommandMessage.from_bytes(message.body)
        except Exception:  # noqa: BLE001
            logger.error("discarding invalid command", exc_info=True)
            await _publish_dead(channel, message.body, "invalid-command")
            await message.ack()
            return

        retry_count = int((message.headers or {}).get("x-retry-count", 0))
        try:
            await dispatch_command(command)
        except BusyCommandError:
            # 等待有效租约属于并发协调，不计为一次业务失败尝试。
            routing_key = ROUTING_KEYS[command.event_type]
            await publish_command(
                channel,
                command,
                retry_count=retry_count,
                retry_queue=f"{routing_key}.retry.{len(settings.rabbitmq_retry_delays)}",
            )
            await message.ack()
            return
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "command failed event_id=%s retry=%s",
                command.event_id,
                retry_count,
                exc_info=True,
            )
            if retry_count >= settings.rabbitmq_consumer_max_retries:
                await _publish_dead(channel, message.body, str(exc))
            else:
                delays = settings.rabbitmq_retry_delays
                retry_index = min(retry_count, len(delays) - 1) + 1
                routing_key = ROUTING_KEYS[command.event_type]
                await publish_command(
                    channel,
                    command,
                    retry_count=retry_count + 1,
                    retry_queue=f"{routing_key}.retry.{retry_index}",
                )
            await message.ack()
            return
        await message.ack()

    for queue_name in sorted(set(ROUTING_KEYS.values())):
        queue = await channel.get_queue(queue_name)
        await queue.consume(on_message, no_ack=False)

    try:
        await asyncio.Future()
    finally:
        await connection.close()


async def _publish_dead(channel, body: bytes, error: str) -> None:
    settings = get_settings()
    exchange = await channel.get_exchange(settings.rabbitmq_dead_exchange)
    await exchange.publish(
        Message(
            body,
            delivery_mode=DeliveryMode.PERSISTENT,
            content_type="application/json",
            headers={"x-error": error[:1000]},
        ),
        routing_key=DEAD_QUEUE,
    )
