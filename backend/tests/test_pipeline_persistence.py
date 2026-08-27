import asyncio
import json
import sqlite3
from pathlib import Path

import pytest
from sqlalchemy.exc import IntegrityError

from app.database.session import Database
from app.pipeline.models import (
    PipelineCallbackRequest,
    PipelineJobSpec,
    PipelineStageSpec,
    PipelineStatus,
    PipelineTriggerRequest,
)
from app.pipeline.persistence import (
    PipelinePersistenceState,
    SQLAlchemyPipelinePersistence,
)
from app.pipeline.service import (
    InMemoryPipelineService,
    PipelineIdempotencyConflictError,
    create_pipeline_service,
)


def clone_state(state: PipelinePersistenceState) -> PipelinePersistenceState:
    return PipelinePersistenceState(
        runs={
            run_id: run.model_copy(deep=True)
            for run_id, run in state.runs.items()
        },
        trigger_keys=dict(state.trigger_keys),
        callback_events={
            run_id: dict(events)
            for run_id, events in state.callback_events.items()
        },
    )


class FailOncePersistence:
    def __init__(self) -> None:
        self.state = PipelinePersistenceState()
        self.fail_next_save = False

    async def load(self) -> PipelinePersistenceState:
        return clone_state(self.state)

    async def save(self, state: PipelinePersistenceState) -> None:
        if self.fail_next_save:
            self.fail_next_save = False
            raise RuntimeError("injected checkpoint failure")
        self.state = clone_state(state)

    async def clear(self) -> None:
        self.state = PipelinePersistenceState()


def persisted_request(
    *,
    auto_start: bool,
    idempotency_key: str | None = None,
    duration_ms: int = 0,
) -> PipelineTriggerRequest:
    return PipelineTriggerRequest(
        name="persisted-pipeline",
        auto_start=auto_start,
        idempotency_key=idempotency_key,
        variables={"branch": "local-learning"},
        stages=[
            PipelineStageSpec(
                name="test",
                jobs=[PipelineJobSpec(name="pytest", duration_ms=duration_ms)],
            )
        ],
    )


@pytest.mark.asyncio
async def test_completed_run_tree_survives_service_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "qa.db"
    service = create_pipeline_service(database_path, initialize_schema=True)
    created = await service.trigger(persisted_request(auto_start=True))
    completed = await service.wait_for_terminal(created.pipeline.id)
    await service.shutdown()

    restarted = create_pipeline_service(database_path, initialize_schema=True)
    restored = await restarted.get(completed.id)

    assert restored.model_dump() == completed.model_dump()
    assert restored.stages[0].status == PipelineStatus.SUCCEEDED
    assert restored.stages[0].jobs[0].status == PipelineStatus.SUCCEEDED
    assert [run.id for run in await restarted.list_runs()] == [completed.id]
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_legacy_json_variable_snapshots_remain_readable(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "legacy-variables.db"
    service = create_pipeline_service(database_path, initialize_schema=True)
    created = await service.trigger(persisted_request(auto_start=False))
    await service.shutdown()

    with sqlite3.connect(database_path) as connection:
        raw = connection.execute(
            "SELECT snapshot_json FROM pipeline_runtime_runs WHERE id = ?",
            (created.pipeline.id,),
        ).fetchone()[0]
        snapshot = json.loads(raw)
        snapshot["variables"] = {
            "retry": 1,
            "enabled": True,
            "matrix": {"python": ["3.9", "3.10"]},
        }
        connection.execute(
            "UPDATE pipeline_runtime_runs SET snapshot_json = ? WHERE id = ?",
            (
                json.dumps(snapshot, separators=(",", ":"), sort_keys=True),
                created.pipeline.id,
            ),
        )

    restarted = create_pipeline_service(database_path, initialize_schema=True)
    try:
        restored = await restarted.get(created.pipeline.id)
        assert restored.variables == snapshot["variables"]
    finally:
        await restarted.shutdown()


@pytest.mark.asyncio
async def test_alembic_owned_schema_supports_pipeline_persistence(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "application-owned-schema.db"
    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    await database.initialize()

    service = create_pipeline_service(
        database_path,
        initialize_schema=False,
    )
    try:
        created = await service.trigger(persisted_request(auto_start=False))
        restored = await service.get(created.pipeline.id)
        assert restored.model_dump() == created.pipeline.model_dump()
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_application_pipeline_uses_shared_async_database_adapter(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "shared-async-adapter.db"
    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    first = create_pipeline_service(database=database)
    created = await first.trigger(persisted_request(auto_start=False))
    await first.shutdown()

    restarted = create_pipeline_service(database=database)
    try:
        restored = await restarted.get(created.pipeline.id)
        assert restored.id == created.pipeline.id
        assert restored.status == PipelineStatus.CANCELLED
        assert restored.message == "pipeline service shut down"
    finally:
        await restarted.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_async_checkpoint_failure_rolls_back_full_table_replacement(
    tmp_path: Path,
) -> None:
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'atomic-checkpoint.db').as_posix()}"
    )
    persistence = SQLAlchemyPipelinePersistence(database)
    service = InMemoryPipelineService(persistence)
    created = await service.trigger(
        persisted_request(
            auto_start=False,
            idempotency_key="durable-before-failure",
        )
    )
    durable = await persistence.load()
    invalid = clone_state(durable)
    invalid.trigger_keys["missing-parent"] = (
        "missing-run-id",
        "invalid-fingerprint",
    )

    try:
        with pytest.raises(IntegrityError):
            await persistence.save(invalid)

        restored = await persistence.load()
        assert set(restored.runs) == {created.pipeline.id}
        assert restored.trigger_keys == durable.trigger_keys
        assert "missing-parent" not in restored.trigger_keys
    finally:
        await service.shutdown()
        await database.shutdown()


