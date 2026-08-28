from __future__ import annotations

import asyncio
import os
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic.migration import MigrationContext
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import validate_database_runtime_target
from app.migrations import (
    SCHEMA_MODE_UPGRADE,
    SCHEMA_MODE_VERIFY,
    build_alembic_config,
    expected_schema_heads,
    upgrade_schema,
)


class Database:
    """异步 SQLAlchemy 引擎与会话生命周期的单一入口。"""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        runtime_mode: str = "sqlite_local",
        app_env: str = "local",
        schema_mode: str | None = None,
    ) -> None:
        # This check is deliberately repeated below Settings so direct callers
        # (CLI, tests, future workers) cannot bypass the database boundary.
        validate_database_runtime_target(
            database_url=url,
            runtime_mode=runtime_mode,
            app_env=app_env,
        )
        self.url = url
        self.runtime_mode = runtime_mode
        self.app_env = app_env
        selected_schema_mode = (
            schema_mode
            if schema_mode is not None
            else os.environ.get("DATABASE_SCHEMA_MODE", SCHEMA_MODE_UPGRADE)
        )
        self.schema_mode = selected_schema_mode.strip().lower()
        if self.schema_mode not in {SCHEMA_MODE_UPGRADE, SCHEMA_MODE_VERIFY}:
            raise RuntimeError(
                "DATABASE_SCHEMA_MODE 只能是 upgrade 或 verify"
            )
        parsed_url = make_url(url)
        self._backend_name = parsed_url.get_backend_name()
        self._is_memory = (
            self._backend_name == "sqlite"
            and parsed_url.database == ":memory:"
        )
        self._ensure_sqlite_directory(url)
        engine_options: dict[str, object] = {"echo": echo}
        if self._backend_name == "postgresql":
            # Detect dead local-container connections before handing a pooled
            # connection to a request. This does not change the target host.
            engine_options["pool_pre_ping"] = True
        self.engine: AsyncEngine = create_async_engine(url, **engine_options)
        self.session_factory = async_sessionmaker(
            self.engine,
            class_=AsyncSession,
            expire_on_commit=False,
        )
        self._initialize_lock = asyncio.Lock()
        self._initialized = False
        self._closed = False
        if self._backend_name == "sqlite":
            self._enable_sqlite_foreign_keys()

    @staticmethod
    def _ensure_sqlite_directory(url: str) -> None:
        parsed = make_url(url)
        if parsed.get_backend_name() != "sqlite":
            return
        database = parsed.database
        if not database or database == ":memory:":
            return
        Path(database).expanduser().parent.mkdir(parents=True, exist_ok=True)

    def _enable_sqlite_foreign_keys(self) -> None:
        @event.listens_for(self.engine.sync_engine, "connect")
        def set_sqlite_pragma(dbapi_connection, _connection_record) -> None:
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.execute("PRAGMA busy_timeout=5000")
            cursor.execute("PRAGMA journal_mode=WAL")
            cursor.close()

    async def initialize(self) -> None:
        if self._closed:
            raise RuntimeError("数据库已经关闭")
        if self._initialized:
            return
        async with self._initialize_lock:
            if self._initialized:
                return
            if self._is_memory:
                # 内存 SQLite 仅用于隔离测试；Alembic 会另建连接并丢失
                # schema，因此必须在应用自己的连接上创建 metadata。
                from app.database import models as _models  # noqa: F401
                from app.runtime import orm as _runtime_models  # noqa: F401
                from app.database.base import Base

                async with self.engine.begin() as connection:
                    await connection.run_sync(Base.metadata.create_all)
            else:
                if self.schema_mode == SCHEMA_MODE_UPGRADE:
                    # Source mode keeps its explicit/backwards-compatible
                    # migration path. Compose processes set verify and wait
                    # for the one-shot migration Job instead.
                    await asyncio.to_thread(self._upgrade_schema)
                else:
                    await self._verify_schema()
            self._initialized = True

    def _upgrade_schema(self) -> None:
        upgrade_schema(
            database_url=self.url,
            runtime_mode=self.runtime_mode,
            app_env=self.app_env,
            # Keep Uvicorn/FastAPI logging configuration in source mode.
            configure_logger=False,
        )

    async def _verify_schema(self) -> None:
        config = build_alembic_config(
            database_url=self.url,
            runtime_mode=self.runtime_mode,
            app_env=self.app_env,
            configure_logger=False,
        )
        expected = expected_schema_heads(config)

        def read_current_heads(connection) -> tuple[str, ...]:
            return tuple(
                sorted(MigrationContext.configure(connection).get_current_heads())
            )

        async with self.engine.connect() as connection:
            current = await connection.run_sync(read_current_heads)
        if current != expected:
            raise RuntimeError(
                "数据库 schema 未由 migration Job 升级到当前 Alembic head"
            )

    @asynccontextmanager
    async def session(self) -> AsyncIterator[AsyncSession]:
        await self.initialize()
        async with self.session_factory() as session:
            try:
                yield session
            except BaseException:
                await session.rollback()
                raise

    async def shutdown(self) -> None:
        if self._closed:
            return
        await self.engine.dispose()
        self._closed = True

    @property
    def is_closed(self) -> bool:
        return self._closed

    @property
    def is_memory(self) -> bool:
        return self._is_memory

    @property
    def backend_name(self) -> str:
        return self._backend_name
