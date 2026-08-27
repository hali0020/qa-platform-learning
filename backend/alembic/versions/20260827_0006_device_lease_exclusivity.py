"""Enforce one active lease per device at the database boundary.

Revision ID: 20260827_0006
Revises: 20260827_0005
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0006"
down_revision: str | Sequence[str] | None = "20260827_0005"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


INDEX_NAME = "uq_device_leases_one_active_per_device"


def upgrade() -> None:
    # A partial unique index is portable across the two supported backends and
    # still permits an unlimited released/expired history for each device.
    # Existing duplicate active rows intentionally make this migration fail
    # loudly instead of silently discarding lease history.
    op.create_index(
        INDEX_NAME,
        "device_leases",
        ["device_id"],
        unique=True,
        sqlite_where=sa.text("status = 'active'"),
        postgresql_where=sa.text("status = 'active'"),
    )


def downgrade() -> None:
    op.drop_index(INDEX_NAME, table_name="device_leases")