def test_pipeline_factory_rejects_two_database_owners(tmp_path: Path) -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    with pytest.raises(ValueError, match="mutually exclusive"):
        create_pipeline_service(tmp_path / "duplicate.db", database=database)


@pytest.mark.asyncio
async def test_failed_checkpoint_rolls_back_idempotent_memory_state() -> None:
    persistence = FailOncePersistence()
    service = InMemoryPipelineService(persistence)
    request = persisted_request(
        auto_start=False,
        idempotency_key="retry-after-storage-error",
    )

    persistence.fail_next_save = True
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        await service.trigger(request)
    assert await service.list_runs() == []

    created = await service.trigger(request)
    assert created.replayed is False

    callback = PipelineCallbackRequest(
        event_id="retry-callback-after-storage-error",
        status=PipelineStatus.CANCELLED,
    )
    persistence.fail_next_save = True
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        await service.apply_callback(created.pipeline.id, callback)
    assert (await service.get(created.pipeline.id)).status == PipelineStatus.QUEUED

    applied = await service.apply_callback(created.pipeline.id, callback)
    assert applied.duplicate is False
    assert applied.pipeline.status == PipelineStatus.CANCELLED
    await service.shutdown()

    restarted = InMemoryPipelineService(persistence)
    duplicate = await restarted.apply_callback(created.pipeline.id, callback)
    assert duplicate.duplicate is True
    assert duplicate.pipeline.status == PipelineStatus.CANCELLED
    await restarted.shutdown()


@pytest.mark.asyncio
async def test_failed_cancel_checkpoint_does_not_stop_active_executor() -> None:
    persistence = FailOncePersistence()
    service = InMemoryPipelineService(persistence)
    created = await service.trigger(
        persisted_request(auto_start=True, duration_ms=500)
    )

    for _ in range(100):
        running = await service.get(created.pipeline.id)
        if running.stages[0].jobs[0].status == PipelineStatus.RUNNING:
            break
        await asyncio.sleep(0.01)
    else:
        pytest.fail("local executor did not start the job")

    persistence.fail_next_save = True
    with pytest.raises(RuntimeError, match="injected checkpoint failure"):
        await service.cancel(created.pipeline.id)

    rolled_back = await service.get(created.pipeline.id)
    assert rolled_back.status == PipelineStatus.RUNNING
    completed = await service.wait_for_terminal(created.pipeline.id, timeout=2.0)
    assert completed.status == PipelineStatus.SUCCEEDED
    await service.shutdown()


@pytest.mark.asyncio
async def test_trigger_and_callback_idempotency_survive_restart(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "qa.db"
    request = persisted_request(
        auto_start=False,
        idempotency_key="trigger-event-001",
    )
    callback = PipelineCallbackRequest(
        event_id="provider-event-001",
        status=PipelineStatus.RUNNING,
    )
    service = create_pipeline_service(database_path, initialize_schema=True)
    created = await service.trigger(request)
    await service.apply_callback(created.pipeline.id, callback)

    # This simulates an abrupt process stop: the next service detects that the
    # durable run was active and recovers it to a safe terminal state.
    restarted = create_pipeline_service(database_path, initialize_schema=True)
    restored = await restarted.get(created.pipeline.id)
    replay = await restarted.trigger(request)
    duplicate = await restarted.apply_callback(created.pipeline.id, callback)

    assert restored.status == PipelineStatus.CANCELLED
    assert restored.message == "pipeline interrupted by service restart"
    assert replay.replayed is True
    assert replay.pipeline.id == created.pipeline.id
    assert duplicate.duplicate is True

    changed_request = request.model_copy(deep=True)
    changed_request.name = "changed"
    with pytest.raises(PipelineIdempotencyConflictError):
        await restarted.trigger(changed_request)

    conflicting_callback = callback.model_copy(deep=True)
    conflicting_callback.status = PipelineStatus.FAILED
    with pytest.raises(PipelineIdempotencyConflictError):
        await restarted.apply_callback(created.pipeline.id, conflicting_callback)

    await restarted.shutdown()


@pytest.mark.asyncio
async def test_reset_clears_all_pipeline_runtime_tables(tmp_path: Path) -> None:
    database_path = tmp_path / "qa.db"
    service = create_pipeline_service(database_path, initialize_schema=True)
    await service.trigger(
        persisted_request(auto_start=False, idempotency_key="clear-me")
    )

    await service.reset()
    assert await service.list_runs() == []
    await service.shutdown()

    restarted = create_pipeline_service(database_path, initialize_schema=True)
    assert await restarted.list_runs() == []
    await restarted.shutdown()

    with sqlite3.connect(database_path) as connection:
        counts = [
            connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in (
                "pipeline_runtime_runs",
                "pipeline_runtime_trigger_keys",
                "pipeline_runtime_callback_events",
            )
        ]
    assert counts == [0, 0, 0]
