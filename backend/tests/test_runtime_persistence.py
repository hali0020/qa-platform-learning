from __future__ import annotations

import asyncio
from datetime import timedelta
from pathlib import Path
import sqlite3
import threading
from types import SimpleNamespace

import pytest
from alembic import command
from alembic.config import Config

from app.automation.cron import CronExpression
from app.core.errors import (
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    InvalidStateError,
)
from app.database.session import Database
from app.runtime.orm import ScheduleRecord
from app.runtime.schemas import (
    DeviceAcquire,
    DeviceCreate,
    DevicePatch,
    ProviderConnectionCreate,
    ProviderConnectionPatch,
    ProviderTriggerPayload,
    ScheduleCreate,
    SchedulePatch,
    TaskEnqueue,
)
from app.runtime.service import create_runtime_service


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def test_runtime_migration_matches_metadata_and_has_no_secret_column(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.db"
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = sqlite_url(database_path)

    command.upgrade(config, "head")
    command.check(config)

    with sqlite3.connect(database_path) as connection:
        tables = {
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master WHERE type = 'table'"
            )
        }
        columns = {
            row[1]
            for row in connection.execute("PRAGMA table_info(provider_connections)")
        }
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
        device_lease_indexes = {
            row[1]: bool(row[2])
            for row in connection.execute("PRAGMA index_list(device_leases)")
        }
    assert {
        "provider_connections",
        "provider_runs",
        "automation_tasks",
        "devices",
        "device_leases",
        "schedules",
        "schedule_fires",
    } <= tables
    assert "secret_env_var" in columns
    assert "secret" not in columns
    assert "token" not in columns
    assert device_lease_indexes["uq_device_leases_one_active_per_device"] is True
    assert revision == ("20260827_0009",)


@pytest.mark.asyncio
async def test_local_provider_runs_are_durable_and_never_need_a_secret(
    tmp_path: Path,
) -> None:
    url = sqlite_url(tmp_path / "runtime.db")
    first_database = Database(url)
    first = create_runtime_service(first_database)
    connection = await first.create_connection(
        ProviderConnectionCreate(
            name="Local CI lesson",
            kind="local",
            definition_ref="safe-local-pipeline",
            enabled=True,
        )
    )
    run = await first.trigger_provider(
        connection.id,
        ProviderTriggerPayload(correlation_id="lesson-1", variables={"SUITE": "smoke"}),
    )
    replay = await first.trigger_provider(
        connection.id,
        ProviderTriggerPayload(correlation_id="lesson-1", variables={"SUITE": "smoke"}),
    )
    assert replay.id == run.id
    with pytest.raises(ConflictError):
        await first.trigger_provider(
            connection.id,
            ProviderTriggerPayload(correlation_id="lesson-1", variables={"SUITE": "full"}),
        )
    await first_database.shutdown()

    second_database = Database(url)
    second = create_runtime_service(second_database)
    try:
        restored = await second.get_provider_run(connection.id, run.id)
        cancelled = await second.cancel_provider_run(connection.id, run.id)
        assert restored.status == "queued"
        assert cancelled.status == "cancelled"
        assert (await second.get_connection(connection.id)).secret_env_var is None
    finally:
        await second_database.shutdown()


