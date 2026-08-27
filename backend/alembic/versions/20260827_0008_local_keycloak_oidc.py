"""Add local Keycloak OIDC identity bindings and login transactions.

Revision ID: 20260827_0008
Revises: 20260827_0007
"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0008"
down_revision: str | Sequence[str] | None = "20260827_0007"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


def upgrade() -> None:
    op.create_table(
        "oidc_identities",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("issuer", sa.String(length=200), nullable=False),
        sa.Column("subject", sa.String(length=255), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "issuer = 'http://127.0.0.1:23010/identity/realms/qa-learning'",
            name="ck_oidc_identities_local_issuer",
        ),
        sa.CheckConstraint(
            "length(subject) >= 1 AND length(subject) <= 255",
            name="ck_oidc_identities_subject_length",
        ),
        sa.ForeignKeyConstraint(
            ["user_id"], ["users.id"], ondelete="CASCADE"
        ),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint(
            "issuer",
            "subject",
            name="uq_oidc_identities_issuer_subject",
        ),
    )
    op.create_index(
        "ix_oidc_identities_user_id",
        "oidc_identities",
        ["user_id"],
        unique=True,
    )

    op.create_table(
        "oidc_login_transactions",
        sa.Column("state_hash", sa.String(length=64), nullable=False),
        sa.Column(
            "browser_binding_hash", sa.String(length=64), nullable=False
        ),
        sa.Column("nonce_hash", sa.String(length=64), nullable=False),
        sa.Column("code_verifier", sa.String(length=128), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("consumed_at", sa.DateTime(timezone=True), nullable=True),
        sa.CheckConstraint(
            "length(state_hash) = 64",
            name="ck_oidc_login_transactions_state_hash",
        ),
        sa.CheckConstraint(
            "length(browser_binding_hash) = 64",
            name="ck_oidc_login_transactions_browser_hash",
        ),
        sa.CheckConstraint(
            "length(nonce_hash) = 64",
            name="ck_oidc_login_transactions_nonce_hash",
        ),
        sa.CheckConstraint(
            "length(code_verifier) >= 43 AND length(code_verifier) <= 128",
            name="ck_oidc_login_transactions_verifier_length",
        ),
        sa.PrimaryKeyConstraint("state_hash"),
    )
    op.create_index(
        "ix_oidc_login_transactions_expires_at",
        "oidc_login_transactions",
        ["expires_at"],
    )
    op.create_index(
        "ix_oidc_login_transactions_consumed_at",
        "oidc_login_transactions",
        ["consumed_at"],
    )


def downgrade() -> None:
    op.drop_index(
        "ix_oidc_login_transactions_consumed_at",
        table_name="oidc_login_transactions",
    )
    op.drop_index(
        "ix_oidc_login_transactions_expires_at",
        table_name="oidc_login_transactions",
    )
    op.drop_table("oidc_login_transactions")
    op.drop_index(
        "ix_oidc_identities_user_id",
        table_name="oidc_identities",
    )
    op.drop_table("oidc_identities")
