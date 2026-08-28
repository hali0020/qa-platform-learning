"""Transactional task wake-up outbox behavior.

RabbitMQ carries only a coalescible wake-up hint.  These tests therefore treat
the database task and outbox rows as the durable facts and exercise the lease
and retry boundaries without requiring a broker container.
"""

from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy.dialects import postgresql

from app.broker.errors import BrokerTransportError
from app.broker.fake import FakeWakeupBroker
from app.database.session import Database
from app.runtime.orm import (
    AutomationTaskRecord,
    AutomationTaskWakeupOutboxRecord,
)
from app.runtime.repository import (
    build_task_wakeup_outbox_claim_statement,
    build_task_wakeup_outbox_settle_statement,
)
from app.runtime.schemas import ScheduleCreate, TaskEnqueue
from app.runtime.service import PersistentRuntimeService, create_runtime_service


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


async def _close(
    service: PersistentRuntimeService,
    database: Database,
) -> None:
    await service.shutdown()
    await database.shutdown()


def test_postgres_outbox_claim_uses_skip_locked_and_settle_uses_full_cas() -> None:
    now = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
    claim = build_task_wakeup_outbox_claim_statement(
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
    assert "RETRY_WAIT" in claim_sql
    assert "LEASE_EXPIRES_AT" in claim_sql

    settle = build_task_wakeup_outbox_settle_statement(
        outbox_id="outbox-1",
        dispatcher_id="dispatcher-a",
        lease_token_hash="a" * 64,
        expected_version=3,
        now=now,
        published=True,
        use_database_clock=True,
    )
    settle_sql = str(
        settle.compile(
            dialect=postgresql.dialect(),
            compile_kwargs={"literal_binds": True},
        )
    ).upper()

    assert settle_sql.startswith("UPDATE AUTOMATION_TASK_WAKEUP_OUTBOX")
    assert "LEASE_OWNER = 'DISPATCHER-A'" in settle_sql
    assert "LEASE_TOKEN_HASH" in settle_sql
    assert "VERSION = 3" in settle_sql
    assert "LEASE_EXPIRES_AT >" in settle_sql
    assert "CLOCK_TIMESTAMP()" in settle_sql
    assert "RETURNING AUTOMATION_TASK_WAKEUP_OUTBOX.ID" in settle_sql


@pytest.mark.asyncio
async def test_task_and_outbox_insert_roll_back_together(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)

    async def fail_outbox_insert(*_args, **_kwargs) -> None:
        raise RuntimeError("simulated outbox insert failure")

    monkeypatch.setattr(
        PersistentRuntimeService,
        "_add_task_wakeup_outbox",
        staticmethod(fail_outbox_insert),
    )
    try:
        with pytest.raises(RuntimeError, match="simulated outbox insert failure"):
            await service.enqueue_task(
                TaskEnqueue(
                    task_type="qa.quality.generate",
                    payload={"project_id": "atomic-lesson"},
                )
            )

        assert await service.list_tasks() == []
        assert await service.list_task_wakeup_outbox() == []
    finally:
        await _close(service, database)


@pytest.mark.asyncio
async def test_idempotent_enqueue_commits_exactly_one_outbox_generation() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    payload = TaskEnqueue(
        task_type="qa.pipeline.poll",
        payload={"observed_status": "queued"},
        idempotency_key="outbox-idempotency-1",
    )
    try:
        first, first_replayed = await service.enqueue_task(payload)
        replay, replayed = await service.enqueue_task(payload)

        outbox = await service.list_task_wakeup_outbox()
        assert first_replayed is False
        assert replayed is True
        assert replay.id == first.id
        assert len(outbox) == 1
        assert outbox[0].task_id == first.id
        assert outbox[0].generation == 0
        assert outbox[0].status == "pending"
    finally:
        await _close(service, database)


@pytest.mark.asyncio
async def test_manual_and_cron_schedule_enqueues_create_outbox_facts() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    try:
        schedule = await service.create_schedule(
            ScheduleCreate(
                name="Outbox schedule lesson",
                task_type="qa.quality.generate",
                payload={"project_id": "schedule-lesson"},
                cron="* * * * *",
                timezone="UTC",
                overlap_policy="allow",
            )
        )
        assert schedule.next_run_at is not None

        manual = await service.run_schedule_now(schedule.id)
        cron = await service.tick_schedules(
            schedule.next_run_at,
            scheduler_id="test-scheduler",
        )

        assert manual.task_id is not None
        assert len(cron) == 1
        assert cron[0].task_id is not None
        outbox = await service.list_task_wakeup_outbox()
        assert {item.task_id for item in outbox} == {
            manual.task_id,
            cron[0].task_id,
        }
        assert {item.generation for item in outbox} == {0}
        assert {item.status for item in outbox} == {"pending"}
    finally:
        await _close(service, database)


@pytest.mark.asyncio
async def test_fail_expiry_recovery_and_manual_retry_advance_generations() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    try:
        task, _ = await service.enqueue_task(
            TaskEnqueue(
                task_type="qa.import.validate",
                payload={"rows": []},
                max_attempts=5,
            )
        )

        first = await service.claim_task("worker-1", ["default"], 30)
        assert first is not None and first.task.id == task.id
        retry_wait = await service.fail_task(
            task.id,
            "worker-1",
            first.lease_token,
            "transient_failure",
            retryable=True,
        )
        assert retry_wait.status == "retry_wait"

        async with service.repository.transaction() as session:
            record = await session.get(AutomationTaskRecord, task.id)
            assert record is not None
            record.available_at = _utc_now() - timedelta(seconds=1)

        second = await service.claim_task("worker-2", ["default"], 30)
        assert second is not None and second.task.attempts == 2
        async with service.repository.transaction() as session:
            record = await session.get(AutomationTaskRecord, task.id)
            assert record is not None
            record.lease_expires_at = _utc_now() - timedelta(seconds=1)

        # Any claim call first performs DB-authoritative expired-lease recovery.
        assert await service.claim_task("recovery-probe", ["default"], 30) is None
        recovered = await service.get_task(task.id)
        assert recovered.status == "retry_wait"
        assert recovered.error_code == "lease_expired"

        async with service.repository.transaction() as session:
            record = await session.get(AutomationTaskRecord, task.id)
            assert record is not None
            record.available_at = _utc_now() - timedelta(seconds=1)

        third = await service.claim_task("worker-3", ["default"], 30)
        assert third is not None and third.task.attempts == 3
        failed = await service.fail_task(
            task.id,
            "worker-3",
            third.lease_token,
            "manual_review_required",
            retryable=False,
        )
        assert failed.status == "failed"
        manually_retried = await service.retry_task(task.id)
        assert manually_retried.status == "retry_wait"

        outbox = await service.list_task_wakeup_outbox()
        assert sorted(item.generation for item in outbox) == [0, 1, 2, 3]
        assert {item.task_id for item in outbox} == {task.id}
    finally:
        await _close(service, database)


@pytest.mark.asyncio
async def test_publisher_failure_is_durable_and_a_later_dispatch_retries() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    failing = FakeWakeupBroker(
        publish_error=BrokerTransportError("test-only broker outage")
    )
    healthy = FakeWakeupBroker()
    try:
        task, _ = await service.enqueue_task(
            TaskEnqueue(task_type="qa.quality.generate", payload={})
        )
        assert await service.dispatch_task_wakeup_once(
            dispatcher_id="dispatcher-a",
            publisher=failing,
            lease_seconds=30,
        ) is True

        failed = (await service.list_task_wakeup_outbox())[0]
        assert failed.task_id == task.id
        assert failed.status == "retry_wait"
        assert failed.publish_attempts == 1
        assert failed.last_error_code == "broker_publish_failed"
        async with service.repository.transaction() as session:
            record = await session.get(AutomationTaskWakeupOutboxRecord, failed.id)
            assert record is not None
            record.available_at = _utc_now() - timedelta(seconds=1)

        await healthy.start()
        assert await service.dispatch_task_wakeup_once(
            dispatcher_id="dispatcher-b",
            publisher=healthy,
            lease_seconds=30,
        ) is True
        published = (await service.list_task_wakeup_outbox())[0]
        assert published.status == "published"
        assert published.publish_attempts == 2
        assert published.last_error_code is None
        assert published.published_at is not None
        assert healthy.publish_count == 1
    finally:
        await failing.close()
        await healthy.close()
        await _close(service, database)


@pytest.mark.asyncio
async def test_outbox_lost_version_cannot_settle_and_expired_lease_is_reclaimed() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    try:
        await service.enqueue_task(
            TaskEnqueue(task_type="qa.quality.generate", payload={})
        )
        first = await service._claim_task_wakeup("dispatcher-a", 5)
        assert first is not None

        async with service.repository.transaction() as session:
            record = await session.get(
                AutomationTaskWakeupOutboxRecord,
                first.outbox_id,
            )
            assert record is not None
            assert record.status == "claimed"
            assert record.lease_owner == "dispatcher-a"
            record.version += 1

        assert await service._settle_task_wakeup(first, published=True) is False
        async with service.repository.transaction() as session:
            record = await session.get(
                AutomationTaskWakeupOutboxRecord,
                first.outbox_id,
            )
            assert record is not None
            record.lease_expires_at = _utc_now() - timedelta(seconds=1)

        second = await service._claim_task_wakeup("dispatcher-b", 5)
        assert second is not None
        assert second.outbox_id == first.outbox_id
        assert second.publish_attempts == 2
        assert await service._settle_task_wakeup(second, published=True) is True

        settled = (await service.list_task_wakeup_outbox())[0]
        assert settled.status == "published"
        assert settled.lease_owner is None
        assert settled.lease_expires_at is None
        assert settled.publish_attempts == 2
    finally:
        await _close(service, database)


@pytest.mark.asyncio
async def test_duplicate_fixed_hints_are_safe_because_database_claim_is_authoritative() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    service = create_runtime_service(database)
    broker = FakeWakeupBroker()
    try:
        await broker.start()
        task, _ = await service.enqueue_task(
            TaskEnqueue(task_type="qa.quality.generate", payload={})
        )
        assert await service.dispatch_task_wakeup_once(
            dispatcher_id="dispatcher-a",
            publisher=broker,
        ) is True
        # Models the publish-confirm / DB-finalize crash window: RabbitMQ may
        # receive the same fixed hint again, but no task payload or identity.
        await broker.publish_wakeup()

        assert broker.publish_count == 2
        assert await broker.wait(timeout=0) is True
        assert await broker.wait(timeout=0) is False

        claimed = await service.claim_task("worker-a", ["default"], 30)
        assert claimed is not None and claimed.task.id == task.id
        assert await service.claim_task("worker-b", ["default"], 30) is None
        completed = await service.complete_task(
            task.id,
            "worker-a",
            claimed.lease_token,
            {"ok": True},
        )
        assert completed.status == "succeeded"
        assert await service.claim_task("worker-b", ["default"], 30) is None
    finally:
        await broker.close()
        await _close(service, database)
