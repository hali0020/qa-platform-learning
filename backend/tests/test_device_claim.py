"""Device claim locking and exclusivity tests.

PostgreSQL assertions compile SQL offline only. SQLite behavior remains the
single-process teaching path and exercises the same state invariants without
claiming that a real PostgreSQL server was started.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy import func, select
from sqlalchemy.dialects import postgresql

from app.automation.models import DeviceLeaseStatus
from app.core.errors import ConflictError
from app.database.session import Database
from app.runtime import service as runtime_service_module
from app.runtime.orm import DeviceLeaseRecord
from app.runtime.repository import (
    RuntimeRepository,
    build_device_candidate_statement,
    build_postgres_clock_statement,
)
from app.runtime.schemas import DeviceAcquire, DeviceCreate, TaskEnqueue
from app.runtime.service import PersistentRuntimeService, create_runtime_service


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _postgres_sql(statement: object) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).upper().split())


async def _claim_task(
    service: PersistentRuntimeService,
    *,
    worker: str,
    case_id: str,
):
    task, replayed = await service.enqueue_task(
        TaskEnqueue(
            task_type="qa.device.execute",
            payload={"case_id": case_id},
        )
    )
    assert replayed is False
    claim = await service.claim_task(worker, ["default"], 60)
    assert claim is not None
    assert claim.task.id == task.id
    return task, claim


def test_postgres_device_claim_is_ordered_skip_locked_limit_one() -> None:
    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    statement = build_device_candidate_statement(
        candidate_ids={"device-b", "device-a"},
        heartbeat_after=now - timedelta(seconds=90),
        use_skip_locked=True,
    )

    sql = _postgres_sql(statement)
    assert "FOR UPDATE SKIP LOCKED" in sql
    assert "ORDER BY LOWER(DEVICES.NAME), DEVICES.ID" in sql
    assert "LIMIT" in sql
    assert statement._for_update_arg is not None
    assert statement._for_update_arg.skip_locked is True


def test_sqlite_device_claim_statement_deliberately_has_no_row_lock() -> None:
    statement = build_device_candidate_statement(
        candidate_ids={"device-a"},
        heartbeat_after=datetime(2026, 8, 27, tzinfo=timezone.utc),
        use_skip_locked=False,
    )

    # PostgreSQL compilation proves the statement itself has no lock clause;
    # SQLite's compiler would omit FOR UPDATE even if it had been requested.
    assert "FOR UPDATE" not in _postgres_sql(statement)
    assert statement._for_update_arg is None


def test_postgres_lease_clock_uses_database_wall_clock_offline_sql() -> None:
    sql = _postgres_sql(build_postgres_clock_statement())
    assert "CLOCK_TIMESTAMP()" in sql


@pytest.mark.asyncio
async def test_device_graph_repository_uses_fixed_lock_order(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    order: list[str] = []

    class LocatorResult:
        @staticmethod
        def one_or_none() -> tuple[str, str]:
            return "task-1", "device-1"

    class LocatorSession:
        async def execute(self, _statement: object) -> LocatorResult:
            order.append("locator")
            return LocatorResult()

    repository = object.__new__(RuntimeRepository)

    async def lock_task(_session: object, task_id: str) -> str:
        order.append(f"task:{task_id}")
        return task_id

    async def lock_device(_session: object, device_id: str) -> str:
        order.append(f"device:{device_id}")
        return device_id

    async def lock_lease(_session: object, lease_id: str) -> str:
        order.append(f"lease:{lease_id}")
        return lease_id

    monkeypatch.setattr(repository, "get_task_for_update", lock_task)
    monkeypatch.setattr(repository, "get_device_for_update", lock_device)
    monkeypatch.setattr(repository, "get_device_lease_for_update", lock_lease)

    graph = await repository.lock_device_lease_graph(LocatorSession(), "lease-1")

    assert graph is not None
    assert order == [
        "locator",
        "task:task-1",
        "device:device-1",
        "lease:lease-1",
    ]


@pytest.mark.asyncio
async def test_capability_filter_continues_past_first_named_device(
    tmp_path: Path,
) -> None:
    database = Database(_sqlite_url(tmp_path / "capability-order.db"))
    service = create_runtime_service(database)
    try:
        task, task_claim = await _claim_task(
            service, worker="worker-android", case_id="android-case"
        )
        first = await service.create_device(
            DeviceCreate(
                name="A iOS device",
                agent_id="agent-ios",
                platform="ios",
                capabilities={"ios"},
            )
        )
        matching = await service.create_device(
            DeviceCreate(
                name="B Android device",
                agent_id="agent-android",
                platform="android",
                capabilities={"android", "api-35"},
            )
        )
        await service.heartbeat_device(first.id, "agent-ios")
        await service.heartbeat_device(matching.id, "agent-android")

        claimed = await service.acquire_device(
            DeviceAcquire(
                task_id=task.id,
                owner="worker-android",
                task_lease_token=task_claim.lease_token,
                required_capabilities={"android"},
            )
        )

        assert claimed is not None
        assert claimed.device.id == matching.id
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_duplicate_sqlite_wakeups_leave_one_active_device_lease(
    tmp_path: Path,
) -> None:
    database = Database(_sqlite_url(tmp_path / "device-exclusive.db"))
    service = create_runtime_service(database)
    try:
        task_a, claim_a = await _claim_task(
            service, worker="worker-a", case_id="case-a"
        )
        task_b, claim_b = await _claim_task(
            service, worker="worker-b", case_id="case-b"
        )
        device = await service.create_device(
            DeviceCreate(
                name="Only Android device",
                agent_id="only-agent",
                platform="android",
                capabilities={"android"},
            )
        )
        await service.heartbeat_device(device.id, "only-agent")

        claims = await asyncio.gather(
            service.acquire_device(
                DeviceAcquire(
                    task_id=task_a.id,
                    owner="worker-a",
                    task_lease_token=claim_a.lease_token,
                    required_capabilities={"android"},
                )
            ),
            service.acquire_device(
                DeviceAcquire(
                    task_id=task_b.id,
                    owner="worker-b",
                    task_lease_token=claim_b.lease_token,
                    required_capabilities={"android"},
                )
            ),
        )
        successful = [claim for claim in claims if claim is not None]

        assert len(successful) == 1
        async with service.repository.transaction() as session:
            active_count = await session.scalar(
                select(func.count(DeviceLeaseRecord.id)).where(
                    DeviceLeaseRecord.device_id == device.id,
                    DeviceLeaseRecord.status == DeviceLeaseStatus.ACTIVE.value,
                )
            )
        assert active_count == 1

        # The database constraint is the final guard even if application-level
        # locking is bypassed. Released history for the same device is allowed.
        now = datetime.now(timezone.utc)
        async with service.repository.transaction() as session:
            session.add(
                DeviceLeaseRecord(
                    id="released-history",
                    device_id=device.id,
                    task_id=task_a.id,
                    owner="history",
                    token_hash="1" * 64,
                    status=DeviceLeaseStatus.RELEASED.value,
                    acquired_at=now,
                    expires_at=now,
                    released_at=now,
                    version=0,
                )
            )
            await session.flush()
        with pytest.raises(ConflictError, match="唯一性"):
            async with service.repository.transaction() as session:
                session.add(
                    DeviceLeaseRecord(
                        id="forbidden-second-active",
                        device_id=device.id,
                        task_id=task_b.id,
                        owner="bypass",
                        token_hash="2" * 64,
                        status=DeviceLeaseStatus.ACTIVE.value,
                        acquired_at=now,
                        expires_at=now + timedelta(seconds=30),
                        released_at=None,
                        version=0,
                    )
                )
                await session.flush()
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_expired_lease_is_recovered_before_next_device_claim(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": datetime(2026, 8, 27, 1, 2, tzinfo=timezone.utc)}
    monkeypatch.setattr(runtime_service_module, "_utc_now", lambda: clock["now"])
    database = Database(_sqlite_url(tmp_path / "device-expiry.db"))
    service = create_runtime_service(database)
    try:
        task_a, claim_a = await _claim_task(
            service, worker="worker-a", case_id="case-a"
        )
        task_b, claim_b = await _claim_task(
            service, worker="worker-b", case_id="case-b"
        )
        device = await service.create_device(
            DeviceCreate(
                name="Reusable device",
                agent_id="reusable-agent",
                capabilities={"android"},
            )
        )
        await service.heartbeat_device(device.id, "reusable-agent")
        old_claim = await service.acquire_device(
            DeviceAcquire(
                task_id=task_a.id,
                owner="worker-a",
                task_lease_token=claim_a.lease_token,
                required_capabilities={"android"},
                lease_seconds=5,
            )
        )
        assert old_claim is not None

        clock["now"] += timedelta(seconds=6)
        new_claim = await service.acquire_device(
            DeviceAcquire(
                task_id=task_b.id,
                owner="worker-b",
                task_lease_token=claim_b.lease_token,
                required_capabilities={"android"},
            )
        )

        assert new_claim is not None
        assert new_claim.device.id == device.id
        async with service.repository.transaction() as session:
            old_status = await session.scalar(
                select(DeviceLeaseRecord.status).where(
                    DeviceLeaseRecord.id == old_claim.lease.id
                )
            )
        assert old_status == DeviceLeaseStatus.EXPIRED.value
    finally:
        await database.shutdown()
