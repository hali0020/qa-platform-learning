"""增加缺陷管理与通用审计记录。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0003"
down_revision: str | Sequence[str] | None = "20260827_0002"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "defects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=True),
        sa.Column("execution_id", sa.String(length=36), nullable=True),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("severity", sa.String(length=32), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("reporter", sa.String(length=100), nullable=False),
        sa.Column("assignee", sa.String(length=100), nullable=False),
        sa.Column("environment", sa.String(length=200), nullable=False),
        sa.Column("reproduction_steps", sa.JSON(), nullable=False),
        sa.Column("expected_result", sa.Text(), nullable=False),
        sa.Column("actual_result", sa.Text(), nullable=False),
        sa.Column("resolution", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("resolved_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("closed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["test_cases.id"],
            name="fk_defects_case_id_test_cases",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["test_executions.id"],
            name="fk_defects_execution_id_test_executions",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_defects_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_defects_assignee", "defects", ["assignee"])
    op.create_index("ix_defects_case_id", "defects", ["case_id"])
    op.create_index("ix_defects_execution_id", "defects", ["execution_id"])
    op.create_index("ix_defects_project_id", "defects", ["project_id"])
    op.create_index("ix_defects_severity", "defects", ["severity"])
    op.create_index("ix_defects_status", "defects", ["status"])

    op.create_table(
        "audit_events",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=True),
        sa.Column("entity_type", sa.String(length=64), nullable=False),
        sa.Column("entity_id", sa.String(length=64), nullable=False),
        sa.Column("action", sa.String(length=32), nullable=False),
        sa.Column("actor", sa.String(length=100), nullable=False),
        sa.Column("changes", sa.JSON(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_audit_events_created_at", "audit_events", ["created_at"])
    op.create_index("ix_audit_events_entity_id", "audit_events", ["entity_id"])
    op.create_index("ix_audit_events_entity_type", "audit_events", ["entity_type"])
    op.create_index("ix_audit_events_project_id", "audit_events", ["project_id"])


def downgrade() -> None:
    op.drop_index("ix_audit_events_project_id", table_name="audit_events")
    op.drop_index("ix_audit_events_entity_type", table_name="audit_events")
    op.drop_index("ix_audit_events_entity_id", table_name="audit_events")
    op.drop_index("ix_audit_events_created_at", table_name="audit_events")
    op.drop_table("audit_events")

    op.drop_index("ix_defects_status", table_name="defects")
    op.drop_index("ix_defects_severity", table_name="defects")
    op.drop_index("ix_defects_project_id", table_name="defects")
    op.drop_index("ix_defects_execution_id", table_name="defects")
    op.drop_index("ix_defects_case_id", table_name="defects")
    op.drop_index("ix_defects_assignee", table_name="defects")
    op.drop_table("defects")
