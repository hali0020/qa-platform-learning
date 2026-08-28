from __future__ import annotations

from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from sqlalchemy.dialects import postgresql
from sqlalchemy.schema import CreateTable

from app.database import models as _database_models  # noqa: F401
from app.database.base import Base
from app.runtime import orm as _runtime_models  # noqa: F401


BACKEND_ROOT = Path(__file__).resolve().parents[1]
POSTGRES_URL = (
    "postgresql+asyncpg://qa_learning:offline_only@postgres:5432/"
    "qa_platform_learning"
)


def postgres_alembic_config(
    database_url: str = POSTGRES_URL,
    *,
    runtime_mode: str = "postgres_local_container",
    app_env: str = "local-container",
) -> tuple[Config, StringIO]:
    output = StringIO()
    config = Config(str(BACKEND_ROOT / "alembic.ini"), output_buffer=output)
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes.update(
        {
            "database_url": database_url,
            "runtime_mode": runtime_mode,
            "app_env": app_env,
            "configure_logger": False,
        }
    )
    return config, output


def test_postgres_offline_upgrade_emits_portable_transactional_ddl() -> None:
    config, output = postgres_alembic_config()

    # sql=True 只调用 PostgreSQL 方言编译器，不解析 DNS、不打开套接字。
    command.upgrade(config, "head", sql=True)
    sql = output.getvalue()

    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "CREATE TABLE projects" in sql
    assert "CREATE TABLE test_case_snapshots" in sql
    assert "CREATE TABLE users" in sql
    assert "CREATE TABLE automation_tasks" in sql
    assert "CREATE TABLE pipeline_runtime_runs" in sql
    assert "CREATE TABLE provider_run_approvals" in sql
    assert "CREATE TABLE provider_run_artifacts" in sql
    assert "CREATE TABLE provider_webhook_events" in sql
    assert "CREATE TABLE provider_trigger_intents" in sql
    assert "CREATE TABLE automation_task_wakeup_outbox" in sql
    assert "TIMESTAMP WITH TIME ZONE" in sql
    assert "JSON" in sql
    assert "ON DELETE CASCADE" in sql
    assert "CONSTRAINT ck_automation_tasks_status CHECK" in sql
    assert (
        "CREATE UNIQUE INDEX uq_device_leases_one_active_per_device "
        "ON device_leases (device_id)"
    ) in sql
    assert "WHERE status = 'active'" in sql
    assert "ADD COLUMN storage_backend VARCHAR(50)" in sql
    assert "ADD COLUMN storage_namespace VARCHAR(200)" in sql
    assert "CONSTRAINT ck_attachments_storage_backend_allowed CHECK" in sql
    assert "CONSTRAINT ck_attachments_storage_route CHECK" in sql
    assert "ADD COLUMN webhook_secret_env_var VARCHAR(128)" in sql
    assert "ADD COLUMN dispatch_status VARCHAR(20)" in sql
    assert "ADD COLUMN quality_gate_status VARCHAR(30)" in sql
    assert "CONSTRAINT ck_provider_runs_dispatch_status CHECK" in sql
    assert "CONSTRAINT ck_provider_runs_quality_gate_status CHECK" in sql
    assert "CONSTRAINT ck_provider_trigger_intents_status CHECK" in sql
    assert "CONSTRAINT ck_task_wakeup_outbox_lease_shape CHECK" in sql
    assert "CONSTRAINT ck_task_wakeup_outbox_published_shape CHECK" in sql
    assert "ADD COLUMN claim_owner VARCHAR(200)" in sql
    assert "ADD COLUMN claim_token_hash VARCHAR(64)" in sql
    assert "ADD COLUMN claim_expires_at TIMESTAMP WITH TIME ZONE" in sql
    assert "CONSTRAINT uq_provider_webhook_events_event UNIQUE" in sql
    assert "pipeline.approve" in sql
    assert "'s3_local_container'" in sql
    assert "'qa-artifacts'" in sql
    assert "PRAGMA" not in sql
    assert "AUTOINCREMENT" not in sql


