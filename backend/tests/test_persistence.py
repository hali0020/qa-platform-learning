import sqlite3
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy.exc import OperationalError

from app.core.config import Settings
from app.main import create_app


def settings_for_database(database_path: Path) -> Settings:
    return Settings(
        app_env="test",
        auth_enabled=False,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )


@pytest.mark.asyncio
async def test_http_data_survives_full_application_restart(tmp_path: Path) -> None:
    database_path = tmp_path / "application-restart.db"
    settings = settings_for_database(database_path)

    first_application = create_app(settings)
    async with first_application.router.lifespan_context(first_application):
        async with AsyncClient(
            transport=ASGITransport(app=first_application),
            base_url="http://testserver",
        ) as client:
            created_response = await client.post(
                "/api/v1/projects",
                json={
                    "key": "RESTART",
                    "name": "重启持久化验证",
                    "description": "只写入 pytest 临时数据库",
                },
            )
            assert created_response.status_code == 201
            created = created_response.json()["data"]
            pipeline_response = await client.post(
                "/api/v1/pipelines",
                json={
                    "name": "restart-pipeline",
                    "auto_start": False,
                    "idempotency_key": "restart-pipeline-001",
                    "stages": [
                        {
                            "name": "test",
                            "jobs": [{"name": "pytest", "duration_ms": 0}],
                        }
                    ],
                },
            )
            assert pipeline_response.status_code == 202
            pipeline_id = pipeline_response.json()["data"]["pipeline"]["id"]

    assert first_application.state.container.database.is_closed
    assert first_application.state.pipeline_service.is_closed
    assert database_path.is_file()

    second_application = create_app(settings)
    async with second_application.router.lifespan_context(second_application):
        async with AsyncClient(
            transport=ASGITransport(app=second_application),
            base_url="http://testserver",
        ) as client:
            restored_response = await client.get(
                f"/api/v1/projects/{created['id']}"
            )
            listed_response = await client.get("/api/v1/projects")
            restored_pipeline_response = await client.get(
                f"/api/v1/pipelines/{pipeline_id}"
            )

    assert restored_response.status_code == 200
    assert restored_response.json()["data"] == created
    assert [item["id"] for item in listed_response.json()["data"]] == [
        created["id"]
    ]
    assert restored_pipeline_response.status_code == 200
    assert restored_pipeline_response.json()["data"]["status"] == "cancelled"
    assert (
        restored_pipeline_response.json()["data"]["message"]
        == "pipeline service shut down"
    )
    assert second_application.state.container.database.is_closed
    assert second_application.state.pipeline_service.is_closed


@pytest.mark.asyncio
async def test_failed_migration_is_not_masked_by_pipeline_cleanup(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "broken-migration.db"
    with sqlite3.connect(database_path) as connection:
        connection.execute("CREATE TABLE projects (id TEXT PRIMARY KEY)")

    application = create_app(settings_for_database(database_path))
    with pytest.raises(OperationalError, match="projects already exists"):
        async with application.router.lifespan_context(application):
            pass

    assert application.state.container.database.is_closed
    assert application.state.pipeline_service.is_closed is False
