from __future__ import annotations

from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import uuid4

from pydantic import BaseModel, Field, field_validator


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class TaskStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"

    @property
    def is_terminal(self) -> bool:
        return self in {
            TaskStatus.SUCCEEDED,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
            TaskStatus.DEAD_LETTER,
        }

    @property
    def is_active(self) -> bool:
        return self in {
            TaskStatus.QUEUED,
            TaskStatus.RUNNING,
            TaskStatus.RETRY_WAIT,
        }


class AutomationTask(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    task_type: str = Field(min_length=1, max_length=100)
    payload: dict[str, Any] = Field(default_factory=dict)
    queue: str = Field(default="default", min_length=1, max_length=100)
    priority: int = Field(default=50, ge=0, le=100)
    status: TaskStatus = TaskStatus.QUEUED
    idempotency_key: str | None = Field(default=None, min_length=1, max_length=200)
    request_fingerprint: str
    source_schedule_id: str | None = None
    attempts: int = 0
    max_attempts: int = Field(default=3, ge=1, le=100)
    available_at: datetime = Field(default_factory=utc_now)
    lease_owner: str | None = None
    lease_token_hash: str | None = Field(default=None, exclude=True, repr=False)
    lease_expires_at: datetime | None = None
    heartbeat_at: datetime | None = None
    cancel_requested: bool = False
    result: dict[str, Any] | None = None
    error_code: str | None = Field(default=None, max_length=100)
    created_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    finished_at: datetime | None = None


class EnqueueResult(BaseModel):
    task: AutomationTask
    replayed: bool = False


class ClaimedTask(BaseModel):
    task: AutomationTask
    lease_token: str = Field(repr=False)


class DeviceStatus(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    RESERVED = "reserved"
    BUSY = "busy"
    MAINTENANCE = "maintenance"


class DeviceLeaseStatus(str, Enum):
    ACTIVE = "active"
    RELEASED = "released"
    EXPIRED = "expired"


class Device(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    name: str = Field(min_length=1, max_length=150)
    kind: str = Field(default="device", min_length=1, max_length=50)
    platform: str = Field(default="unknown", min_length=1, max_length=100)
    capabilities: set[str] = Field(default_factory=set)
    agent_id: str = Field(min_length=1, max_length=200)
    enabled: bool = True
    status: DeviceStatus = DeviceStatus.OFFLINE
    last_heartbeat_at: datetime | None = None
    active_lease_id: str | None = None
    version: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)

    @field_validator("capabilities")
    @classmethod
    def bounded_capabilities(cls, value: set[str]) -> set[str]:
        if len(value) > 100 or any(not item or len(item) > 100 for item in value):
            raise ValueError("device capabilities are invalid")
        return value


class DeviceLease(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    device_id: str
    task_id: str
    owner: str
    token_hash: str = Field(exclude=True, repr=False)
    status: DeviceLeaseStatus = DeviceLeaseStatus.ACTIVE
    acquired_at: datetime
    expires_at: datetime
    released_at: datetime | None = None
    version: int = 0


class ClaimedDeviceLease(BaseModel):
    device: Device
    lease: DeviceLease
    lease_token: str = Field(repr=False)


class MisfirePolicy(str, Enum):
    SKIP = "skip"
    FIRE_ONCE = "fire_once"
    CATCH_UP_LIMITED = "catch_up_limited"


class OverlapPolicy(str, Enum):
    FORBID = "forbid"
    ALLOW = "allow"
    REPLACE = "replace"


class ScheduleFireStatus(str, Enum):
    ENQUEUED = "enqueued"
    SKIPPED_MISFIRE = "skipped_misfire"
    SKIPPED_OVERLAP = "skipped_overlap"


class AutomationSchedule(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
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
    next_run_at: datetime | None = None
    last_run_at: datetime | None = None
    version: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class ScheduleFire(BaseModel):
    id: str = Field(default_factory=lambda: str(uuid4()))
    schedule_id: str
    scheduled_for: datetime
    status: ScheduleFireStatus
    task_id: str | None = None
    created_at: datetime = Field(default_factory=utc_now)


__all__ = [
    "AutomationSchedule",
    "AutomationTask",
    "ClaimedDeviceLease",
    "ClaimedTask",
    "Device",
    "DeviceLease",
    "DeviceLeaseStatus",
    "DeviceStatus",
    "EnqueueResult",
    "MisfirePolicy",
    "OverlapPolicy",
    "ScheduleFire",
    "ScheduleFireStatus",
    "TaskStatus",
    "utc_now",
]