def test_postgres_offline_downgrade_compiles_without_sqlite_operations() -> None:
    config, output = postgres_alembic_config()

    command.downgrade(config, "head:base", sql=True)
    sql = output.getvalue()

    assert "BEGIN;" in sql
    assert "COMMIT;" in sql
    assert "DROP TABLE automation_tasks" in sql
    assert "DROP TABLE provider_trigger_intents" in sql
    assert "DROP TABLE automation_task_wakeup_outbox" in sql
    assert "DROP TABLE provider_webhook_events" in sql
    assert "DROP TABLE provider_run_artifacts" in sql
    assert "DROP TABLE provider_run_approvals" in sql
    assert "DROP TABLE users" in sql
    assert "DROP TABLE projects" in sql
    assert "ALTER TABLE audit_events DROP COLUMN actor_user_id" in sql
    assert "DROP CONSTRAINT ck_attachments_storage_route" in sql
    assert "IF EXISTS" in sql
    assert "cannot downgrade attachment storage routing" in sql
    assert "ALTER TABLE attachments DROP COLUMN storage_namespace" in sql
    assert "ALTER TABLE attachments DROP COLUMN storage_backend" in sql
    assert "ALTER TABLE provider_connections DROP COLUMN webhook_secret_env_var" in sql
    assert (
        "UPDATE provider_runs SET external_id = "
        "'local-downgrade-pending-' || id WHERE external_id IS NULL" in sql
    )
    assert "PRAGMA" not in sql


@pytest.mark.parametrize(
    ("database_url", "runtime_mode", "app_env"),
    [
        (
            "postgresql+asyncpg://qa_learning:offline_only@127.0.0.1:5432/qa",
            "postgres_local_container",
            "local-container",
        ),
        (
            "postgresql+asyncpg://qa_learning:offline_only@localhost:5432/qa",
            "postgres_local_container",
            "local-container",
        ),
        (
            "postgresql+asyncpg://qa_learning:offline_only@postgres:5433/qa",
            "postgres_local_container",
            "local-container",
        ),
        (
            "postgresql+asyncpg://qa_learning:offline_only@postgres/qa",
            "postgres_local_container",
            "local-container",
        ),
        (
            "postgresql+psycopg://qa_learning:offline_only@postgres:5432/qa",
            "postgres_local_container",
            "local-container",
        ),
        (
            "postgresql+asyncpg://qa_learning:offline_only@postgres:5432/"
            "qa?host=outside",
            "postgres_local_container",
            "local-container",
        ),
        (
            "postgresql+asyncpg://qa_learning@postgres:5432/qa",
            "postgres_local_container",
            "local-container",
        ),
        (POSTGRES_URL, "sqlite_local", "local-container"),
        (POSTGRES_URL, "postgres_local_container", "local"),
    ],
)
def test_postgres_migration_rejects_every_non_container_boundary(
    database_url: str,
    runtime_mode: str,
    app_env: str,
) -> None:
    config, _ = postgres_alembic_config(
        database_url,
        runtime_mode=runtime_mode,
        app_env=app_env,
    )

    with pytest.raises(RuntimeError):
        command.upgrade(config, "head", sql=True)


def test_all_registered_orm_tables_compile_with_postgres_dialect() -> None:
    dialect = postgresql.dialect()

    compiled_tables = {
        table.name: str(CreateTable(table).compile(dialect=dialect))
        for table in Base.metadata.sorted_tables
    }

    assert "projects" in compiled_tables
    assert "provider_connections" in compiled_tables
    assert "provider_run_approvals" in compiled_tables
    assert "provider_run_artifacts" in compiled_tables
    assert "provider_webhook_events" in compiled_tables
    assert "provider_trigger_intents" in compiled_tables
    assert "automation_task_wakeup_outbox" in compiled_tables
    assert "automation_tasks" in compiled_tables
    assert all("CREATE TABLE" in sql for sql in compiled_tables.values())
    assert all("DATETIME" not in sql for sql in compiled_tables.values())
