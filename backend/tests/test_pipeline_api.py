import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.pipeline.router import router as pipeline_router
from app.pipeline.service import InMemoryPipelineService, get_pipeline_service


@pytest.mark.asyncio
async def test_pipeline_http_trigger_query_and_cancel() -> None:
    application = FastAPI()
    application.include_router(pipeline_router, prefix="/api/v1")
    service = InMemoryPipelineService()
    application.dependency_overrides[get_pipeline_service] = lambda: service

    payload = {
        "name": "api-pipeline",
        "stages": [
            {
                "name": "test",
                "jobs": [{"name": "pytest", "duration_ms": 1000}],
            }
        ],
    }
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        triggered = await client.post("/api/v1/pipelines", json=payload)
        assert triggered.status_code == 202
        run_id = triggered.json()["data"]["pipeline"]["id"]

        queried = await client.get(f"/api/v1/pipelines/{run_id}")
        assert queried.status_code == 200

        cancelled = await client.post(f"/api/v1/pipelines/{run_id}/cancel")
        assert cancelled.status_code == 200
        assert cancelled.json()["data"]["pipeline"]["status"] == "cancelled"

    assert len(await service.list_runs()) == 1
    assert await get_pipeline_service().list_runs() == []
    await service.shutdown()
