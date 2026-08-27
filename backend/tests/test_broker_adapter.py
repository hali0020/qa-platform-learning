from __future__ import annotations

import asyncio
import json
from typing import Any

import pytest

import app.broker.rabbitmq as rabbitmq_module
from app.broker import (
    BrokerDependencyError,
    BrokerStateError,
    BrokerTransportError,
    DisabledLocalWakeupBroker,
    FakeWakeupBroker,
    RabbitMQWakeupPublisher,
    RabbitMQWakeupSource,
    WakeupPublisher,
    WakeupSource,
    build_wakeup_publisher,
    build_wakeup_source,
)
from app.broker.messages import (
    WAKEUP_EXCHANGE_NAME,
    WAKEUP_HINT_BODY,
    WAKEUP_MESSAGE_TYPE,
    WAKEUP_QUEUE_NAME,
    WAKEUP_ROUTING_KEY,
)
from app.broker.rabbitmq import (
    BROKER_FAILURE_COOLDOWN_SECONDS,
    BROKER_OPERATION_TIMEOUT_SECONDS,
)
from app.core.config import Settings


VALID_BROKER_URL = (
    "amqp://qa_learning:lesson-secret@rabbitmq:5672/qa_platform_learning"
)


class _FakeMonotonicClock:
    def __init__(self, value: float = 100.0) -> None:
        self.value = value

    def __call__(self) -> float:
        return self.value

    def advance(self, seconds: float) -> None:
        self.value += seconds


class _FakeDeliveryMode:
    PERSISTENT = "persistent"


class _FakeExchangeType:
    DIRECT = "direct"


class _FakeMessage:
    def __init__(self, **values: Any) -> None:
        self.__dict__.update(values)


class _FakeIncomingMessage:
    def __init__(self, body: bytes) -> None:
        self.body = body
        self.ack_count = 0
        self.reject_calls: list[dict[str, Any]] = []

    async def ack(self) -> None:
        self.ack_count += 1

    async def reject(self, **values: Any) -> None:
        self.reject_calls.append(values)


class _FakeExchange:
    def __init__(self, owner: _FakeAioPika) -> None:
        self.owner = owner
        self.publish_calls: list[tuple[_FakeMessage, dict[str, Any]]] = []
        self.publish_error: Exception | None = None

    async def publish(self, message: _FakeMessage, **values: Any) -> None:
        if self.owner.block_publish:
            self.owner.publish_started.set()
            await self.owner.publish_release.wait()
        if self.publish_error is not None:
            raise self.publish_error
        self.publish_calls.append((message, values))


class _FakeQueue:
    def __init__(self, owner: _FakeAioPika) -> None:
        self.owner = owner
        self.bind_calls: list[tuple[_FakeExchange, dict[str, Any]]] = []
        self.consume_calls: list[tuple[Any, dict[str, Any]]] = []
        self.cancel_calls: list[tuple[str, dict[str, Any]]] = []

    async def bind(self, exchange: _FakeExchange, **values: Any) -> None:
        self.bind_calls.append((exchange, values))

    async def consume(self, callback: Any, **values: Any) -> str:
        self.consume_calls.append((callback, values))
        if self.owner.block_consume:
            self.owner.operation_started.set()
            await self.owner.operation_release.wait()
        return "consumer-tag"

    async def cancel(self, consumer_tag: str, **values: Any) -> None:
        self.cancel_calls.append((consumer_tag, values))


class _FakeChannel:
    def __init__(
        self,
        channel_options: dict[str, Any],
        owner: _FakeAioPika,
    ) -> None:
        self.channel_options = channel_options
        self.owner = owner
        self.exchange = _FakeExchange(owner)
        self.queue = _FakeQueue(owner)
        self.exchange_declarations: list[tuple[str, Any, dict[str, Any]]] = []
        self.queue_declarations: list[tuple[str, dict[str, Any]]] = []
        self.qos_calls: list[dict[str, Any]] = []

    async def declare_exchange(
        self,
        name: str,
        exchange_type: Any,
        **values: Any,
    ) -> _FakeExchange:
        self.exchange_declarations.append((name, exchange_type, values))
        return self.exchange

    async def declare_queue(self, name: str, **values: Any) -> _FakeQueue:
        self.queue_declarations.append((name, values))
        if self.owner.block_declare_queue:
            self.owner.operation_started.set()
            await self.owner.operation_release.wait()
        return self.queue

    async def set_qos(self, **values: Any) -> None:
        self.qos_calls.append(values)


