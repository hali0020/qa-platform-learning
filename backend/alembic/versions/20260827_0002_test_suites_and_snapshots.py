"""增加测试套件与不可变用例快照。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0002"
down_revision: str | Sequence[str] | None = "20260827_0001"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "test_suites",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("name", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["parent_id"],
            ["test_suites.id"],
            name="fk_test_suites_parent_id_test_suites",
            ondelete="RESTRICT",
        ),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_test_suites_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "project_id",
            "parent_id",
            "name",
            name="uq_test_suites_sibling_name",
        ),
    )
    op.create_index("ix_test_suites_parent_id", "test_suites", ["parent_id"])
    op.create_index("ix_test_suites_project_id", "test_suites", ["project_id"])
    op.create_index("ix_test_suites_status", "test_suites", ["status"])

    with op.batch_alter_table("test_cases") as batch:
        batch.add_column(sa.Column("suite_id", sa.String(length=36), nullable=True))
        batch.create_index("ix_test_cases_suite_id", ["suite_id"], unique=False)
        batch.create_foreign_key(
            "fk_test_cases_suite_id_test_suites",
            "test_suites",
            ["suite_id"],
            ["id"],
            ondelete="RESTRICT",
        )

    op.create_table(
        "test_case_snapshots",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("scope_type", sa.String(length=32), nullable=False),
        sa.Column("scope_id", sa.String(length=36), nullable=False),
        sa.Column("scope_name", sa.String(length=150), nullable=False),
        sa.Column("version", sa.Integer(), nullable=False),
        sa.Column("label", sa.String(length=150), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("case_count", sa.Integer(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["project_id"],
            ["projects.id"],
            name="fk_test_case_snapshots_project_id_projects",
            ondelete="RESTRICT",
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "scope_type",
            "scope_id",
            "version",
            name="uq_test_case_snapshots_scope_version",
        ),
    )
    op.create_index(
        "ix_test_case_snapshots_project_id",
        "test_case_snapshots",
        ["project_id"],
    )
    op.create_index(
        "ix_test_case_snapshots_scope_id",
        "test_case_snapshots",
        ["scope_id"],
    )
    op.create_index(
        "ix_test_case_snapshots_scope_type",
        "test_case_snapshots",
        ["scope_type"],
    )

    op.create_table(
        "test_case_snapshot_items",
        sa.Column("snapshot_id", sa.String(length=36), nullable=False),
        sa.Column("source_case_id", sa.String(length=36), nullable=False),
        sa.Column("source_suite_id", sa.String(length=36), nullable=True),
        sa.Column("suite_path", sa.JSON(), nullable=False),
        sa.Column("position", sa.Integer(), nullable=False),
        sa.Column("title", sa.String(length=200), nullable=False),
        sa.Column("preconditions", sa.Text(), nullable=False),
        sa.Column("steps", sa.JSON(), nullable=False),
        sa.Column("priority", sa.String(length=8), nullable=False),
        sa.Column("case_type", sa.String(length=32), nullable=False),
        sa.Column("status", sa.String(length=32), nullable=False),
        sa.Column("tags", sa.JSON(), nullable=False),
        sa.Column("source_created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("source_updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(
            ["snapshot_id"],
            ["test_case_snapshots.id"],
            name="fk_test_case_snapshot_items_snapshot_id",
            ondelete="CASCADE",
        ),
        sa.PrimaryKeyConstraint("snapshot_id", "source_case_id"),
    )


def downgrade() -> None:
    op.drop_table("test_case_snapshot_items")
    op.drop_index(
        "ix_test_case_snapshots_scope_type",
        table_name="test_case_snapshots",
    )
    op.drop_index(
        "ix_test_case_snapshots_scope_id",
        table_name="test_case_snapshots",
    )
    op.drop_index(
        "ix_test_case_snapshots_project_id",
        table_name="test_case_snapshots",
    )
    op.drop_table("test_case_snapshots")

    with op.batch_alter_table("test_cases") as batch:
        batch.drop_constraint(
            "fk_test_cases_suite_id_test_suites",
            type_="foreignkey",
        )
        batch.drop_index("ix_test_cases_suite_id")
        batch.drop_column("suite_id")

    op.drop_index("ix_test_suites_status", table_name="test_suites")
    op.drop_index("ix_test_suites_project_id", table_name="test_suites")
    op.drop_index("ix_test_suites_parent_id", table_name="test_suites")
    op.drop_table("test_suites")
