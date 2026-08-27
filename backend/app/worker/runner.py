"""Single-concurrency worker loop with DB-authoritative leases."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from app.worker.contracts import ClaimedWork, TaskLeaseBackend, WakeupSource
from app.worker.handlers import FixedHandlerRegistry, HandlerFailure


@dataclass(frozen=True, slots=True)
class WorkerOptions:
    worker_id: str
    queues: tuple[str, ...] = ("default",)
    lease_seconds: int = 30
    heartbeat_interval_seconds: float = 10.0
    poll_interval_seconds: float = 5.0
    shutdown_grace_seconds: float = 30.0

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or len(self.worker_id) > 200:
            raise ValueError("worker_id must contain 1-200 characters")
        if (
            not self.queues
            or len(self.queues) > 20
            or any(not queue.strip() or len(queue) > 100 for queue in self.queues)
            or len(set(self.queues)) != len(self.queues)
        ):
            raise ValueError("worker queues must be non-empty and unique")
        if not 5 <= self.lease_seconds <= 3_600:
            raise ValueError("worker lease must be between 5 and 3600 seconds")
        if not 0 < self.heartbeat_interval_seconds < self.lease_seconds:
            raise ValueError("heartbeat interval must be shorter than the lease")
        if not 0.05 <= self.poll_interval_seconds <= 60:
            raise ValueError("poll interval must be between 0.05 and 60 seconds")
        if not 0.1 <= self.shutdown_grace_seconds <= 30:
            raise ValueError("shutdown grace must be between 0.1 and 30 seconds")


class WorkerRunner:
    def __init__(
        self,
        backend: TaskLeaseBackend,
        wakeup_source: WakeupSource,
        handlers: FixedHandlerRegistry,
        options: WorkerOptions,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._backend = backend
        self._wakeup_source = wakeup_source
        self._handlers = handlers
        self.options = options
        self._stop = asyncio.Event()
        self._logger = logger or logging.getLogger("qa.worker")
        self._wakeup_started = False
        self._wakeup_start_task: asyncio.Task[None] | None = None
        self._wakeup_start_failures = 0
        self._next_wakeup_start_at = 0.0

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                self._observe_wakeup_start()
                self._schedule_wakeup_start_if_due()
                try:
                    work = await self._backend.claim(
                        self.options.worker_id,
                        self.options.queues,
                        self.options.lease_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._log_boundary_failure("claim", error)
                    await self._wait_for_hint_or_poll()
                    continue

                if work is None:
                    await self._wait_for_hint_or_poll()
                    continue
                await self._execute(work)
        finally:
            if self._wakeup_start_task is not None:
                if not self._wakeup_start_task.done():
                    self._wakeup_start_task.cancel()
                await asyncio.gather(
                    self._wakeup_start_task,
                    return_exceptions=True,
                )
                self._wakeup_start_task = None
            try:
                await self._wakeup_source.close()
            except asyncio.CancelledError:
                raise
            except Exception as error:
                self._log_boundary_failure("wakeup_close", error)

    async def _wait_for_hint_or_poll(self) -> None:
        if self._stop.is_set():
            return
        stop_wait = asyncio.create_task(self._stop.wait())
        if self._wakeup_started:
            wakeup_wait = asyncio.create_task(
                self._wakeup_source.wait(self.options.poll_interval_seconds)
            )
        else:
            wakeup_wait = asyncio.create_task(
                asyncio.sleep(self.options.poll_interval_seconds, result=False)
            )
        waiters = {stop_wait, wakeup_wait}
        start_wait = self._wakeup_start_task
        if start_wait is not None:
            waiters.add(start_wait)
        try:
            done, pending = await asyncio.wait(
                waiters,
                return_when=asyncio.FIRST_COMPLETED,
            )
            for task in pending:
                if task is start_wait:
                    continue
                task.cancel()
            await asyncio.gather(
                *(task for task in pending if task is not start_wait),
                return_exceptions=True,
            )
            if start_wait is not None and start_wait in done:
                self._observe_wakeup_start()
            if wakeup_wait in done:
                try:
                    wakeup_wait.result()
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    self._log_boundary_failure("wakeup_wait", error)
        finally:
            for task in (stop_wait, wakeup_wait):
                if not task.done():
                    task.cancel()
            await asyncio.gather(stop_wait, wakeup_wait, return_exceptions=True)

    def _schedule_wakeup_start_if_due(self) -> None:
        if self._wakeup_started or self._wakeup_start_task is not None:
            return
        loop = asyncio.get_running_loop()
        if loop.time() < self._next_wakeup_start_at:
            return
        self._wakeup_start_task = asyncio.create_task(
            self._wakeup_source.start()
        )

    def _observe_wakeup_start(self) -> None:
        task = self._wakeup_start_task
        if task is None or not task.done():
            return
        self._wakeup_start_task = None
        try:
            task.result()
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._wakeup_start_failures += 1
            delay = self._bounded_wakeup_retry_delay(
                self._wakeup_start_failures
            )
            self._next_wakeup_start_at = asyncio.get_running_loop().time() + delay
            self._log_boundary_failure("wakeup_start", error)
        else:
            self._wakeup_started = True
            self._wakeup_start_failures = 0
            self._next_wakeup_start_at = 0.0

    def _bounded_wakeup_retry_delay(self, failures: int) -> float:
        exponent = min(max(0, failures - 1), 10)
        return min(
            30.0,
            self.options.poll_interval_seconds * (2**exponent),
        )

    async def _execute(self, work: ClaimedWork) -> None:
        try:
            handler = self._handlers.resolve(work.task_type)
        except HandlerFailure as error:
            await self._record_failure(work, error.error_code, error.retryable)
            return

        async def invoke_handler() -> dict[str, Any]:
            result = await handler(work.payload)
            if not isinstance(result, dict):
                raise HandlerFailure("worker_invalid_result", retryable=False)
            return result

        handler_task = asyncio.create_task(invoke_handler())
        heartbeat_done = asyncio.Event()
        heartbeat_task = asyncio.create_task(
            self._heartbeat_loop(work, heartbeat_done)
        )
        stop_task = asyncio.create_task(self._stop.wait())
        shutdown_deadline: float | None = None
        try:
            while True:
                timeout: float | None = None
                waiters: set[asyncio.Task[Any]] = {handler_task, heartbeat_task}
                if shutdown_deadline is None:
                    waiters.add(stop_task)
                else:
                    timeout = max(0.0, shutdown_deadline - asyncio.get_running_loop().time())

                done, _ = await asyncio.wait(
                    waiters,
                    timeout=timeout,
                    return_when=asyncio.FIRST_COMPLETED,
                )
                if not done:
                    handler_task.cancel()
                    await asyncio.gather(handler_task, return_exceptions=True)
                    await self._record_failure(
                        work,
                        "worker_shutdown",
                        retryable=True,
                    )
                    return

                if heartbeat_task in done:
                    heartbeat_error = heartbeat_task.exception()
                    if heartbeat_error is not None:
                        self._log_boundary_failure("heartbeat", heartbeat_error)
                        handler_task.cancel()
                        await asyncio.gather(handler_task, return_exceptions=True)
                        # Lease ownership is uncertain.  Do not write a terminal
                        # state; expiry recovery will make the task claimable.
                        return
                    if heartbeat_task.result():
                        handler_task.cancel()
                        await asyncio.gather(handler_task, return_exceptions=True)
                        # complete_task observes cancel_requested and records the
                        # authoritative cancelled terminal state without a result.
                        await self._record_completion(work, {})
                        return

                if handler_task in done:
                    try:
                        result = handler_task.result()
                    except asyncio.CancelledError:
                        await self._record_failure(
                            work,
                            "worker_shutdown",
                            retryable=True,
                        )
                    except HandlerFailure as error:
                        await self._record_failure(
                            work,
                            error.error_code,
                            error.retryable,
                        )
                    except Exception as error:
                        self._log_boundary_failure("handler", error)
                        await self._record_failure(
                            work,
                            "worker_handler_error",
                            retryable=True,
                        )
                    else:
                        await self._record_completion(work, result)
                    return

                if stop_task in done and shutdown_deadline is None:
                    shutdown_deadline = (
                        asyncio.get_running_loop().time()
                        + self.options.shutdown_grace_seconds
                    )
        finally:
            heartbeat_done.set()
            for task in (handler_task, heartbeat_task, stop_task):
                if not task.done():
                    task.cancel()
            await asyncio.gather(
                handler_task,
                heartbeat_task,
                stop_task,
                return_exceptions=True,
            )

    async def _heartbeat_loop(
        self,
        work: ClaimedWork,
        done: asyncio.Event,
    ) -> bool:
        while True:
            try:
                await asyncio.wait_for(
                    done.wait(),
                    timeout=self.options.heartbeat_interval_seconds,
                )
                return False
            except asyncio.TimeoutError:
                cancel_requested = await self._backend.heartbeat(
                    work,
                    self.options.worker_id,
                    self.options.lease_seconds,
                )
                if cancel_requested:
                    return True

    async def _record_completion(
        self,
        work: ClaimedWork,
        result: dict[str, Any],
    ) -> None:
        try:
            await self._backend.complete(work, self.options.worker_id, result)
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log_boundary_failure("complete", error)

    async def _record_failure(
        self,
        work: ClaimedWork,
        error_code: str,
        retryable: bool,
    ) -> None:
        try:
            await self._backend.fail(
                work,
                self.options.worker_id,
                error_code,
                retryable,
            )
        except asyncio.CancelledError:
            raise
        except Exception as error:
            self._log_boundary_failure("fail", error)

    def _log_boundary_failure(self, operation: str, error: BaseException) -> None:
        # Do not log exception messages: adapters may include URLs, credentials
        # or task payload fragments in them.
        self._logger.error(
            "worker boundary operation failed operation=%s error_type=%s",
            operation,
            type(error).__name__,
        )


__all__ = ["WorkerOptions", "WorkerRunner"]
