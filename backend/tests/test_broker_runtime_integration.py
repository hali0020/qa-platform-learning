from __future__ import annotations

from pathlib import Path

import pytest

from app.broker.errors import BrokerTransportError
from app.broker.fake import FakeWakeupBroker
from app.database.session import Database
from app.runtime.schemas import TaskEnqueue
from app.runtime.service import create_runtime_service


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_committed_task_publishes_durable_content_free_wakeup(
    tmp_path: Path,
) -> None:
    database = Database(_sqlite_url(tmp_path / "wakeup.db"))
    broker = FakeWakeupBroker()
    service = create_runtime_service(database, wakeup_publisher=broker)
    try:
        await broker.start()
        task, replayed = await service.enqueue_task(
            TaskEnqueue(
                task_type="qa.quality.generate",
                payload={"passed": 3, "failed": 1, "skipped": 0},
                idempotency_key="quality-lesson-1",
            )
        )

        assert replayed is False
        assert broker.publish_count == 1
        assert await broker.wait(timeout=0) is True
        assert (await service.get_task(task.id)).id == task.id
        outbox = await service.list_task_wakeup_outbox()
        assert len(outbox) == 1
        assert outbox[0].task_id == task.id
        assert outbox[0].status == "published"
        assert outbox[0].publish_attempts == 1
    finally:
        await broker.close()
        await database.shutdown()


@pytest.mark.asyncio
async def test_broker_failure_cannot_roll_back_database_task(tmp_path: Path) -> None:
    database = Database(_sqlite_url(tmp_path / "broker-failure.db"))
    broker = FakeWakeupBroker(
        publish_error=BrokerTransportError("test-only transport failure")
    )
    service = create_runtime_service(database, wakeup_publisher=broker)
    try:
        task, replayed = await service.enqueue_task(
            TaskEnqueue(
                task_type="qa.import.validate",
                payload={"rows": []},
            )
        )

        assert replayed is False
        persisted = await service.get_task(task.id)
        assert persisted.status == "queued"
        assert persisted.payload == {"rows": []}
        outbox = await service.list_task_wakeup_outbox()
        assert len(outbox) == 1
        assert outbox[0].task_id == task.id
        assert outbox[0].status == "retry_wait"
        assert outbox[0].publish_attempts == 1
        assert outbox[0].last_error_code == "broker_publish_failed"
    finally:
        await broker.close()
        await database.shutdown()


@pytest.mark.asyncio
async def test_idempotent_replay_does_not_create_or_publish_a_second_fact(
    tmp_path: Path,
) -> None:
    database = Database(_sqlite_url(tmp_path / "replay-wakeup.db"))
    broker = FakeWakeupBroker()
    service = create_runtime_service(database, wakeup_publisher=broker)
    payload = TaskEnqueue(
        task_type="qa.pipeline.poll",
        payload={"observed_status": "running"},
        idempotency_key="poll-lesson-1",
    )
    try:
        first, first_replayed = await service.enqueue_task(payload)
        replay, replayed = await service.enqueue_task(payload)

        assert first_replayed is False
        assert replayed is True
        assert replay.id == first.id
        assert broker.publish_count == 1
        outbox = await service.list_task_wakeup_outbox()
        assert len(outbox) == 1
        assert outbox[0].task_id == first.id
        assert outbox[0].generation == 0
        assert outbox[0].status == "published"
    finally:
        await broker.close()
        await database.shutdown()
