from __future__ import annotations

import sqlite3
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy import text

from app.database.session import Database


BACKEND_ROOT = Path(__file__).resolve().parents[1]
QA_TABLES = {
    "projects",
    "test_cases",
    "test_plans",
    "test_plan_cases",
    "test_executions",
    "case_execution_results",
    "test_suites",
    "test_case_snapshots",
    "test_case_snapshot_items",
    "defects",
    "audit_events",
    "roles",
    "permissions",
    "role_permissions",
    "users",
    "auth_sessions",
    "oidc_identities",
    "oidc_login_transactions",
    "comments",
    "attachments",
}
PIPELINE_TABLES = {
    "pipeline_runtime_runs",
    "pipeline_runtime_trigger_keys",
    "pipeline_runtime_callback_events",
}
RUNTIME_TABLES = {
    "provider_connections",
    "provider_runs",
    "automation_tasks",
    "devices",
    "device_leases",
    "schedules",
    "schedule_fires",
}
EXPECTED_TABLES = QA_TABLES | PIPELINE_TABLES | RUNTIME_TABLES


def make_alembic_config(database_path: Path) -> Config:
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = (
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    return config


def read_table_names(database_path: Path) -> set[str]:
    with sqlite3.connect(database_path) as connection:
        rows = connection.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
        ).fetchall()
    return {row[0] for row in rows}