@pytest.mark.asyncio
async def test_disabled_local_provider_rejects_operations(tmp_path: Path) -> None:
    database = Database(sqlite_url(tmp_path / "local-disabled.db"))
    service = create_runtime_service(database)
    connection = await service.create_connection(
        ProviderConnectionCreate(
            name="Disabled local lesson",
            kind="local",
            definition_ref="disabled-local",
            enabled=False,
        )
    )
    try:
        with pytest.raises(AuthorizationError, match="连接未开启"):
            await service.test_connection(connection.id)
        with pytest.raises(AuthorizationError, match="连接未开启"):
            await service.trigger_provider(connection.id, ProviderTriggerPayload())
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_external_provider_requires_global_connection_and_environment_gates(
    tmp_path: Path,
) -> None:
    with pytest.raises(ValueError):
        ProviderConnectionCreate(
            name="Unsafe environment reference",
            kind="gitlab",
            base_url="https://ci.example.test",
            definition_ref="42",
            config={"project_id": "42"},
            secret_env_var="PATH",
            enabled=False,
        )
    settings = SimpleNamespace(
        app_env="local-container",
        provider_runtime_mode="local_lab",
        provider_self_hosted_ownership_acknowledged=False,
        provider_allowed_hosts=("ci.example.test",),
        provider_allowed_ports=(443,),
        provider_allowed_networks=("10.20.30.40/32",),
        provider_allow_loopback_http=False,
        provider_secret_env_names=("QA_PROVIDER_SECRET_GITLAB_LESSON_TOKEN",),
    )
    database = Database(sqlite_url(tmp_path / "gates.db"))
    service = create_runtime_service(database, settings, environ={})
    with pytest.raises(BusinessValidationError, match="definition_ref"):
        await service.create_connection(
            ProviderConnectionCreate(
                name="Mismatched GitLab lesson",
                kind="gitlab",
                base_url="https://ci.example.test",
                definition_ref="99",
                config={"project_id": "42"},
                secret_env_var="QA_PROVIDER_SECRET_GITLAB_LESSON_TOKEN",
                enabled=False,
            )
        )
    connection = await service.create_connection(
        ProviderConnectionCreate(
            name="GitLab disabled lesson",
            kind="gitlab",
            base_url="https://ci.example.test",
            definition_ref="42",
            config={"project_id": "42"},
            secret_env_var="QA_PROVIDER_SECRET_GITLAB_LESSON_TOKEN",
            enabled=False,
        )
    )
    try:
        with pytest.raises(AuthorizationError, match="local_lab"):
            await service.test_connection(connection.id)

        enabled_settings = SimpleNamespace(**vars(settings))
        enabled_settings.provider_runtime_mode = "self_hosted_lab"
        enabled_settings.provider_self_hosted_ownership_acknowledged = True
        enabled_service = create_runtime_service(database, enabled_settings, environ={})
        with pytest.raises(AuthorizationError, match="连接未开启"):
            await enabled_service.test_connection(connection.id)
        updated = await enabled_service.update_connection(
            connection.id,
            ProviderConnectionPatch(enabled=True, version=connection.version),
        )
        assert updated.secret_configured is False
        with pytest.raises(AuthorizationError, match="环境变量"):
            await enabled_service.test_connection(connection.id)
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_task_queue_persists_idempotency_leases_retry_and_dead_letter(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_url(tmp_path / "tasks.db"))
    service = create_runtime_service(database)
    payload = TaskEnqueue(
        task_type="qa.quality.generate",
        payload={"project_id": "demo"},
        idempotency_key="report-demo",
        max_attempts=1,
    )
    task, replayed = await service.enqueue_task(payload)
    same, replayed_again = await service.enqueue_task(payload)
    assert replayed is False
    assert replayed_again is True
    assert same.id == task.id
    with pytest.raises(BusinessValidationError, match="固定注册表"):
        await service.enqueue_task(
            TaskEnqueue(task_type="subprocess.run", payload={"command": "whoami"})
        )

    claimed = await service.claim_task("worker-1", ["default"], 30)
    assert claimed is not None
    assert claimed.task.status == "running"
    failed = await service.fail_task(
        task.id,
        "worker-1",
        claimed.lease_token,
        "test_failed",
        retryable=True,
    )
    assert failed.status == "dead_letter"
    assert failed.error_code == "test_failed"
    assert (await service.get_task(task.id)).status == "dead_letter"
    await database.shutdown()


