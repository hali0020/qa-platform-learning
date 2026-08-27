import asyncio

import pytest

from app.pipeline.models import (
    PipelineCallbackRequest,
    PipelineJobSpec,
    PipelineStageSpec,
    PipelineStatus,
    PipelineTriggerRequest,
)
from app.pipeline.service import (
    InMemoryPipelineService,
    PipelineIdempotencyConflictError,
)


def pipeline_request(
    *,
    duration_ms: int = 0,
    should_fail: bool = False,
    auto_start: bool = True,
    idempotency_key: str | None = None,
) -> PipelineTriggerRequest:
    return PipelineTriggerRequest(
        name="teaching-pipeline",
        idempotency_key=idempotency_key,
        auto_start=auto_start,
        stages=[
            PipelineStageSpec(
                name="build",
                jobs=[
                    PipelineJobSpec(
                        name="compile",
                        duration_ms=duration_ms,
                        should_fail=should_fail,
                    )
                ],
            ),
            PipelineStageSpec(
                name="test",
                jobs=[PipelineJobSpec(name="unit-test", duration_ms=0)],
            ),
        ],
    )


@pytest.mark.asyncio
async def test_local_pipeline_records_successful_stage_and_job_results() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(pipeline_request())

    assert created.pipeline.status == PipelineStatus.QUEUED
    completed = await service.wait_for_terminal(created.pipeline.id)

    assert completed.status == PipelineStatus.SUCCEEDED
    assert completed.started_at is not None
    assert completed.finished_at is not None
    assert [stage.status for stage in completed.stages] == [
        PipelineStatus.SUCCEEDED,
        PipelineStatus.SUCCEEDED,
    ]
    assert all(
        job.status == PipelineStatus.SUCCEEDED
        for stage in completed.stages
        for job in stage.jobs
    )


@pytest.mark.asyncio
async def test_failure_stops_and_cancels_unexecuted_work() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(pipeline_request(should_fail=True))

    completed = await service.wait_for_terminal(created.pipeline.id)

    assert completed.status == PipelineStatus.FAILED
    assert completed.stages[0].status == PipelineStatus.FAILED
    assert completed.stages[0].jobs[0].status == PipelineStatus.FAILED
    assert completed.stages[1].status == PipelineStatus.CANCELLED
    assert completed.stages[1].jobs[0].status == PipelineStatus.CANCELLED


@pytest.mark.asyncio
async def test_running_pipeline_can_be_cancelled_idempotently() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(pipeline_request(duration_ms=1_000))
    await asyncio.sleep(0)

    cancellation = await service.cancel(created.pipeline.id)
    replay = await service.cancel(created.pipeline.id)
    completed = await service.wait_for_terminal(created.pipeline.id)

    assert cancellation.pipeline.status == PipelineStatus.CANCELLED
    assert cancellation.replayed is False
    assert replay.replayed is True
    assert completed.status == PipelineStatus.CANCELLED
    assert all(
        item.status == PipelineStatus.CANCELLED for item in completed.stages
    )


@pytest.mark.asyncio
async def test_trigger_idempotency_replays_only_an_identical_request() -> None:
    service = InMemoryPipelineService()
    request = pipeline_request(auto_start=False, idempotency_key="request-001")

    first = await service.trigger(request)
    second = await service.trigger(request)

    assert second.replayed is True
    assert second.pipeline.id == first.pipeline.id

    changed = pipeline_request(auto_start=False, idempotency_key="request-001")
    changed.name = "different-pipeline"
    with pytest.raises(PipelineIdempotencyConflictError):
        await service.trigger(changed)


@pytest.mark.asyncio
async def test_callback_event_is_applied_once_and_conflicts_are_detected() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(pipeline_request(auto_start=False))
    run_id = created.pipeline.id
    running_event = PipelineCallbackRequest(
        event_id="provider-event-001",
        status=PipelineStatus.RUNNING,
    )

    first = await service.apply_callback(run_id, running_event)
    duplicate = await service.apply_callback(run_id, running_event)

    assert first.duplicate is False
    assert duplicate.duplicate is True
    assert duplicate.pipeline.status == PipelineStatus.RUNNING

    conflicting_event = PipelineCallbackRequest(
        event_id="provider-event-001",
        status=PipelineStatus.FAILED,
    )
    with pytest.raises(PipelineIdempotencyConflictError):
        await service.apply_callback(run_id, conflicting_event)

    completed = await service.apply_callback(
        run_id,
        PipelineCallbackRequest(
            event_id="provider-event-002",
            status=PipelineStatus.FAILED,
        ),
    )
    assert completed.pipeline.status == PipelineStatus.FAILED
    assert all(stage.status.is_terminal for stage in completed.pipeline.stages)
