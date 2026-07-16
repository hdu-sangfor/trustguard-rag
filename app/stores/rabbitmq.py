"""RabbitMQ connection, durable topology and health check."""
from __future__ import annotations

import time

import aio_pika
from aio_pika import DeliveryMode, ExchangeType, Message
from aio_pika.abc import AbstractChannel, AbstractRobustConnection

from app.schemas.api import DependencyStatus
from app.settings import get_settings
from app.workers.messages import ROUTING_KEYS, CommandMessage

DEAD_QUEUE = "rag.dead"


async def connect() -> AbstractRobustConnection:
    """Open a robust Worker connection."""
    return await aio_pika.connect_robust(get_settings().rabbitmq_url)


async def declare_topology(channel: AbstractChannel) -> None:
    """Declare command, retry and dead-letter topology idempotently."""
    settings = get_settings()
    exchange = await channel.declare_exchange(
        settings.rabbitmq_exchange,
        ExchangeType.DIRECT,
        durable=True,
    )
    dead_exchange = await channel.declare_exchange(
        settings.rabbitmq_dead_exchange,
        ExchangeType.DIRECT,
        durable=True,
    )
    dead_queue = await channel.declare_queue(DEAD_QUEUE, durable=True)
    await dead_queue.bind(dead_exchange, routing_key=DEAD_QUEUE)

    for routing_key in sorted(set(ROUTING_KEYS.values())):
        queue = await channel.declare_queue(
            routing_key,
            durable=True,
            arguments={
                "x-dead-letter-exchange": settings.rabbitmq_dead_exchange,
                "x-dead-letter-routing-key": DEAD_QUEUE,
            },
        )
        await queue.bind(exchange, routing_key=routing_key)
        for index, delay in enumerate(settings.rabbitmq_retry_delays, start=1):
            await channel.declare_queue(
                f"{routing_key}.retry.{index}",
                durable=True,
                arguments={
                    "x-message-ttl": delay,
                    "x-dead-letter-exchange": settings.rabbitmq_exchange,
                    "x-dead-letter-routing-key": routing_key,
                },
            )


async def publish_command(
    channel: AbstractChannel,
    command: CommandMessage,
    *,
    retry_count: int = 0,
    retry_queue: str | None = None,
) -> None:
    """Publish a persistent command and wait for broker confirmation."""
    settings = get_settings()
    message = Message(
        command.to_bytes(),
        delivery_mode=DeliveryMode.PERSISTENT,
        content_type="application/json",
        message_id=command.event_id,
        type=command.event_type,
        headers={"x-retry-count": retry_count},
    )
    if retry_queue:
        await channel.default_exchange.publish(message, routing_key=retry_queue)
        return
    exchange = await channel.get_exchange(settings.rabbitmq_exchange)
    await exchange.publish(message, routing_key=ROUTING_KEYS[command.event_type])


async def check() -> DependencyStatus:
    """通过短连接的打开和关闭验证 RabbitMQ 就绪状态。"""
    t0 = time.perf_counter()
    conn = None
    try:
        s = get_settings()
        conn = await aio_pika.connect_robust(
            s.rabbitmq_url, timeout=s.health_check_timeout_seconds
        )
        return DependencyStatus(status="up", latency_ms=round((time.perf_counter() - t0) * 1000, 1))
    except Exception as e:  # noqa: BLE001
        return DependencyStatus(status="down", detail=str(e))
    finally:
        if conn is not None:
            await conn.close()