@pytest.mark.asyncio
async def test_device_lease_token_is_one_time_output_and_lease_is_exclusive(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_url(tmp_path / "devices.db"))
    service = create_runtime_service(database)
    task, _ = await service.enqueue_task(
        TaskEnqueue(task_type="qa.device.execute", payload={"case_id": "C-1"})
    )
    task_claim = await service.claim_task("worker-1", ["default"], 60)
    assert task_claim is not None
    assert task_claim.task.id == task.id
    device = await service.create_device(
        DeviceCreate(
            name="Android emulator 1",
            agent_id="agent-local-1",
            platform="android",
            capabilities={"android", "api-35"},
        )
    )
    heartbeat = await service.heartbeat_device(device.id, "agent-local-1")
    with pytest.raises(AuthorizationError, match="任务租约"):
        await service.acquire_device(
            DeviceAcquire(
                task_id=task.id,
                owner="another-worker",
                task_lease_token=task_claim.lease_token,
            )
        )
    with pytest.raises(AuthorizationError, match="任务租约"):
        await service.acquire_device(
            DeviceAcquire(
                task_id=task.id,
                owner="worker-1",
                task_lease_token="wrong-task-token" * 2,
            )
        )
    claimed = await service.acquire_device(
        DeviceAcquire(
            task_id=task.id,
            owner="worker-1",
            task_lease_token=task_claim.lease_token,
            required_capabilities={"android"},
        )
    )
    assert heartbeat.status == "idle"
    assert claimed is not None
    assert claimed.device.status == "reserved"
    assert "token" not in claimed.lease.model_dump()
    assert claimed.lease.expires_at <= task_claim.task.lease_expires_at
    assert await service.acquire_device(
        DeviceAcquire(
            task_id=task.id,
            owner="worker-1",
            task_lease_token=task_claim.lease_token,
        )
    ) is None
    with pytest.raises(AuthorizationError, match="任务租约"):
        await service.renew_device_lease(
            claimed.lease.id,
            "worker-1",
            claimed.lease_token,
            "wrong-task-token" * 2,
            30,
        )
    renewed = await service.renew_device_lease(
        claimed.lease.id,
        "worker-1",
        claimed.lease_token,
        task_claim.lease_token,
        30,
    )
    assert renewed.expires_at <= task_claim.task.lease_expires_at
    await service.cancel_task(task.id)
    with pytest.raises(InvalidStateError, match="请求取消"):
        await service.start_device_work(
            claimed.lease.id,
            "worker-1",
            claimed.lease_token,
        )
    with pytest.raises(InvalidStateError, match="请求取消"):
        await service.renew_device_lease(
            claimed.lease.id,
            "worker-1",
            claimed.lease_token,
            task_claim.lease_token,
            30,
        )
    released = await service.release_device_lease(
        claimed.lease.id, "worker-1", claimed.lease_token
    )
    assert released.status == "released"
    current = await service.get_device(device.id)
    assert current.status == "idle"

    updated = await service.update_device(
        device.id,
        DevicePatch(maintenance=True, version=current.version),
    )
    assert updated.status == "maintenance"
    available = await service.update_device(
        device.id,
        DevicePatch(maintenance=False, version=updated.version),
    )
    assert available.status == "idle"
    await database.shutdown()


@pytest.mark.asyncio
async def test_schedule_tick_persists_one_fire_and_one_task_per_due_instant(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_url(tmp_path / "schedules.db"))
    service = create_runtime_service(database)
    schedule = await service.create_schedule(
        ScheduleCreate(
            name="Minute report lesson",
            task_type="qa.quality.generate",
            payload={"project_id": "demo"},
            cron="* * * * *",
            timezone="UTC",
        )
    )
    assert schedule.next_run_at is not None
    first = await service.tick_schedules(schedule.next_run_at)
    duplicate = await service.tick_schedules(schedule.next_run_at)
    assert len(first) == 1
    assert first[0].status == "enqueued"
    assert first[0].task_id is not None
    assert duplicate == []
    fires = await service.list_schedule_fires(schedule.id)
    tasks = await service.list_tasks()
    assert [fire.id for fire in fires] == [first[0].id]
    assert [task.id for task in tasks] == [first[0].task_id]
    await database.shutdown()


