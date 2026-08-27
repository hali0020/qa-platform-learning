import asyncio
import json
from datetime import datetime, timezone
from hashlib import sha256
from pathlib import Path
from typing import Protocol, TypeVar
from uuid import uuid4

from pydantic import BaseModel

from app.database.session import Database
from app.pipeline.errors import (
    PipelineError,
    PipelineIdempotencyConflictError,
    PipelineInvariantError,
    PipelineNotFoundError,
    PipelineServiceClosedError,
    PipelineTargetNotFoundError,
    PipelineTransitionError,
)
from app.pipeline.models import (
    CallbackTarget,
    PipelineCallbackRequest,
    PipelineCallbackResult,
    PipelineCancellationResult,
    PipelineJobResult,
    PipelineRun,
    PipelineStageResult,
    PipelineStatus,
    PipelineTriggerRequest,
    PipelineTriggerResult,
)
from app.pipeline.persistence import (
    PipelinePersistence,
    PipelinePersistenceState,
    SQLAlchemyPipelinePersistence,
    SQLitePipelinePersistence,
)


_ALLOWED_TRANSITIONS: dict[PipelineStatus, set[PipelineStatus]] = {
    PipelineStatus.QUEUED: {PipelineStatus.RUNNING, PipelineStatus.CANCELLED},
    PipelineStatus.RUNNING: {
        PipelineStatus.SUCCEEDED,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    },
    PipelineStatus.SUCCEEDED: set(),
    PipelineStatus.FAILED: set(),
    PipelineStatus.CANCELLED: set(),
}

