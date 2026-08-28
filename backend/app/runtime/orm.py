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
    webhook_secret_env_var: Mapped[str | None] = mapped_column(
        String(128), default=None
    )
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
        Index("ix_provider_runs_dispatch", "dispatch_status", "created_at"),
        Index(
            "ix_provider_runs_reconciliation",
            "reconciliation_required",
            "updated_at",
        ),
        CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_provider_runs_status",
        ),
        CheckConstraint(
            "dispatch_status IN ('pending', 'dispatching', 'dispatched', "
            "'retry_wait', 'unknown', 'failed', 'cancelled')",
            name="ck_provider_runs_dispatch_status",
        ),
        CheckConstraint(
            "quality_gate_status IN ('not_required', 'evaluating', "
            "'waiting_approval', 'approved', 'rejected', 'failed', 'cancelled')",
            name="ck_provider_runs_quality_gate_status",
        ),
        CheckConstraint(
            "last_provider_sequence >= 0",
            name="ck_provider_runs_provider_sequence",
        ),
        CheckConstraint("version >= 0", name="ck_provider_runs_version"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_connections.id", ondelete="RESTRICT")
    )
    # The durable local intent exists before an external request is made.
    # ``external_id`` is therefore absent until a dispatcher finalizes it.
    external_id: Mapped[str | None] = mapped_column(String(300))
    status: Mapped[str] = mapped_column(String(20))
    raw_status: Mapped[str] = mapped_column(String(100))
    web_url: Mapped[str | None] = mapped_column(String(2048))
    message: Mapped[str | None] = mapped_column(String(500))
    run_metadata: Mapped[dict[str, Any]] = mapped_column("metadata", JSON)
    correlation_id: Mapped[str | None] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    dispatch_status: Mapped[str] = mapped_column(String(20), default="dispatched")
    quality_gate_status: Mapped[str] = mapped_column(
        String(30), default="not_required"
    )
    last_provider_sequence: Mapped[int] = mapped_column(Integer, default=0)
    last_provider_occurred_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True)
    )
    reconciliation_required: Mapped[bool] = mapped_column(Boolean, default=False)
    triggered_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    triggered_by_name: Mapped[str] = mapped_column(String(100), default="local-user")
    version: Mapped[int] = mapped_column(Integer, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderRunApprovalRecord(Base):
    """Append-only human decision observed by the QA orchestrator."""

    __tablename__ = "provider_run_approvals"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_provider_run_approvals_run"),
        UniqueConstraint(
            "run_id", "event_id", name="uq_provider_run_approvals_event"
        ),
        Index("ix_provider_run_approvals_actor", "actor_user_id", "created_at"),
        CheckConstraint(
            "decision IN ('approve', 'reject')",
            name="ck_provider_run_approvals_decision",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_runs.id", ondelete="RESTRICT")
    )
    event_id: Mapped[str] = mapped_column(String(200))
    decision: Mapped[str] = mapped_column(String(10))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    actor_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    actor_name: Mapped[str] = mapped_column(String(100))
    comment: Mapped[str] = mapped_column(String(1000))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderRunArtifactRecord(Base):
    """Relational truth for test reports and CI artifacts.

    Object bytes live behind ``AttachmentStorage``.  Pending/failed rows make
    cross-resource compensation visible instead of pretending storage and SQL
    participate in one transaction.
    """

    __tablename__ = "provider_run_artifacts"
    __table_args__ = (
        UniqueConstraint("storage_key", name="uq_provider_run_artifacts_storage_key"),
        Index("ix_provider_run_artifacts_run", "run_id", "created_at"),
        Index("ix_provider_run_artifacts_status", "status", "updated_at"),
        CheckConstraint(
            "kind IN ('test_report', 'artifact')",
            name="ck_provider_run_artifacts_kind",
        ),
        CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'deleted')",
            name="ck_provider_run_artifacts_status",
        ),
        CheckConstraint("size_bytes >= 0", name="ck_provider_run_artifacts_size"),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_runs.id", ondelete="RESTRICT")
    )
    kind: Mapped[str] = mapped_column(String(20))
    status: Mapped[str] = mapped_column(String(20))
    original_filename: Mapped[str] = mapped_column(String(255))
    storage_backend: Mapped[str | None] = mapped_column(String(50))
    storage_namespace: Mapped[str | None] = mapped_column(String(200))
    storage_key: Mapped[str | None] = mapped_column(String(200))
    media_type: Mapped[str | None] = mapped_column(String(200))
    size_bytes: Mapped[int] = mapped_column(Integer)
    sha256: Mapped[str | None] = mapped_column(String(64))
    error_code: Mapped[str | None] = mapped_column(String(100))
    created_by_user_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("users.id", ondelete="RESTRICT")
    )
    created_by_name: Mapped[str] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    ready_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    failed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ProviderWebhookEventRecord(Base):
    """Verified webhook receipt; never stores the signature or raw body."""

    __tablename__ = "provider_webhook_events"
    __table_args__ = (
        UniqueConstraint(
            "connection_id", "event_id", name="uq_provider_webhook_events_event"
        ),
        Index("ix_provider_webhook_events_run", "run_id", "received_at"),
        Index("ix_provider_webhook_events_result", "result", "received_at"),
        CheckConstraint("sequence >= 1", name="ck_provider_webhook_events_sequence"),
        CheckConstraint(
            "result IN ('applied', 'stale', 'reconcile_required', 'ignored')",
            name="ck_provider_webhook_events_result",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_connections.id", ondelete="RESTRICT")
    )
    run_id: Mapped[str | None] = mapped_column(
        String(36), ForeignKey("provider_runs.id", ondelete="RESTRICT")
    )
    event_id: Mapped[str] = mapped_column(String(200))
    external_id: Mapped[str] = mapped_column(String(300))
    body_sha256: Mapped[str] = mapped_column(String(64))
    sequence: Mapped[int] = mapped_column(Integer)
    occurred_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    normalized_status: Mapped[str] = mapped_column(String(30))
    result: Mapped[str] = mapped_column(String(30))
    received_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    processed_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))


