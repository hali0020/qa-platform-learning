from __future__ import annotations

import asyncio
from collections import deque
from collections.abc import Awaitable, Callable, Mapping
from typing import Any

import pytest

from app.worker.contracts import ClaimedWork
from app.worker.handlers import FixedHandlerRegistry
from app.worker.runner import WorkerOptions, WorkerRunner


class FakeBackend:
    def __init__(self, claims: list[ClaimedWork | None]) -> None:
        self.claims = deque(claims)
        self.claim_count = 0
        self.heartbeats = 0
        self.cancel_on_heartbeat = False
        self.raise_on_heartbeat = False
        self.completed: list[tuple[str, dict[str, Any]]] = []
        self.failed: list[tuple[str, str, bool]] = []
        self.claimed = asyncio.Event()
        self.heartbeat_seen = asyncio.Event()
        self.terminal = asyncio.Event()

    async def claim(
        self,
        _worker_id: str,
        _queues: tuple[str, ...],
        _lease_seconds: int,
    ) -> ClaimedWork | None:
        self.claim_count += 1
        if not self.claims:
            return None
        work = self.claims.popleft()
        if work is not None:
            self.claimed.set()
        return work

    async def heartbeat(
        self,
        _work: ClaimedWork,
        _worker_id: str,
        _lease_seconds: int,
    ) -> bool:
        self.heartbeats += 1
        self.heartbeat_seen.set()
        if self.raise_on_heartbeat:
            raise RuntimeError("simulated lease loss")
        return self.cancel_on_heartbeat

    async def complete(
        self,
        work: ClaimedWork,
        _worker_id: str,
        result: dict[str, Any],
    ) -> None:
        self.completed.append((work.task_id, result))
        self.terminal.set()

    async def fail(
        self,
        work: ClaimedWork,
        _worker_id: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        self.failed.append((work.task_id, error_code, retryable))
        self.terminal.set()


class FakeWakeupSource:
    def __init__(self) -> None:
        self.started = False
        self.closed = False
        self.start_calls = 0
        self.start_failures_remaining = 0
        self.started_event = asyncio.Event()
        self._hint = asyncio.Event()

    async def start(self) -> None:
        self.start_calls += 1
        if self.start_failures_remaining:
            self.start_failures_remaining -= 1
            raise RuntimeError("sensitive broker startup detail")
        self.started = True
        self.started_event.set()

    async def wait(self, timeout_seconds: float) -> bool:
        try:
            await asyncio.wait_for(self._hint.wait(), timeout_seconds)
        except asyncio.TimeoutError:
            return False
        self._hint.clear()
        return True

    def hint(self) -> None:
        self._hint.set()

    async def close(self) -> None:
        self.closed = True


def work(task_type: str = "qa.test") -> ClaimedWork:
    return ClaimedWork(
        task_id="task-1",
        task_type=task_type,
        payload={},
        lease_token="lease-token-for-worker-tests",
    )


def options(**overrides: object) -> WorkerOptions:
    values: dict[str, object] = {
        "worker_id": "worker-test-1",
        "lease_seconds": 5,
        "heartbeat_interval_seconds": 0.01,
        "poll_interval_seconds": 0.05,
        "shutdown_grace_seconds": 0.1,
    }
    values.update(overrides)
    return WorkerOptions(**values)  # type: ignore[arg-type]


def registry(
    handler: Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]],
) -> FixedHandlerRegistry:
    return FixedHandlerRegistry({"qa.test": handler})


async def run_until_terminal(
    runner: WorkerRunner,
    backend: FakeBackend,
) -> None:
    running = asyncio.create_task(runner.run())
    await asyncio.wait_for(backend.terminal.wait(), 1)
    runner.request_stop()
    await asyncio.wait_for(running, 1)


@pytest.mark.asyncio
async def test_worker_heartbeats_and_completes_database_claim() -> None:
    backend = FakeBackend([work()])
    wakeup = FakeWakeupSource()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        await asyncio.sleep(0.035)
        return {"ok": True}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    await run_until_terminal(runner, backend)

    assert backend.heartbeats >= 2
    assert backend.completed == [("task-1", {"ok": True})]
    assert backend.failed == []
    assert wakeup.started is True
    assert wakeup.closed is True


@pytest.mark.asyncio
async def test_message_is_only_a_hint_and_claim_remains_authoritative() -> None:
    backend = FakeBackend([None, work()])
    wakeup = FakeWakeupSource()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"claimed_from": "database"}

    runner = WorkerRunner(
        backend,
        wakeup,
        registry(handler),
        options(poll_interval_seconds=1.0),
    )
    running = asyncio.create_task(runner.run())
    await asyncio.sleep(0)
    wakeup.hint()
    await asyncio.wait_for(backend.terminal.wait(), 0.2)
    runner.request_stop()
    await asyncio.wait_for(running, 1)

    assert backend.claim_count >= 2
    assert backend.completed[0][1] == {"claimed_from": "database"}


@pytest.mark.asyncio
async def test_periodic_polling_claims_work_when_no_message_arrives() -> None:
    backend = FakeBackend([None, work()])
    wakeup = FakeWakeupSource()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"fallback": "poll"}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    await run_until_terminal(runner, backend)

    assert backend.claim_count >= 2
    assert backend.completed[0][1] == {"fallback": "poll"}