class _FakeConnection:
    def __init__(self, owner: _FakeAioPika) -> None:
        self.owner = owner
        self.channels: list[_FakeChannel] = []
        self.close_count = 0

    async def channel(self, **values: Any) -> _FakeChannel:
        channel = _FakeChannel(values, self.owner)
        self.channels.append(channel)
        return channel

    async def close(self) -> None:
        self.close_count += 1
        if self.owner.block_close:
            self.owner.close_started.set()
            await self.owner.close_release.wait()


class _FakeAioPika:
    DeliveryMode = _FakeDeliveryMode
    ExchangeType = _FakeExchangeType
    Message = _FakeMessage

    def __init__(self) -> None:
        self.connect_calls: list[tuple[str, dict[str, Any]]] = []
        self.connections: list[_FakeConnection] = []
        self.connect_error: Exception | None = None
        self.block_connect = False
        self.block_publish = False
        self.block_declare_queue = False
        self.block_consume = False
        self.block_close = False
        self.connect_started = asyncio.Event()
        self.connect_release = asyncio.Event()
        self.publish_started = asyncio.Event()
        self.publish_release = asyncio.Event()
        self.close_started = asyncio.Event()
        self.close_release = asyncio.Event()
        self.operation_started = asyncio.Event()
        self.operation_release = asyncio.Event()

    async def connect_robust(
        self,
        url: str,
        **values: Any,
    ) -> _FakeConnection:
        self.connect_calls.append((url, values))
        if self.block_connect:
            self.connect_started.set()
            await self.connect_release.wait()
        if self.connect_error is not None:
            raise self.connect_error
        connection = _FakeConnection(self)
        self.connections.append(connection)
        return connection


def _rabbitmq_settings() -> Settings:
    return Settings(
        app_env="local-container",
        broker_runtime_mode="rabbitmq_local_container",
        broker_url=VALID_BROKER_URL,
    )


@pytest.mark.asyncio
async def test_disabled_factory_never_loads_aio_pika_or_opens_network() -> None:
    loader_calls = 0

    def fail_if_loaded() -> Any:
        nonlocal loader_calls
        loader_calls += 1
        raise AssertionError("disabled broker must not load aio-pika")

    publisher = build_wakeup_publisher(Settings(), aio_pika_loader=fail_if_loaded)
    source = build_wakeup_source(Settings(), aio_pika_loader=fail_if_loaded)

    assert isinstance(publisher, DisabledLocalWakeupBroker)
    assert isinstance(source, DisabledLocalWakeupBroker)
    await publisher.start()
    await publisher.publish_wakeup()
    await source.start()
    assert await source.wait(timeout=0) is False
    await publisher.close()
    await source.close()
    assert loader_calls == 0


@pytest.mark.asyncio
async def test_fake_is_injectable_and_coalesces_duplicate_hints() -> None:
    broker = FakeWakeupBroker()

    assert isinstance(broker, WakeupPublisher)
    assert isinstance(broker, WakeupSource)
    await broker.start()
    await broker.publish_wakeup()
    await broker.publish_wakeup()

    assert broker.publish_count == 2
    assert await broker.wait(timeout=0) is True
    assert await broker.wait(timeout=0) is False

    await broker.close()
    assert await broker.wait(timeout=0) is False


@pytest.mark.asyncio
async def test_fake_can_inject_publish_failure_without_a_message_payload() -> None:
    expected = BrokerTransportError("injected")
    broker = FakeWakeupBroker(publish_error=expected)

    with pytest.raises(BrokerTransportError) as caught:
        await broker.publish_wakeup()

    assert caught.value is expected
    assert broker.publish_count == 0


