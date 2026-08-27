"""Persistence adapters for pipeline runtime snapshots.

The pipeline simulator intentionally keeps its domain model independent from
the QA platform's ORM.  A run (including all stages and jobs) is stored as one
validated JSON snapshot, while trigger and callback idempotency records use
small relational tables so they survive process restarts as well.

All public operations are asynchronous.  The legacy lesson adapter performs
standard-library SQLite work in worker threads.  The application adapter uses
the shared async SQLAlchemy engine so the same transaction boundary works with
both local SQLite and the isolated PostgreSQL container.
"""

from __future__ import annotations

import asyncio
import sqlite3
from contextlib import closing
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Protocol

from sqlalchemy import text
from sqlalchemy.ext.asyncio import AsyncSession

from app.database.session import Database
from app.pipeline.models import PipelineRun


@dataclass(slots=True)
class PipelinePersistenceState:
    """Complete durable state required to restore a pipeline service."""

    runs: dict[str, PipelineRun] = field(default_factory=dict)
    trigger_keys: dict[str, tuple[str, str]] = field(default_factory=dict)
    callback_events: dict[str, dict[str, str]] = field(default_factory=dict)


class PipelinePersistence(Protocol):
    """Storage boundary used by the local pipeline service."""

    async def load(self) -> PipelinePersistenceState: ...

    async def save(self, state: PipelinePersistenceState) -> None: ...

    async def clear(self) -> None: ...


