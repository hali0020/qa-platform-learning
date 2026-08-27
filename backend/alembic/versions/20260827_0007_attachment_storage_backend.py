"""Add internal attachment storage routing metadata.

Revision ID: 20260827_0007
Revises: 20260827_0006
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0007"
down_revision: str | Sequence[str] | None = "20260827_0006"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


BACKEND_CHECK = "ck_attachments_storage_backend_length"
NAMESPACE_CHECK = "ck_attachments_storage_namespace_length"
BACKEND_ALLOWED_CHECK = "ck_attachments_storage_backend_allowed"
ROUTE_CHECK = "ck_attachments_storage_route"
DOWNGRADE_GUARD_MESSAGE = (
    "cannot downgrade attachment storage routing while S3-backed attachments exist"
)


def upgrade() -> None:
    # Constant server defaults both preserve legacy rows during the table copy
    # used by SQLite and backfill existing PostgreSQL rows. They intentionally
    # remain as defaults for internal writers that omit routing metadata.
    with op.batch_alter_table("attachments") as batch:
        batch.add_column(
            sa.Column(
                "storage_backend",
                sa.String(length=50),
                nullable=False,
                server_default="local_filesystem",
            )
        )
        batch.add_column(
            sa.Column(
                "storage_namespace",
                sa.String(length=200),
                nullable=False,
                server_default="",
            )
        )
        batch.create_check_constraint(
            BACKEND_CHECK,
            "length(trim(storage_backend)) >= 1 "
            "AND length(storage_backend) <= 50",
        )
        batch.create_check_constraint(
            NAMESPACE_CHECK,
            "length(storage_namespace) <= 200",
        )
        batch.create_check_constraint(
            BACKEND_ALLOWED_CHECK,
            "storage_backend IN ('local_filesystem', 's3_local_container')",
        )
        batch.create_check_constraint(
            ROUTE_CHECK,
            "(storage_backend = 'local_filesystem' AND storage_namespace = '') "
            "OR (storage_backend = 's3_local_container' "
            "AND storage_namespace = 'qa-artifacts')",
        )


def downgrade() -> None:
    migration_context = op.get_context()
    if migration_context.as_sql:
        if migration_context.dialect.name != "postgresql":
            raise RuntimeError(
                "offline downgrade is only supported for PostgreSQL because "
                "attachment storage routing must be checked before dropping columns"
            )
        # Offline SQL cannot inspect the database while it is generated. Emit
        # a PostgreSQL guard into the script so applying it still fails before
        # any routing column is removed.
        op.execute(
            sa.text(
                """
                DO $$
                BEGIN
                    IF EXISTS (
                        SELECT 1 FROM attachments
                        WHERE storage_backend <> 'local_filesystem'
                           OR storage_namespace <> ''
                    ) THEN
                        RAISE EXCEPTION
                            'cannot downgrade attachment storage routing while S3-backed attachments exist';
                    END IF;
                END
                $$
                """
            )
        )
    else:
        unsafe_route = op.get_bind().execute(
            sa.text(
                """
                SELECT 1 FROM attachments
                WHERE storage_backend <> 'local_filesystem'
                   OR storage_namespace <> ''
                LIMIT 1
                """
            )
        ).first()
        if unsafe_route is not None:
            raise RuntimeError(DOWNGRADE_GUARD_MESSAGE)

    with op.batch_alter_table("attachments") as batch:
        batch.drop_constraint(ROUTE_CHECK, type_="check")
        batch.drop_constraint(BACKEND_ALLOWED_CHECK, type_="check")
        batch.drop_constraint(NAMESPACE_CHECK, type_="check")
        batch.drop_constraint(BACKEND_CHECK, type_="check")
        batch.drop_column("storage_namespace")
        batch.drop_column("storage_backend")
