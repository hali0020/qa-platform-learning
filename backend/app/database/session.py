from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path

from alembic import command
from alembic.config import Config
from sqlalchemy import event
from sqlalchemy.engine import make_url
from sqlalchemy.ext.asyncio import (
    AsyncEngine,
    AsyncSession,
    async_sessionmaker,
    create_async_engine,
)

from app.core.config import validate_database_runtime_target


class Database:
    """异步 SQLAlchemy 引擎与会话生命周期的单一入口。"""

    def __init__(
        self,
        url: str,
        *,
        echo: bool = False,
        runtime_mode: str = "sqlite_local",
        app_env: str = "local",
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
                # 持久化数据库始终由 Alembic 统一升级。放到工作线程运行，
                # 避免其同步命令和内部 asyncio.run 阻塞 FastAPI 事件循环。
                await asyncio.to_thread(self._upgrade_schema)
            self._initialized = True

    def _upgrade_schema(self) -> None:
        backend_root = Path(__file__).resolve().parents[2]
        config = Config(str(backend_root / "alembic.ini"))
        config.set_main_option("script_location", str(backend_root / "alembic"))
        config.attributes["database_url"] = self.url
        config.attributes["runtime_mode"] = self.runtime_mode
        config.attributes["app_env"] = self.app_env
        # 应用启动时保留 Uvicorn/FastAPI 已有日志配置；直接运行
        # `alembic` CLI 时仍由 env.py 配置迁移日志。
        config.attributes["configure_logger"] = False
        command.upgrade(config, "head")

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