class ProviderTriggerIntentRecord(Base):
    """Transactional outbox row claimed before provider HTTP is attempted."""

    __tablename__ = "provider_trigger_intents"
    __table_args__ = (
        UniqueConstraint("run_id", name="uq_provider_trigger_intents_run"),
        UniqueConstraint(
            "connection_id",
            "idempotency_key",
            name="uq_provider_trigger_intents_idempotency",
        ),
        Index(
            "ix_provider_trigger_intents_claim",
            "status",
            "available_at",
            "created_at",
        ),
        Index("ix_provider_trigger_intents_lease", "lease_expires_at"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'retry_wait', 'succeeded', "
            "'unknown', 'failed', 'cancelled')",
            name="ck_provider_trigger_intents_status",
        ),
        CheckConstraint("attempts >= 0", name="ck_provider_trigger_intents_attempts"),
        CheckConstraint(
            "max_attempts BETWEEN 1 AND 20",
            name="ck_provider_trigger_intents_max_attempts",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    run_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_runs.id", ondelete="CASCADE")
    )
    connection_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("provider_connections.id", ondelete="RESTRICT")
    )
    connection_version: Mapped[int] = mapped_column(Integer)
    request_payload: Mapped[dict[str, Any]] = mapped_column(JSON)
    idempotency_key: Mapped[str] = mapped_column(String(200))
    request_fingerprint: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20))
    attempts: Mapped[int] = mapped_column(Integer)
    max_attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class ScheduleRecord(Base):
    __tablename__ = "schedules"
    __table_args__ = (
        UniqueConstraint("name", name="uq_schedules_name"),
        Index("ix_schedules_due", "enabled", "next_run_at"),
        Index(
            "ix_schedules_claim",
            "enabled",
            "next_run_at",
            "claim_expires_at",
        ),
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
        CheckConstraint(
            "(claim_owner IS NULL AND claim_token_hash IS NULL AND "
            "claim_expires_at IS NULL) OR "
            "(claim_owner IS NOT NULL AND claim_token_hash IS NOT NULL AND "
            "claim_expires_at IS NOT NULL)",
            name="ck_schedules_claim_shape",
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
    claim_owner: Mapped[str | None] = mapped_column(String(200), default=None)
    claim_token_hash: Mapped[str | None] = mapped_column(String(64), default=None)
    claim_expires_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), default=None
    )
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


class AutomationTaskWakeupOutboxRecord(Base):
    """Durable fact that a content-free Worker wake-up still needs publishing."""

    __tablename__ = "automation_task_wakeup_outbox"
    __table_args__ = (
        UniqueConstraint(
            "task_id",
            "generation",
            name="uq_task_wakeup_outbox_generation",
        ),
        Index(
            "ix_task_wakeup_outbox_claim",
            "status",
            "available_at",
            "created_at",
        ),
        Index("ix_task_wakeup_outbox_lease", "lease_expires_at"),
        CheckConstraint(
            "status IN ('pending', 'claimed', 'retry_wait', 'published')",
            name="ck_task_wakeup_outbox_status",
        ),
        CheckConstraint(
            "generation >= 0",
            name="ck_task_wakeup_outbox_generation",
        ),
        CheckConstraint(
            "publish_attempts >= 0",
            name="ck_task_wakeup_outbox_attempts",
        ),
        CheckConstraint("version >= 0", name="ck_task_wakeup_outbox_version"),
        CheckConstraint(
            "(status = 'claimed' AND lease_owner IS NOT NULL AND "
            "lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_owner IS NULL AND "
            "lease_token_hash IS NULL AND lease_expires_at IS NULL)",
            name="ck_task_wakeup_outbox_lease_shape",
        ),
        CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR "
            "(status <> 'published' AND published_at IS NULL)",
            name="ck_task_wakeup_outbox_published_shape",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    task_id: Mapped[str] = mapped_column(
        String(36), ForeignKey("automation_tasks.id", ondelete="CASCADE")
    )
    generation: Mapped[int] = mapped_column(Integer)
    status: Mapped[str] = mapped_column(String(20))
    publish_attempts: Mapped[int] = mapped_column(Integer)
    available_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    lease_owner: Mapped[str | None] = mapped_column(String(200))
    lease_token_hash: Mapped[str | None] = mapped_column(String(64))
    lease_expires_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_error_code: Mapped[str | None] = mapped_column(String(100))
    version: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    published_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


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
    "AutomationTaskWakeupOutboxRecord",
    "DeviceLeaseRecord",
    "DeviceRecord",
    "ProviderConnectionRecord",
    "ProviderRunApprovalRecord",
    "ProviderRunArtifactRecord",
    "ProviderRunRecord",
    "ProviderTriggerIntentRecord",
    "ProviderWebhookEventRecord",
    "ScheduleFireRecord",
    "ScheduleRecord",
]
