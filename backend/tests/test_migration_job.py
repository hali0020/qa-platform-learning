from __future__ import annotations

import asyncio
import sqlite3
from pathlib import Path

import pytest

from app.core.config import Settings
from app.database.session import Database
from app.migrations.main import main, run_migration_job
from app.migrations.runner import build_alembic_config, expected_schema_heads


def migration_settings(database_path: Path) -> Settings:
    return Settings(
        app_env="local-container",
        debug=False,
        database_runtime_mode="sqlite_local",
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        local_data_root=str(database_path.parent),
    )


def test_migration_job_upgrades_local_compose_database_to_repository_head(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "compose-migration.db"
    settings = migration_settings(database_path)

    migrated_heads = run_migration_job(settings)
    expected_heads = expected_schema_heads(
        build_alembic_config(
            database_url=settings.database_url,
            runtime_mode=settings.database_runtime_mode,
            app_env=settings.app_env,
            configure_logger=False,
        )
    )

    with sqlite3.connect(database_path) as connection:
        current_heads = tuple(
            sorted(
                row[0]
                for row in connection.execute(
                    "SELECT version_num FROM alembic_version"
                )
            )
        )
    assert migrated_heads == expected_heads
    assert current_heads == expected_heads


def test_migration_job_refuses_non_container_and_ephemeral_database(
    tmp_path: Path,
) -> None:
    source_settings = Settings(
        app_env="local",
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'source.db').as_posix()}"
        ),
    )
    with pytest.raises(RuntimeError, match="local-container"):
        run_migration_job(source_settings)

    with pytest.raises(RuntimeError, match="内存 SQLite"):
        run_migration_job(
            Settings(
                app_env="local-container",
                database_runtime_mode="sqlite_local",
                database_url="sqlite+aiosqlite:///:memory:",
            )
        )


@pytest.mark.asyncio
async def test_verify_only_process_requires_successful_migration_job(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "verify-only.db"
    settings = migration_settings(database_path)
    database = Database(
        settings.database_url,
        runtime_mode=settings.database_runtime_mode,
        app_env=settings.app_env,
        schema_mode="verify",
    )
    try:
        with pytest.raises(RuntimeError, match="migration Job"):
            await database.initialize()
    finally:
        await database.shutdown()

    await asyncio.to_thread(run_migration_job, settings)
    restarted = Database(
        settings.database_url,
        runtime_mode=settings.database_runtime_mode,
        app_env=settings.app_env,
        schema_mode="verify",
    )
    try:
        await restarted.initialize()
    finally:
        await restarted.shutdown()


def test_database_rejects_unknown_schema_management_mode(tmp_path: Path) -> None:
    with pytest.raises(RuntimeError, match="upgrade 或 verify"):
        Database(
            f"sqlite+aiosqlite:///{(tmp_path / 'unknown.db').as_posix()}",
            schema_mode="automatic",
        )


def test_migration_entrypoint_redacts_exception_details(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    def fail() -> tuple[str, ...]:
        raise RuntimeError("sensitive-detail-private-123")

    monkeypatch.setattr("app.migrations.main.run_migration_job", fail)
    with pytest.raises(SystemExit) as error:
        main()

    assert error.value.code == 1
    assert "RuntimeError" in caplog.text
    assert "sensitive-detail-private-123" not in caplog.text
