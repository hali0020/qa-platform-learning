"""Small port used by the Scheduler loop and its deterministic tests."""

from __future__ import annotations

from typing import Protocol


class ScheduleBackend(Protocol):
    async def tick(self, scheduler_id: str, lease_seconds: int) -> int: ...


__all__ = ["ScheduleBackend"]
