from __future__ import annotations

import asyncio
import logging

import pytest

from app.core.config import Settings
from app.outbox.main import (
    OutboxDispatcherOptions,
    TaskWakeupOutboxDispatcher,
    build_outbox_dispatcher,
    build_outbox_dispatcher_options,
)


def test_outbox_options_use_explicit_or_container_identity() -> None:
    explicit = build_outbox_dispatcher_options(
        {
            "OUTBOX_DISPATCHER_ID": "outbox-explicit",
            "HOSTNAME": "ignored-hostname",
            "OUTBOX_LEASE_SECONDS": "60",
            "OUTBOX_POLL_SECONDS": "2.5",
        }
    )
    fallback = build_outbox_dispatcher_options({"HOSTNAME": "outbox-container"})
    local = build_outbox_dispatcher_options({})

    assert explicit == OutboxDispatcherOptions(
        dispatcher_id="outbox-explicit",
        lease_seconds=60,
        poll_seconds=2.5,
    )
    assert fallback.dispatcher_id == "outbox-container"
    assert local.dispatcher_id == "task-outbox-local"


def test_outbox_options_reject_invalid_identity_and_numbers() -> None:
    with pytest.raises(ValueError, match="dispatcher id"):
        OutboxDispatcherOptions("")
    with pytest.raises(RuntimeError, match="格式无效"):
        build_outbox_dispatcher_options({"OUTBOX_LEASE_SECONDS": "3.5"})
    with pytest.raises(ValueError, match="lease"):
        OutboxDispatcherOptions("outbox-a", lease_seconds=4)
    with pytest.raises(ValueError, match="poll"):
        OutboxDispatcherOptions("outbox-a", poll_seconds=0.01)


@pytest.mark.asyncio
async def test_independent_outbox_fails_closed_before_connecting_to_unsafe_modes() -> None:
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        await build_outbox_dispatcher(Settings())

    with pytest.raises(RuntimeError, match="postgres_local_container"):
        await build_outbox_dispatcher(Settings(app_env="local-container"))

    settings = Settings(
        app_env="local-container",
        database_runtime_mode="postgres_local_container",
        database_url="postgresql+asyncpg://qa:local-test@postgres:5432/qa",
        broker_runtime_mode="disabled_local",
        broker_url="",
    )
    with pytest.raises(RuntimeError, match="rabbitmq_local_container"):
        await build_outbox_dispatcher(settings)


class _FakeService:
    def __init__(self, publisher: _FakePublisher) -> None:
        self.publisher = publisher
        self.calls = 0
        self.observed = asyncio.Event()
        self.shutdown_called = False

    async def dispatch_task_wakeup_once(self, **kwargs) -> bool:
        assert kwargs["dispatcher_id"] == "outbox-a"
        assert kwargs["publisher"] is self.publisher
        assert kwargs["lease_seconds"] == 30
        self.calls += 1
        if self.calls == 1:
            raise RuntimeError("secret-task-payload-must-not-be-logged")
        self.observed.set()
        return False

    async def shutdown(self) -> None:
        self.shutdown_called = True


class _FakePublisher:
    def __init__(self) -> None:
        self.close_called = False

    async def close(self) -> None:
        self.close_called = True


class _FakeDatabase:
    def __init__(self) -> None:
        self.shutdown_called = False

    async def shutdown(self) -> None:
        self.shutdown_called = True

@pytest.mark.asyncio
async def test_outbox_runner_retries_without_logging_sensitive_error_text(
    caplog: pytest.LogCaptureFixture,
) -> None:
    publisher = _FakePublisher()
    service = _FakeService(publisher)
    database = _FakeDatabase()
    dispatcher = TaskWakeupOutboxDispatcher(
        service,  # type: ignore[arg-type]
        publisher,
        database,  # type: ignore[arg-type]
        OutboxDispatcherOptions(
            dispatcher_id="outbox-a",
            poll_seconds=0.05,
        ),
    )

    with caplog.at_level(logging.ERROR, logger="qa.task_wakeup_outbox"):
        operation = asyncio.create_task(dispatcher.run())
        try:
            await asyncio.wait_for(service.observed.wait(), timeout=1)
        finally:
            dispatcher.request_stop()
            await asyncio.wait_for(operation, timeout=1)

    assert service.calls >= 2
    assert service.shutdown_called is True
    assert publisher.close_called is True
    assert database.shutdown_called is True
    assert "RuntimeError" in caplog.text
    assert "secret-task-payload-must-not-be-logged" not in caplog.text
