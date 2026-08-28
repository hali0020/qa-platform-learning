"""Add Scheduler claims and the transactional task wake-up outbox.

Revision ID: 20260828_0011
Revises: 20260828_0010
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260828_0011"
down_revision: str | Sequence[str] | None = "20260828_0010"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    with op.batch_alter_table("schedules", recreate="auto") as batch:
        batch.add_column(
            sa.Column("claim_owner", sa.String(length=200), nullable=True)
        )
        batch.add_column(
            sa.Column("claim_token_hash", sa.String(length=64), nullable=True)
        )
        batch.add_column(
            sa.Column(
                "claim_expires_at",
                sa.DateTime(timezone=True),
                nullable=True,
            )
        )
        batch.create_check_constraint(
            "ck_schedules_claim_shape",
            "(claim_owner IS NULL AND claim_token_hash IS NULL AND "
            "claim_expires_at IS NULL) OR "
            "(claim_owner IS NOT NULL AND claim_token_hash IS NOT NULL AND "
            "claim_expires_at IS NOT NULL)",
        )
        batch.create_index(
            "ix_schedules_claim",
            ["enabled", "next_run_at", "claim_expires_at"],
        )

    op.create_table(
        "automation_task_wakeup_outbox",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("task_id", sa.String(length=36), nullable=False),
        sa.Column("generation", sa.Integer(), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("publish_attempts", sa.Integer(), nullable=False),
        sa.Column("available_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("lease_owner", sa.String(length=200), nullable=True),
        sa.Column("lease_token_hash", sa.String(length=64), nullable=True),
        sa.Column(
            "lease_expires_at",
            sa.DateTime(timezone=True),
            nullable=True,
        ),
        sa.Column("last_error_code", sa.String(length=100), nullable=True),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("published_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "status IN ('pending', 'claimed', 'retry_wait', 'published')",
            name="ck_task_wakeup_outbox_status",
        ),
        sa.CheckConstraint(
            "generation >= 0",
            name="ck_task_wakeup_outbox_generation",
        ),
        sa.CheckConstraint(
            "publish_attempts >= 0",
            name="ck_task_wakeup_outbox_attempts",
        ),
        sa.CheckConstraint(
            "version >= 0",
            name="ck_task_wakeup_outbox_version",
        ),
        sa.CheckConstraint(
            "(status = 'claimed' AND lease_owner IS NOT NULL AND "
            "lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
            "(status <> 'claimed' AND lease_owner IS NULL AND "
            "lease_token_hash IS NULL AND lease_expires_at IS NULL)",
            name="ck_task_wakeup_outbox_lease_shape",
        ),
        sa.CheckConstraint(
            "(status = 'published' AND published_at IS NOT NULL) OR "
            "(status <> 'published' AND published_at IS NULL)",
            name="ck_task_wakeup_outbox_published_shape",
        ),
        sa.ForeignKeyConstraint(
            ["task_id"],
            ["automation_tasks.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "task_id",
            "generation",
            name="uq_task_wakeup_outbox_generation",
        ),
    )
    op.create_index(
        "ix_task_wakeup_outbox_claim",
        "automation_task_wakeup_outbox",
        ["status", "available_at", "created_at"],
    )
    op.create_index(
        "ix_task_wakeup_outbox_lease",
        "automation_task_wakeup_outbox",
        ["lease_expires_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_task_wakeup_outbox_lease",
        table_name="automation_task_wakeup_outbox",
    )
    op.drop_index(
        "ix_task_wakeup_outbox_claim",
        table_name="automation_task_wakeup_outbox",
    )
    op.drop_table("automation_task_wakeup_outbox")

    with op.batch_alter_table("schedules", recreate="auto") as batch:
        batch.drop_index("ix_schedules_claim")
        batch.drop_constraint("ck_schedules_claim_shape", type_="check")
        batch.drop_column("claim_expires_at")
        batch.drop_column("claim_token_hash")
        batch.drop_column("claim_owner")