@pytest.mark.asyncio
async def test_cancellation_seen_on_heartbeat_cooperatively_completes() -> None:
    backend = FakeBackend([work()])
    backend.cancel_on_heartbeat = True
    wakeup = FakeWakeupSource()
    handler_cancelled = asyncio.Event()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()
        return {"must_not": "complete"}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    await run_until_terminal(runner, backend)

    assert handler_cancelled.is_set()
    assert backend.completed == [("task-1", {})]
    assert backend.failed == []


@pytest.mark.asyncio
async def test_unknown_task_type_fails_without_dynamic_dispatch() -> None:
    backend = FakeBackend([work("package.module:function")])
    wakeup = FakeWakeupSource()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        raise AssertionError("unknown task must not select this handler")

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    await run_until_terminal(runner, backend)

    assert backend.completed == []
    assert backend.failed == [
        ("task-1", "worker_unknown_task_type", False)
    ]


@pytest.mark.asyncio
async def test_unexpected_handler_failure_is_retryable_and_sanitized() -> None:
    backend = FakeBackend([work()])
    wakeup = FakeWakeupSource()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        raise RuntimeError("do not persist this sensitive exception text")

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    await run_until_terminal(runner, backend)

    assert backend.failed == [("task-1", "worker_handler_error", True)]


@pytest.mark.asyncio
async def test_shutdown_waits_for_grace_then_requeues_work() -> None:
    backend = FakeBackend([work()])
    wakeup = FakeWakeupSource()
    handler_cancelled = asyncio.Event()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()
        return {}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    running = asyncio.create_task(runner.run())
    await asyncio.wait_for(backend.claimed.wait(), 1)
    runner.request_stop()
    await asyncio.wait_for(running, 1)

    assert handler_cancelled.is_set()
    assert backend.failed == [("task-1", "worker_shutdown", True)]


@pytest.mark.asyncio
async def test_heartbeat_failure_cancels_handler_without_terminal_write() -> None:
    backend = FakeBackend([work()])
    backend.raise_on_heartbeat = True
    wakeup = FakeWakeupSource()
    handler_cancelled = asyncio.Event()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()
        return {}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    running = asyncio.create_task(runner.run())
    await asyncio.wait_for(backend.heartbeat_seen.wait(), 1)
    await asyncio.wait_for(handler_cancelled.wait(), 1)
    runner.request_stop()
    await asyncio.wait_for(running, 1)

    assert backend.completed == []
    assert backend.failed == []


@pytest.mark.asyncio
async def test_external_task_cancellation_is_not_swallowed() -> None:
    backend = FakeBackend([work()])
    wakeup = FakeWakeupSource()
    handler_cancelled = asyncio.Event()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        try:
            await asyncio.Event().wait()
        finally:
            handler_cancelled.set()
        return {}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    running = asyncio.create_task(runner.run())
    await asyncio.wait_for(backend.claimed.wait(), 1)
    running.cancel()

    with pytest.raises(asyncio.CancelledError):
        await running
    assert handler_cancelled.is_set()
    assert wakeup.closed is True


@pytest.mark.asyncio
async def test_initial_wakeup_failures_retry_while_database_polling_continues(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = FakeBackend([])
    wakeup = FakeWakeupSource()
    wakeup.start_failures_remaining = 2

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        return {"recovered": True}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())
    running = asyncio.create_task(runner.run())
    await asyncio.wait_for(wakeup.started_event.wait(), 1)
    claims_before_hint = backend.claim_count
    backend.claims.append(work())
    wakeup.hint()
    await asyncio.wait_for(backend.terminal.wait(), 1)
    runner.request_stop()
    await asyncio.wait_for(running, 1)

    assert wakeup.start_calls == 3
    assert claims_before_hint >= 3
    assert backend.completed == [("task-1", {"recovered": True})]
    assert "sensitive broker startup detail" not in caplog.text


def test_wakeup_retry_delay_is_exponential_and_bounded() -> None:
    backend = FakeBackend([])
    wakeup = FakeWakeupSource()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        return {}

    runner = WorkerRunner(
        backend,
        wakeup,
        registry(handler),
        options(poll_interval_seconds=1.0),
    )

    assert [runner._bounded_wakeup_retry_delay(value) for value in (1, 2, 3)] == [
        1.0,
        2.0,
        4.0,
    ]
    assert runner._bounded_wakeup_retry_delay(100) == 30.0


@pytest.mark.asyncio
async def test_cancelled_error_from_wakeup_start_is_not_swallowed() -> None:
    backend = FakeBackend([None])

    class CancelledStartSource(FakeWakeupSource):
        async def start(self) -> None:
            raise asyncio.CancelledError

    wakeup = CancelledStartSource()

    async def handler(_payload: Mapping[str, Any]) -> dict[str, Any]:
        return {}

    runner = WorkerRunner(backend, wakeup, registry(handler), options())

    with pytest.raises(asyncio.CancelledError):
        await asyncio.wait_for(runner.run(), 1)
    assert wakeup.closed is True
