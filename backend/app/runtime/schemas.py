"""Public API schemas for the persistent runtime teaching adapter."""

from __future__ import annotations

import json
import re
from datetime import datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.automation.models import MisfirePolicy, OverlapPolicy
from app.pipeline.providers.models import ProviderKind


_LEARNING_CI_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_LEARNING_CI_CORRELATION_ID = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z"
)
_LEARNING_CI_VARIABLE_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_LEARNING_CI_SENSITIVE_SEGMENTS = frozenset(
    {
        "AUTH",
        "AUTHORIZATION",
        "COOKIE",
        "CREDENTIAL",
        "KEY",
        "PASS",
        "PASSWD",
        "PASSWORD",
        "PRIVATE",
        "SECRET",
        "TOKEN",
    }
)
_LEARNING_CI_MAX_VARIABLES = 32
_LEARNING_CI_MAX_VARIABLE_VALUE_BYTES = 512
_LEARNING_CI_MAX_VARIABLES_JSON_BYTES = 8 * 1024


class StrictSchema(BaseModel):
    model_config = ConfigDict(extra="forbid")


class ProviderConnectionCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=150)
    kind: ProviderKind
    base_url: str | None = Field(default=None, max_length=2048)
    definition_ref: str = Field(min_length=1, max_length=300)
    config: dict[str, str] = Field(default_factory=dict)
    secret_env_var: str | None = Field(default=None, max_length=128)
    webhook_secret_env_var: str | None = Field(default=None, max_length=128)
    enabled: bool = False

    @field_validator("secret_env_var", "webhook_secret_env_var")
    @classmethod
    def valid_secret_environment_name(cls, value: str | None) -> str | None:
        if value is not None and re.fullmatch(
            r"QA_PROVIDER_SECRET_[A-Z0-9_]{1,109}", value
        ) is None:
            raise ValueError(
                "secret_env_var must use the dedicated QA_PROVIDER_SECRET_ prefix"
            )
        return value


