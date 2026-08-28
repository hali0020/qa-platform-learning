"""Private SQLite/SQLAlchemy Core persistence for CI Lab.

The constructor accepts only a local filesystem path.  It cannot be pointed at
a network database URL, and no public API accepts or returns this path.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from os import fspath
from pathlib import Path

from sqlalchemy import (
    JSON,
    CheckConstraint,
    Column,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    MetaData,
    String,
    Table,
    Text,
    UniqueConstraint,
    event,
)
from sqlalchemy.exc import OperationalError
from sqlalchemy.ext.asyncio import AsyncConnection, AsyncEngine, create_async_engine


metadata = MetaData()

runs = Table(
    "ci_lab_runs",
    metadata,
    Column("id", String(36), primary_key=True),
    Column("definition", String(100), nullable=False),
    Column("definition_revision", Integer, nullable=False),
    Column("ref", String(128), nullable=True),
    Column("variables", JSON, nullable=False),
    Column("idempotency_key", String(200), nullable=False, unique=True),
    Column("request_fingerprint", String(64), nullable=False),
    Column("status", String(20), nullable=False),
    Column("message", String(500), nullable=True),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("started_at", DateTime(timezone=True), nullable=True),
    Column("finished_at", DateTime(timezone=True), nullable=True),
    Column("cancelled_at", DateTime(timezone=True), nullable=True),
    CheckConstraint(
        "status IN ('queued', 'running', 'succeeded', 'failed', 'cancelled')",
        name="ck_ci_lab_runs_status",
    ),
    CheckConstraint(
        "definition_revision >= 1",
        name="ck_ci_lab_runs_definition_revision",
    ),
)
Index("ix_ci_lab_runs_created", runs.c.created_at)
Index("ix_ci_lab_runs_definition", runs.c.definition, runs.c.created_at)


quality_gates = Table(
    "ci_lab_quality_gates",
    metadata,
    Column(
        "run_id",
        String(36),
        ForeignKey("ci_lab_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    Column("policy_revision", Integer, nullable=False),
    Column("status", String(30), nullable=False),
    Column("reached_at", DateTime(timezone=True), nullable=True),
    Column("decided_at", DateTime(timezone=True), nullable=True),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "status IN ('evaluating', 'waiting_approval', 'approved', "
        "'rejected', 'failed', 'cancelled')",
        name="ck_ci_lab_quality_gates_status",
    ),
    CheckConstraint(
        "policy_revision >= 1",
        name="ck_ci_lab_quality_gates_policy_revision",
    ),
)
Index("ix_ci_lab_quality_gates_status", quality_gates.c.status)


approvals = Table(
    "ci_lab_run_approvals",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "run_id",
        String(36),
        ForeignKey("ci_lab_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("event_id", String(200), nullable=False),
    Column("request_fingerprint", String(64), nullable=False),
    Column("decision", String(20), nullable=False),
    Column("actor_id", String(100), nullable=False),
    Column("actor_name", String(100), nullable=False),
    Column("comment", String(1000), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    CheckConstraint(
        "decision IN ('approve', 'reject')",
        name="ck_ci_lab_run_approvals_decision",
    ),
    UniqueConstraint("run_id", name="uq_ci_lab_run_approvals_run"),
    UniqueConstraint(
        "run_id",
        "event_id",
        name="uq_ci_lab_run_approvals_event",
    ),
)
Index("ix_ci_lab_run_approvals_created", approvals.c.run_id, approvals.c.created_at)


webhook_subscriptions = Table(
    "ci_lab_webhook_subscriptions",
    metadata,
    Column(
        "run_id",
        String(36),
        ForeignKey("ci_lab_runs.id", ondelete="CASCADE"),
        primary_key=True,
    ),
    # This is a routing identifier, never a callback URL.  The delivery worker
    # combines it with one code-owned local target selected at startup.
    Column("connection_id", String(36), nullable=False),
    Column("correlation_id", String(200), nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
)
Index(
    "ix_ci_lab_webhook_subscriptions_connection",
    webhook_subscriptions.c.connection_id,
    webhook_subscriptions.c.created_at,
)


webhook_deliveries = Table(
    "ci_lab_webhook_deliveries",
    metadata,
    Column("id", String(36), primary_key=True),
    Column(
        "run_id",
        String(36),
        ForeignKey("ci_lab_runs.id", ondelete="CASCADE"),
        nullable=False,
    ),
    Column("connection_id", String(36), nullable=False),
    Column("event_id", String(200), nullable=False, unique=True),
    Column("sequence", Integer, nullable=False),
    Column("occurred_at", DateTime(timezone=True), nullable=False),
    Column("normalized_status", String(30), nullable=False),
    Column("message", String(500), nullable=True),
    # Exact canonical UTF-8 JSON is retained so every retry signs the same
    # bytes. URLs, signatures, secrets and plaintext lease tokens are absent.
    Column("payload_body", Text, nullable=False),
    Column("body_sha256", String(64), nullable=False),
    Column("status", String(20), nullable=False),
    Column("attempts", Integer, nullable=False),
    Column("max_attempts", Integer, nullable=False),
    Column("available_at", DateTime(timezone=True), nullable=False),
    Column("lease_owner", String(200), nullable=True),
    Column("lease_token_hash", String(64), nullable=True),
    Column("lease_expires_at", DateTime(timezone=True), nullable=True),
    Column("last_error_code", String(100), nullable=True),
    Column("version", Integer, nullable=False),
    Column("created_at", DateTime(timezone=True), nullable=False),
    Column("updated_at", DateTime(timezone=True), nullable=False),
    Column("delivered_at", DateTime(timezone=True), nullable=True),
    Column("dead_lettered_at", DateTime(timezone=True), nullable=True),
    UniqueConstraint(
        "run_id",
        "sequence",
        name="uq_ci_lab_webhook_deliveries_run_sequence",
    ),
    CheckConstraint(
        "status IN ('pending', 'claimed', 'retry_wait', 'delivered', 'dead_letter')",
        name="ck_ci_lab_webhook_deliveries_status",
    ),
    CheckConstraint(
        "sequence BETWEEN 1 AND 2147483647",
        name="ck_ci_lab_webhook_deliveries_sequence",
    ),
    CheckConstraint(
        "attempts >= 0 AND attempts <= max_attempts",
        name="ck_ci_lab_webhook_deliveries_attempts",
    ),
    CheckConstraint(
        "max_attempts BETWEEN 1 AND 20",
        name="ck_ci_lab_webhook_deliveries_max_attempts",
    ),
    CheckConstraint("version >= 0", name="ck_ci_lab_webhook_deliveries_version"),
    CheckConstraint(
        "(status = 'claimed' AND lease_owner IS NOT NULL "
        "AND lease_token_hash IS NOT NULL AND lease_expires_at IS NOT NULL) OR "
        "(status != 'claimed' AND lease_owner IS NULL "
        "AND lease_token_hash IS NULL AND lease_expires_at IS NULL)",
        name="ck_ci_lab_webhook_deliveries_lease_shape",
    ),
    CheckConstraint(
        "(status = 'delivered' AND delivered_at IS NOT NULL) OR "
        "(status != 'delivered' AND delivered_at IS NULL)",
        name="ck_ci_lab_webhook_deliveries_delivered_shape",
    ),
    CheckConstraint(
        "(status = 'dead_letter' AND dead_lettered_at IS NOT NULL) OR "
        "(status != 'dead_letter' AND dead_lettered_at IS NULL)",
        name="ck_ci_lab_webhook_deliveries_dead_letter_shape",
    ),
)
Index(
    "ix_ci_lab_webhook_deliveries_claim",
    webhook_deliveries.c.status,
    webhook_deliveries.c.available_at,
    webhook_deliveries.c.created_at,
    webhook_deliveries.c.id,
)
Index(
    "ix_ci_lab_webhook_deliveries_run",
    webhook_deliveries.c.run_id,
    webhook_deliveries.c.sequence,
)
Index(
    "ix_ci_lab_webhook_deliveries_lease",
    webhook_deliveries.c.lease_expires_at,
)


def require_local_filesystem_path(value: str | Path) -> Path:
    """Reject database URLs and Windows/POSIX UNC network-share forms."""

    raw = fspath(value)
    folded = raw.casefold()
    if (
        not raw
        or raw.startswith("\\\\")
        or raw.startswith("//")
        or folded.startswith("file:")
        or folded.startswith("sqlite:")
        or "://" in raw
    ):
        raise ValueError("CI Lab accepts only a local filesystem path")
    return Path(raw)


class CiLabDatabase:
    """Local SQLite database shared by one CI API and one delivery worker."""

    def __init__(self, path: str | Path) -> None:
        selected = require_local_filesystem_path(path).expanduser()
        if selected.name in {"", ".", ".."}:
            raise ValueError("CI Lab database path must name a local SQLite file")
        self.path = selected.resolve()
        # Resolve can expose a pre-existing junction/symlink target. Recheck
        # the normalized result before creating a file or parent directory.
        require_local_filesystem_path(self.path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.engine: AsyncEngine = create_async_engine(
            f"sqlite+aiosqlite:///{self.path.as_posix()}",
            echo=False,
        )
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False
        self._configure_sqlite()

    def _configure_sqlite(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_pragmas(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.close()

    async def initialize(self) -> None:
        if self._closed:
            raise RuntimeError("CI Lab database is closed")
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            # The API and delivery worker are separate processes, so the
            # in-process lock above cannot by itself fence concurrent first
            # startup. Acquire SQLite's writer lock before create_all performs
            # its existence checks; the second process then observes the
            # committed schema instead of racing into duplicate CREATE TABLE.
            async with self.engine.connect() as connection:
                await connection.exec_driver_sql("BEGIN IMMEDIATE")
                try:
                    await connection.run_sync(metadata.create_all)
                    await connection.commit()
                except BaseException:
                    await connection.rollback()
                    raise
            await self._enable_wal_mode()
            self._initialized = True

    async def _enable_wal_mode(self) -> None:
        """Enable the persistent journal mode without a connect-hook race."""

        for attempt in range(5):
            try:
                async with self.engine.connect() as connection:
                    selected = (
                        await connection.exec_driver_sql("PRAGMA journal_mode=WAL")
                    ).scalar_one()
                if str(selected).casefold() == "wal":
                    return
                if attempt == 4:
                    raise RuntimeError("CI Lab SQLite WAL mode could not be enabled")
            except OperationalError as error:
                # A concurrently starting API/worker may briefly hold the
                # schema writer lock. Retry only SQLite's lock condition; all
                # other database errors remain fail-closed and visible.
                message = str(error).casefold()
                is_lock_error = (
                    "database is locked" in message
                    or "database table is locked" in message
                    or "database schema is locked" in message
                )
                if not is_lock_error or attempt == 4:
                    raise
            await asyncio.sleep(0.05 * (attempt + 1))

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        await self.initialize()
        async with self.engine.connect() as connection:
            yield connection

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncConnection]:
        """Serialize writes with SQLite BEGIN IMMEDIATE.

        This makes the idempotency lookup plus insert one local transaction.
        SQLite serializes writers across the API and the single delivery
        worker. This is intentionally not a multi-instance/HA claim.
        """

        await self.initialize()
        async with self.engine.connect() as connection:
            await connection.exec_driver_sql("BEGIN IMMEDIATE")
            try:
                yield connection
                await connection.commit()
            except BaseException:
                await connection.rollback()
                raise

    async def close(self) -> None:
        if self._closed:
            return
        await self.engine.dispose()
        self._closed = True


__all__ = [
    "CiLabDatabase",
    "approvals",
    "metadata",
    "quality_gates",
    "require_local_filesystem_path",
    "runs",
    "webhook_deliveries",
    "webhook_subscriptions",
]
