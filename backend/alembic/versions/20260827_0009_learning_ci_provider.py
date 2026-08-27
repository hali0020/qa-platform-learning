"""Allow the project-owned Learning CI provider kind.

Revision ID: 20260827_0009
Revises: 20260827_0008
"""

from collections.abc import Sequence

from alembic import context, op


revision: str = "20260827_0009"
down_revision: str | Sequence[str] | None = "20260827_0008"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


_OLD_KINDS = "kind IN ('local', 'jenkins', 'gitlab', 'bk_ci')"
_NEW_KINDS = (
    "kind IN ('local', 'learning_ci', 'jenkins', 'gitlab', 'bk_ci')"
)


def _replace_kind_constraint(expression: str) -> None:
    if context.get_context().dialect.name == "sqlite":
        # SQLite has no ALTER CONSTRAINT, so preserve the table through a
        # controlled batch rebuild.
        with op.batch_alter_table(
            "provider_connections", recreate="always"
        ) as batch:
            batch.drop_constraint(
                "ck_provider_connections_kind",
                type_="check",
            )
            batch.create_check_constraint(
                "ck_provider_connections_kind",
                expression,
            )
        return

    # PostgreSQL can update the constraint in place, avoiding a table rebuild
    # while provider_runs holds a foreign key to provider_connections.
    op.drop_constraint(
        "ck_provider_connections_kind",
        "provider_connections",
        type_="check",
    )
    op.create_check_constraint(
        "ck_provider_connections_kind",
        "provider_connections",
        expression,
    )


def upgrade() -> None:
    _replace_kind_constraint(_NEW_KINDS)


def downgrade() -> None:
    # If a Learning CI connection still exists, copying it into the stricter
    # table fails rather than silently deleting or rewriting user data.
    _replace_kind_constraint(_OLD_KINDS)
