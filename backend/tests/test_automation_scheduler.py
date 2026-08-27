from datetime import datetime, timedelta, timezone

import pytest

from app.automation import (
    AutomationSchedule,
    CronExpression,
    InMemoryScheduler,
    InMemoryTaskQueue,
    MisfirePolicy,
    OverlapPolicy,
    ScheduleFireStatus,
    TaskStatus,
)


BASE = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


def schedule(**updates: object) -> AutomationSchedule:
    values: dict[str, object] = {
        "name": "minute-quality-gate",
        "task_type": "quality.calculate",
        "payload": {"project_id": "local-project"},
        "cron": "* * * * *",
        "timezone": "UTC",
    }
    values.update(updates)
    return AutomationSchedule(**values)


def test_cron_expression_has_explicit_timezone_and_standard_day_semantics() -> None:
    daily = CronExpression.parse("0 9 * * *")
    next_fire = daily.next_after(BASE, "Asia/Shanghai")
    assert next_fire == datetime(2026, 8, 27, 1, 0, tzinfo=timezone.utc)

    every_quarter = CronExpression.parse("*/15 * * * *")
    assert every_quarter.next_after(BASE, "UTC") == BASE + timedelta(minutes=15)


@pytest.mark.asyncio
async def test_fire_once_collapses_misfires_and_forbid_skips_overlap() -> None:
    queue = InMemoryTaskQueue()
    scheduler = InMemoryScheduler(queue)
    item = schedule(
        next_run_at=BASE + timedelta(minutes=1),
        misfire_policy=MisfirePolicy.FIRE_ONCE,
        overlap_policy=OverlapPolicy.FORBID,
    )
    await scheduler.add(item, now=BASE)

    first_tick = await scheduler.tick(now=BASE + timedelta(minutes=3))
    assert [fire.status for fire in first_tick].count(ScheduleFireStatus.ENQUEUED) == 1
    assert [fire.status for fire in first_tick].count(ScheduleFireStatus.SKIPPED_MISFIRE) == 2

    second_tick = await scheduler.tick(now=BASE + timedelta(minutes=4))
    assert second_tick[0].status == ScheduleFireStatus.SKIPPED_OVERLAP
    tasks = await queue.list_tasks()
    assert len(tasks) == 1
    assert tasks[0].status == TaskStatus.QUEUED


@pytest.mark.asyncio
async def test_catch_up_limit_with_allow_enqueues_only_most_recent_occurrences() -> None:
    queue = InMemoryTaskQueue()
    scheduler = InMemoryScheduler(queue)
    item = schedule(
        next_run_at=BASE + timedelta(minutes=1),
        misfire_policy=MisfirePolicy.CATCH_UP_LIMITED,
        catch_up_limit=2,
        overlap_policy=OverlapPolicy.ALLOW,
    )
    await scheduler.add(item, now=BASE)

    fires = await scheduler.tick(now=BASE + timedelta(minutes=5))
    assert [fire.status for fire in fires].count(ScheduleFireStatus.ENQUEUED) == 2
    assert [fire.status for fire in fires].count(ScheduleFireStatus.SKIPPED_MISFIRE) == 3
    assert len(await queue.list_tasks()) == 2


@pytest.mark.asyncio
async def test_replace_requests_cancellation_before_new_fire() -> None:
    queue = InMemoryTaskQueue()
    scheduler = InMemoryScheduler(queue)
    item = schedule(
        next_run_at=BASE + timedelta(minutes=1),
        overlap_policy=OverlapPolicy.REPLACE,
    )
    await scheduler.add(item, now=BASE)
    await scheduler.tick(now=BASE + timedelta(minutes=1))
    await scheduler.tick(now=BASE + timedelta(minutes=2))

    tasks = await queue.list_tasks()
    assert len(tasks) == 2
    assert {task.status for task in tasks} == {
        TaskStatus.CANCELLED,
        TaskStatus.QUEUED,
    }
