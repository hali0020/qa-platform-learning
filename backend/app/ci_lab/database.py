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
    Index,
    Integer,
    MetaData,
    String,
    Table,
    event,
)
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
    """Single-process SQLite database owned exclusively by CI Lab."""

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
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    async def initialize(self) -> None:
        if self._closed:
            raise RuntimeError("CI Lab database is closed")
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            async with self.engine.begin() as connection:
                await connection.run_sync(metadata.create_all)
            self._initialized = True

    @asynccontextmanager
    async def read(self) -> AsyncIterator[AsyncConnection]:
        await self.initialize()
        async with self.engine.connect() as connection:
            yield connection

    @asynccontextmanager
    async def write(self) -> AsyncIterator[AsyncConnection]:
        """Serialize writes with SQLite BEGIN IMMEDIATE.

        This makes the idempotency lookup plus insert one local transaction.
        CI Lab remains explicitly single-process until its later PostgreSQL
        lesson; the API never claims multi-instance safety here.
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
    "metadata",
    "require_local_filesystem_path",
    "runs",
]