def test_initial_migration_can_upgrade_downgrade_and_upgrade_again(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "migration.db"
    config = make_alembic_config(database_path)

    command.upgrade(config, "head")
    assert EXPECTED_TABLES <= read_table_names(database_path)
    command.check(config)

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    assert revision == ("20260827_0009",)

    command.downgrade(config, "base")
    assert EXPECTED_TABLES.isdisjoint(read_table_names(database_path))

    command.upgrade(config, "head")
    assert EXPECTED_TABLES <= read_table_names(database_path)


def test_two_migrated_databases_are_isolated(tmp_path: Path) -> None:
    first_path = tmp_path / "first.db"
    second_path = tmp_path / "second.db"
    command.upgrade(make_alembic_config(first_path), "head")
    command.upgrade(make_alembic_config(second_path), "head")

    with sqlite3.connect(first_path) as connection:
        connection.execute(
            """
            INSERT INTO projects (
                id, key, name, description, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000001",
                "FIRST",
                "first database",
                "",
                "active",
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
            ),
        )
        connection.commit()

    with sqlite3.connect(first_path) as first_connection:
        first_count = first_connection.execute(
            "SELECT COUNT(*) FROM projects"
        ).fetchone()[0]
    with sqlite3.connect(second_path) as second_connection:
        second_count = second_connection.execute(
            "SELECT COUNT(*) FROM projects"
        ).fetchone()[0]

    assert first_count == 1
    assert second_count == 0


def test_attachment_storage_migration_backfills_and_constrains_legacy_rows(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "attachment-storage-upgrade.db"
    config = make_alembic_config(database_path)
    command.upgrade(config, "20260827_0006")

    project_id = "00000000-0000-0000-0000-000000000021"
    user_id = "00000000-0000-0000-0000-000000000022"
    attachment_id = "00000000-0000-0000-0000-000000000023"
    timestamp = "2026-08-27T00:00:00+00:00"
    with sqlite3.connect(database_path) as connection:
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute(
            """
            INSERT INTO projects (
                id, key, name, description, status, created_at, updated_at
            ) VALUES (?, 'ATTACH', 'Attachment project', '', 'active', ?, ?)
            """,
            (project_id, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO users (
                id, username, username_normalized, display_name,
                password_hash, role_key, status, failed_login_count,
                locked_until, last_login_at, password_changed_at,
                created_at, updated_at
            ) VALUES (
                ?, 'legacy', 'legacy', 'Legacy User', 'not-a-real-secret',
                'system_admin', 'active', 0, NULL, NULL, ?, ?, ?
            )
            """,
            (user_id, timestamp, timestamp, timestamp),
        )
        connection.execute(
            """
            INSERT INTO attachments (
                id, project_id, entity_type, entity_id, comment_id,
                uploader_id, uploader_name, original_filename, storage_key,
                media_type, size_bytes, sha256, is_image, created_at,
                deleted_at, deleted_by_id
            ) VALUES (
                ?, ?, 'project', ?, NULL, ?, 'Legacy User', 'legacy.log',
                'legacy-storage-key', 'text/plain', 6, ?, 0, ?, NULL, NULL
            )
            """,
            (attachment_id, project_id, project_id, user_id, "a" * 64, timestamp),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        backfilled = connection.execute(
            """
            SELECT storage_backend, storage_namespace
            FROM attachments WHERE id = ?
            """,
            (attachment_id,),
        ).fetchone()
        assert backfilled == ("local_filesystem", "")

        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE attachments SET storage_backend = '' WHERE id = ?",
                (attachment_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE attachments SET storage_backend = ? WHERE id = ?",
                ("b" * 51, attachment_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE attachments SET storage_backend = 'other' WHERE id = ?",
                (attachment_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                "UPDATE attachments SET storage_namespace = ? WHERE id = ?",
                ("n" * 201, attachment_id),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE attachments
                SET storage_backend = 's3_local_container', storage_namespace = ''
                WHERE id = ?
                """,
                (attachment_id,),
            )
        connection.rollback()
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(
                """
                UPDATE attachments
                SET storage_backend = 'local_filesystem',
                    storage_namespace = 'qa-artifacts'
                WHERE id = ?
                """,
                (attachment_id,),
            )
        connection.rollback()
        connection.execute(
            """
            UPDATE attachments
            SET storage_backend = 's3_local_container',
                storage_namespace = 'qa-artifacts'
            WHERE id = ?
            """,
            (attachment_id,),
        )
        assert connection.execute(
            """
            SELECT storage_backend, storage_namespace
            FROM attachments WHERE id = ?
            """,
            (attachment_id,),
        ).fetchone() == ("s3_local_container", "qa-artifacts")
        connection.commit()

        with pytest.raises(
            RuntimeError,
            match="cannot downgrade attachment storage routing",
        ):
            command.downgrade(config, "20260827_0006")
        assert connection.execute(
            """
            SELECT storage_backend, storage_namespace
            FROM attachments WHERE id = ?
            """,
            (attachment_id,),
        ).fetchone() == ("s3_local_container", "qa-artifacts")
        assert connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone() == ("20260827_0007",)

        connection.execute(
            """
            UPDATE attachments
            SET storage_backend = 'local_filesystem', storage_namespace = ''
            WHERE id = ?
            """,
            (attachment_id,),
        )

        connection.execute(
            """
            INSERT INTO attachments (
                id, project_id, entity_type, entity_id, comment_id,
                uploader_id, uploader_name, original_filename, storage_key,
                media_type, size_bytes, sha256, is_image, created_at,
                deleted_at, deleted_by_id
            ) VALUES (
                '00000000-0000-0000-0000-000000000024', ?, 'project', ?,
                NULL, ?, 'Legacy User', 'new.log', 'new-storage-key',
                'text/plain', 3, ?, 0, ?, NULL, NULL
            )
            """,
            (project_id, project_id, user_id, "b" * 64, timestamp),
        )
        defaulted = connection.execute(
            """
            SELECT storage_backend, storage_namespace
            FROM attachments WHERE storage_key = 'new-storage-key'
            """
        ).fetchone()
        assert defaulted == ("local_filesystem", "")
        connection.commit()

    command.downgrade(config, "20260827_0006")
    with sqlite3.connect(database_path) as connection:
        legacy_columns = {
            row[1] for row in connection.execute("PRAGMA table_info(attachments)")
        }
        assert "storage_backend" not in legacy_columns
        assert "storage_namespace" not in legacy_columns

    command.upgrade(config, "head")
    with sqlite3.connect(database_path) as connection:
        rebackfilled = connection.execute(
            """
            SELECT storage_backend, storage_namespace
            FROM attachments WHERE id = ?
            """,
            (attachment_id,),
        ).fetchone()
    assert rebackfilled == ("local_filesystem", "")


@pytest.mark.asyncio
async def test_database_initialize_applies_alembic_revision(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "automatic-migration.db"
    database = Database(
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )

    await database.initialize()
    await database.shutdown()

    with sqlite3.connect(database_path) as connection:
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()
    assert revision == ("20260827_0009",)


def test_existing_initial_database_upgrades_without_losing_qa_data(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "upgrade-from-initial.db"
    config = make_alembic_config(database_path)
    command.upgrade(config, "20260827_0001")

    with sqlite3.connect(database_path) as connection:
        connection.execute(
            """
            INSERT INTO projects (
                id, key, name, description, status, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000010",
                "LEGACY",
                "legacy project",
                "",
                "active",
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
            ),
        )
        connection.execute(
            """
            INSERT INTO test_cases (
                id, project_id, title, preconditions, steps, priority,
                case_type, status, tags, created_at, updated_at
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                "00000000-0000-0000-0000-000000000011",
                "00000000-0000-0000-0000-000000000010",
                "legacy case",
                "",
                "[]",
                "P2",
                "manual",
                "draft",
                "[]",
                "2026-08-27T00:00:00+00:00",
                "2026-08-27T00:00:00+00:00",
            ),
        )
        connection.commit()

    command.upgrade(config, "head")

    with sqlite3.connect(database_path) as connection:
        project = connection.execute(
            "SELECT key, name FROM projects WHERE key = 'LEGACY'"
        ).fetchone()
        test_case = connection.execute(
            "SELECT title, suite_id FROM test_cases WHERE title = 'legacy case'"
        ).fetchone()
        revision = connection.execute(
            "SELECT version_num FROM alembic_version"
        ).fetchone()

    assert project == ("LEGACY", "legacy project")
    assert test_case == ("legacy case", None)
    assert revision == ("20260827_0009",)


@pytest.mark.asyncio
async def test_memory_database_uses_its_own_connection_schema() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")

    try:
        await database.initialize()
        async with database.session() as session:
            project_table = await session.scalar(
                text(
                    "SELECT name FROM sqlite_master "
                    "WHERE type = 'table' AND name = 'projects'"
                )
            )
        assert project_table == "projects"
    finally:
        await database.shutdown()
