import asyncio

import pytest
from pydantic import ValidationError

from app.pipeline.errors import (
    PipelineInvariantError,
    PipelineServiceClosedError,
    PipelineTransitionError,
    map_pipeline_error,
)
from app.pipeline.models import (
    CallbackTarget,
    PipelineCallbackRequest,
    PipelineJobSpec,
    PipelineStageSpec,
    PipelineStatus,
    PipelineTriggerRequest,
)
from app.pipeline.service import InMemoryPipelineService


def callback_request() -> PipelineTriggerRequest:
    return PipelineTriggerRequest(
        name="provider-callback-pipeline",
        auto_start=False,
        stages=[
            PipelineStageSpec(
                name="build",
                jobs=[
                    PipelineJobSpec(name="compile"),
                    PipelineJobSpec(name="package"),
                ],
            ),
            PipelineStageSpec(
                name="test",
                jobs=[PipelineJobSpec(name="unit-test")],
            ),
        ],
    )


def test_pipeline_request_has_bounded_jobs_duration_and_non_secret_variables() -> None:
    with pytest.raises(ValidationError, match="100 jobs"):
        PipelineTriggerRequest(
            name="too-many-jobs",
            stages=[
                PipelineStageSpec(
                    name=f"stage-{stage}",
                    jobs=[
                        PipelineJobSpec(name=f"job-{stage}-{job}")
                        for job in range(20)
                    ],
                )
                for stage in range(6)
            ],
        )

    with pytest.raises(ValidationError, match="five minutes"):
        PipelineTriggerRequest(
            name="too-long",
            stages=[
                PipelineStageSpec(
                    name="long-stage",
                    jobs=[
                        PipelineJobSpec(name=f"job-{job}", duration_ms=60_000)
                        for job in range(6)
                    ],
                )
            ],
        )

    with pytest.raises(ValidationError, match="cannot contain secrets"):
        PipelineTriggerRequest(
            name="secret-rejected",
            stages=[
                PipelineStageSpec(
                    name="safe",
                    jobs=[PipelineJobSpec(name="safe")],
                )
            ],
            variables={"API_TOKEN": "must-not-be-persisted"},
        )


def job_event(
    event_id: str,
    stage_name: str,
    job_name: str,
    status: PipelineStatus,
) -> PipelineCallbackRequest:
    return PipelineCallbackRequest(
        event_id=event_id,
        target=CallbackTarget.JOB,
        stage_name=stage_name,
        job_name=job_name,
        status=status,
    )


@pytest.mark.asyncio
async def test_pipeline_success_requires_children_and_job_success_aggregates() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(callback_request())
    run_id = created.pipeline.id
    early_finish = PipelineCallbackRequest(
        event_id="pipeline-finished",
        status=PipelineStatus.SUCCEEDED,
    )

    with pytest.raises(PipelineInvariantError):
        await service.apply_callback(run_id, early_finish)
    assert (await service.get(run_id)).status == PipelineStatus.QUEUED

    await service.apply_callback(
        run_id,
        job_event("compile-ok", "build", "compile", PipelineStatus.SUCCEEDED),
    )
    partial = await service.apply_callback(
        run_id,
        job_event("package-ok", "build", "package", PipelineStatus.SUCCEEDED),
    )
    assert partial.pipeline.stages[0].status == PipelineStatus.SUCCEEDED
    assert partial.pipeline.status == PipelineStatus.RUNNING

    completed = await service.apply_callback(
        run_id,
        job_event("tests-ok", "test", "unit-test", PipelineStatus.SUCCEEDED),
    )
    assert completed.pipeline.status == PipelineStatus.SUCCEEDED
    assert all(
        stage.status == PipelineStatus.SUCCEEDED
        and all(job.status == PipelineStatus.SUCCEEDED for job in stage.jobs)
        for stage in completed.pipeline.stages
    )

    # The rejected event was not consumed. It can be redelivered after the
    # children make it valid, and becomes a harmless acknowledgement.
    accepted = await service.apply_callback(run_id, early_finish)
    assert accepted.duplicate is False
    assert accepted.pipeline.status == PipelineStatus.SUCCEEDED


