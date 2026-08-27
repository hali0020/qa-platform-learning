from __future__ import annotations

import asyncio
import importlib
import time
from collections.abc import Callable
from contextlib import suppress
from typing import Any

from app.broker.errors import (
    BrokerDependencyError,
    BrokerStateError,
    BrokerTransportError,
)
from app.broker.local import _validate_timeout
from app.broker.messages import (
    WAKEUP_EXCHANGE_NAME,
    WAKEUP_HINT_BODY,
    WAKEUP_MESSAGE_TYPE,
    WAKEUP_QUEUE_NAME,
    WAKEUP_ROUTING_KEY,
)
from app.core.config import validate_broker_runtime_target


AioPikaLoader = Callable[[], Any]
MonotonicClock = Callable[[], float]
BROKER_OPERATION_TIMEOUT_SECONDS = 2.0
BROKER_FAILURE_COOLDOWN_SECONDS = 2.0
_PUBLISHER_COOLDOWN_ERROR = (
    "RabbitMQ wake-up publisher is temporarily unavailable"
)


def _load_aio_pika() -> Any:
    """Import aio-pika only after the RabbitMQ mode is explicitly used."""

    try:
        return importlib.import_module("aio_pika")
    except ModuleNotFoundError:
        raise BrokerDependencyError(
            "rabbitmq_local_container requires the aio-pika dependency"
        ) from None


def _validate_rabbitmq_target(url: str, app_env: str) -> None:
    # Defense in depth: direct adapter construction cannot bypass Settings.
    validate_broker_runtime_target(
        broker_url=url,
        runtime_mode="rabbitmq_local_container",
        app_env=app_env,
    )


async def _close_quietly(resource: Any | None) -> None:
    if resource is None:
        return
    with suppress(Exception):
        await asyncio.wait_for(
            resource.close(),
            timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
        )


async def _close_after_cancellation(resource: Any | None) -> None:
    """Finish bounded cleanup even while the caller propagates cancellation."""

    if resource is None:
        return
    cleanup = asyncio.create_task(_close_quietly(resource))
    with suppress(asyncio.CancelledError):
        await asyncio.shield(cleanup)


async def _declare_topology(channel: Any, aio_pika: Any) -> tuple[Any, Any]:
    exchange = await channel.declare_exchange(
        WAKEUP_EXCHANGE_NAME,
        aio_pika.ExchangeType.DIRECT,
        durable=True,
        auto_delete=False,
        timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
    )
    queue = await channel.declare_queue(
        WAKEUP_QUEUE_NAME,
        durable=True,
        auto_delete=False,
        timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
    )
    await queue.bind(
        exchange,
        routing_key=WAKEUP_ROUTING_KEY,
        timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
    )
    return exchange, queue


