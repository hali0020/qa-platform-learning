from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app
from app.pipeline.models import PipelineJobSpec, PipelineStageSpec, PipelineTriggerRequest


def temporary_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "app_env": "test",
        "auth_enabled": False,
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'http.db').as_posix()}"
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_local_frontend_cors_preflight(tmp_path: Path) -> None:
    application = create_app(
        temporary_settings(
            tmp_path,
            cors_origins=("http://127.0.0.1:5173",),
        )
    )
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        response = await client.options(
            "/api/v1/projects",
            headers={
                "Origin": "http://127.0.0.1:5173",
                "Access-Control-Request-Method": "GET",
            },
        )

    assert response.status_code == 200
    assert response.headers["access-control-allow-origin"] == (
        "http://127.0.0.1:5173"
    )


@pytest.mark.asyncio
async def test_http_errors_use_api_envelope(tmp_path: Path) -> None:
    application = create_app(temporary_settings(tmp_path))
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/api/v1/pipelines/missing")

    assert response.status_code == 404
    assert response.json() == {
        "code": 40400,
        "message": "pipeline not found: missing",
        "data": None,
    }


@pytest.mark.asyncio
async def test_lifespan_stops_local_pipeline_tasks(tmp_path: Path) -> None:
    application = create_app(temporary_settings(tmp_path))
    async with application.router.lifespan_context(application):
        await application.state.pipeline_service.trigger(
            PipelineTriggerRequest(
                name="shutdown-check",
                stages=[
                    PipelineStageSpec(
                        name="test",
                        jobs=[
                            PipelineJobSpec(name="slow-job", duration_ms=10_000)
                        ],
                    )
                ],
            )
        )

    assert application.state.pipeline_service.is_closed is True
