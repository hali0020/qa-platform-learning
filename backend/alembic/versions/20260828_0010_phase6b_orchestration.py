"""Add durable quality gates, artifacts, webhooks and trigger outbox.

Revision ID: 20260828_0010
Revises: 20260827_0009
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0010"
down_revision: str | Sequence[str] | None = "20260827_0009"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.add_column(
        "provider_connections",
        sa.Column("webhook_secret_env_var", sa.String(length=128), nullable=True),
    )

    with op.batch_alter_table("provider_runs", recreate="auto") as batch:
        batch.alter_column(
            "external_id",
            existing_type=sa.String(length=300),
            nullable=True,
        )
        batch.add_column(
            sa.Column(
                "dispatch_status",
                sa.String(length=20),
                nullable=False,
                server_default="dispatched",
            )
        )
        batch.add_column(
            sa.Column(
                "quality_gate_status",
                sa.String(length=30),
                nullable=False,
                server_default="not_required",
            )
        )
        batch.add_column(
            sa.Column(
                "last_provider_sequence",
                sa.Integer(),
                nullable=False,
                server_default="0",
            )
        )
        batch.add_column(
            sa.Column(
                "last_provider_occurred_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch.add_column(
            sa.Column(
                "reconciliation_required",
                sa.Boolean(),
                nullable=False,
                server_default=sa.false(),
            )
        )
        batch.add_column(
            sa.Column("triggered_by_user_id", sa.String(length=36), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "triggered_by_name",
                sa.String(length=100),
                nullable=False,
                server_default="local-user",
            )
        )
        batch.add_column(
            sa.Column(
                "version", sa.Integer(), nullable=False, server_default="0"
            )
        )
        batch.create_foreign_key(
            "fk_provider_runs_triggered_by_user_id_users",
            "users",
            ["triggered_by_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_check_constraint(
            "ck_provider_runs_dispatch_status",
            "dispatch_status IN ('pending', 'dispatching', 'dispatched', "
            "'retry_wait', 'unknown', 'failed', 'cancelled')",
        )
        batch.create_check_constraint(
            "ck_provider_runs_quality_gate_status",
            "quality_gate_status IN ('not_required', 'evaluating', "
            "'waiting_approval', 'approved', 'rejected', 'failed', 'cancelled')",
        )
        batch.create_check_constraint(
            "ck_provider_runs_provider_sequence",
            "last_provider_sequence >= 0",
        )
        batch.create_check_constraint("ck_provider_runs_version", "version >= 0")
        batch.create_index(
            "ix_provider_runs_dispatch", ["dispatch_status", "created_at"]
        )
        batch.create_index(
            "ix_provider_runs_reconciliation",
            ["reconciliation_required", "updated_at"],
        )

    op.create_table(
        "provider_run_approvals",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("decision", sa.String(length=10), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("actor_user_id", sa.String(length=36), nullable=True),
        sa.Column("actor_name", sa.String(length=100), nullable=False),
        sa.Column("comment", sa.String(length=1000), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "decision IN ('approve', 'reject')",
            name="ck_provider_run_approvals_decision",
        ),
        sa.ForeignKeyConstraint(
            ["actor_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["provider_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_provider_run_approvals_run"),
        sa.UniqueConstraint(
            "run_id", "event_id", name="uq_provider_run_approvals_event"
        ),
    )
    op.create_index(
        "ix_provider_run_approvals_actor",
        "provider_run_approvals",
        ["actor_user_id", "created_at"],
    )

    op.create_table(
        "provider_run_artifacts",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("kind", sa.String(length=20), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_backend", sa.String(length=50), nullable=True),
        sa.Column("storage_namespace", sa.String(length=200), nullable=True),
        sa.Column("storage_key", sa.String(length=200), nullable=True),
        sa.Column("media_type", sa.String(length=200), nullable=True),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=True),
        sa.Column("error_code", sa.String(length=100), nullable=True),
        sa.Column("created_by_user_id", sa.String(length=36), nullable=True),
        sa.Column("created_by_name", sa.String(length=100), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("ready_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("failed_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "kind IN ('test_report', 'artifact')",
            name="ck_provider_run_artifacts_kind",
        ),
        sa.CheckConstraint(
            "status IN ('pending', 'ready', 'failed', 'deleted')",
            name="ck_provider_run_artifacts_status",
        ),
        sa.CheckConstraint(
            "size_bytes >= 0", name="ck_provider_run_artifacts_size"
        ),
        sa.ForeignKeyConstraint(
            ["created_by_user_id"], ["users.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["provider_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "storage_key", name="uq_provider_run_artifacts_storage_key"
        ),
    )
    op.create_index(
        "ix_provider_run_artifacts_run",
        "provider_run_artifacts",
        ["run_id", "created_at"],
    )
    op.create_index(
        "ix_provider_run_artifacts_status",
        "provider_run_artifacts",
        ["status", "updated_at"],
    )

    op.create_table(
        "provider_webhook_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=True),
        sa.Column("event_id", sa.String(length=200), nullable=False),
        sa.Column("external_id", sa.String(length=300), nullable=False),
        sa.Column("body_sha256", sa.String(length=64), nullable=False),
        sa.Column("sequence", sa.Integer(), nullable=False),
        sa.Column("occurred_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("normalized_status", sa.String(length=30), nullable=False),
        sa.Column("result", sa.String(length=30), nullable=False),
        sa.Column("received_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("processed_at", sa.DateTime(timezone=True), nullable=False),
        sa.CheckConstraint(
            "sequence >= 1", name="ck_provider_webhook_events_sequence"
        ),
        sa.CheckConstraint(
            "result IN ('applied', 'stale', 'reconcile_required', 'ignored')",
            name="ck_provider_webhook_events_result",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["provider_connections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["provider_runs.id"], ondelete="RESTRICT"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "connection_id", "event_id", name="uq_provider_webhook_events_event"
        ),
    )
    op.create_index(
        "ix_provider_webhook_events_run",
        "provider_webhook_events",
        ["run_id", "received_at"],
    )
    op.create_index(
        "ix_provider_webhook_events_result",
        "provider_webhook_events",
        ["result", "received_at"],
    )

    op.create_table(
        "provider_trigger_intents",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("run_id", sa.String(length=36), nullable=False),
        sa.Column("connection_id", sa.String(length=36), nullable=False),
        sa.Column("connection_version", sa.Integer(), nullable=False),
        sa.Column("request_payload", sa.JSON(), nullable=False),
        sa.Column("idempotency_key", sa.String(length=200), nullable=False),
        sa.Column("request_fingerprint", sa.String(length=64), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("attempts", sa.Integer(), nullable=False),
        sa.Column("max_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column("lease_expires_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'retry_wait', 'succeeded', "
            "'unknown', 'failed', 'cancelled')",
            name="ck_provider_trigger_intents_status",
        ),
        sa.CheckConstraint(
            "attempts >= 0", name="ck_provider_trigger_intents_attempts"
        ),
        sa.CheckConstraint(
            "max_attempts BETWEEN 1 AND 20",
            name="ck_provider_trigger_intents_max_attempts",
        ),
        sa.ForeignKeyConstraint(
            ["connection_id"], ["provider_connections.id"], ondelete="RESTRICT"
        ),
        sa.ForeignKeyConstraint(
            ["run_id"], ["provider_runs.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("run_id", name="uq_provider_trigger_intents_run"),
        sa.UniqueConstraint(
            "connection_id",
            "idempotency_key",
            name="uq_provider_trigger_intents_idempotency",
        ),
    )
    op.create_index(
        "ix_provider_trigger_intents_claim",
        "provider_trigger_intents",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_provider_trigger_intents_lease",
        "provider_trigger_intents",
        ["lease_expires_at"],
    )

    permission_table = sa.table(
        "permissions",
        sa.column("code", sa.String),
        sa.column("description", sa.Text),
    )
    role_permission_table = sa.table(
        "role_permissions",
        sa.column("role_key", sa.String),
        sa.column("permission_code", sa.String),
    )
    op.bulk_insert(
        permission_table,
        [{"code": "pipeline.approve", "description": "审批本地流水线质量门禁"}],
    )
    op.bulk_insert(
        role_permission_table,
        [
            {"role_key": "system_admin", "permission_code": "pipeline.approve"},
            {"role_key": "qa_lead", "permission_code": "pipeline.approve"},
        ],
    )


def downgrade() -> None:
    op.execute(
        "DELETE FROM role_permissions WHERE permission_code = 'pipeline.approve'"
    )
    op.execute("DELETE FROM permissions WHERE code = 'pipeline.approve'")

    op.drop_index(
        "ix_provider_trigger_intents_lease",
        table_name="provider_trigger_intents",
    )
    op.drop_index(
        "ix_provider_trigger_intents_claim",
        table_name="provider_trigger_intents",
    )
    op.drop_table("provider_trigger_intents")

    op.drop_index(
        "ix_provider_webhook_events_result",
        table_name="provider_webhook_events",
    )
    op.drop_index(
        "ix_provider_webhook_events_run",
        table_name="provider_webhook_events",
    )
    op.drop_table("provider_webhook_events")

    op.drop_index(
        "ix_provider_run_artifacts_status",
        table_name="provider_run_artifacts",
    )
    op.drop_index(
        "ix_provider_run_artifacts_run",
        table_name="provider_run_artifacts",
    )
    op.drop_table("provider_run_artifacts")

    op.drop_index(
        "ix_provider_run_approvals_actor",
        table_name="provider_run_approvals",
    )
    op.drop_table("provider_run_approvals")

    # Phase 6B creates a durable local Run before provider HTTP returns, so
    # ``external_id`` can legitimately be NULL.  The preceding schema requires
    # a value and also keeps (connection_id, external_id) unique.  A stable
    # prefix plus the Run primary key preserves recognizability and gives every
    # pending row a distinct local-only identity during a deliberate rollback.
    op.execute(
        "UPDATE provider_runs "
        "SET external_id = 'local-downgrade-pending-' || id "
        "WHERE external_id IS NULL"
    )

    with op.batch_alter_table("provider_runs", recreate="auto") as batch:
        batch.drop_index("ix_provider_runs_reconciliation")
        batch.drop_index("ix_provider_runs_dispatch")
        batch.drop_constraint("ck_provider_runs_version", type_="check")
        batch.drop_constraint(
            "ck_provider_runs_provider_sequence", type_="check"
        )
        batch.drop_constraint(
            "ck_provider_runs_quality_gate_status", type_="check"
        )
        batch.drop_constraint(
            "ck_provider_runs_dispatch_status", type_="check"
        )
        batch.drop_constraint(
            "fk_provider_runs_triggered_by_user_id_users", type_="foreignkey"
        )
        batch.drop_column("version")
        batch.drop_column("triggered_by_name")
        batch.drop_column("triggered_by_user_id")
        batch.drop_column("reconciliation_required")
        batch.drop_column("last_provider_occurred_at")
        batch.drop_column("last_provider_sequence")
        batch.drop_column("quality_gate_status")
        batch.drop_column("dispatch_status")
        batch.alter_column(
            "external_id",
            existing_type=sa.String(length=300),
            nullable=False,
        )

    op.drop_column("provider_connections", "webhook_secret_env_var")