class RabbitMQWakeupPublisher:
    """Publish a persistent, content-free hint to a durable local queue."""

    def __init__(
        self,
        url: str,
        *,
        app_env: str,
        aio_pika_loader: AioPikaLoader | None = None,
        monotonic_clock: MonotonicClock | None = None,
    ) -> None:
        _validate_rabbitmq_target(url, app_env)
        self._url = url
        self._aio_pika_loader = aio_pika_loader or _load_aio_pika
        self._monotonic_clock = monotonic_clock or time.monotonic
        self._lock = asyncio.Lock()
        self._connection: Any | None = None
        self._channel: Any | None = None
        self._exchange: Any | None = None
        self._aio_pika: Any | None = None
        self._failure_cooldown_until: float | None = None
        self._closed = False

    def _raise_if_failure_cooldown_active_locked(self) -> None:
        cooldown_until = self._failure_cooldown_until
        if (
            cooldown_until is not None
            and self._monotonic_clock() < cooldown_until
        ):
            raise BrokerTransportError(_PUBLISHER_COOLDOWN_ERROR)

    def _record_transport_failure_locked(self) -> None:
        self._failure_cooldown_until = (
            self._monotonic_clock() + BROKER_FAILURE_COOLDOWN_SECONDS
        )

    def _clear_transport_failure_locked(self) -> None:
        self._failure_cooldown_until = None

    async def _start_locked(self) -> None:
        if self._exchange is not None:
            self._clear_transport_failure_locked()
            return
        self._raise_if_failure_cooldown_active_locked()
        aio_pika = self._aio_pika_loader()
        connection: Any | None = None
        transport_failed = False
        try:
            connection = await aio_pika.connect_robust(
                self._url,
                timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
            )
            channel = await connection.channel(
                publisher_confirms=True,
                on_return_raises=True,
            )
            exchange, _ = await _declare_topology(channel, aio_pika)
        except asyncio.CancelledError:
            await _close_after_cancellation(connection)
            raise
        except Exception:
            transport_failed = True
        if transport_failed:
            self._record_transport_failure_locked()
            await _close_quietly(connection)
            raise BrokerTransportError(
                "RabbitMQ wake-up publisher could not start"
            )
        self._connection = connection
        self._channel = channel
        self._exchange = exchange
        self._aio_pika = aio_pika
        self._clear_transport_failure_locked()

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise BrokerStateError("wake-up publisher is closed")
            await self._start_locked()

    async def publish_wakeup(self) -> None:
        async with self._lock:
            if self._closed:
                raise BrokerStateError("wake-up publisher is closed")
            await self._start_locked()
            transport_failed = False
            try:
                message = self._aio_pika.Message(
                    body=WAKEUP_HINT_BODY,
                    delivery_mode=self._aio_pika.DeliveryMode.PERSISTENT,
                    content_type="application/json",
                    type=WAKEUP_MESSAGE_TYPE,
                )
                await self._exchange.publish(
                    message,
                    routing_key=WAKEUP_ROUTING_KEY,
                    mandatory=True,
                    timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                connection = self._connection
                self._connection = None
                self._channel = None
                self._exchange = None
                self._aio_pika = None
                await _close_after_cancellation(connection)
                raise
            except Exception:
                transport_failed = True
            if transport_failed:
                self._record_transport_failure_locked()
                await self._reset_locked()
                raise BrokerTransportError(
                    "RabbitMQ wake-up hint could not be published"
                )
            self._clear_transport_failure_locked()

    async def _reset_locked(self) -> None:
        connection = self._connection
        self._connection = None
        self._channel = None
        self._exchange = None
        self._aio_pika = None
        await _close_quietly(connection)

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            await self._reset_locked()


class RabbitMQWakeupSource:
    """Consume hints, manually acknowledge them, and coalesce duplicates."""

    def __init__(
        self,
        url: str,
        *,
        app_env: str,
        aio_pika_loader: AioPikaLoader | None = None,
    ) -> None:
        _validate_rabbitmq_target(url, app_env)
        self._url = url
        self._aio_pika_loader = aio_pika_loader or _load_aio_pika
        self._lock = asyncio.Lock()
        self._pending: asyncio.Queue[bool] = asyncio.Queue(maxsize=1)
        self._connection: Any | None = None
        self._channel: Any | None = None
        self._queue: Any | None = None
        self._consumer_tag: str | None = None
        self._started = False
        self._closed = False

    async def _on_message(self, message: Any) -> None:
        if message.body != WAKEUP_HINT_BODY:
            await message.reject(requeue=False)
            return
        if self._closed:
            await message.reject(requeue=True)
            return
        if self._pending.empty():
            self._pending.put_nowait(True)
        # Ack after the local hint is visible. A crash before ack causes a safe
        # duplicate delivery; the database claim remains authoritative.
        await message.ack()

    async def start(self) -> None:
        async with self._lock:
            if self._closed:
                raise BrokerStateError("wake-up source is closed")
            if self._started:
                return
            aio_pika = self._aio_pika_loader()
            connection: Any | None = None
            transport_failed = False
            try:
                connection = await aio_pika.connect_robust(
                    self._url,
                    timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
                )
                channel = await connection.channel(publisher_confirms=False)
                await channel.set_qos(
                    prefetch_count=1,
                    timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
                )
                _, queue = await _declare_topology(channel, aio_pika)
                consumer_tag = await queue.consume(
                    self._on_message,
                    no_ack=False,
                    timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
                )
            except asyncio.CancelledError:
                await _close_after_cancellation(connection)
                raise
            except Exception:
                transport_failed = True
            if transport_failed:
                await _close_quietly(connection)
                raise BrokerTransportError(
                    "RabbitMQ wake-up source could not start"
                )
            self._connection = connection
            self._channel = channel
            self._queue = queue
            self._consumer_tag = consumer_tag
            self._started = True

    async def wait(self, timeout: float | None = None) -> bool:
        _validate_timeout(timeout)
        if self._closed:
            return False
        if not self._started:
            raise BrokerStateError("wake-up source has not been started")
        try:
            if timeout is None:
                return await self._pending.get()
            if timeout == 0:
                return self._pending.get_nowait()
            return await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return False

    async def close(self) -> None:
        async with self._lock:
            if self._closed:
                return
            self._closed = True
            if self._queue is not None and self._consumer_tag is not None:
                with suppress(Exception):
                    await self._queue.cancel(
                        self._consumer_tag,
                        timeout=BROKER_OPERATION_TIMEOUT_SECONDS,
                    )
            await _close_quietly(self._connection)
            self._connection = None
            self._channel = None
            self._queue = None
            self._consumer_tag = None
            self._started = False
            with suppress(asyncio.QueueEmpty):
                self._pending.get_nowait()
            self._pending.put_nowait(False)


__all__ = [
    "AioPikaLoader",
    "BROKER_FAILURE_COOLDOWN_SECONDS",
    "BROKER_OPERATION_TIMEOUT_SECONDS",
    "MonotonicClock",
    "RabbitMQWakeupPublisher",
    "RabbitMQWakeupSource",
]