class ProviderConnectionPatch(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    base_url: str | None = Field(default=None, max_length=2048)
    definition_ref: str | None = Field(default=None, min_length=1, max_length=300)
    config: dict[str, str] | None = None
    secret_env_var: str | None = Field(default=None, max_length=128)
    webhook_secret_env_var: str | None = Field(default=None, max_length=128)
    enabled: bool | None = None
    version: int = Field(ge=0)

    @field_validator("secret_env_var", "webhook_secret_env_var")
    @classmethod
    def valid_secret_environment_name(cls, value: str | None) -> str | None:
        return ProviderConnectionCreate.valid_secret_environment_name(value)


class ProviderConnectionView(BaseModel):
    id: str
    name: str
    kind: ProviderKind
    base_url: str | None
    definition_ref: str
    config: dict[str, str]
    secret_env_var: str | None
    secret_configured: bool
    webhook_secret_env_var: str | None
    webhook_secret_configured: bool
    enabled: bool
    version: int
    created_at: datetime
    updated_at: datetime


class ProviderTriggerPayload(StrictSchema):
    ref: str | None = Field(default=None, min_length=1, max_length=300)
    variables: dict[str, str] = Field(default_factory=dict)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("variables")
    @classmethod
    def bounded_non_secret_variables(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100:
            raise ValueError("at most 100 variables are allowed")
        sensitive = (
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
        for key, item in value.items():
            normalized = key.casefold()
            if any(marker in normalized for marker in sensitive):
                raise ValueError("secrets must be configured by environment, not trigger input")
            if not key or len(key) > 128 or len(item) > 4096:
                raise ValueError("provider variable is too large")
        return value

    def validate_learning_ci_contract(self) -> None:
        """Reject input that the fixed Learning CI HTTP contract cannot accept."""

        if self.ref is not None and _LEARNING_CI_REF.fullmatch(self.ref) is None:
            raise ValueError(
                "ref must use 1-128 ASCII letters, digits, dots, underscores, or dashes"
            )
        if (
            self.correlation_id is not None
            and _LEARNING_CI_CORRELATION_ID.fullmatch(self.correlation_id) is None
        ):
            raise ValueError("correlation_id must use 1-200 safe ASCII characters")
        if len(self.variables) > _LEARNING_CI_MAX_VARIABLES:
            raise ValueError(
                f"at most {_LEARNING_CI_MAX_VARIABLES} variables are allowed"
            )

        for key, item in self.variables.items():
            if _LEARNING_CI_VARIABLE_NAME.fullmatch(key) is None:
                raise ValueError(
                    "variable names must use 1-64 uppercase ASCII letters, "
                    "digits, or underscores"
                )
            if _LEARNING_CI_SENSITIVE_SEGMENTS.intersection(key.split("_")):
                raise ValueError("secret-like variable names are not allowed")
            if (
                not item
                or len(item.encode("utf-8"))
                > _LEARNING_CI_MAX_VARIABLE_VALUE_BYTES
            ):
                raise ValueError(
                    "variable values must contain 1-512 UTF-8 bytes"
                )
            if any(
                ord(character) < 32 or ord(character) == 127
                for character in item
            ):
                raise ValueError("variable values cannot contain control characters")
            if "://" in item or "../" in item or "..\\" in item:
                raise ValueError(
                    "URLs and parent-relative paths are not allowed in variables"
                )

        canonical = json.dumps(
            self.variables,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(canonical) > _LEARNING_CI_MAX_VARIABLES_JSON_BYTES:
            raise ValueError("the canonical variable document cannot exceed 8192 bytes")


class ProviderRunView(BaseModel):
    id: str
    connection_id: str
    external_id: str | None
    status: str
    raw_status: str
    web_url: str | None
    message: str | None
    metadata: dict[str, Any]
    correlation_id: str | None
    dispatch_status: str
    quality_gate_status: str
    reconciliation_required: bool
    last_provider_sequence: int
    triggered_by_name: str
    approvals: list["ProviderRunApprovalView"] = Field(default_factory=list)
    created_at: datetime
    updated_at: datetime


class ProviderRunApprovalPayload(StrictSchema):
    event_id: str = Field(min_length=1, max_length=200, pattern=r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}")
    decision: Literal["approve", "reject"]
    comment: str = Field(default="", max_length=1000)


class ProviderRunApprovalView(BaseModel):
    id: str
    run_id: str
    event_id: str
    decision: Literal["approve", "reject"]
    actor_name: str
    comment: str
    created_at: datetime


class ProviderTriggerDispatch(StrictSchema):
    worker_id: str = Field(default="manual-dispatcher", min_length=1, max_length=200)
    lease_seconds: int = Field(default=30, ge=5, le=3600)


class ProviderTriggerIntentView(BaseModel):
    id: str
    run_id: str
    connection_id: str
    status: str
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    created_at: datetime
    updated_at: datetime
    completed_at: datetime | None


class ProviderWebhookPayload(StrictSchema):
    external_id: str = Field(min_length=1, max_length=300, pattern=r"[A-Za-z0-9_-]{1,300}")
    sequence: int = Field(ge=1)
    occurred_at: datetime
    status: Literal[
        "queued",
        "running",
        "waiting_approval",
        "succeeded",
        "failed",
        "cancelled",
    ]
    message: str | None = Field(default=None, max_length=500)


class ProviderWebhookResult(BaseModel):
    event_id: str
    result: Literal["applied", "duplicate", "stale", "reconcile_required", "ignored"]
    run_id: str | None
    reconciliation_required: bool


class TaskEnqueue(StrictSchema):
    task_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    queue: str = Field(default="default", min_length=1, max_length=100)
    priority: int = Field(default=50, ge=0, le=100)
    max_attempts: int = Field(default=3, ge=1, le=100)
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    available_at: datetime | None = None


class TaskClaim(StrictSchema):
    worker_id: str = Field(min_length=1, max_length=200)
    queues: list[str] = Field(default_factory=lambda: ["default"], min_length=1, max_length=20)
    lease_seconds: int = Field(default=30, ge=1, le=3600)


class TaskLeaseAction(StrictSchema):
    worker_id: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=20, max_length=500, repr=False)


class TaskHeartbeat(TaskLeaseAction):
    lease_seconds: int = Field(default=30, ge=1, le=3600)


class TaskComplete(TaskLeaseAction):
    result: dict[str, Any] = Field(default_factory=dict)


class TaskFail(TaskLeaseAction):
    error_code: str = Field(min_length=1, max_length=100)
    retryable: bool = True


class TaskDeadLetter(StrictSchema):
    error_code: str = Field(default="manual_dead_letter", min_length=1, max_length=100)


class TaskView(BaseModel):
    id: str
    task_type: str
    payload: dict[str, Any]
    queue: str
    priority: int
    status: str
    idempotency_key: str | None
    source_schedule_id: str | None
    attempts: int
    max_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    heartbeat_at: datetime | None
    cancel_requested: bool
    result: dict[str, Any] | None
    error_code: str | None
    created_at: datetime
    started_at: datetime | None
    finished_at: datetime | None


class TaskWakeupOutboxView(BaseModel):
    id: str
    task_id: str
    generation: int
    status: Literal["pending", "claimed", "retry_wait", "published"]
    publish_attempts: int
    available_at: datetime
    lease_owner: str | None
    lease_expires_at: datetime | None
    last_error_code: str | None
    version: int
    created_at: datetime
    updated_at: datetime
    published_at: datetime | None


class ClaimedTaskView(StrictSchema):
    task: TaskView
    lease_token: str = Field(repr=False)


class DeviceCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=150)
    agent_id: str = Field(min_length=1, max_length=200)
    kind: str = Field(default="device", min_length=1, max_length=50)
    platform: str = Field(default="unknown", min_length=1, max_length=100)
    capabilities: set[str] = Field(default_factory=set, max_length=100)


class DevicePatch(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    kind: str | None = Field(default=None, min_length=1, max_length=50)
    platform: str | None = Field(default=None, min_length=1, max_length=100)
    capabilities: set[str] | None = Field(default=None, max_length=100)
    enabled: bool | None = None
    maintenance: bool | None = None
    version: int = Field(ge=0)


class DeviceHeartbeat(StrictSchema):
    agent_id: str = Field(min_length=1, max_length=200, repr=False)


class DeviceAcquire(StrictSchema):
    task_id: str = Field(min_length=1, max_length=36)
    owner: str = Field(min_length=1, max_length=200)
    task_lease_token: str = Field(min_length=20, max_length=500, repr=False)
    required_capabilities: set[str] = Field(default_factory=set, max_length=100)
    lease_seconds: int = Field(default=60, ge=1, le=3600)


class DeviceLeaseAction(StrictSchema):
    owner: str = Field(min_length=1, max_length=200)
    lease_token: str = Field(min_length=20, max_length=500, repr=False)


class DeviceLeaseRenew(DeviceLeaseAction):
    task_lease_token: str = Field(min_length=20, max_length=500, repr=False)
    lease_seconds: int = Field(default=60, ge=1, le=3600)


class DeviceView(BaseModel):
    id: str
    name: str
    kind: str
    platform: str
    capabilities: list[str]
    enabled: bool
    status: str
    last_heartbeat_at: datetime | None
    active_lease_id: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class DeviceLeaseView(BaseModel):
    id: str
    device_id: str
    task_id: str
    owner: str
    status: str
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None
    version: int


class ClaimedDeviceView(StrictSchema):
    device: DeviceView
    lease: DeviceLeaseView
    lease_token: str = Field(repr=False)


class ScheduleCreate(StrictSchema):
    name: str = Field(min_length=1, max_length=150)
    task_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    queue: str = Field(default="default", min_length=1, max_length=100)
    priority: int = Field(default=50, ge=0, le=100)
    max_attempts: int = Field(default=3, ge=1, le=100)
    cron: str = Field(min_length=9, max_length=200)
    timezone: str = Field(default="UTC", min_length=1, max_length=100)
    misfire_policy: MisfirePolicy = MisfirePolicy.FIRE_ONCE
    overlap_policy: OverlapPolicy = OverlapPolicy.FORBID
    misfire_grace_seconds: int = Field(default=60, ge=0, le=86400)
    catch_up_limit: int = Field(default=3, ge=1, le=100)
    enabled: bool = True


class SchedulePatch(StrictSchema):
    name: str | None = Field(default=None, min_length=1, max_length=150)
    payload: dict[str, Any] | None = None
    queue: str | None = Field(default=None, min_length=1, max_length=100)
    priority: int | None = Field(default=None, ge=0, le=100)
    max_attempts: int | None = Field(default=None, ge=1, le=100)
    cron: str | None = Field(default=None, min_length=9, max_length=200)
    timezone: str | None = Field(default=None, min_length=1, max_length=100)
    misfire_policy: MisfirePolicy | None = None
    overlap_policy: OverlapPolicy | None = None
    misfire_grace_seconds: int | None = Field(default=None, ge=0, le=86400)
    catch_up_limit: int | None = Field(default=None, ge=1, le=100)
    enabled: bool | None = None
    version: int = Field(ge=0)


class ScheduleTick(StrictSchema):
    now: datetime | None = None


class ScheduleView(BaseModel):
    id: str
    name: str
    task_type: str
    payload: dict[str, Any]
    queue: str
    priority: int
    max_attempts: int
    cron: str
    timezone: str
    misfire_policy: str
    overlap_policy: str
    misfire_grace_seconds: int
    catch_up_limit: int
    enabled: bool
    next_run_at: datetime | None
    last_run_at: datetime | None
    version: int
    created_at: datetime
    updated_at: datetime


class ScheduleFireView(BaseModel):
    id: str
    schedule_id: str
    scheduled_for: datetime
    status: str
    task_id: str | None
    created_at: datetime


class ProviderTestResult(BaseModel):
    ready: bool
    network_probe_performed: bool = False
    message: str


__all__ = [name for name in globals() if not name.startswith("_")]
