from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config


BACKEND_ROOT = Path(__file__).resolve().parents[1]


def migrated_database(database_path: Path) -> sqlite3.Connection:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes.update(
        {
            "database_url": (
                f"sqlite+aiosqlite:///{database_path.as_posix()}"
            ),
            "configure_logger": False,
        }
    )
    command.upgrade(config, "head")
    connection = sqlite3.connect(database_path)
    connection.execute("PRAGMA foreign_keys = ON")
    return connection


def project_values(key: str) -> tuple[str, str, str, str, str, str, str]:
    return (
        f"00000000-0000-0000-0000-{key.lower():0>12}",
        key,
        f"{key} project",
        "",
        "active",
        "2026-08-27T00:00:00+00:00",
        "2026-08-27T00:00:00+00:00",
    )


PROJECT_INSERT = """
    INSERT INTO projects (
        id, key, name, description, status, created_at, updated_at
    ) VALUES (?, ?, ?, ?, ?, ?, ?)
"""


def test_migration_schema_enforces_transactions_and_constraints(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "constraints.db"
    connection = migrated_database(database_path)
    try:
        connection.execute("BEGIN")
        connection.execute(PROJECT_INSERT, project_values("ROLLBACK"))
        connection.rollback()
        assert connection.execute(
            "SELECT COUNT(*) FROM projects WHERE key = 'ROLLBACK'"
        ).fetchone() == (0,)

        connection.execute(PROJECT_INSERT, project_values("UNIQUE"))
        connection.commit()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(PROJECT_INSERT, project_values("UNIQUE"))
        connection.rollback()

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO pipeline_runtime_trigger_keys (
                    idempotency_key, run_id, request_fingerprint
                ) VALUES ('orphan-key', 'missing-run', 'fingerprint')
                """
            )
        connection.rollback()

        connection.execute(
            """
            INSERT INTO pipeline_runtime_runs (id, snapshot_json, updated_at)
            VALUES ('run-1', '{}', '2026-08-27T00:00:00+00:00')
            """
        )
        connection.execute(
            """
            INSERT INTO pipeline_runtime_trigger_keys (
                idempotency_key, run_id, request_fingerprint
            ) VALUES ('key-1', 'run-1', 'fingerprint')
            """
        )
        connection.execute(
            "DELETE FROM pipeline_runtime_runs WHERE id = 'run-1'"
        )
        assert connection.execute(
            "SELECT COUNT(*) FROM pipeline_runtime_trigger_keys"
        ).fetchone() == (0,)

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                INSERT INTO provider_connections (
                    id, name, kind, base_url, definition_ref, config,
                    secret_env_var, enabled, version, created_at, updated_at
                ) VALUES (
                    'invalid-provider', 'invalid', 'remote', NULL,
                    'local-only', '{}', NULL, 1, 0,
                    '2026-08-27T00:00:00+00:00',
                    '2026-08-27T00:00:00+00:00'
                )
                """
            )
        connection.rollback()

        role_count = connection.execute("SELECT COUNT(*) FROM roles").fetchone()
        permission_count = connection.execute(
            "SELECT COUNT(*) FROM role_permissions"
        ).fetchone()
        assert role_count is not None and role_count[0] >= 5
        assert permission_count is not None and permission_count[0] > 0
    finally:
        connection.close()
