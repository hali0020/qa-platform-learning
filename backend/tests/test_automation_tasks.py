from datetime import datetime, timedelta, timezone

import pytest

from app.automation import (
    AutomationConflictError,
    AutomationLeaseError,
    InMemoryTaskQueue,
    RetryPolicy,
    TaskStatus,
)


BASE = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


@pytest.mark.asyncio
async def test_enqueue_is_idempotent_and_conflicting_payload_is_rejected() -> None:
    queue = InMemoryTaskQueue()

    first = await queue.enqueue(
        "pipeline.sync",
        {"run_id": "local-1"},
        idempotency_key="event-1",
    )
    replay = await queue.enqueue(
        "pipeline.sync",
        {"run_id": "local-1"},
        idempotency_key="event-1",
    )

    assert replay.replayed is True
    assert replay.task.id == first.task.id
    with pytest.raises(AutomationConflictError):
        await queue.enqueue(
            "pipeline.sync",
            {"run_id": "different"},
            idempotency_key="event-1",
        )


@pytest.mark.asyncio
async def test_task_lease_heartbeat_and_completion_reject_stale_worker() -> None:
    queue = InMemoryTaskQueue()
    created = await queue.enqueue("report.build", {}, available_at=BASE)
    claimed = await queue.claim("worker-a", now=BASE, lease_seconds=10)

    assert claimed is not None
    assert claimed.task.id == created.task.id
    assert claimed.task.attempts == 1
    with pytest.raises(AutomationLeaseError):
        await queue.complete(
            claimed.task.id,
            "worker-a",
            "wrong-token",
            now=BASE + timedelta(seconds=1),
        )

    renewed = await queue.heartbeat(
        claimed.task.id,
        "worker-a",
        claimed.lease_token,
        now=BASE + timedelta(seconds=5),
        lease_seconds=10,
    )
    assert renewed.lease_expires_at == BASE + timedelta(seconds=15)

    completed = await queue.complete(
        claimed.task.id,
        "worker-a",
        claimed.lease_token,
        {"report_id": "r-1"},
        now=BASE + timedelta(seconds=14),
    )
    assert completed.status == TaskStatus.SUCCEEDED
    assert completed.result == {"report_id": "r-1"}


@pytest.mark.asyncio
async def test_expired_leases_retry_then_move_to_dead_letter() -> None:
    queue = InMemoryTaskQueue(
        retry_policy=RetryPolicy(base_seconds=1, maximum_seconds=1, jitter_ratio=0),
        entropy=lambda: 0.5,
    )
    created = await queue.enqueue(
        "device.run",
        {},
        max_attempts=2,
        available_at=BASE,
    )
    first = await queue.claim("worker-a", now=BASE, lease_seconds=2)
    assert first is not None

    recovered = await queue.recover_expired(now=BASE + timedelta(seconds=2))
    assert recovered[0].status == TaskStatus.RETRY_WAIT
    assert recovered[0].available_at == BASE + timedelta(seconds=3)

    second = await queue.claim(
        "worker-b",
        now=BASE + timedelta(seconds=3),
        lease_seconds=2,
    )
    assert second is not None
    assert second.task.id == created.task.id
    assert second.task.attempts == 2

    final = await queue.recover_expired(now=BASE + timedelta(seconds=5))
    assert final[0].status == TaskStatus.DEAD_LETTER
    assert final[0].error_code == "lease_expired"


@pytest.mark.asyncio
async def test_running_task_cancellation_is_cooperative() -> None:
    queue = InMemoryTaskQueue()
    await queue.enqueue("long.task", {}, available_at=BASE)
    claimed = await queue.claim("worker-a", now=BASE, lease_seconds=30)
    assert claimed is not None

    requested = await queue.request_cancel(
        claimed.task.id,
        now=BASE + timedelta(seconds=1),
    )
    assert requested.status == TaskStatus.RUNNING
    assert requested.cancel_requested is True

    cancelled = await queue.acknowledge_cancel(
        claimed.task.id,
        "worker-a",
        claimed.lease_token,
        now=BASE + timedelta(seconds=2),
    )
    assert cancelled.status == TaskStatus.CANCELLED
