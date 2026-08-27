from __future__ import annotations

from datetime import datetime
from typing import Any, Protocol

from app.automation.models import AutomationTask, EnqueueResult


class TaskQueuePort(Protocol):
    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        queue: str = "default",
        priority: int = 50,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        source_schedule_id: str | None = None,
        available_at: datetime | None = None,
    ) -> EnqueueResult: ...

    async def active_for_schedule(self, schedule_id: str) -> list[AutomationTask]: ...

    async def request_cancel(
        self,
        task_id: str,
        *,
        now: datetime | None = None,
    ) -> AutomationTask: ...


__all__ = ["TaskQueuePort"]