class SQLitePipelinePersistence:
    """Persist pipeline snapshots in one local SQLite file.

    A fresh connection is used for each operation.  This keeps lifecycle
    handling simple and allows the QA SQLAlchemy layer to use the same SQLite
    file without sharing a driver connection.
    """

    def __init__(
        self,
        database_path: str | Path,
        *,
        initialize_schema: bool = False,
    ) -> None:
        self.database_path = Path(database_path).expanduser().resolve()
        # False is the safe application default: Alembic owns the schema.
        # True exists only for isolated adapter lessons/tests using a fresh DB.
        self.initialize_schema = initialize_schema

    async def load(self) -> PipelinePersistenceState:
        return await asyncio.to_thread(self._load_sync)

    async def save(self, state: PipelinePersistenceState) -> None:
        await asyncio.to_thread(self._save_sync, state)

    async def clear(self) -> None:
        await asyncio.to_thread(self._clear_sync)

    def _connect(self) -> sqlite3.Connection:
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.database_path, timeout=5.0)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _prepare_database(self, connection: sqlite3.Connection) -> None:
        connection.executescript(
            """
            PRAGMA journal_mode = WAL;
            PRAGMA synchronous = NORMAL;
            """
        )
        if not self.initialize_schema:
            return
        connection.executescript(
            """

            CREATE TABLE IF NOT EXISTS pipeline_runtime_runs (
                id TEXT PRIMARY KEY,
                snapshot_json TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );

            CREATE TABLE IF NOT EXISTS pipeline_runtime_trigger_keys (
                idempotency_key TEXT PRIMARY KEY,
                run_id TEXT NOT NULL,
                request_fingerprint TEXT NOT NULL,
                FOREIGN KEY (run_id)
                    REFERENCES pipeline_runtime_runs(id)
                    ON DELETE CASCADE
            );

            CREATE TABLE IF NOT EXISTS pipeline_runtime_callback_events (
                run_id TEXT NOT NULL,
                event_id TEXT NOT NULL,
                event_fingerprint TEXT NOT NULL,
                PRIMARY KEY (run_id, event_id),
                FOREIGN KEY (run_id)
                    REFERENCES pipeline_runtime_runs(id)
                    ON DELETE CASCADE
            );

            CREATE INDEX IF NOT EXISTS
                ix_pipeline_runtime_trigger_keys_run_id
                ON pipeline_runtime_trigger_keys(run_id);

            CREATE INDEX IF NOT EXISTS
                ix_pipeline_runtime_callback_events_run_id
                ON pipeline_runtime_callback_events(run_id);
            """
        )

    def _load_sync(self) -> PipelinePersistenceState:
        with closing(self._connect()) as connection:
            self._prepare_database(connection)
            runs = {
                row["id"]: PipelineRun.model_validate_json(row["snapshot_json"])
                for row in connection.execute(
                    "SELECT id, snapshot_json FROM pipeline_runtime_runs"
                )
            }
            trigger_keys = {
                row["idempotency_key"]: (
                    row["run_id"],
                    row["request_fingerprint"],
                )
                for row in connection.execute(
                    """
                    SELECT idempotency_key, run_id, request_fingerprint
                    FROM pipeline_runtime_trigger_keys
                    """
                )
            }
            callback_events: dict[str, dict[str, str]] = {
                run_id: {} for run_id in runs
            }
            for row in connection.execute(
                """
                SELECT run_id, event_id, event_fingerprint
                FROM pipeline_runtime_callback_events
                """
            ):
                callback_events.setdefault(row["run_id"], {})[
                    row["event_id"]
                ] = row["event_fingerprint"]

        return PipelinePersistenceState(
            runs=runs,
            trigger_keys=trigger_keys,
            callback_events=callback_events,
        )

    def _save_sync(self, state: PipelinePersistenceState) -> None:
        checkpointed_at = datetime.now(timezone.utc).isoformat()
        with closing(self._connect()) as connection:
            self._prepare_database(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM pipeline_runtime_callback_events")
                connection.execute("DELETE FROM pipeline_runtime_trigger_keys")
                connection.execute("DELETE FROM pipeline_runtime_runs")
                connection.executemany(
                    """
                    INSERT INTO pipeline_runtime_runs (
                        id, snapshot_json, updated_at
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (run.id, run.model_dump_json(), checkpointed_at)
                        for run in state.runs.values()
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO pipeline_runtime_trigger_keys (
                        idempotency_key, run_id, request_fingerprint
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (key, run_id, fingerprint)
                        for key, (run_id, fingerprint) in state.trigger_keys.items()
                    ],
                )
                connection.executemany(
                    """
                    INSERT INTO pipeline_runtime_callback_events (
                        run_id, event_id, event_fingerprint
                    ) VALUES (?, ?, ?)
                    """,
                    [
                        (run_id, event_id, fingerprint)
                        for run_id, events in state.callback_events.items()
                        for event_id, fingerprint in events.items()
                    ],
                )
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()

    def _clear_sync(self) -> None:
        with closing(self._connect()) as connection:
            self._prepare_database(connection)
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute("DELETE FROM pipeline_runtime_callback_events")
                connection.execute("DELETE FROM pipeline_runtime_trigger_keys")
                connection.execute("DELETE FROM pipeline_runtime_runs")
            except Exception:
                connection.rollback()
                raise
            else:
                connection.commit()


class SQLAlchemyPipelinePersistence:
    """Persist snapshots through the application's validated async database.

    Alembic remains the only schema owner.  Every replacement checkpoint runs
    in one database transaction, so a failed child insert cannot leave the
    three snapshot tables partially cleared.
    """

    def __init__(self, database: Database) -> None:
        self.database = database

    async def load(self) -> PipelinePersistenceState:
        await self.database.initialize()
        async with self.database.session_factory() as session:
            run_rows = (
                await session.execute(
                    text("SELECT id, snapshot_json FROM pipeline_runtime_runs")
                )
            ).mappings()
            runs = {
                row["id"]: PipelineRun.model_validate_json(row["snapshot_json"])
                for row in run_rows
            }

            trigger_rows = (
                await session.execute(
                    text(
                        "SELECT idempotency_key, run_id, request_fingerprint "
                        "FROM pipeline_runtime_trigger_keys"
                    )
                )
            ).mappings()
            trigger_keys = {
                row["idempotency_key"]: (
                    row["run_id"],
                    row["request_fingerprint"],
                )
                for row in trigger_rows
            }

            callback_events: dict[str, dict[str, str]] = {
                run_id: {} for run_id in runs
            }
            callback_rows = (
                await session.execute(
                    text(
                        "SELECT run_id, event_id, event_fingerprint "
                        "FROM pipeline_runtime_callback_events"
                    )
                )
            ).mappings()
            for row in callback_rows:
                callback_events.setdefault(row["run_id"], {})[
                    row["event_id"]
                ] = row["event_fingerprint"]

        return PipelinePersistenceState(
            runs=runs,
            trigger_keys=trigger_keys,
            callback_events=callback_events,
        )

    async def save(self, state: PipelinePersistenceState) -> None:
        await self.database.initialize()
        checkpointed_at = datetime.now(timezone.utc).isoformat()
        async with self.database.session_factory.begin() as session:
            await self._delete_all(session)

            run_rows = [
                {
                    "id": run.id,
                    "snapshot_json": run.model_dump_json(),
                    "updated_at": checkpointed_at,
                }
                for run in state.runs.values()
            ]
            if run_rows:
                await session.execute(
                    text(
                        "INSERT INTO pipeline_runtime_runs "
                        "(id, snapshot_json, updated_at) "
                        "VALUES (:id, :snapshot_json, :updated_at)"
                    ),
                    run_rows,
                )

            trigger_rows = [
                {
                    "idempotency_key": key,
                    "run_id": run_id,
                    "request_fingerprint": fingerprint,
                }
                for key, (run_id, fingerprint) in state.trigger_keys.items()
            ]
            if trigger_rows:
                await session.execute(
                    text(
                        "INSERT INTO pipeline_runtime_trigger_keys "
                        "(idempotency_key, run_id, request_fingerprint) "
                        "VALUES (:idempotency_key, :run_id, :request_fingerprint)"
                    ),
                    trigger_rows,
                )

            callback_rows = [
                {
                    "run_id": run_id,
                    "event_id": event_id,
                    "event_fingerprint": fingerprint,
                }
                for run_id, events in state.callback_events.items()
                for event_id, fingerprint in events.items()
            ]
            if callback_rows:
                await session.execute(
                    text(
                        "INSERT INTO pipeline_runtime_callback_events "
                        "(run_id, event_id, event_fingerprint) "
                        "VALUES (:run_id, :event_id, :event_fingerprint)"
                    ),
                    callback_rows,
                )

    async def clear(self) -> None:
        await self.database.initialize()
        async with self.database.session_factory.begin() as session:
            await self._delete_all(session)

    @staticmethod
    async def _delete_all(session: AsyncSession) -> None:
        # Delete children first on every supported dialect.  We deliberately
        # avoid TRUNCATE because it has different transaction/lock semantics.
        await session.execute(text("DELETE FROM pipeline_runtime_callback_events"))
        await session.execute(text("DELETE FROM pipeline_runtime_trigger_keys"))
        await session.execute(text("DELETE FROM pipeline_runtime_runs"))


__all__ = [
    "PipelinePersistence",
    "PipelinePersistenceState",
    "SQLAlchemyPipelinePersistence",
    "SQLitePipelinePersistence",
]