@pytest.mark.asyncio
async def test_scheduler_bounds_long_misfire_backlog_by_policy(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_url(tmp_path / "bounded-schedules.db"))
    service = create_runtime_service(database)
    schedule = await service.create_schedule(
        ScheduleCreate(
            name="Bounded catch-up lesson",
            task_type="qa.quality.generate",
            cron="* * * * *",
            timezone="UTC",
            misfire_policy="catch_up_limited",
            catch_up_limit=3,
            overlap_policy="allow",
        )
    )
    assert schedule.next_run_at is not None
    long_after = schedule.next_run_at + timedelta(days=3650)

    fires = await service.tick_schedules(long_after)
    current = await service.get_schedule(schedule.id)

    assert len(fires) == 3
    assert all(fire.status == "enqueued" for fire in fires)
    assert [fire.scheduled_for for fire in fires] == [
        long_after.replace(second=0, microsecond=0) - timedelta(minutes=2),
        long_after.replace(second=0, microsecond=0) - timedelta(minutes=1),
        long_after.replace(second=0, microsecond=0),
    ]
    assert current.next_run_at is not None and current.next_run_at > long_after
    await database.shutdown()


@pytest.mark.asyncio
async def test_schedule_rule_change_and_reenable_recalculate_next_run(
    tmp_path: Path,
) -> None:
    database = Database(sqlite_url(tmp_path / "schedule-recalculate.db"))
    service = create_runtime_service(database)
    schedule = await service.create_schedule(
        ScheduleCreate(
            name="Recalculate lesson",
            task_type="qa.quality.generate",
            cron="* * * * *",
            timezone="UTC",
        )
    )
    changed = await service.update_schedule(
        schedule.id,
        SchedulePatch(cron="0 * * * *", version=schedule.version),
    )
    assert changed.next_run_at is not None
    assert CronExpression.parse(changed.cron).matches(changed.next_run_at, "UTC")

    disabled = await service.update_schedule(
        schedule.id,
        SchedulePatch(enabled=False, version=changed.version),
    )
    async with service.repository.transaction() as session:
        record = await session.get(ScheduleRecord, schedule.id)
        assert record is not None
        record.next_run_at = changed.next_run_at + timedelta(days=3650)

    reenabled = await service.update_schedule(
        schedule.id,
        SchedulePatch(enabled=True, version=disabled.version),
    )
    assert reenabled.next_run_at is not None
    assert reenabled.next_run_at < changed.next_run_at + timedelta(days=3650)
    assert CronExpression.parse(reenabled.cron).matches(
        reenabled.next_run_at, reenabled.timezone
    )
    await database.shutdown()


@pytest.mark.asyncio
async def test_impossible_cron_rejects_fast_and_search_runs_off_event_loop(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    database = Database(sqlite_url(tmp_path / "cron-safety.db"))
    service = create_runtime_service(database)
    loop = asyncio.get_running_loop()
    started_at = loop.time()
    with pytest.raises(BusinessValidationError, match="never exists"):
        await service.create_schedule(
            ScheduleCreate(
                name="Impossible lesson",
                task_type="qa.quality.generate",
                cron="0 0 31 2 *",
                timezone="UTC",
            )
        )
    assert loop.time() - started_at < 0.2

    original = CronExpression.next_after
    search_started = threading.Event()
    release_search = threading.Event()

    def controlled_search(
        expression: CronExpression,
        instant,
        timezone_name: str,
        **kwargs,
    ):
        search_started.set()
        release_search.wait(timeout=2)
        return original(expression, instant, timezone_name, **kwargs)

    monkeypatch.setattr(CronExpression, "next_after", controlled_search)
    operation = asyncio.create_task(
        service.create_schedule(
            ScheduleCreate(
                name="Threaded cron lesson",
                task_type="qa.quality.generate",
                cron="0 * * * *",
                timezone="UTC",
            )
        )
    )
    try:
        assert await asyncio.to_thread(search_started.wait, 1)
        await asyncio.wait_for(asyncio.sleep(0.01), timeout=0.1)
        assert not operation.done()
    finally:
        release_search.set()
    created = await asyncio.wait_for(operation, timeout=3)
    assert created.next_run_at is not None
    await database.shutdown()
