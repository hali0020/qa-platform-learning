"""Bounded polling loop for the independent Scheduler process."""

from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from app.scheduler.contracts import ScheduleBackend


@dataclass(frozen=True, slots=True)
class SchedulerOptions:
    scheduler_id: str
    lease_seconds: int = 30
    poll_interval_seconds: float = 5.0

    def __post_init__(self) -> None:
        if not self.scheduler_id.strip() or len(self.scheduler_id) > 200:
            raise ValueError("scheduler_id must contain 1-200 characters")
        if not 5 <= self.lease_seconds <= 3_600:
            raise ValueError("scheduler lease must be between 5 and 3600 seconds")
        if not 0.05 <= self.poll_interval_seconds <= 60:
            raise ValueError("scheduler poll interval must be between 0.05 and 60 seconds")


class SchedulerRunner:
    """Run one tick at a time; SQL leases coordinate multiple instances."""

    def __init__(
        self,
        backend: ScheduleBackend,
        options: SchedulerOptions,
        *,
        logger: logging.Logger | None = None,
    ) -> None:
        self._backend = backend
        self.options = options
        self._stop = asyncio.Event()
        self._logger = logger or logging.getLogger("qa.scheduler")

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        while not self._stop.is_set():
            try:
                created = await self._backend.tick(
                    self.options.scheduler_id,
                    self.options.lease_seconds,
                )
            except asyncio.CancelledError:
                raise
            except Exception as error:
                # Exception text may contain task payload fragments. Keep the
                # process boundary log metadata-only and retry by bounded poll.
                self._logger.error(
                    "scheduler tick failed error_type=%s",
                    type(error).__name__,
                )
                created = 0
            if self._stop.is_set():
                return
            if created:
                # Drain work that became due during the previous batch before
                # falling back to the bounded database poll.
                await asyncio.sleep(0)
                continue
            try:
                await asyncio.wait_for(
                    self._stop.wait(),
                    timeout=self.options.poll_interval_seconds,
                )
            except asyncio.TimeoutError:
                pass


__all__ = ["SchedulerOptions", "SchedulerRunner"]
