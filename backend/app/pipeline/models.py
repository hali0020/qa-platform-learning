from datetime import datetime
from enum import Enum
import json
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator


_SENSITIVE_VARIABLE_MARKERS = (
    "secret",
    "token",
    "password",
    "passwd",
    "credential",
    "api_key",
    "apikey",
    "private_key",
    "authorization",
)


class PipelineStatus(str, Enum):
    """States shared by pipelines, stages, and jobs.

    ``queued`` and ``running`` are active states.  The other three states are
    terminal and cannot transition again.
    """

    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            PipelineStatus.SUCCEEDED,
            PipelineStatus.FAILED,
            PipelineStatus.CANCELLED,
        }


class PipelineJobSpec(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    duration_ms: int = Field(
        default=10,
        ge=0,
        le=60_000,
        description="Local simulated execution time; no command is executed.",
    )
    should_fail: bool = Field(
        default=False,
        description="Deterministically fail this simulated job.",
    )


class PipelineStageSpec(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    jobs: list[PipelineJobSpec] = Field(min_length=1, max_length=20)

    @model_validator(mode="after")
    def job_names_are_unique(self) -> "PipelineStageSpec":
        names = [job.name for job in self.jobs]
        if len(names) != len(set(names)):
            raise ValueError("job names must be unique inside a stage")
        return self


class PipelineTriggerRequest(BaseModel):
    name: str = Field(min_length=1, max_length=100)
    stages: list[PipelineStageSpec] = Field(min_length=1, max_length=20)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    auto_start: bool = Field(
        default=True,
        description=(
            "Run with the local simulator. Set false when learning provider "
            "callbacks."
        ),
    )
    variables: dict[str, str] = Field(default_factory=dict, max_length=50)

    @field_validator("variables")
    @classmethod
    def bounded_non_secret_variables(
        cls,
        value: dict[str, str],
    ) -> dict[str, str]:
        for key, item in value.items():
            normalized = key.casefold()
            if any(marker in normalized for marker in _SENSITIVE_VARIABLE_MARKERS):
                raise ValueError("pipeline variables cannot contain secrets")
            if (
                not key
                or len(key) > 128
                or len(item) > 4096
                or any(ord(character) < 32 for character in key)
            ):
                raise ValueError("pipeline variable key or value is too large")
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(encoded) > 32_768:
            raise ValueError("pipeline variables exceed the serialized size limit")
        return value

    @model_validator(mode="after")
    def stage_names_are_unique(self) -> "PipelineTriggerRequest":
        names = [stage.name for stage in self.stages]
        if len(names) != len(set(names)):
            raise ValueError("stage names must be unique inside a pipeline")
        jobs = [job for stage in self.stages for job in stage.jobs]
        if len(jobs) > 100:
            raise ValueError("a pipeline cannot contain more than 100 jobs")
        if sum(job.duration_ms for job in jobs) > 300_000:
            raise ValueError("simulated pipeline duration cannot exceed five minutes")
        return self


class PipelineJobResult(BaseModel):
    name: str
    status: PipelineStatus = PipelineStatus.QUEUED
    duration_ms: int
    should_fail: bool
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None


class PipelineStageResult(BaseModel):
    name: str
    status: PipelineStatus = PipelineStatus.QUEUED
    jobs: list[PipelineJobResult]
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None


class PipelineRun(BaseModel):
    id: str
    name: str
    status: PipelineStatus = PipelineStatus.QUEUED
    stages: list[PipelineStageResult]
    # Historical snapshots accepted JSON values of any shape. Keep the read
    # model backward-compatible while new trigger requests are string-only.
    variables: dict[str, Any] = Field(default_factory=dict)
    idempotency_key: str | None = None
    created_at: datetime
    started_at: datetime | None = None
    finished_at: datetime | None = None
    message: str | None = None


class PipelineTriggerResult(BaseModel):
    pipeline: PipelineRun
    replayed: bool = False


class PipelineCancellationResult(BaseModel):
    pipeline: PipelineRun
    replayed: bool = False


class CallbackTarget(str, Enum):
    PIPELINE = "pipeline"
    STAGE = "stage"
    JOB = "job"


class PipelineCallbackRequest(BaseModel):
    """A provider event delivered to the platform.

    ``event_id`` is the callback idempotency key. Re-delivery of an identical
    event is acknowledged without applying the transition twice. Reusing the
    identifier with different content is rejected as a conflict.
    """

    event_id: str = Field(min_length=1, max_length=200)
    target: CallbackTarget = CallbackTarget.PIPELINE
    status: PipelineStatus
    stage_name: str | None = Field(default=None, min_length=1, max_length=100)
    job_name: str | None = Field(default=None, min_length=1, max_length=100)
    message: str | None = Field(default=None, max_length=500)

    @model_validator(mode="after")
    def target_has_required_names(self) -> "PipelineCallbackRequest":
        if self.target == CallbackTarget.PIPELINE:
            if self.stage_name is not None or self.job_name is not None:
                raise ValueError("pipeline callbacks cannot name a stage or job")
        elif self.target == CallbackTarget.STAGE:
            if self.stage_name is None or self.job_name is not None:
                raise ValueError("stage callbacks require only stage_name")
        elif self.stage_name is None or self.job_name is None:
            raise ValueError("job callbacks require stage_name and job_name")
        return self


class PipelineCallbackResult(BaseModel):
    pipeline: PipelineRun
    duplicate: bool = False
