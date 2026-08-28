from __future__ import annotations

import asyncio
import threading
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.database.session import Database
from app.runtime.orm import ScheduleRecord
from app.runtime.repository import (
    build_schedule_claim_statement,
    build_schedule_finalize_statement,
)
from app.runtime.schemas import ScheduleCreate
from app.runtime.service import create_runtime_service


def test_postgres_schedule_claim_and_finalize_use_skip_locked_and_cas() -> None:
    now = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
    claim = build_schedule_claim_statement(
        now=now,
        use_skip_locked=True,
    )
    claim_sql = str(
        claim.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "CLAIM_EXPIRES_AT" in claim_sql
    assert "NEXT_RUN_AT" in claim_sql

    finalize = build_schedule_finalize_statement(
        schedule_id="schedule-1",
        expected_version=3,
        expected_next_run_at=now,
        scheduler_id="scheduler-a",
        claim_token_hash="a" * 64,
        now=now + timedelta(seconds=1),
        last_run_at=now,
        next_run_at=now + timedelta(minutes=1),
        use_database_clock=True,
    )
    finalize_sql = str(
        finalize.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert finalize_sql.startswith("UPDATE SCHEDULES")
    assert "SCHEDULES.VERSION = 3" in finalize_sql
    assert "SCHEDULES.CLAIM_OWNER = 'SCHEDULER-A'" in finalize_sql
    assert "SCHEDULES.CLAIM_TOKEN_HASH" in finalize_sql
    assert "SCHEDULES.CLAIM_EXPIRES_AT >" in finalize_sql
    assert "CLOCK_TIMESTAMP()" in finalize_sql
    assert "RETURNING SCHEDULES.ID" in finalize_sql


@pytest.mark.asyncio
async def test_schedule_plan_runs_after_claim_commit_and_lost_version_cannot_fire(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    schedule = await service.create_schedule(
        ScheduleCreate(
            name="CAS schedule lesson",
            task_type="qa.quality.generate",
            cron="* * * * *",
            timezone="UTC",
        )
    )
    assert schedule.next_run_at is not None

    from app.runtime import service as runtime_service_module

    original = runtime_service_module._cron_due_plan
    planning_started = threading.Event()
    release_planning = threading.Event()

    def controlled_plan(*args, **kwargs):
        planning_started.set()
        release_planning.wait(timeout=2)
        return original(*args, **kwargs)

    monkeypatch.setattr(runtime_service_module, "_cron_due_plan", controlled_plan)
    operation = asyncio.create_task(
        service.tick_schedule_once(
            "scheduler-a",
            30,
            schedule.next_run_at,
        )
    )
    try:
        assert await asyncio.to_thread(planning_started.wait, 1)
        # T1 has committed: another transaction can observe and invalidate the
        # claim while CPU-bound cron planning is still running.
        async with service.repository.transaction() as session:
            record = await session.get(ScheduleRecord, schedule.id)
            assert record is not None
            assert record.claim_owner == "scheduler-a"
            assert record.claim_token_hash is not None
            assert record.claim_expires_at is not None
            record.version += 1
        release_planning.set()
        assert await asyncio.wait_for(operation, timeout=3) == []
    finally:
        release_planning.set()
        if not operation.done():
            operation.cancel()
            await asyncio.gather(operation, return_exceptions=True)

    assert await service.list_schedule_fires(schedule.id) == []
    async with service.repository.transaction() as session:
        record = await session.get(ScheduleRecord, schedule.id)
        assert record is not None
        assert record.claim_owner is None
        assert record.claim_token_hash is None
        assert record.claim_expires_at is None
    await service.shutdown()
    await database.shutdown()


@pytest.mark.asyncio
async def test_expired_schedule_claim_is_recovered_and_concurrent_ticks_do_not_duplicate() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    schedule = await service.create_schedule(
        ScheduleCreate(
            name="Expired claim lesson",
            task_type="qa.quality.generate",
            cron="* * * * *",
            timezone="UTC",
        )
    )
    assert schedule.next_run_at is not None
    async with service.repository.transaction() as session:
        record = await session.get(ScheduleRecord, schedule.id)
        assert record is not None
        record.claim_owner = "crashed-scheduler"
        record.claim_token_hash = "b" * 64
        record.claim_expires_at = datetime.now(timezone.utc) - timedelta(seconds=1)

    first, second = await asyncio.gather(
        service.tick_schedule_once("scheduler-a", 30, schedule.next_run_at),
        service.tick_schedule_once("scheduler-b", 30, schedule.next_run_at),
    )

    assert len(first) + len(second) == 1
    fires = await service.list_schedule_fires(schedule.id)
    tasks = await service.list_tasks()
    assert len(fires) == 1
    assert len(tasks) == 1
    assert fires[0].task_id == tasks[0].id
    await service.shutdown()
    await database.shutdown()
