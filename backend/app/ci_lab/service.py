"""Deterministic CI Lab application service."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import insert, select, update
from sqlalchemy.engine import RowMapping

from app.ci_lab.database import CiLabDatabase, runs
from app.ci_lab.models import (
    DefinitionView,
    JobView,
    RunStatus,
    RunView,
    StageView,
    TriggerRunRequest,
)
from app.ci_lab.registry import DefinitionRegistry, JobDefinition, PipelineDefinition


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")


class CiLabError(Exception):
    def __init__(self, message: str, *, status_code: int, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class CiLabNotFound(CiLabError):
    def __init__(self, resource: str) -> None:
        super().__init__(
            f"{resource} was not found",
            status_code=404,
            code="ci_lab_not_found",
        )


class CiLabConflict(CiLabError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409, code="ci_lab_conflict")


class CiLabValidationError(CiLabError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=422,
            code="ci_lab_validation_error",
        )


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fingerprint(
    definition: PipelineDefinition,
    request: TriggerRunRequest,
) -> str:
    payload = {
        "definition": definition.key,
        "definition_revision": definition.revision,
        "ref": request.ref,
        "variables": request.variables,
    }
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _JobTimeline:
    definition: JobDefinition
    start_ms: int
    finish_ms: int


class CiLabService:
    def __init__(
        self,
        database: CiLabDatabase,
        registry: DefinitionRegistry,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if not registry:
            raise ValueError("CI Lab definition registry cannot be empty")
        self.database = database
        self.registry = registry
        self._clock = clock

    async def initialize(self) -> None:
        await self.database.initialize()

    async def close(self) -> None:
        await self.database.close()

    def list_definitions(self) -> list[DefinitionView]:
        return [self.registry[key].to_view() for key in sorted(self.registry)]

    async def trigger(
        self,
        definition_key: str,
        request: TriggerRunRequest,
        idempotency_key: str,
    ) -> RunView:
        definition = self._require_definition(definition_key)
        selected_key = self._validate_idempotency_key(idempotency_key)
        request_fingerprint = _fingerprint(definition, request)
        now = self._now()

        async with self.database.write() as connection:
            previous = (
                await connection.execute(
                    select(runs).where(runs.c.idempotency_key == selected_key)
                )
            ).mappings().one_or_none()
            if previous is not None:
                if not hmac.compare_digest(
                    str(previous["request_fingerprint"]),
                    request_fingerprint,
                ):
                    raise CiLabConflict(
                        "Idempotency-Key was reused with different trigger input"
                    )
                current = await self._refresh_record(connection, previous, now)
                return self._run_view(current, definition, now, replayed=True)

            run_id = str(uuid4())
            await connection.execute(
                insert(runs).values(
                    id=run_id,
                    definition=definition.key,
                    definition_revision=definition.revision,
                    ref=request.ref,
                    variables=dict(request.variables),
                    idempotency_key=selected_key,
                    request_fingerprint=request_fingerprint,
                    status=RunStatus.QUEUED.value,
                    message=None,
                    created_at=now,
                    updated_at=now,
                    started_at=None,
                    finished_at=None,
                    cancelled_at=None,
                )
            )
            created = (
                await connection.execute(select(runs).where(runs.c.id == run_id))
            ).mappings().one()
            return self._run_view(created, definition, now, replayed=False)

    async def get(self, run_id: str | UUID) -> RunView:
        selected_id = str(run_id)
        now = self._now()
        async with self.database.write() as connection:
            record = (
                await connection.execute(select(runs).where(runs.c.id == selected_id))
            ).mappings().one_or_none()
            if record is None:
                raise CiLabNotFound("run")
            definition = self._definition_for_record(record)
            current = await self._refresh_record(connection, record, now)
            return self._run_view(current, definition, now, replayed=False)

    async def cancel(self, run_id: str | UUID) -> RunView:
        selected_id = str(run_id)
        now = self._now()
        async with self.database.write() as connection:
            record = (
                await connection.execute(select(runs).where(runs.c.id == selected_id))
            ).mappings().one_or_none()
            if record is None:
                raise CiLabNotFound("run")
            definition = self._definition_for_record(record)
            current = await self._refresh_record(connection, record, now)
            status = RunStatus(str(current["status"]))
            if status == RunStatus.CANCELLED:
                return self._run_view(current, definition, now, replayed=True)
            if status.is_terminal:
                raise CiLabConflict(f"a {status.value} run cannot be cancelled")

            created_at = _aware(current["created_at"])
            started_at = (
                created_at + timedelta(milliseconds=definition.queue_delay_ms)
                if now >= created_at
                + timedelta(milliseconds=definition.queue_delay_ms)
                else None
            )
            values = {
                "status": RunStatus.CANCELLED.value,
                "message": "cancelled by an authenticated machine request",
                "updated_at": now,
                "started_at": started_at,
                "finished_at": now,
                "cancelled_at": now,
            }
            await connection.execute(
                update(runs).where(runs.c.id == selected_id).values(**values)
            )
            cancelled = {**dict(current), **values}
            return self._run_view(cancelled, definition, now, replayed=False)

    async def _refresh_record(
        self,
        connection,
        record: RowMapping,
        now: datetime,
    ) -> dict[str, Any]:
        definition = self._definition_for_record(record)
        current = dict(record)
        if current["cancelled_at"] is not None:
            return current
        current_status = RunStatus(str(current["status"]))
        if current_status.is_terminal:
            return current

        created_at = _aware(current["created_at"])
        elapsed_ms = max(0, int((now - created_at).total_seconds() * 1000))
        terminal_ms, terminal_status, terminal_message = self._terminal_point(definition)
        started_at: datetime | None = None
        finished_at: datetime | None = None
        status = RunStatus.QUEUED
        message: str | None = None

        if elapsed_ms >= definition.queue_delay_ms:
            started_at = created_at + timedelta(milliseconds=definition.queue_delay_ms)
            status = RunStatus.RUNNING
        if elapsed_ms >= terminal_ms:
            status = terminal_status
            message = terminal_message
            finished_at = created_at + timedelta(milliseconds=terminal_ms)

        # Wall clocks may be adjusted backwards. Once observed, a running
        # state cannot become queued again; terminal states returned above are
        # immutable. This also keeps injected teaching clocks deterministic.
        if current_status == RunStatus.RUNNING and status == RunStatus.QUEUED:
            status = RunStatus.RUNNING
            message = current["message"]
            started_at = _aware(current["started_at"])
            finished_at = None

        if (
            str(current["status"]) != status.value
            or (
                _aware(current["started_at"])
                if current["started_at"] is not None
                else None
            )
            != started_at
            or (
                _aware(current["finished_at"])
                if current["finished_at"] is not None
                else None
            )
            != finished_at
            or current["message"] != message
        ):
            values = {
                "status": status.value,
                "message": message,
                "updated_at": now,
                "started_at": started_at,
                "finished_at": finished_at,
            }
            await connection.execute(
                update(runs).where(runs.c.id == current["id"]).values(**values)
            )
            current.update(values)
        return current

    def _run_view(
        self,
        record: RowMapping | dict[str, Any],
        definition: PipelineDefinition,
        now: datetime,
        *,
        replayed: bool,
    ) -> RunView:
        values = dict(record)
        created_at = _aware(values["created_at"])
        cancelled_at = (
            _aware(values["cancelled_at"])
            if values["cancelled_at"] is not None
            else None
        )
        status = RunStatus(str(values["status"]))
        started_at = (
            _aware(values["started_at"])
            if values["started_at"] is not None
            else None
        )
        finished_at = (
            _aware(values["finished_at"])
            if values["finished_at"] is not None
            else None
        )
        observation_time = cancelled_at or now
        if status.is_terminal and finished_at is not None:
            observation_time = max(observation_time, finished_at)
        elif status == RunStatus.RUNNING and started_at is not None:
            observation_time = max(observation_time, started_at)
        elapsed_ms = max(
            0,
            int((observation_time - created_at).total_seconds() * 1000),
        )
        stages = self._stage_views(
            definition,
            created_at=created_at,
            elapsed_ms=elapsed_ms,
            cancelled_at=cancelled_at,
        )
        return RunView(
            id=str(values["id"]),
            definition=definition.key,
            definition_revision=definition.revision,
            status=status,
            web_url=None,
            message=values["message"],
            metadata={
                "definition": definition.key,
                "definition_revision": definition.revision,
                "ref": values["ref"],
                "stage_count": len(definition.stages),
                "deterministic": True,
            },
            created_at=created_at,
            updated_at=_aware(values["updated_at"]),
            started_at=started_at,
            finished_at=finished_at,
            stages=stages,
            replayed=replayed,
        )

    def _stage_views(
        self,
        definition: PipelineDefinition,
        *,
        created_at: datetime,
        elapsed_ms: int,
        cancelled_at: datetime | None,
    ) -> list[StageView]:
        timeline = self._timeline(definition)
        first_failure = next(
            (item for item in timeline if item.definition.should_fail),
            None,
        )
        failed = first_failure is not None and elapsed_ms >= first_failure.finish_ms
        by_key: dict[str, JobView] = {}

        for item in timeline:
            start_at = created_at + timedelta(milliseconds=item.start_ms)
            finish_at = created_at + timedelta(milliseconds=item.finish_ms)
            status = RunStatus.QUEUED
            actual_started: datetime | None = None
            actual_finished: datetime | None = None
            message: str | None = None

            blocked_by_failure = (
                failed
                and first_failure is not None
                and item.start_ms >= first_failure.finish_ms
            )
            if blocked_by_failure:
                status = RunStatus.CANCELLED
                actual_finished = created_at + timedelta(
                    milliseconds=first_failure.finish_ms
                )
                message = "cancelled after an earlier fixed job failed"
            elif cancelled_at is not None and elapsed_ms < item.finish_ms:
                status = RunStatus.CANCELLED
                if elapsed_ms >= item.start_ms:
                    actual_started = start_at
                actual_finished = cancelled_at
                message = "cancelled by machine request"
            elif elapsed_ms >= item.finish_ms:
                actual_started = start_at
                actual_finished = finish_at
                if item.definition.should_fail:
                    status = RunStatus.FAILED
                    message = "failed by the immutable local teaching definition"
                else:
                    status = RunStatus.SUCCEEDED
            elif elapsed_ms >= item.start_ms:
                status = RunStatus.RUNNING
                actual_started = start_at

            by_key[item.definition.key] = JobView(
                key=item.definition.key,
                name=item.definition.name,
                status=status,
                duration_ms=item.definition.duration_ms,
                started_at=actual_started,
                finished_at=actual_finished,
                message=message,
            )

        stages: list[StageView] = []
        for stage in definition.stages:
            jobs = [by_key[job.key] for job in stage.jobs]
            statuses = {job.status for job in jobs}
            if RunStatus.FAILED in statuses:
                stage_status = RunStatus.FAILED
            elif statuses == {RunStatus.SUCCEEDED}:
                stage_status = RunStatus.SUCCEEDED
            elif RunStatus.CANCELLED in statuses:
                stage_status = RunStatus.CANCELLED
            elif RunStatus.RUNNING in statuses or RunStatus.SUCCEEDED in statuses:
                stage_status = RunStatus.RUNNING
            else:
                stage_status = RunStatus.QUEUED

            started_values = [job.started_at for job in jobs if job.started_at is not None]
            finished_values = [
                job.finished_at for job in jobs if job.finished_at is not None
            ]
            stages.append(
                StageView(
                    key=stage.key,
                    name=stage.name,
                    status=stage_status,
                    jobs=jobs,
                    started_at=min(started_values) if started_values else None,
                    finished_at=(
                        max(finished_values)
                        if stage_status.is_terminal and finished_values
                        else None
                    ),
                    message=(
                        "stage failed"
                        if stage_status == RunStatus.FAILED
                        else "stage was cancelled"
                        if stage_status == RunStatus.CANCELLED
                        else None
                    ),
                )
            )
        return stages

    @staticmethod
    def _timeline(definition: PipelineDefinition) -> list[_JobTimeline]:
        offset = definition.queue_delay_ms
        values: list[_JobTimeline] = []
        for stage in definition.stages:
            for job in stage.jobs:
                values.append(
                    _JobTimeline(
                        definition=job,
                        start_ms=offset,
                        finish_ms=offset + job.duration_ms,
                    )
                )
                offset += job.duration_ms
        return values

    def _terminal_point(
        self,
        definition: PipelineDefinition,
    ) -> tuple[int, RunStatus, str]:
        timeline = self._timeline(definition)
        failure = next((item for item in timeline if item.definition.should_fail), None)
        if failure is not None:
            return (
                failure.finish_ms,
                RunStatus.FAILED,
                f"fixed job {failure.definition.key} failed",
            )
        return (
            timeline[-1].finish_ms,
            RunStatus.SUCCEEDED,
            "completed by the deterministic local executor",
        )

    def _require_definition(self, definition_key: str) -> PipelineDefinition:
        try:
            return self.registry[definition_key]
        except KeyError as error:
            raise CiLabNotFound("definition") from error

    def _definition_for_record(self, record: RowMapping) -> PipelineDefinition:
        definition = self._require_definition(str(record["definition"]))
        if int(record["definition_revision"]) != definition.revision:
            raise CiLabConflict(
                "the immutable definition revision for this run is unavailable"
            )
        return definition

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if _IDEMPOTENCY_KEY.fullmatch(value) is None:
            raise CiLabValidationError(
                "Idempotency-Key must contain 1-200 safe ASCII characters"
            )
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("CI Lab clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


__all__ = [
    "CiLabConflict",
    "CiLabError",
    "CiLabNotFound",
    "CiLabService",
    "CiLabValidationError",
    "Clock",
    "utc_now",
]