@pytest.mark.asyncio
async def test_rabbitmq_publisher_is_lazy_and_publishes_only_fixed_durable_hint() -> None:
    aio_pika = _FakeAioPika()
    loader_calls = 0

    def load() -> _FakeAioPika:
        nonlocal loader_calls
        loader_calls += 1
        return aio_pika

    publisher = build_wakeup_publisher(
        _rabbitmq_settings(),
        aio_pika_loader=load,
    )

    assert isinstance(publisher, RabbitMQWakeupPublisher)
    assert loader_calls == 0
    assert aio_pika.connect_calls == []

    await publisher.publish_wakeup()
    await publisher.start()
    await publisher.start()

    assert loader_calls == 1
    assert aio_pika.connect_calls == [
        (VALID_BROKER_URL, {"timeout": BROKER_OPERATION_TIMEOUT_SECONDS})
    ]
    connection = aio_pika.connections[0]
    channel = connection.channels[0]
    assert channel.channel_options == {
        "publisher_confirms": True,
        "on_return_raises": True,
    }
    assert channel.exchange_declarations == [
        (
            WAKEUP_EXCHANGE_NAME,
            _FakeExchangeType.DIRECT,
            {
                "durable": True,
                "auto_delete": False,
                "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
            },
        )
    ]
    assert channel.queue_declarations == [
        (
            WAKEUP_QUEUE_NAME,
            {
                "durable": True,
                "auto_delete": False,
                "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
            },
        )
    ]
    assert channel.queue.bind_calls == [
        (
            channel.exchange,
            {
                "routing_key": WAKEUP_ROUTING_KEY,
                "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
            },
        )
    ]

    [(message, publish_options)] = channel.exchange.publish_calls
    assert message.body == WAKEUP_HINT_BODY
    assert json.loads(message.body) == {
        "kind": "automation_task_available",
        "version": 1,
    }
    assert set(message.__dict__) == {
        "body",
        "content_type",
        "delivery_mode",
        "type",
    }
    assert message.delivery_mode == _FakeDeliveryMode.PERSISTENT
    assert message.content_type == "application/json"
    assert message.type == WAKEUP_MESSAGE_TYPE
    assert publish_options == {
        "routing_key": WAKEUP_ROUTING_KEY,
        "mandatory": True,
        "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
    }

    await publisher.close()
    await publisher.close()
    assert connection.close_count == 1
    with pytest.raises(BrokerStateError):
        await publisher.publish_wakeup()


@pytest.mark.asyncio
async def test_publisher_transport_error_does_not_leak_broker_url_or_secret() -> None:
    aio_pika = _FakeAioPika()
    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )
    await publisher.start()
    channel = aio_pika.connections[0].channels[0]
    channel.exchange.publish_error = RuntimeError(
        f"connection failed for {VALID_BROKER_URL}"
    )

    with pytest.raises(BrokerTransportError) as caught:
        await publisher.publish_wakeup()

    assert "lesson-secret" not in str(caught.value)
    assert VALID_BROKER_URL not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None
    assert aio_pika.connections[0].close_count == 1


