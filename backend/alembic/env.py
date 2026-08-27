from __future__ import annotations

import asyncio
import os
from logging.config import fileConfig
from pathlib import Path

from alembic import context
from sqlalchemy import pool
from sqlalchemy.engine import Connection
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import async_engine_from_config

from app.core.config import validate_database_runtime_target
from app.database import models as _models  # noqa: F401
from app.database.base import Base
from app.runtime import orm as _runtime_models  # noqa: F401

config = context.config

if (
    config.config_file_name is not None
    and config.attributes.get("configure_logger", True)
):
    fileConfig(config.config_file_name)

database_url = (
    config.attributes.get("database_url")
    or os.getenv("DATABASE_URL")
    or config.get_main_option("sqlalchemy.url")
)
runtime_mode = str(
    config.attributes.get("runtime_mode")
    or os.getenv("DATABASE_RUNTIME_MODE", "sqlite_local")
).strip()
app_env = str(
    config.attributes.get("app_env")
    or os.getenv("APP_ENV", "local")
).strip()


try:
    validate_database_runtime_target(
        database_url=database_url,
        runtime_mode=runtime_mode,
        app_env=app_env,
    )
except RuntimeError as exc:
    raise RuntimeError(f"Alembic 数据库安全边界校验失败: {exc}") from exc

parsed_database_url = make_url(database_url)
is_sqlite_migration = parsed_database_url.get_backend_name() == "sqlite"
is_postgres_migration = parsed_database_url.get_backend_name() == "postgresql"
if is_postgres_migration and parsed_database_url.port != 5432:
    raise RuntimeError("Alembic PostgreSQL URL 必须显式使用 postgres:5432")
config.set_main_option("sqlalchemy.url", database_url)

database_file = parsed_database_url.database if is_sqlite_migration else None
if database_file and database_file != ":memory:":
    Path(database_file).expanduser().parent.mkdir(parents=True, exist_ok=True)

target_metadata = Base.metadata
PIPELINE_RUNTIME_TABLES = {
    "pipeline_runtime_runs",
    "pipeline_runtime_trigger_keys",
    "pipeline_runtime_callback_events",
}


def include_object(
    _object: object,
    name: str | None,
    type_: str,
    _reflected: bool,
    _compare_to: object | None,
) -> bool:
    """流水线表由手写 Alembic 迁移维护，不属于 ORM metadata。"""

    return not (type_ == "table" and name in PIPELINE_RUNTIME_TABLES)


def run_migrations_offline() -> None:
    context.configure(
        url=database_url,
        target_metadata=target_metadata,
        literal_binds=True,
        dialect_opts={"paramstyle": "named"},
        compare_type=True,
        render_as_batch=is_sqlite_migration,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


def do_run_migrations(connection: Connection) -> None:
    context.configure(
        connection=connection,
        target_metadata=target_metadata,
        compare_type=True,
        render_as_batch=is_sqlite_migration,
        include_object=include_object,
    )

    with context.begin_transaction():
        context.run_migrations()


async def run_async_migrations() -> None:
    connectable = async_engine_from_config(
        config.get_section(config.config_ini_section, {}),
        prefix="sqlalchemy.",
        poolclass=pool.NullPool,
    )

    try:
        async with connectable.connect() as connection:
            await connection.run_sync(do_run_migrations)
    finally:
        # Migration errors must not leave a Windows SQLite file handle open.
        await connectable.dispose()


def run_migrations_online() -> None:
    asyncio.run(run_async_migrations())


if context.is_offline_mode():
    run_migrations_offline()
else:
    run_migrations_online()
