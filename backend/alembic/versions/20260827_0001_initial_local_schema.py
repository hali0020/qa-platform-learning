"""创建 QA 核心表与本地流水线运行表。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa

revision: str = "20260827_0001"
down_revision: str | Sequence[str] | None = None
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "projects",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index("ix_projects_key", "projects", ["key"], unique=True)
    op.create_index("ix_projects_status", "projects", ["status"], unique=False)

    op.create_table(
        "test_cases",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("preconditions", sa.Text(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("case_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_test_cases_project_id", "test_cases", ["project_id"], unique=False
    )
    op.create_index(
        "ix_test_cases_status", "test_cases", ["status"], unique=False
    )

    op.create_table(
        "test_plans",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_test_plans_project_id", "test_plans", ["project_id"], unique=False
    )
    op.create_index(
        "ix_test_plans_status", "test_plans", ["status"], unique=False
    )

    op.create_table(
        "test_plan_cases",
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["test_cases.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["test_plans.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("plan_id", "case_id"),
    )

    op.create_table(
        "test_executions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("plan_id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("started_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("completed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["plan_id"],
            ["test_plans.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_index(
        "ix_test_executions_plan_id",
        "test_executions",
        ["plan_id"],
        unique=True,
    )
    op.create_index(
        "ix_test_executions_project_id",
        "test_executions",
        ["project_id"],
        unique=False,
    )
    op.create_index(
        "ix_test_executions_status",
        "test_executions",
        ["status"],
        unique=False,
    )

    op.create_table(
        "case_execution_results",
        sa.Column("execution_id", sa.String(length=36), nullable=False),
        sa.Column("case_id", sa.String(length=36), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("case_title", sa.String(length=200), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("actual_result", sa.Text(), nullable=False),
        sa.Column("comment", sa.Text(), nullable=False),
        sa.Column("executed_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(
            ["case_id"],
            ["test_cases.id"],
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["execution_id"],
            ["test_executions.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("execution_id", "case_id"),
    )

    op.create_table(
        "pipeline_runtime_runs",
        sa.Column("id", sa.Text(), nullable=False),
        sa.Column("snapshot_json", sa.Text(), nullable=False),
        sa.Column("updated_at", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("id"),
    )
    op.create_table(
        "pipeline_runtime_trigger_keys",
        sa.Column("idempotency_key", sa.Text(), nullable=False),
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("request_fingerprint", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runtime_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("idempotency_key"),
    )
    op.create_index(
        "ix_pipeline_runtime_trigger_keys_run_id",
        "pipeline_runtime_trigger_keys",
        ["run_id"],
        unique=False,
    )
    op.create_table(
        "pipeline_runtime_callback_events",
        sa.Column("run_id", sa.Text(), nullable=False),
        sa.Column("event_id", sa.Text(), nullable=False),
        sa.Column("event_fingerprint", sa.Text(), nullable=False),
        sa.ForeignKeyConstraint(
            ["run_id"],
            ["pipeline_runtime_runs.id"],
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("run_id", "event_id"),
    )
    op.create_index(
        "ix_pipeline_runtime_callback_events_run_id",
        "pipeline_runtime_callback_events",
        ["run_id"],
        unique=False,
    )


def downgrade() -> None:
    op.drop_index(
        "ix_pipeline_runtime_callback_events_run_id",
        table_name="pipeline_runtime_callback_events",
    )
    op.drop_table("pipeline_runtime_callback_events")
    op.drop_index(
        "ix_pipeline_runtime_trigger_keys_run_id",
        table_name="pipeline_runtime_trigger_keys",
    )
    op.drop_table("pipeline_runtime_trigger_keys")
    op.drop_table("pipeline_runtime_runs")
    op.drop_table("case_execution_results")
    op.drop_index("ix_test_executions_status", table_name="test_executions")
    op.drop_index("ix_test_executions_project_id", table_name="test_executions")
    op.drop_index("ix_test_executions_plan_id", table_name="test_executions")
    op.drop_table("test_executions")
    op.drop_table("test_plan_cases")
    op.drop_index("ix_test_plans_status", table_name="test_plans")
    op.drop_index("ix_test_plans_project_id", table_name="test_plans")
    op.drop_table("test_plans")
    op.drop_index("ix_test_cases_status", table_name="test_cases")
    op.drop_index("ix_test_cases_project_id", table_name="test_cases")
    op.drop_table("test_cases")
    op.drop_index("ix_projects_status", table_name="projects")
    op.drop_index("ix_projects_key", table_name="projects")
    op.drop_table("projects")