@pytest.mark.asyncio
async def test_publisher_start_error_has_no_secret_exception_context() -> None:
    aio_pika = _FakeAioPika()
    aio_pika.connect_error = RuntimeError(
        f"connection failed for {VALID_BROKER_URL}"
    )
    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )

    with pytest.raises(BrokerTransportError) as caught:
        await publisher.start()

    assert "lesson-secret" not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_concurrent_publish_failures_use_one_connect_and_cooldown() -> None:
    aio_pika = _FakeAioPika()
    aio_pika.block_connect = True
    aio_pika.connect_error = RuntimeError(
        f"connection failed for {VALID_BROKER_URL}"
    )
    clock = _FakeMonotonicClock()
    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
        monotonic_clock=clock,
    )

    publish_tasks = [
        asyncio.create_task(publisher.publish_wakeup()) for _ in range(20)
    ]
    await asyncio.wait_for(aio_pika.connect_started.wait(), timeout=1)
    await asyncio.sleep(0)
    assert len(aio_pika.connect_calls) == 1

    aio_pika.connect_release.set()
    results = await asyncio.wait_for(
        asyncio.gather(*publish_tasks, return_exceptions=True),
        timeout=0.5,
    )

    assert len(aio_pika.connect_calls) == 1
    assert "lesson-secret" not in repr(publisher)
    for result in results:
        assert isinstance(result, BrokerTransportError)
        assert "lesson-secret" not in str(result)
        assert VALID_BROKER_URL not in str(result)
        assert result.__cause__ is None
        assert result.__context__ is None

    with pytest.raises(BrokerTransportError) as cooldown_error:
        await asyncio.wait_for(publisher.publish_wakeup(), timeout=0.05)
    assert cooldown_error.value.__cause__ is None
    assert cooldown_error.value.__context__ is None
    assert len(aio_pika.connect_calls) == 1

    # At the exact deadline, one caller becomes the half-open retry. Its
    # failure opens a fresh cooldown before the other callers take the lock.
    clock.advance(BROKER_FAILURE_COOLDOWN_SECONDS)
    aio_pika.block_connect = False
    retry_results = await asyncio.gather(
        *(publisher.publish_wakeup() for _ in range(20)),
        return_exceptions=True,
    )
    assert len(aio_pika.connect_calls) == 2
    assert all(
        isinstance(result, BrokerTransportError) for result in retry_results
    )

    # A successful half-open retry clears the failure state and reuses the
    # healthy connection for later publishes.
    clock.advance(BROKER_FAILURE_COOLDOWN_SECONDS)
    aio_pika.connect_error = None
    await publisher.publish_wakeup()
    await publisher.publish_wakeup()
    assert len(aio_pika.connect_calls) == 3
    successful_connection = aio_pika.connections[0]
    assert len(successful_connection.channels[0].exchange.publish_calls) == 2
    await publisher.close()


@pytest.mark.asyncio
async def test_connected_publish_failure_short_circuits_concurrent_followups() -> None:
    aio_pika = _FakeAioPika()
    clock = _FakeMonotonicClock()
    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
        monotonic_clock=clock,
    )
    await publisher.start()
    first_connection = aio_pika.connections[0]
    first_connection.channels[0].exchange.publish_error = RuntimeError(
        f"publish failed for {VALID_BROKER_URL}"
    )

    with pytest.raises(BrokerTransportError):
        await publisher.publish_wakeup()

    cooldown_results = await asyncio.gather(
        *(publisher.publish_wakeup() for _ in range(20)),
        return_exceptions=True,
    )
    assert len(aio_pika.connect_calls) == 1
    assert first_connection.close_count == 1
    assert all(
        isinstance(result, BrokerTransportError)
        and result.__cause__ is None
        and result.__context__ is None
        for result in cooldown_results
    )

    clock.advance(BROKER_FAILURE_COOLDOWN_SECONDS)
    await asyncio.gather(*(publisher.publish_wakeup() for _ in range(20)))
    assert len(aio_pika.connect_calls) == 2
    retry_connection = aio_pika.connections[1]
    assert len(retry_connection.channels[0].exchange.publish_calls) == 20
    await publisher.close()


@pytest.mark.asyncio
async def test_cancelled_publisher_start_closes_partial_connection_and_can_retry() -> None:
    aio_pika = _FakeAioPika()
    aio_pika.block_declare_queue = True
    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )
    start_task = asyncio.create_task(publisher.start())
    await asyncio.wait_for(aio_pika.operation_started.wait(), timeout=1)
    first_connection = aio_pika.connections[0]

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert first_connection.close_count == 1
    aio_pika.block_declare_queue = False
    await publisher.start()
    assert len(aio_pika.connections) == 2
    await publisher.close()


@pytest.mark.asyncio
async def test_cancelled_publish_does_not_open_failure_cooldown() -> None:
    aio_pika = _FakeAioPika()
    clock = _FakeMonotonicClock()
    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
        monotonic_clock=clock,
    )
    await publisher.start()
    aio_pika.block_publish = True
    publish_task = asyncio.create_task(publisher.publish_wakeup())
    await asyncio.wait_for(aio_pika.publish_started.wait(), timeout=1)
    first_connection = aio_pika.connections[0]

    publish_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await publish_task

    assert first_connection.close_count == 1
    aio_pika.block_publish = False
    await publisher.publish_wakeup()
    assert len(aio_pika.connect_calls) == 2
    await publisher.close()