@pytest.mark.asyncio
async def test_job_failure_rolls_up_and_terminal_parent_blocks_child_changes() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(callback_request())
    run_id = created.pipeline.id
    failure = job_event(
        "compile-failed", "build", "compile", PipelineStatus.FAILED
    )

    failed = await service.apply_callback(run_id, failure)
    run = failed.pipeline
    assert run.status == PipelineStatus.FAILED
    assert run.stages[0].status == PipelineStatus.FAILED
    assert run.stages[0].jobs[0].status == PipelineStatus.FAILED
    assert run.stages[0].jobs[1].status == PipelineStatus.CANCELLED
    assert run.stages[1].status == PipelineStatus.CANCELLED
    assert all(
        stage.status.is_terminal
        and all(job.status.is_terminal for job in stage.jobs)
        for stage in run.stages
    )

    duplicate = await service.apply_callback(run_id, failure)
    assert duplicate.duplicate is True

    before = (await service.get(run_id)).model_dump()
    with pytest.raises(PipelineTransitionError):
        await service.apply_callback(
            run_id,
            job_event(
                "late-test-start",
                "test",
                "unit-test",
                PipelineStatus.RUNNING,
            ),
        )
    assert (await service.get(run_id)).model_dump() == before


@pytest.mark.asyncio
async def test_stage_cancellation_cancels_pipeline_and_all_unfinished_nodes() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(callback_request())

    cancelled = await service.apply_callback(
        created.pipeline.id,
        PipelineCallbackRequest(
            event_id="build-cancelled",
            target=CallbackTarget.STAGE,
            stage_name="build",
            status=PipelineStatus.CANCELLED,
        ),
    )

    assert cancelled.pipeline.status == PipelineStatus.CANCELLED
    assert all(
        stage.status == PipelineStatus.CANCELLED
        and all(job.status == PipelineStatus.CANCELLED for job in stage.jobs)
        for stage in cancelled.pipeline.stages
    )


@pytest.mark.asyncio
async def test_stage_cannot_succeed_before_all_of_its_jobs() -> None:
    service = InMemoryPipelineService()
    created = await service.trigger(callback_request())

    with pytest.raises(PipelineInvariantError):
        await service.apply_callback(
            created.pipeline.id,
            PipelineCallbackRequest(
                event_id="early-stage-success",
                target=CallbackTarget.STAGE,
                stage_name="build",
                status=PipelineStatus.SUCCEEDED,
            ),
        )

    unchanged = await service.get(created.pipeline.id)
    assert unchanged.status == PipelineStatus.QUEUED
    assert unchanged.stages[0].status == PipelineStatus.QUEUED


@pytest.mark.asyncio
async def test_shutdown_terminates_work_and_reset_reopens_the_service() -> None:
    service = InMemoryPipelineService()
    request = callback_request()
    request.auto_start = True
    request.stages[0].jobs[0].duration_ms = 10_000
    created = await service.trigger(request)
    await asyncio.sleep(0)

    await service.shutdown()

    assert service.is_shutdown is True
    stopped = await service.get(created.pipeline.id)
    assert stopped.status == PipelineStatus.CANCELLED
    assert all(
        stage.status.is_terminal
        and all(job.status.is_terminal for job in stage.jobs)
        for stage in stopped.stages
    )
    with pytest.raises(PipelineServiceClosedError):
        await service.trigger(callback_request())

    await service.reset()
    assert service.is_shutdown is False
    restarted = await service.trigger(callback_request())
    assert restarted.pipeline.status == PipelineStatus.QUEUED
    await service.shutdown()


def test_pipeline_error_mapping_is_transport_neutral_and_stable() -> None:
    invariant = map_pipeline_error(PipelineInvariantError("invalid tree"))
    unavailable = map_pipeline_error(
        PipelineServiceClosedError("service unavailable")
    )

    assert (invariant.status_code, invariant.code) == (409, 40921)
    assert (unavailable.status_code, unavailable.code) == (503, 50320)
