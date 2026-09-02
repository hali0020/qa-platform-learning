from __future__ import annotations

import re

import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.area("横切质量属性")]


@pytest.mark.parametrize("request_id,accepted", [
    ("learning-run-001", True),
    ("A_B.C-123", True),
    ("has space", False),
    ("has/slash", False),
    ("x" * 129, False),
])
async def test_request_id_accepts_only_safe_bounded_values(client, request_id, accepted):
    response = await client.get("/api/v1/health", headers={"X-Request-ID": request_id})
    returned = response.headers["x-request-id"]
    assert returned == request_id if accepted else returned != request_id
    assert re.fullmatch(r"[A-Za-z0-9._-]+", returned)


@pytest.mark.parametrize("payload", [
    {},
    {"key": "1", "name": ""},
    {"key": "A", "name": "too short key"},
    {"key": "A B C", "name": "spaces"},
    {"key": "A" * 65, "name": "too long"},
])
async def test_validation_failures_use_unified_shape(client, payload):
    response = await client.post("/api/v1/projects", json=payload)
    assert response.status_code == 422
    body = response.json()
    assert body["code"] == 42200
    assert body["message"] == "请求参数校验失败"
    assert isinstance(body["data"]["errors"], list)


async def test_metrics_excludes_own_scrape_and_uses_route_template(client):
    await client.get("/api/v1/projects/not-a-uuid")
    first = await client.get("/metrics")
    second = await client.get("/metrics")
    assert first.status_code == second.status_code == 200
    text = second.text
    assert '/projects/not-a-uuid' not in text
    assert '/api/v1/projects/{project_id}' in text
    assert 'qa_http_requests_total' in text


@pytest.mark.parametrize("origin", ["http://localhost:5173", "https://evil.example"])
async def test_cors_preflight_never_reflects_untrusted_origin(client, origin):
    response = await client.options("/api/v1/projects", headers={
        "Origin": origin,
        "Access-Control-Request-Method": "GET",
    })
    reflected = response.headers.get("access-control-allow-origin")
    assert reflected != "https://evil.example"
    assert response.status_code in {200, 400}


@pytest.mark.parametrize("path", ["/api/v1/projects", "/api/v1/test-cases", "/api/v1/quality/summary", "/metrics"])
async def test_head_does_not_mutate_get_endpoints(client, path):
    response = await client.head(path)
    assert response.status_code in {405, 422}


async def test_health_response_contains_only_safe_environment_metadata(client):
    response = await client.get("/api/v1/health")
    body = response.json()["data"]
    assert set(body) == {"service", "environment", "local_only"}
    serialized = response.text.casefold()
    for forbidden in ("password", "secret", "token", "sqlite", "database_url"):
        assert forbidden not in serialized


async def test_metrics_labels_do_not_contain_request_id(client):
    marker = "sensitive-request-id-987"
    await client.get("/api/v1/health", headers={"X-Request-ID": marker})
    metrics = await client.get("/metrics")
    assert marker not in metrics.text