@pytest.mark.asyncio
async def test_publisher_close_keeps_cleanup_bounded(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    assert BROKER_OPERATION_TIMEOUT_SECONDS == 2.0
    aio_pika = _FakeAioPika()
    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )
    await publisher.start()
    connection = aio_pika.connections[0]
    aio_pika.block_close = True
    monkeypatch.setattr(
        rabbitmq_module,
        "BROKER_OPERATION_TIMEOUT_SECONDS",
        0.01,
    )

    await asyncio.wait_for(publisher.close(), timeout=0.25)

    assert connection.close_count == 1
    assert aio_pika.close_started.is_set()


@pytest.mark.asyncio
async def test_enabled_adapter_imports_aio_pika_only_on_start() -> None:
    loader_calls = 0

    def missing_dependency() -> Any:
        nonlocal loader_calls
        loader_calls += 1
        raise BrokerDependencyError("missing")

    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=missing_dependency,
    )
    assert loader_calls == 0

    with pytest.raises(BrokerDependencyError):
        await publisher.start()

    assert loader_calls == 1


@pytest.mark.asyncio
async def test_unused_publisher_closes_without_loading_aio_pika() -> None:
    loader_calls = 0

    def fail_if_loaded() -> Any:
        nonlocal loader_calls
        loader_calls += 1
        raise AssertionError("unused publisher must remain lazy")

    publisher = RabbitMQWakeupPublisher(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=fail_if_loaded,
    )

    await publisher.close()

    assert loader_calls == 0


@pytest.mark.asyncio
async def test_rabbitmq_source_manually_acks_valid_hints_and_coalesces_duplicates() -> None:
    aio_pika = _FakeAioPika()
    source = build_wakeup_source(
        _rabbitmq_settings(),
        aio_pika_loader=lambda: aio_pika,
    )

    assert isinstance(source, RabbitMQWakeupSource)
    await source.start()
    await source.start()

    connection = aio_pika.connections[0]
    channel = connection.channels[0]
    assert channel.channel_options == {"publisher_confirms": False}
    assert channel.qos_calls == [
        {
            "prefetch_count": 1,
            "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
        }
    ]
    assert channel.exchange_declarations[0][2] == {
        "durable": True,
        "auto_delete": False,
        "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
    }
    assert channel.queue_declarations == [
        (
            WAKEUP_QUEUE_NAME,
            {
                "durable": True,
                "auto_delete": False,
                "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
            },
        )
    ]
    [(callback, consume_options)] = channel.queue.consume_calls
    assert consume_options == {
        "no_ack": False,
        "timeout": BROKER_OPERATION_TIMEOUT_SECONDS,
    }

    first = _FakeIncomingMessage(WAKEUP_HINT_BODY)
    duplicate = _FakeIncomingMessage(WAKEUP_HINT_BODY)
    await callback(first)
    await callback(duplicate)

    assert first.ack_count == 1
    assert duplicate.ack_count == 1
    assert first.reject_calls == []
    assert duplicate.reject_calls == []
    assert await source.wait(timeout=0) is True
    assert await source.wait(timeout=0) is False

    await source.close()
    assert channel.queue.cancel_calls == [
        (
            "consumer-tag",
            {"timeout": BROKER_OPERATION_TIMEOUT_SECONDS},
        )
    ]
    assert connection.close_count == 1


@pytest.mark.asyncio
async def test_rabbitmq_source_rejects_unknown_message_without_waking_worker() -> None:
    aio_pika = _FakeAioPika()
    source = RabbitMQWakeupSource(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )
    await source.start()
    channel = aio_pika.connections[0].channels[0]
    callback = channel.queue.consume_calls[0][0]
    message = _FakeIncomingMessage(b'{"command":"run arbitrary payload"}')

    await callback(message)

    assert message.ack_count == 0
    assert message.reject_calls == [{"requeue": False}]
    assert await source.wait(timeout=0) is False
    await source.close()


