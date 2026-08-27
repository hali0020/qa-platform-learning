"""SQLAlchemy mappings owned by the persistent runtime learning adapter."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import Mapped, mapped_column

from app.database.base import Base


class ProviderConnectionRecord(Base):
    __tablename__ = "provider_connections"
    __table_args__ = (
        UniqueConstraint("name", name="uq_provider_connections_name"),
        Index("ix_provider_connections_kind_enabled", "kind", "enabled"),
        CheckConstraint(
            "kind IN ('local', 'learning_ci', 'jenkins', 'gitlab', 'bk_ci')",
            name="ck_provider_connections_kind",
        ),
        CheckConstraint("version >= 0", name="ck_provider_connections_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(20))
    base_url: Mapped[str | None] = mapped_column(String(2048))
    definition_ref: Mapped[str] = mapped_column(String(300))
    config: Mapped[dict[str, Any]] = mapped_column(JSON)
    secret_env_var: Mapped[str | None] = mapped_column(String(128))
    enabled: Mapped[bool] = mapped_column(Boolean)
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderRunRecord(Base):
    __tablename__ = "provider_runs"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "external_id", name="uq_provider_runs_external"
        ),
        UniqueConstraint(
            "connection_id", "correlation_id", name="uq_provider_runs_correlation"
        ),
        Index("ix_provider_runs_connection", "connection_id"),
        Index("ix_provider_runs_status", "status"),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_provider_runs_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_connections.id", ondelete="RESTRICT")
    )
    external_id: Mapped[str] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))
    raw_status: Mapped[str] = mapped_column(String(100))
    web_url: Mapped[str | None] = mapped_column(String(2048))
    message: Mapped[str | None] = mapped_column(String(500))
    run_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON)
    correlation_id: Mapped[str | None] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ScheduleRecord(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint("name", name="uq_schedules_name"),
        Index("ix_schedules_due", "enabled", "next_run_at"),
        CheckConstraint(
            "priority BETWEEN 0 AND 100",
            name="ck_schedules_priority",
        ),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 100",
            name="ck_schedules_attempts",
        ),
        CheckConstraint(
            "misfire_policy IN ('skip', 'fire_once', 'catch_up_limited')",
            name="ck_schedules_misfire_policy",
        ),
        CheckConstraint(
            "overlap_policy IN ('forbid', 'allow', 'replace')",
            name="ck_schedules_overlap_policy",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    task_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    queue: Mapped[str] = mapped_column(String(100))
    priority: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    cron: Mapped[str] = mapped_column(String(200))
    timezone: Mapped[str] = mapped_column(String(100))
    misfire_policy: Mapped[str] = mapped_column(String(30))
    overlap_policy: Mapped[str] = mapped_column(String(20))
    misfire_grace_seconds: Mapped[int] = mapped_column(Integer)
    catch_up_limit: Mapped[int] = mapped_column(Integer)
    enabled: Mapped[bool] = mapped_column(Boolean)
    next_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_run_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class AutomationTaskRecord(Base):
    __tablename__ = "automation_tasks"
    __table_args__ = (
        UniqueConstraint(
            "task_type", "idempotency_key", name="uq_automation_tasks_idempotency"
        ),
        Index(
            "ix_automation_tasks_claim",
            "queue",
            "status",
            "available_at",
            "priority",
        ),
        Index("ix_automation_tasks_lease", "lease_expires_at"),
        Index("ix_automation_tasks_schedule", "source_schedule_id"),
        CheckConstraint(
            "priority BETWEEN 0 AND 100",
            name="ck_automation_tasks_priority",
        ),
        CheckConstraint("attempts >= 0", name="ck_automation_tasks_attempts"),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 100",
            name="ck_automation_tasks_max_attempts",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
            "'failed', 'cancelled', 'dead_letter')",
            name="ck_automation_tasks_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_type: Mapped[str] = mapped_column(String(100))
    payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    queue: Mapped[str] = mapped_column(String(100))
    priority: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    idempotency_key: Mapped[str | None] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    source_schedule_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("schedules.id", ondelete="RESTRICT")
    )
    attempts: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    cancel_requested: Mapped[bool] = mapped_column(Boolean)
    result: Mapped[dict[str, Any] | None] = mapped_column(JSON)
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    finished_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class DeviceRecord(Base):
    __tablename__ = "devices"
    __table_args__ = (
        UniqueConstraint("name", name="uq_devices_name"),
        UniqueConstraint("agent_id", name="uq_devices_agent_id"),
        UniqueConstraint("active_lease_id", name="uq_devices_active_lease"),
        Index("ix_devices_available", "enabled", "status"),
        CheckConstraint(
            "status IN ('offline', 'idle', 'reserved', 'busy', 'maintenance')",
            name="ck_devices_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    name: Mapped[str] = mapped_column(String(150))
    kind: Mapped[str] = mapped_column(String(50))
    platform: Mapped[str] = mapped_column(String(100))
    capabilities: Mapped[list[str]] = mapped_column(JSON)
    agent_id: Mapped[str] = mapped_column(String(200))
    enabled: Mapped[bool] = mapped_column(Boolean)
    status: Mapped[str] = mapped_column(String(20))
    last_heartbeat_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    active_lease_id: Mapped[str | None] = mapped_column(String(36))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class DeviceLeaseRecord(Base):
    __tablename__ = "device_leases"
    __table_args__ = (
        UniqueConstraint("token_hash", name="uq_device_leases_token_hash"),
        Index(
            "uq_device_leases_one_active_per_device",
            "device_id",
            unique=True,
            sqlite_where=text("status = 'active'"),
            postgresql_where=text("status = 'active'"),
        ),
        Index("ix_device_leases_active", "device_id", "status"),
        Index("ix_device_leases_expiry", "expires_at"),
        CheckConstraint(
            "status IN ('active', 'released', 'expired')",
            name="ck_device_leases_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    device_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("devices.id", ondelete="RESTRICT")
    )
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_tasks.id", ondelete="RESTRICT")
    )
    owner: Mapped[str] = mapped_column(String(200))
    token_hash: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    acquired_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    released_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    version: Mapped[int] = mapped_column(Integer)


class ScheduleFireRecord(Base):
    __tablename__ = "schedule_fires"
    __table_args__ = (
        UniqueConstraint("schedule_id", "fire_key", name="uq_schedule_fires_key"),
        Index("ix_schedule_fires_history", "schedule_id", "scheduled_for"),
        CheckConstraint(
            "status IN ('enqueued', 'skipped_misfire', 'skipped_overlap')",
            name="ck_schedule_fires_status",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    schedule_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("schedules.id", ondelete="CASCADE")
    )
    fire_key: Mapped[str] = mapped_column(String(250))
    scheduled_for: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    status: Mapped[str] = mapped_column(String(30))
    task_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("automation_tasks.id", ondelete="RESTRICT")
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


__all__ = [
    "AutomationTaskRecord",
    "DeviceLeaseRecord",
    "DeviceRecord",
    "ProviderConnectionRecord",
    "ProviderRunRecord",
    "ScheduleFireRecord",
    "ScheduleRecord",
]
