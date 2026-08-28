"""Adapter from the Scheduler port to the persistent runtime service."""

from __future__ import annotations

from app.runtime.service import PersistentRuntimeService


class RuntimeScheduleBackend:
    def __init__(self, service: PersistentRuntimeService) -> None:
        self._service = service

    async def tick(self, scheduler_id: str, lease_seconds: int) -> int:
        fires = await self._service.tick_schedules(
            scheduler_id=scheduler_id,
            lease_seconds=lease_seconds,
        )
        return len(fires)


__all__ = ["RuntimeScheduleBackend"]
