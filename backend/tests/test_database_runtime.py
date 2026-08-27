from __future__ import annotations

from typing import Any

import pytest
from sqlalchemy.engine import make_url

from app.core.config import Settings
from app.database.session import Database
from app.main import create_app
from app.pipeline.persistence import SQLAlchemyPipelinePersistence


class _FakeAsyncEngine:
    def __init__(self, url: str) -> None:
        self.url = make_url(url)
        self.sync_engine = object()


def test_postgres_engine_uses_async_pool_pre_ping_without_sqlite_pragmas(
    monkeypatch,
) -> None:
    url = "postgresql+asyncpg://qa:secret@postgres:5432/qa"
    captured: dict[str, Any] = {}
    sqlite_pragma_calls: list[bool] = []

    def fake_create_async_engine(value: str, **options: object):
        captured.update(url=value, options=options)
        return _FakeAsyncEngine(value)

    monkeypatch.setattr(
        "app.database.session.create_async_engine",
        fake_create_async_engine,
    )
    monkeypatch.setattr(
        Database,
        "_enable_sqlite_foreign_keys",
        lambda self: sqlite_pragma_calls.append(True),
    )

    database = Database(
        url,
        runtime_mode="postgres_local_container",
        app_env="local-container",
    )

    assert database.backend_name == "postgresql"
    assert not database.is_memory
    assert captured == {
        "url": url,
        "options": {"echo": False, "pool_pre_ping": True},
    }
    assert sqlite_pragma_calls == []


def test_sqlite_engine_keeps_sqlite_only_initialization(monkeypatch) -> None:
    url = "sqlite+aiosqlite:///:memory:"
    captured: dict[str, Any] = {}
    sqlite_pragma_calls: list[bool] = []

    def fake_create_async_engine(value: str, **options: object):
        captured.update(url=value, options=options)
        return _FakeAsyncEngine(value)

    monkeypatch.setattr(
        "app.database.session.create_async_engine",
        fake_create_async_engine,
    )
    monkeypatch.setattr(
        Database,
        "_enable_sqlite_foreign_keys",
        lambda self: sqlite_pragma_calls.append(True),
    )

    database = Database(url)

    assert database.backend_name == "sqlite"
    assert database.is_memory
    assert captured == {"url": url, "options": {"echo": False}}
    assert sqlite_pragma_calls == [True]


def test_database_constructor_rejects_unvalidated_postgres_target() -> None:
    url = "postgresql+asyncpg://qa:secret@postgres:5432/qa"

    with pytest.raises(RuntimeError, match="sqlite_local"):
        Database(url)


def test_database_constructor_rejects_other_postgres_host() -> None:
    url = "postgresql+asyncpg://test_user:test_password@db.example.test:5432/qa"

    with pytest.raises(RuntimeError, match="postgres:5432"):
        Database(
            url,
            runtime_mode="postgres_local_container",
            app_env="local-container",
        )


@pytest.mark.asyncio
async def test_app_wires_validated_postgres_without_connecting() -> None:
    application = create_app(
        Settings(
            app_env="local-container",
            local_only=False,
            database_runtime_mode="postgres_local_container",
            database_url=(
                "postgresql+asyncpg://qa:local-only@postgres:5432/qa_learning"
            ),
        )
    )
    database = application.state.container.database

    assert database is not None
    assert database.backend_name == "postgresql"
    assert isinstance(
        application.state.pipeline_service._persistence,
        SQLAlchemyPipelinePersistence,
    )
    assert database._initialized is False
    await database.shutdown()
