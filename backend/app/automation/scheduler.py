from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from uuid import uuid4

from app.automation.cron import CronExpression
from app.automation.errors import (
    AutomationConflictError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from app.automation.models import (
    AutomationSchedule,
    MisfirePolicy,
    OverlapPolicy,
    ScheduleFire,
    ScheduleFireStatus,
    utc_now,
)
from app.automation.ports import TaskQueuePort


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AutomationValidationError("schedule timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


class InMemoryScheduler:
    """Persistable cron decisions separated from the web process.

    A future SQL implementation must atomically insert the unique
    ``(schedule_id, scheduled_for)`` fire and enqueue its task (or use an
    outbox). The in-memory lock provides that boundary only for local lessons.
    """

    def __init__(
        self,
        task_queue: TaskQueuePort,
        *,
        max_due_occurrences: int = 10_000,
    ) -> None:
        if max_due_occurrences < 1:
            raise AutomationValidationError("scheduler due limit must be positive")
        self._queue = task_queue
        self._schedules: dict[str, AutomationSchedule] = {}
        self._expressions: dict[str, CronExpression] = {}
        self._fires: dict[tuple[str, str], ScheduleFire] = {}
        self._lock = asyncio.Lock()
        self._max_due = max_due_occurrences

    async def add(
        self,
        schedule: AutomationSchedule,
        *,
        now: datetime | None = None,
    ) -> AutomationSchedule:
        current = _as_utc(now or utc_now())
        expression = CronExpression.parse(schedule.cron)
        # Also validates the IANA timezone even when a disabled schedule is added.
        calculated_next = expression.next_after(current, schedule.timezone)
        async with self._lock:
            if schedule.id in self._schedules:
                raise AutomationConflictError("schedule id already exists")
            stored = schedule.model_copy(deep=True)
            stored.next_run_at = (
                _as_utc(stored.next_run_at)
                if stored.next_run_at is not None
                else calculated_next
            )
            stored.created_at = current
            stored.updated_at = current
            self._schedules[stored.id] = stored
            self._expressions[stored.id] = expression
            return stored.model_copy(deep=True)

    async def get(self, schedule_id: str) -> AutomationSchedule:
        async with self._lock:
            return self._require(schedule_id).model_copy(deep=True)

    async def list_schedules(self) -> list[AutomationSchedule]:
        async with self._lock:
            return [
                item.model_copy(deep=True)
                for item in sorted(
                    self._schedules.values(),
                    key=lambda schedule: (schedule.name.casefold(), schedule.id),
                )
            ]

    async def list_fires(self, schedule_id: str | None = None) -> list[ScheduleFire]:
        async with self._lock:
            values = [
                fire
                for fire in self._fires.values()
                if schedule_id is None or fire.schedule_id == schedule_id
            ]
            return [
                fire.model_copy(deep=True)
                for fire in sorted(values, key=lambda item: (item.scheduled_for, item.id))
            ]

    async def set_enabled(
        self,
        schedule_id: str,
        enabled: bool,
        *,
        now: datetime | None = None,
    ) -> AutomationSchedule:
        current = _as_utc(now or utc_now())
        async with self._lock:
            schedule = self._require(schedule_id)
            schedule.enabled = enabled
            if enabled and (
                schedule.next_run_at is None or schedule.next_run_at <= current
            ):
                schedule.next_run_at = self._expressions[schedule_id].next_after(
                    current,
                    schedule.timezone,
                )
            schedule.version += 1
            schedule.updated_at = current
            return schedule.model_copy(deep=True)

    async def tick(self, *, now: datetime | None = None) -> list[ScheduleFire]:
        current = _as_utc(now or utc_now())
        created: list[ScheduleFire] = []
        async with self._lock:
            due_schedules = sorted(
                (
                    schedule
                    for schedule in self._schedules.values()
                    if schedule.enabled
                    and schedule.next_run_at is not None
                    and schedule.next_run_at <= current
                ),
                key=lambda item: (item.next_run_at or current, item.id),
            )
            for schedule in due_schedules:
                created.extend(await self._fire_due_locked(schedule, current))
        return [fire.model_copy(deep=True) for fire in created]

    async def run_now(
        self,
        schedule_id: str,
        *,
        now: datetime | None = None,
    ) -> ScheduleFire:
        current = _as_utc(now or utc_now())
        async with self._lock:
            schedule = self._require(schedule_id)
            manual_id = str(uuid4())
            result = await self._queue.enqueue(
                schedule.task_type,
                schedule.payload,
                queue=schedule.queue,
                priority=schedule.priority,
                max_attempts=schedule.max_attempts,
                idempotency_key=f"schedule-manual:{schedule.id}:{manual_id}",
                source_schedule_id=schedule.id,
                available_at=current,
            )
            fire = ScheduleFire(
                id=manual_id,
                schedule_id=schedule.id,
                scheduled_for=current,
                status=ScheduleFireStatus.ENQUEUED,
                task_id=result.task.id,
                created_at=current,
            )
            # A manual fire id is unique even if its minute equals a cron fire.
            self._fires[(schedule.id, f"manual:{manual_id}")] = fire
            return fire.model_copy(deep=True)

    async def _fire_due_locked(
        self,
        schedule: AutomationSchedule,
        now: datetime,
    ) -> list[ScheduleFire]:
        expression = self._expressions[schedule.id]
        due: list[datetime] = []
        cursor = schedule.next_run_at
        if cursor is None:
            return []
        while cursor <= now:
            due.append(cursor)
            if len(due) > self._max_due:
                raise AutomationValidationError(
                    "schedule exceeded the bounded catch-up window"
                )
            cursor = expression.next_after(cursor, schedule.timezone)

        selected, skipped = self._apply_misfire_policy(schedule, due, now)
        created: list[ScheduleFire] = []
        for scheduled_for in skipped:
            fire = self._record_fire(
                schedule,
                scheduled_for,
                ScheduleFireStatus.SKIPPED_MISFIRE,
                now,
            )
            if fire is not None:
                created.append(fire)

        for scheduled_for in selected:
            key = self._cron_fire_key(schedule.id, scheduled_for)
            if key in self._fires:
                continue
            active = await self._queue.active_for_schedule(schedule.id)
            if schedule.overlap_policy == OverlapPolicy.FORBID and active:
                fire = self._record_fire(
                    schedule,
                    scheduled_for,
                    ScheduleFireStatus.SKIPPED_OVERLAP,
                    now,
                )
            else:
                if schedule.overlap_policy == OverlapPolicy.REPLACE:
                    for task in active:
                        await self._queue.request_cancel(task.id, now=now)
                result = await self._queue.enqueue(
                    schedule.task_type,
                    schedule.payload,
                    queue=schedule.queue,
                    priority=schedule.priority,
                    max_attempts=schedule.max_attempts,
                    idempotency_key=(
                        f"schedule:{schedule.id}:{scheduled_for.isoformat()}"
                    ),
                    source_schedule_id=schedule.id,
                    available_at=scheduled_for,
                )
                fire = ScheduleFire(
                    schedule_id=schedule.id,
                    scheduled_for=scheduled_for,
                    status=ScheduleFireStatus.ENQUEUED,
                    task_id=result.task.id,
                    created_at=now,
                )
                self._fires[key] = fire
            if fire is not None:
                created.append(fire)

        schedule.last_run_at = due[-1]
        schedule.next_run_at = cursor
        schedule.version += 1
        schedule.updated_at = now
        return created

    @staticmethod
    def _apply_misfire_policy(
        schedule: AutomationSchedule,
        due: list[datetime],
        now: datetime,
    ) -> tuple[list[datetime], list[datetime]]:
        if not due:
            return [], []
        if schedule.misfire_policy == MisfirePolicy.FIRE_ONCE:
            return [due[-1]], due[:-1]
        if schedule.misfire_policy == MisfirePolicy.CATCH_UP_LIMITED:
            limit = schedule.catch_up_limit
            return due[-limit:], due[:-limit]
        cutoff = now - timedelta(seconds=schedule.misfire_grace_seconds)
        selected = [instant for instant in due if instant >= cutoff]
        skipped = [instant for instant in due if instant < cutoff]
        return selected, skipped

    def _record_fire(
        self,
        schedule: AutomationSchedule,
        scheduled_for: datetime,
        status: ScheduleFireStatus,
        now: datetime,
    ) -> ScheduleFire | None:
        key = self._cron_fire_key(schedule.id, scheduled_for)
        if key in self._fires:
            return None
        fire = ScheduleFire(
            schedule_id=schedule.id,
            scheduled_for=scheduled_for,
            status=status,
            created_at=now,
        )
        self._fires[key] = fire
        return fire

    @staticmethod
    def _cron_fire_key(schedule_id: str, scheduled_for: datetime) -> tuple[str, str]:
        return schedule_id, f"cron:{scheduled_for.isoformat()}"

    def _require(self, schedule_id: str) -> AutomationSchedule:
        try:
            return self._schedules[schedule_id]
        except KeyError as error:
            raise AutomationNotFoundError("automation schedule was not found") from error


__all__ = ["InMemoryScheduler"]