_ResultModel = TypeVar(
    "_ResultModel", PipelineRun, PipelineStageResult, PipelineJobResult
)


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _fingerprint(model: BaseModel, *excluded_fields: str) -> str:
    payload = json.dumps(
        model.model_dump(exclude=set(excluded_fields), mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(payload.encode("utf-8")).hexdigest()


def _clone_persistence_state(
    state: PipelinePersistenceState,
) -> PipelinePersistenceState:
    """Detach a checkpoint from every mutable in-memory domain object."""

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


def _assert_transition_allowed(
    result: _ResultModel, new_status: PipelineStatus
) -> None:
    if result.status == new_status:
        return
    # Providers commonly omit a running event. Internally we still pass
    # through running so timestamps and the state machine remain consistent.
    if result.status == PipelineStatus.QUEUED and new_status in {
        PipelineStatus.SUCCEEDED,
        PipelineStatus.FAILED,
    }:
        return
    if new_status not in _ALLOWED_TRANSITIONS[result.status]:
        raise PipelineTransitionError(
            f"cannot transition {result.status.value} to {new_status.value}"
        )


def _transition(
    result: _ResultModel,
    new_status: PipelineStatus,
    *,
    message: str | None = None,
) -> None:
    if result.status == new_status:
        # A fresh provider event with the same state is harmless, but it must
        # not mutate a terminal result (including its message).
        return
    if new_status not in _ALLOWED_TRANSITIONS[result.status]:
        raise PipelineTransitionError(
            f"cannot transition {result.status.value} to {new_status.value}"
        )

    changed_at = _now()
    result.status = new_status
    if new_status == PipelineStatus.RUNNING and result.started_at is None:
        result.started_at = changed_at
    if new_status.is_terminal:
        result.finished_at = changed_at
    if message is not None:
        result.message = message


def _start(result: _ResultModel) -> None:
    if result.status == PipelineStatus.QUEUED:
        _transition(result, PipelineStatus.RUNNING)
    elif result.status != PipelineStatus.RUNNING:
        raise PipelineTransitionError(
            f"cannot start a {result.status.value} result"
        )


def _finish(
    result: _ResultModel,
    new_status: PipelineStatus,
    *,
    message: str | None = None,
) -> None:
    if new_status not in {
        PipelineStatus.SUCCEEDED,
        PipelineStatus.FAILED,
        PipelineStatus.CANCELLED,
    }:
        raise ValueError("_finish requires a terminal status")
    if result.status == new_status:
        return
    if result.status == PipelineStatus.QUEUED and new_status != PipelineStatus.CANCELLED:
        _start(result)
    _transition(result, new_status, message=message)


class PipelineService(Protocol):
    """Interface consumed by the API and future CI/CD provider adapters."""

    async def initialize(self) -> None: ...

    async def trigger(
        self, request: PipelineTriggerRequest
    ) -> PipelineTriggerResult: ...

    async def get(self, run_id: str) -> PipelineRun: ...

    async def list_runs(self) -> list[PipelineRun]: ...

    async def cancel(self, run_id: str) -> PipelineCancellationResult: ...

    async def apply_callback(
        self, run_id: str, callback: PipelineCallbackRequest
    ) -> PipelineCallbackResult: ...


class InMemoryPipelineService:
    """Concurrency-safe, local-only pipeline simulator.

    It executes deterministic sleeps only. It never starts a subprocess, opens
    a socket, or calls a CI/CD server. By default all data remains in memory;
    callers may opt into a durable adapter.  The application adapter shares its
    validated async SQLAlchemy database; the path-based SQLite adapter remains
    only for focused lessons.  All mutations happen while holding one process
    lock, so observers never see partially propagated parent and child states.
    """

    def __init__(
        self,
        persistence: PipelinePersistence | None = None,
    ) -> None:
        self._runs: dict[str, PipelineRun] = {}
        self._trigger_keys: dict[str, tuple[str, str]] = {}
        self._callback_events: dict[str, dict[str, str]] = {}
        self._cancel_events: dict[str, asyncio.Event] = {}
        self._completion_events: dict[str, asyncio.Event] = {}
        self._tasks: dict[str, asyncio.Task[None]] = {}
        self._lock = asyncio.Lock()
        self._persistence = persistence
        self._durable_state: PipelinePersistenceState | None = None
        self._initialized = persistence is None
        self._closed = False

    @property
    def is_closed(self) -> bool:
        """Expose lifecycle state without leaking mutable internals."""
        return self._closed

    @property
    def is_shutdown(self) -> bool:
        return self._closed

    async def __aenter__(self) -> "InMemoryPipelineService":
        return self

    async def __aexit__(self, *_exc_info: object) -> None:
        await self.shutdown()

    async def initialize(self) -> None:
        """Restore durable state before the application starts serving."""

        async with self._lock:
            await self._initialize_locked()

    async def trigger(self, request: PipelineTriggerRequest) -> PipelineTriggerResult:
        """Create a queued run and optionally schedule the local executor."""

        request_fingerprint = _fingerprint(request, "idempotency_key")
        async with self._lock:
            await self._initialize_locked()
            self._ensure_open()
            if request.idempotency_key is not None:
                previous = self._trigger_keys.get(request.idempotency_key)
                if previous is not None:
                    run_id, previous_fingerprint = previous
                    if previous_fingerprint != request_fingerprint:
                        raise PipelineIdempotencyConflictError(
                            "idempotency key was reused with a different request"
                        )
                    return PipelineTriggerResult(
                        pipeline=self._snapshot(self._runs[run_id]),
                        replayed=True,
                    )

            run_id = str(uuid4())
            run = PipelineRun(
                id=run_id,
                name=request.name,
                stages=[
                    PipelineStageResult(
                        name=stage.name,
                        jobs=[
                            PipelineJobResult(
                                name=job.name,
                                duration_ms=job.duration_ms,
                                should_fail=job.should_fail,
                            )
                            for job in stage.jobs
                        ],
                    )
                    for stage in request.stages
                ],
                variables=dict(request.variables),
                idempotency_key=request.idempotency_key,
                created_at=_now(),
            )
            self._runs[run_id] = run
            self._callback_events[run_id] = {}
            self._cancel_events[run_id] = asyncio.Event()
            self._completion_events[run_id] = asyncio.Event()
            if request.idempotency_key is not None:
                self._trigger_keys[request.idempotency_key] = (
                    run_id,
                    request_fingerprint,
                )
            snapshot = self._snapshot(run)
            await self._checkpoint_locked()

            if request.auto_start:
                task = asyncio.create_task(
                    self._execute(run_id), name=f"local-pipeline-{run_id}"
                )
                self._tasks[run_id] = task
                task.add_done_callback(
                    lambda completed, current_id=run_id: self._task_finished(
                        current_id, completed
                    )
                )

        return PipelineTriggerResult(pipeline=snapshot)

    async def get(self, run_id: str) -> PipelineRun:
        async with self._lock:
            await self._initialize_locked()
            return self._snapshot(self._require_run(run_id))

    async def list_runs(self) -> list[PipelineRun]:
        async with self._lock:
            await self._initialize_locked()
            ordered = sorted(
                self._runs.values(), key=lambda run: run.created_at, reverse=True
            )
            return [self._snapshot(run) for run in ordered]

    async def cancel(self, run_id: str) -> PipelineCancellationResult:
        async with self._lock:
            await self._initialize_locked()
            self._ensure_open()
            run = self._require_run(run_id)
            if run.status == PipelineStatus.CANCELLED:
                return PipelineCancellationResult(
                    pipeline=self._snapshot(run), replayed=True
                )
            if run.status.is_terminal:
                raise PipelineTransitionError(
                    f"cannot cancel a {run.status.value} pipeline"
                )

            _finish(run, PipelineStatus.CANCELLED, message="cancelled by user")
            self._cancel_unfinished_children(run, "pipeline was cancelled")
            self._mark_terminal(run_id, run)
            self._assert_run_invariants(run)
            await self._checkpoint_locked()
            return PipelineCancellationResult(pipeline=self._snapshot(run))

    async def apply_callback(
        self, run_id: str, callback: PipelineCallbackRequest
    ) -> PipelineCallbackResult:
        """Apply one provider event and atomically reconcile the full tree."""

        callback_fingerprint = _fingerprint(callback, "event_id")
        async with self._lock:
            await self._initialize_locked()
            self._ensure_open()
            run = self._require_run(run_id)
            delivered_events = self._callback_events[run_id]
            previous_fingerprint = delivered_events.get(callback.event_id)
            if previous_fingerprint is not None:
                if previous_fingerprint != callback_fingerprint:
                    raise PipelineIdempotencyConflictError(
                        "callback event_id was reused with different content"
                    )
                return PipelineCallbackResult(
                    pipeline=self._snapshot(run), duplicate=True
                )

            target = self._callback_target(run, callback)
            self._validate_callback(run, callback, target)
            self._apply_callback(run_id, run, callback, target)
            self._assert_run_invariants(run)
            # Failed validation or transition attempts are deliberately not
            # recorded, allowing a provider to correct and redeliver them.
            delivered_events[callback.event_id] = callback_fingerprint
            await self._checkpoint_locked()
            return PipelineCallbackResult(pipeline=self._snapshot(run))

    async def wait_for_terminal(
        self, run_id: str, *, timeout: float = 5.0
    ) -> PipelineRun:
        """Wait for completion without polling (useful in tests and lessons)."""

        async with self._lock:
            await self._initialize_locked()
            run = self._require_run(run_id)
            if run.status.is_terminal:
                return self._snapshot(run)
            completion_event = self._completion_events[run_id]
        await asyncio.wait_for(completion_event.wait(), timeout=timeout)
        return await self.get(run_id)

    async def shutdown(self) -> None:
        """Stop executors and leave every existing run in a terminal state.

        Queries remain available after shutdown. Mutating operations fail with
        ``PipelineServiceClosedError`` until ``reset`` is called.
        """

        async with self._lock:
            await self._initialize_locked()
            self._closed = True
            for run_id, run in self._runs.items():
                if not run.status.is_terminal:
                    _finish(
                        run,
                        PipelineStatus.CANCELLED,
                        message="pipeline service shut down",
                    )
                    self._cancel_unfinished_children(
                        run, "pipeline service shut down"
                    )
                    self._mark_terminal(run_id, run)
                    self._assert_run_invariants(run)
            await self._checkpoint_locked()
            tasks = list(self._tasks.values())
            for task in tasks:
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._tasks.clear()

    async def reset(self) -> None:
        """Stop tasks, clear all local state, and reopen the service."""

        async with self._lock:
            await self._initialize_locked()
            # Close during reset so another coroutine cannot create work that
            # would be cleared after the task snapshot below.
            self._closed = True
            tasks = list(self._tasks.values())
            for event in self._cancel_events.values():
                event.set()
            for event in self._completion_events.values():
                event.set()
            for task in tasks:
                task.cancel()

        if tasks:
            await asyncio.gather(*tasks, return_exceptions=True)
        async with self._lock:
            self._runs.clear()
            self._trigger_keys.clear()
            self._callback_events.clear()
            self._cancel_events.clear()
            self._completion_events.clear()
            self._tasks.clear()
            if self._persistence is not None:
                await self._persistence.clear()
                self._durable_state = PipelinePersistenceState()
            self._closed = False

    async def _execute(self, run_id: str) -> None:
        try:
            async with self._lock:
                await self._initialize_locked()
                run = self._require_run(run_id)
                if run.status != PipelineStatus.QUEUED:
                    return
                _start(run)
                stage_count = len(run.stages)
                await self._checkpoint_locked()

            for stage_index in range(stage_count):
                async with self._lock:
                    run = self._require_run(run_id)
                    if run.status != PipelineStatus.RUNNING:
                        return
                    stage = run.stages[stage_index]
                    if stage.status == PipelineStatus.SUCCEEDED:
                        continue
                    _start(stage)
                    job_count = len(stage.jobs)
                    await self._checkpoint_locked()

                for job_index in range(job_count):
                    async with self._lock:
                        run = self._require_run(run_id)
                        if run.status != PipelineStatus.RUNNING:
                            return
                        stage = run.stages[stage_index]
                        if stage.status != PipelineStatus.RUNNING:
                            return
                        job = stage.jobs[job_index]
                        if job.status == PipelineStatus.SUCCEEDED:
                            self._aggregate_success(run_id, run, stage)
                            await self._checkpoint_locked()
                            continue
                        _start(job)
                        duration_ms = job.duration_ms
                        await self._checkpoint_locked()

                    stopped = await self._wait_or_cancel(run_id, duration_ms)
                    if stopped:
                        return

                    async with self._lock:
                        run = self._require_run(run_id)
                        if run.status != PipelineStatus.RUNNING:
                            return
                        current_stage = run.stages[stage_index]
                        current_job = current_stage.jobs[job_index]
                        if current_job.status == PipelineStatus.SUCCEEDED:
                            self._aggregate_success(run_id, run, current_stage)
                            await self._checkpoint_locked()
                            continue
                        if current_job.should_fail:
                            self._fail_job(
                                run_id,
                                run,
                                current_stage,
                                current_job,
                                message="deterministic simulated failure",
                            )
                            self._assert_run_invariants(run)
                            await self._checkpoint_locked()
                            return
                        _finish(current_job, PipelineStatus.SUCCEEDED)
                        self._aggregate_success(run_id, run, current_stage)
                        self._assert_run_invariants(run)
                        await self._checkpoint_locked()

                async with self._lock:
                    run = self._require_run(run_id)
                    if run.status != PipelineStatus.RUNNING:
                        return
                    self._aggregate_success(
                        run_id, run, run.stages[stage_index]
                    )
                    self._assert_run_invariants(run)
                    await self._checkpoint_locked()

            async with self._lock:
                run = self._require_run(run_id)
                if run.status == PipelineStatus.RUNNING:
                    self._aggregate_success(run_id, run)
                    self._assert_run_invariants(run)
                    await self._checkpoint_locked()
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # pragma: no cover - defensive task boundary
            async with self._lock:
                run = self._runs.get(run_id)
                if run is not None and not run.status.is_terminal:
                    self._fail_run(
                        run_id,
                        run,
                        message=f"internal simulator error: {exc}",
                    )
                    await self._checkpoint_locked()

    async def _wait_or_cancel(self, run_id: str, duration_ms: int) -> bool:
        cancel_event = self._cancel_events[run_id]
        if duration_ms == 0:
            await asyncio.sleep(0)
            return cancel_event.is_set()
        try:
            await asyncio.wait_for(cancel_event.wait(), timeout=duration_ms / 1000)
            return True
        except asyncio.TimeoutError:
            return False

    def _validate_callback(
        self,
        run: PipelineRun,
        callback: PipelineCallbackRequest,
        target: PipelineRun | PipelineStageResult | PipelineJobResult,
    ) -> None:
        if callback.target != CallbackTarget.PIPELINE and run.status.is_terminal:
            raise PipelineTransitionError(
                f"cannot change a child of a {run.status.value} pipeline"
            )

        stage = self._stage_for_callback(run, callback)
        if callback.target == CallbackTarget.JOB and stage is not None:
            if stage.status.is_terminal:
                raise PipelineTransitionError(
                    f"cannot change a child of a {stage.status.value} stage"
                )

        _assert_transition_allowed(target, callback.status)

        if callback.status == PipelineStatus.SUCCEEDED:
            if callback.target == CallbackTarget.PIPELINE:
                if not all(
                    stage.status == PipelineStatus.SUCCEEDED
                    and all(
                        job.status == PipelineStatus.SUCCEEDED
                        for job in stage.jobs
                    )
                    for stage in run.stages
                ):
                    raise PipelineInvariantError(
                        "pipeline cannot succeed until every stage and job succeeds"
                    )
            elif callback.target == CallbackTarget.STAGE:
                assert stage is not None
                if not all(
                    job.status == PipelineStatus.SUCCEEDED for job in stage.jobs
                ):
                    raise PipelineInvariantError(
                        "stage cannot succeed until every job succeeds"
                    )

    def _apply_callback(
        self,
        run_id: str,
        run: PipelineRun,
        callback: PipelineCallbackRequest,
        target: PipelineRun | PipelineStageResult | PipelineJobResult,
    ) -> None:
        if callback.target == CallbackTarget.PIPELINE:
            assert isinstance(target, PipelineRun)
            self._apply_pipeline_callback(run_id, target, callback)
            return

        stage = self._stage_for_callback(run, callback)
        assert stage is not None
        if callback.target == CallbackTarget.STAGE:
            assert isinstance(target, PipelineStageResult)
            self._apply_stage_callback(run_id, run, target, callback)
            return

        assert isinstance(target, PipelineJobResult)
        self._apply_job_callback(run_id, run, stage, target, callback)

    def _apply_pipeline_callback(
        self,
        run_id: str,
        run: PipelineRun,
        callback: PipelineCallbackRequest,
    ) -> None:
        if callback.status == PipelineStatus.QUEUED:
            _transition(run, PipelineStatus.QUEUED, message=callback.message)
        elif callback.status == PipelineStatus.RUNNING:
            _start(run)
        elif callback.status == PipelineStatus.SUCCEEDED:
            _finish(run, PipelineStatus.SUCCEEDED, message=callback.message)
            self._mark_terminal(run_id, run)
        elif callback.status == PipelineStatus.FAILED:
            self._fail_run(run_id, run, message=callback.message)
        else:
            _finish(run, PipelineStatus.CANCELLED, message=callback.message)
            self._cancel_unfinished_children(
                run, "pipeline was cancelled by provider"
            )
            self._mark_terminal(run_id, run)

    def _apply_stage_callback(
        self,
        run_id: str,
        run: PipelineRun,
        stage: PipelineStageResult,
        callback: PipelineCallbackRequest,
    ) -> None:
        if callback.status == PipelineStatus.QUEUED:
            _transition(stage, PipelineStatus.QUEUED, message=callback.message)
            return
        if callback.status == PipelineStatus.RUNNING:
            _start(run)
            _start(stage)
            return
        if callback.status == PipelineStatus.SUCCEEDED:
            _start(run)
            _finish(stage, PipelineStatus.SUCCEEDED, message=callback.message)
            self._aggregate_success(run_id, run, stage)
            return
        if callback.status == PipelineStatus.FAILED:
            _start(run)
            _finish(stage, PipelineStatus.FAILED, message=callback.message)
            self._cancel_unfinished_jobs(
                stage, "not executed after the stage failed"
            )
            self._fail_run(
                run_id, run, message=f"stage {stage.name} failed"
            )
            return

        _finish(stage, PipelineStatus.CANCELLED, message=callback.message)
        self._cancel_unfinished_jobs(stage, "stage was cancelled by provider")
        _finish(
            run,
            PipelineStatus.CANCELLED,
            message=f"stage {stage.name} was cancelled",
        )
        self._cancel_unfinished_children(
            run, "not executed after a stage was cancelled"
        )
        self._mark_terminal(run_id, run)

    def _apply_job_callback(
        self,
        run_id: str,
        run: PipelineRun,
        stage: PipelineStageResult,
        job: PipelineJobResult,
        callback: PipelineCallbackRequest,
    ) -> None:
        if callback.status == PipelineStatus.QUEUED:
            _transition(job, PipelineStatus.QUEUED, message=callback.message)
            return
        if callback.status == PipelineStatus.RUNNING:
            _start(run)
            _start(stage)
            _start(job)
            return
        if callback.status == PipelineStatus.SUCCEEDED:
            _start(run)
            _start(stage)
            _finish(job, PipelineStatus.SUCCEEDED, message=callback.message)
            self._aggregate_success(run_id, run, stage)
            return
        if callback.status == PipelineStatus.FAILED:
            _start(run)
            _start(stage)
            self._fail_job(
                run_id, run, stage, job, message=callback.message
            )
            return

        _finish(job, PipelineStatus.CANCELLED, message=callback.message)
        _finish(
            stage,
            PipelineStatus.CANCELLED,
            message=f"job {job.name} was cancelled",
        )
        self._cancel_unfinished_jobs(
            stage, "not executed after a job was cancelled"
        )
        _finish(
            run,
            PipelineStatus.CANCELLED,
            message=f"stage {stage.name}, job {job.name} was cancelled",
        )
        self._cancel_unfinished_children(
            run, "not executed after a job was cancelled"
        )
        self._mark_terminal(run_id, run)

    def _aggregate_success(
        self,
        run_id: str,
        run: PipelineRun,
        stage: PipelineStageResult | None = None,
    ) -> None:
        if stage is not None and all(
            job.status == PipelineStatus.SUCCEEDED for job in stage.jobs
        ):
            _finish(stage, PipelineStatus.SUCCEEDED)
        if all(
            current_stage.status == PipelineStatus.SUCCEEDED
            for current_stage in run.stages
        ):
            _finish(run, PipelineStatus.SUCCEEDED)
            self._mark_terminal(run_id, run)

    def _fail_job(
        self,
        run_id: str,
        run: PipelineRun,
        stage: PipelineStageResult,
        job: PipelineJobResult,
        *,
        message: str | None,
    ) -> None:
        _finish(job, PipelineStatus.FAILED, message=message)
        _finish(
            stage,
            PipelineStatus.FAILED,
            message=f"job {job.name} failed",
        )
        self._cancel_unfinished_jobs(
            stage, "not executed after an earlier job failed"
        )
        self._fail_run(
            run_id,
            run,
            message=f"stage {stage.name}, job {job.name} failed",
        )

    def _fail_run(
        self, run_id: str, run: PipelineRun, *, message: str | None
    ) -> None:
        _finish(run, PipelineStatus.FAILED, message=message)
        self._cancel_unfinished_children(
            run, "not executed after the pipeline failed"
        )
        self._mark_terminal(run_id, run)

    def _mark_terminal(self, _run_id: str, run: PipelineRun) -> None:
        if not run.status.is_terminal:
            raise PipelineInvariantError("completion requires a terminal pipeline")
        # Events are synchronized by _checkpoint_locked only after a durable
        # save succeeds. This method deliberately validates without publishing.

    def _callback_target(
        self, run: PipelineRun, callback: PipelineCallbackRequest
    ) -> PipelineRun | PipelineStageResult | PipelineJobResult:
        if callback.target == CallbackTarget.PIPELINE:
            return run

        stage = self._stage_for_callback(run, callback)
        if stage is None:
            raise PipelineTargetNotFoundError(
                f"stage not found: {callback.stage_name}"
            )
        if callback.target == CallbackTarget.STAGE:
            return stage

        job = next(
            (item for item in stage.jobs if item.name == callback.job_name), None
        )
        if job is None:
            raise PipelineTargetNotFoundError(
                f"job not found: {callback.job_name}"
            )
        return job

    @staticmethod
    def _stage_for_callback(
        run: PipelineRun, callback: PipelineCallbackRequest
    ) -> PipelineStageResult | None:
        if callback.stage_name is None:
            return None
        return next(
            (item for item in run.stages if item.name == callback.stage_name),
            None,
        )

    @staticmethod
    def _cancel_unfinished_jobs(
        stage: PipelineStageResult, message: str
    ) -> None:
        for job in stage.jobs:
            if not job.status.is_terminal:
                _finish(job, PipelineStatus.CANCELLED, message=message)

    @classmethod
    def _cancel_unfinished_children(cls, run: PipelineRun, message: str) -> None:
        for stage in run.stages:
            cls._cancel_unfinished_jobs(stage, message)
            if not stage.status.is_terminal:
                _finish(stage, PipelineStatus.CANCELLED, message=message)

    @staticmethod
    def _assert_run_invariants(run: PipelineRun) -> None:
        for stage in run.stages:
            if stage.status == PipelineStatus.SUCCEEDED and not all(
                job.status == PipelineStatus.SUCCEEDED for job in stage.jobs
            ):
                raise PipelineInvariantError(
                    f"succeeded stage {stage.name} has an unfinished job"
                )
            if stage.status.is_terminal and any(
                not job.status.is_terminal for job in stage.jobs
            ):
                raise PipelineInvariantError(
                    f"terminal stage {stage.name} has an active job"
                )

        if run.status == PipelineStatus.SUCCEEDED and not all(
            stage.status == PipelineStatus.SUCCEEDED for stage in run.stages
        ):
            raise PipelineInvariantError(
                "succeeded pipeline has a non-succeeded stage"
            )
        if run.status.is_terminal and any(
            not stage.status.is_terminal for stage in run.stages
        ):
            raise PipelineInvariantError("terminal pipeline has an active stage")

    async def _initialize_locked(self) -> None:
        """Lazily restore durable state while the service lock is held.

        A graceful shutdown already persists active work as cancelled. If the
        process stopped abruptly, an active snapshot cannot safely resume its
        interrupted sleep, so it is deterministically recovered as cancelled.
        """

        if self._initialized:
            return
        assert self._persistence is not None
        state = await self._persistence.load()
        self._restore_state_locked(state)
        self._durable_state = _clone_persistence_state(state)
        self._initialized = True

        recovered_active_run = False
        for run_id, run in self._runs.items():
            if not run.status.is_terminal:
                _finish(
                    run,
                    PipelineStatus.CANCELLED,
                    message="pipeline interrupted by service restart",
                )
                self._cancel_unfinished_children(
                    run, "not resumed after service restart"
                )
                recovered_active_run = True
            self._assert_run_invariants(run)
            self._mark_terminal(run_id, run)

        if recovered_active_run:
            try:
                await self._checkpoint_locked()
            except Exception:
                # A later call must retry the load-and-recovery sequence rather
                # than exposing an active run that cannot actually be resumed.
                self._initialized = False
                raise

    async def _checkpoint_locked(self) -> None:
        if self._persistence is None:
            self._synchronize_terminal_events_locked()
            return
        candidate = _clone_persistence_state(
            PipelinePersistenceState(
                runs=self._runs,
                trigger_keys=self._trigger_keys,
                callback_events=self._callback_events,
            )
        )
        try:
            await self._persistence.save(candidate)
        except Exception:
            # The caller sees the storage error. Before releasing the service
            # lock, reconcile memory with what is actually durable so retrying
            # an idempotent request cannot falsely report replay/duplicate.
            try:
                durable_state = await self._persistence.load()
            except Exception:
                durable_state = self._durable_state
            if durable_state is not None:
                self._restore_state_locked(durable_state)
            raise
        else:
            self._durable_state = _clone_persistence_state(candidate)
            # Publish completion/cancellation only after the snapshot is
            # durable. Otherwise a failed save could stop the executor and
            # leave a rolled-back active run without a worker.
            self._synchronize_terminal_events_locked()

    def _restore_state_locked(self, state: PipelinePersistenceState) -> None:
        restored = _clone_persistence_state(state)
        previous_cancel_events = self._cancel_events
        previous_completion_events = self._completion_events
        self._runs = restored.runs
        self._trigger_keys = restored.trigger_keys
        self._callback_events = {
            run_id: dict(restored.callback_events.get(run_id, {}))
            for run_id in self._runs
        }
        self._cancel_events = {}
        self._completion_events = {}
        for run_id, run in self._runs.items():
            cancel_event = previous_cancel_events.get(run_id, asyncio.Event())
            completion_event = previous_completion_events.get(
                run_id, asyncio.Event()
            )
            if run.status.is_terminal:
                cancel_event.set()
                completion_event.set()
            else:
                cancel_event.clear()
                completion_event.clear()
            self._cancel_events[run_id] = cancel_event
            self._completion_events[run_id] = completion_event

    def _synchronize_terminal_events_locked(self) -> None:
        for run_id, run in self._runs.items():
            if run.status.is_terminal:
                self._cancel_events[run_id].set()
                self._completion_events[run_id].set()
            else:
                self._cancel_events[run_id].clear()
                self._completion_events[run_id].clear()

    def _ensure_open(self) -> None:
        if self._closed:
            raise PipelineServiceClosedError("pipeline service is shut down")

    def _require_run(self, run_id: str) -> PipelineRun:
        try:
            return self._runs[run_id]
        except KeyError as exc:
            raise PipelineNotFoundError(f"pipeline not found: {run_id}") from exc

    @staticmethod
    def _snapshot(run: PipelineRun) -> PipelineRun:
        return run.model_copy(deep=True)

    def _task_finished(
        self, run_id: str, completed_task: asyncio.Task[None]
    ) -> None:
        self._tasks.pop(run_id, None)
        if not completed_task.cancelled():
            # Consume a task exception so asyncio never reports an unobserved
            # background error. _execute normally catches its own exceptions.
            completed_task.exception()


def create_pipeline_service(
    database_path: str | Path | None = None,
    *,
    database: Database | None = None,
    initialize_schema: bool = False,
) -> InMemoryPipelineService:
    """Create a service with one explicit persistence adapter.

    ``database_path`` keeps the isolated SQLite adapter available for focused
    lessons.  The FastAPI application passes its validated ``Database`` so
    SQLite and the local PostgreSQL container share the same async engine and
    transaction semantics.
    """

    if database_path is not None and database is not None:
        raise ValueError("database_path and database are mutually exclusive")
    if initialize_schema and database is not None:
        raise ValueError("Alembic must own the application database schema")

    # The snapshot adapter opens short-lived connections. Keep Python's
    # connection-scoped ``:memory:`` mode on the existing in-memory service.
    if database_path is not None and str(database_path) == ":memory:":
        database_path = None
    if database is not None:
        persistence: PipelinePersistence | None = SQLAlchemyPipelinePersistence(
            database
        )
    elif database_path is not None:
        persistence = SQLitePipelinePersistence(
            database_path,
            initialize_schema=initialize_schema,
        )
    else:
        persistence = None
    return InMemoryPipelineService(persistence=persistence)


pipeline_service = create_pipeline_service()


def get_pipeline_service() -> InMemoryPipelineService:
    """FastAPI dependency; override this function in isolated application tests."""

    return pipeline_service


__all__ = [
    "InMemoryPipelineService",
    "PipelineError",
    "PipelineIdempotencyConflictError",
    "PipelineInvariantError",
    "PipelineNotFoundError",
    "PipelineService",
    "PipelineServiceClosedError",
    "PipelineTargetNotFoundError",
    "PipelineTransitionError",
    "create_pipeline_service",
    "get_pipeline_service",
]
