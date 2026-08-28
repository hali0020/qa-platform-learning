from __future__ import annotations

import asyncio
import logging

import pytest

from app.scheduler.runner import SchedulerOptions, SchedulerRunner


class FakeScheduleBackend:
    def __init__(self, outcomes: list[int | Exception]) -> None:
        self.outcomes = outcomes
        self.calls = 0
        self.observed = asyncio.Event()

    async def tick(self, scheduler_id: str, lease_seconds: int) -> int:
        assert scheduler_id == "scheduler-a"
        assert lease_seconds == 30
        outcome = self.outcomes[min(self.calls, len(self.outcomes) - 1)]
        self.calls += 1
        if self.calls >= len(self.outcomes):
            self.observed.set()
        if isinstance(outcome, Exception):
            raise outcome
        return outcome


def test_scheduler_options_validate_identity_lease_and_poll_bounds() -> None:
    assert SchedulerOptions("scheduler-a").scheduler_id == "scheduler-a"
    with pytest.raises(ValueError, match="scheduler_id"):
        SchedulerOptions("")
    with pytest.raises(ValueError, match="lease"):
        SchedulerOptions("scheduler-a", lease_seconds=4)
    with pytest.raises(ValueError, match="poll"):
        SchedulerOptions("scheduler-a", poll_interval_seconds=0.01)


@pytest.mark.asyncio
async def test_scheduler_runner_retries_safely_and_stop_interrupts_poll(
    caplog: pytest.LogCaptureFixture,
) -> None:
    backend = FakeScheduleBackend(
        [RuntimeError("secret-payload-must-not-be-logged"), 1, 0]
    )
    runner = SchedulerRunner(
        backend,
        SchedulerOptions("scheduler-a", poll_interval_seconds=0.05),
    )

    with caplog.at_level(logging.ERROR, logger="qa.scheduler"):
        operation = asyncio.create_task(runner.run())
        try:
            await asyncio.wait_for(backend.observed.wait(), timeout=1)
        finally:
            runner.request_stop()
            await asyncio.wait_for(operation, timeout=1)

    assert backend.calls >= 3
    assert "RuntimeError" in caplog.text
    assert "secret-payload-must-not-be-logged" not in caplog.text