@pytest.mark.asyncio
async def test_source_start_error_does_not_leak_broker_url_or_secret() -> None:
    aio_pika = _FakeAioPika()
    aio_pika.connect_error = RuntimeError(
        f"connection failed for {VALID_BROKER_URL}"
    )
    source = RabbitMQWakeupSource(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )

    with pytest.raises(BrokerTransportError) as caught:
        await source.start()

    assert "lesson-secret" not in str(caught.value)
    assert VALID_BROKER_URL not in str(caught.value)
    assert caught.value.__cause__ is None
    assert caught.value.__context__ is None


@pytest.mark.asyncio
async def test_cancelled_source_start_closes_partial_consumer_connection() -> None:
    aio_pika = _FakeAioPika()
    aio_pika.block_consume = True
    source = RabbitMQWakeupSource(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )
    start_task = asyncio.create_task(source.start())
    await asyncio.wait_for(aio_pika.operation_started.wait(), timeout=1)
    first_connection = aio_pika.connections[0]

    start_task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await start_task

    assert first_connection.close_count == 1
    aio_pika.block_consume = False
    await source.start()
    assert len(aio_pika.connections) == 2
    await source.close()


@pytest.mark.asyncio
async def test_message_arriving_after_source_close_is_requeued_without_wakeup() -> None:
    aio_pika = _FakeAioPika()
    source = RabbitMQWakeupSource(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )
    await source.start()
    callback = aio_pika.connections[0].channels[0].queue.consume_calls[0][0]
    await source.close()
    message = _FakeIncomingMessage(WAKEUP_HINT_BODY)

    await callback(message)

    assert message.ack_count == 0
    assert message.reject_calls == [{"requeue": True}]
    assert await source.wait(timeout=0) is False


@pytest.mark.asyncio
async def test_source_wait_requires_start_and_close_unblocks_waiter() -> None:
    aio_pika = _FakeAioPika()
    source = RabbitMQWakeupSource(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: aio_pika,
    )

    with pytest.raises(BrokerStateError):
        await source.wait(timeout=0)
    with pytest.raises(ValueError):
        await source.wait(timeout=-1)
    with pytest.raises(ValueError):
        await source.wait(timeout=float("nan"))
    with pytest.raises(ValueError):
        await source.wait(timeout=float("inf"))

    await source.start()
    waiter = asyncio.create_task(source.wait())
    await asyncio.sleep(0)
    await source.close()

    assert await asyncio.wait_for(waiter, timeout=1) is False
    assert await source.wait(timeout=0) is False


def test_direct_rabbitmq_adapter_construction_rejects_remote_url() -> None:
    remote_url = (
        "amqp://test_user:test_password@broker.example.test:5672/"
        "qa_platform_learning"
    )

    with pytest.raises(RuntimeError, match="rabbitmq:5672"):
        RabbitMQWakeupPublisher(
            remote_url,
            app_env="local-container",
            aio_pika_loader=lambda: None,
        )
    with pytest.raises(RuntimeError, match="rabbitmq:5672"):
        RabbitMQWakeupSource(
            remote_url,
            app_env="local-container",
            aio_pika_loader=lambda: None,
        )


def test_direct_rabbitmq_adapter_requires_local_container_environment() -> None:
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        RabbitMQWakeupPublisher(
            VALID_BROKER_URL,
            app_env="local",
            aio_pika_loader=lambda: None,
        )
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        RabbitMQWakeupSource(
            VALID_BROKER_URL,
            app_env="test",
            aio_pika_loader=lambda: None,
        )


@pytest.mark.asyncio
async def test_all_sources_return_false_when_closed_before_start() -> None:
    disabled = DisabledLocalWakeupBroker()
    fake = FakeWakeupBroker()
    rabbitmq = RabbitMQWakeupSource(
        VALID_BROKER_URL,
        app_env="local-container",
        aio_pika_loader=lambda: None,
    )

    for source in (disabled, fake, rabbitmq):
        await source.close()
        assert await source.wait(timeout=0) is False


@pytest.mark.asyncio
async def test_close_discards_a_pending_fake_hint() -> None:
    broker = FakeWakeupBroker()
    await broker.start()
    await broker.publish_wakeup()

    await broker.close()

    assert await broker.wait(timeout=0) is False
