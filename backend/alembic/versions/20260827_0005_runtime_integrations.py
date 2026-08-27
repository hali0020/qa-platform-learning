"""Add persistent CI integrations, task queue, devices and schedules.

The tables are deliberately provider-neutral.  Provider credentials are not
columns: ``secret_env_var`` stores only the *name* of an environment variable.
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0005"
down_revision: str | Sequence[str] | None = "20260827_0004"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "provider_connections",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("base_url", sa.String(length=2048), nullable=True),
        sa.Column("definition_ref", sa.String(length=300), nullable=False),
        sa.Column("config", sa.JSON(), nullable=False),
        sa.Column("secret_env_var", sa.String(length=128), nullable=True),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "kind IN ('local', 'jenkins', 'gitlab', 'bk_ci')",
            name="ck_provider_connections_kind",
        ),
        sa.CheckConstraint("version >= 0", name="ck_provider_connections_version"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_provider_connections_name"),
    )
    op.create_index(
        "ix_provider_connections_kind_enabled",
        "provider_connections",
        ["kind", "enabled"],
    )

    op.create_table(
        "provider_runs",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("external_id", sa.String(length=300), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("raw_status", sa.String(length=100), nullable=False),
        sa.Column("web_url", sa.String(length=2048), nullable=True),
        sa.Column("message", sa.String(length=500), nullable=True),
        sa.Column("metadata", sa.JSON(), nullable=False),
        sa.Column("correlation_id", sa.String(length=200), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
            name="ck_provider_runs_status",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["provider_connections.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "external_id", name="uq_provider_runs_external"
        ),
        sa.UniqueConstraint(
            "connection_id", "correlation_id", name="uq_provider_runs_correlation"
        ),
    )
    op.create_index("ix_provider_runs_connection", "provider_runs", ["connection_id"])
    op.create_index("ix_provider_runs_status", "provider_runs", ["status"])

    op.create_table(
        "schedules",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("queue", sa.String(length=100), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("cron", sa.String(length=200), nullable=False),
        sa.Column("timezone", sa.String(length=100), nullable=False),
        sa.Column("misfire_policy", sa.String(length=30), nullable=False),
        sa.Column("overlap_policy", sa.String(length=20), nullable=False),
        sa.Column("misfire_grace_seconds", sa.Integer(), nullable=False),
        sa.Column("catch_up_limit", sa.Integer(), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("next_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_run_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name="ck_schedules_priority"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 100", name="ck_schedules_attempts"),
        sa.CheckConstraint(
            "misfire_policy IN ('skip', 'fire_once', 'catch_up_limited')",
            name="ck_schedules_misfire_policy",
        ),
        sa.CheckConstraint(
            "overlap_policy IN ('forbid', 'allow', 'replace')",
            name="ck_schedules_overlap_policy",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_schedules_name"),
    )
    op.create_index("ix_schedules_due", "schedules", ["enabled", "next_run_at"])

    op.create_table(
        "automation_tasks",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_type", sa.String(length=100), nullable=False),
        sa.Column("payload", sa.JSON(), nullable=False),
        sa.Column("queue", sa.String(length=100), nullable=False),
        sa.Column("priority", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=True),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("source_schedule_id", sa.String(length=36), nullable=True),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("cancel_requested", sa.Boolean(), nullable=False),
        sa.Column("result", sa.JSON(), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("finished_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["source_schedule_id"], ["schedules.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint("priority BETWEEN 0 AND 100", name="ck_automation_tasks_priority"),
        sa.CheckConstraint("attempts >= 0", name="ck_automation_tasks_attempts"),
        sa.CheckConstraint("max_attempts BETWEEN 1 AND 100", name="ck_automation_tasks_max_attempts"),
        sa.CheckConstraint(
            "status IN ('queued', 'running', 'retry_wait', 'succeeded', "
            "'failed', 'cancelled', 'dead_letter')",
            name="ck_automation_tasks_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_type", "idempotency_key", name="uq_automation_tasks_idempotency"
        ),
    )
    op.create_index(
        "ix_automation_tasks_claim",
        "automation_tasks",
        ["queue", "status", "available_at", "priority"],
    )
    op.create_index(
        "ix_automation_tasks_lease", "automation_tasks", ["lease_expires_at"]
    )
    op.create_index(
        "ix_automation_tasks_schedule", "automation_tasks", ["source_schedule_id"]
    )

    op.create_table(
        "devices",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("kind", sa.String(length=50), nullable=False),
        sa.Column("platform", sa.String(length=100), nullable=False),
        sa.Column("capabilities", sa.JSON(), nullable=False),
        sa.Column("agent_id", sa.String(length=200), nullable=False),
        sa.Column("enabled", sa.Boolean(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("last_heartbeat_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("active_lease_id", sa.String(length=36), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "status IN ('offline', 'idle', 'reserved', 'busy', 'maintenance')",
            name="ck_devices_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("name", name="uq_devices_name"),
        sa.UniqueConstraint("agent_id", name="uq_devices_agent_id"),
        sa.UniqueConstraint("active_lease_id", name="uq_devices_active_lease"),
    )
    op.create_index("ix_devices_available", "devices", ["enabled", "status"])

    op.create_table(
        "device_leases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("device_id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("owner", sa.String(length=200), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("acquired_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("released_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(["device_id"], ["devices.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(
            ["task_id"], ["automation_tasks.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('active', 'released', 'expired')",
            name="ck_device_leases_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash", name="uq_device_leases_token_hash"),
    )
    op.create_index(
        "ix_device_leases_active", "device_leases", ["device_id", "status"]
    )
    op.create_index("ix_device_leases_expiry", "device_leases", ["expires_at"])

    op.create_table(
        "schedule_fires",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("schedule_id", sa.String(length=36), nullable=False),
        sa.Column("fire_key", sa.String(length=250), nullable=False),
        sa.Column("scheduled_for", sa.DateTime(timezone=True), nullable=False),
        sa.Column("status", sa.String(length=30), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["schedule_id"], ["schedules.id"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(
            ["task_id"], ["automation_tasks.id"], ondelete="RESTRICT"
        ),
        sa.CheckConstraint(
            "status IN ('enqueued', 'skipped_misfire', 'skipped_overlap')",
            name="ck_schedule_fires_status",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "schedule_id", "fire_key", name="uq_schedule_fires_key"
        ),
    )
    op.create_index(
        "ix_schedule_fires_history",
        "schedule_fires",
        ["schedule_id", "scheduled_for"],
    )


def downgrade() -> None:
    op.drop_index("ix_schedule_fires_history", table_name="schedule_fires")
    op.drop_table("schedule_fires")
    op.drop_index("ix_device_leases_expiry", table_name="device_leases")
    op.drop_index("ix_device_leases_active", table_name="device_leases")
    op.drop_table("device_leases")
    op.drop_index("ix_devices_available", table_name="devices")
    op.drop_table("devices")
    op.drop_index("ix_automation_tasks_schedule", table_name="automation_tasks")
    op.drop_index("ix_automation_tasks_lease", table_name="automation_tasks")
    op.drop_index("ix_automation_tasks_claim", table_name="automation_tasks")
    op.drop_table("automation_tasks")
    op.drop_index("ix_schedules_due", table_name="schedules")
    op.drop_table("schedules")
    op.drop_index("ix_provider_runs_status", table_name="provider_runs")
    op.drop_index("ix_provider_runs_connection", table_name="provider_runs")
    op.drop_table("provider_runs")
    op.drop_index(
        "ix_provider_connections_kind_enabled", table_name="provider_connections"
    )
    op.drop_table("provider_connections")
