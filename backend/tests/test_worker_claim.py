"""Worker-claim SQL and state-machine tests.

The PostgreSQL assertions compile SQL offline and do not claim that a real
server or concurrent container was started.  SQLite behavior tests cover the
shared lease invariants while preserving its documented single-process mode.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from sqlalchemy.dialects import postgresql

from app.core.errors import AuthorizationError
from app.database.session import Database
from app.runtime import service as runtime_service_module
from app.runtime.repository import (
    build_expired_task_lease_statement,
    build_task_claim_statement,
)
from app.runtime.schemas import TaskEnqueue
from app.runtime.service import create_runtime_service


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _postgres_sql(statement: object) -> str:
    compiled = statement.compile(dialect=postgresql.dialect())
    return " ".join(str(compiled).upper().split())


def test_postgres_claim_and_recovery_use_skip_locked_offline_sql() -> None:
    now = datetime(2026, 1, 2, tzinfo=timezone.utc)
    claim = build_task_claim_statement(
        queues={"default", "slow"},
        now=now,
        use_skip_locked=True,
    )
    recovery = build_expired_task_lease_statement(
        now=now,
        use_skip_locked=True,
    )

    claim_sql = _postgres_sql(claim)
    recovery_sql = _postgres_sql(recovery)
    assert "FOR UPDATE SKIP LOCKED" in claim_sql
    assert "ORDER BY AUTOMATION_TASKS.PRIORITY DESC" in claim_sql
    assert "LIMIT" in claim_sql
    assert "FOR UPDATE SKIP LOCKED" in recovery_sql
    assert "AUTOMATION_TASKS.LEASE_EXPIRES_AT" in recovery_sql
    assert claim._for_update_arg is not None
    assert claim._for_update_arg.skip_locked is True
    assert recovery._for_update_arg is not None
    assert recovery._for_update_arg.skip_locked is True


def test_sqlite_claim_query_deliberately_omits_row_lock_clause() -> None:
    statement = build_task_claim_statement(
        queues={"default"},
        now=datetime(2026, 1, 2, tzinfo=timezone.utc),
        use_skip_locked=False,
    )

    # Compile with PostgreSQL so this proves the statement itself has no lock;
    # SQLite's compiler would silently omit FOR UPDATE even if one were set.
    assert "FOR UPDATE" not in _postgres_sql(statement)
    assert statement._for_update_arg is None


@pytest.mark.asyncio
async def test_sqlite_duplicate_wakeups_claim_each_task_once(tmp_path: Path) -> None:
    database = Database(_sqlite_url(tmp_path / "duplicate-wakeups.db"))
    service = create_runtime_service(database)
    try:
        created = []
        for priority in (10, 90):
            task, replayed = await service.enqueue_task(
                TaskEnqueue(
                    task_type="qa.quality.generate",
                    payload={"priority": priority},
                    priority=priority,
                )
            )
            assert replayed is False
            created.append(task.id)

        wakeups = await asyncio.gather(
            service.claim_task("worker-1", ["default"], 30),
            service.claim_task("worker-2", ["default"], 30),
            service.claim_task("worker-3", ["default"], 30),
        )
        claimed = [result for result in wakeups if result is not None]

        assert len(claimed) == 2
        assert {result.task.id for result in claimed} == set(created)
        assert len({result.lease_token for result in claimed}) == 2
        assert await service.claim_task("worker-4", ["default"], 30) is None
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_recovery_reclaim_invalidates_the_old_worker_lease(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    clock = {"now": datetime(2026, 1, 2, 3, 4, tzinfo=timezone.utc)}
    monkeypatch.setattr(runtime_service_module, "_utc_now", lambda: clock["now"])
    database = Database(_sqlite_url(tmp_path / "lease-recovery.db"))
    service = create_runtime_service(database)
    try:
        task, _ = await service.enqueue_task(
            TaskEnqueue(
                task_type="qa.quality.generate",
                payload={"project_id": "lease-test"},
                max_attempts=3,
            )
        )
        old_claim = await service.claim_task("old-worker", ["default"], 30)
        assert old_claim is not None

        clock["now"] += timedelta(seconds=31)
        # This wakeup owns the recovery transaction. Backoff means it must not
        # immediately reclaim the just-expired row in the same call.
        assert await service.claim_task("recovery-worker", ["default"], 30) is None
        recovered = await service.get_task(task.id)
        assert recovered.status == "retry_wait"
        assert recovered.error_code == "lease_expired"
        assert recovered.lease_owner is None

        clock["now"] += timedelta(seconds=2)
        new_claim = await service.claim_task("new-worker", ["default"], 30)
        assert new_claim is not None
        assert new_claim.task.id == task.id
        assert new_claim.task.attempts == 2
        assert new_claim.lease_token != old_claim.lease_token

        with pytest.raises(AuthorizationError, match="任务租约"):
            await service.heartbeat_task(
                task.id,
                "old-worker",
                old_claim.lease_token,
                30,
            )
        with pytest.raises(AuthorizationError, match="任务租约"):
            await service.complete_task(
                task.id,
                "old-worker",
                old_claim.lease_token,
                {"stale": True},
            )

        completed = await service.complete_task(
            task.id,
            "new-worker",
            new_claim.lease_token,
            {"ok": True},
        )
        assert completed.status == "succeeded"
        assert completed.result == {"ok": True}
    finally:
        await database.shutdown()
