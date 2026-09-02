from __future__ import annotations

from uuid import uuid4

import pytest


pytestmark = [pytest.mark.asyncio, pytest.mark.area("API 路径矩阵")]


READ_PATHS = [
    ("/api/v1/health", 200),
    ("/api/v1/auth/status", 200),
    ("/api/v1/projects", 200),
    ("/api/v1/test-suites", 200),
    ("/api/v1/test-cases", 200),
    ("/api/v1/test-case-snapshots", 200),
    ("/api/v1/test-plans", 200),
    ("/api/v1/executions", 200),
    ("/api/v1/defects", 200),
    ("/api/v1/audit-events", 200),
    ("/api/v1/comments", 422),
    ("/api/v1/attachments", 422),
    ("/api/v1/quality/report", 422),
    ("/api/v1/quality/summary", 422),
    ("/api/v1/quality/trends", 422),
    ("/api/v1/quality/coverage", 422),
    ("/api/v1/pipelines", 200),
    ("/health/live", 200),
    ("/health/ready", 200),
    ("/metrics", 200),
]


@pytest.mark.parametrize("path,expected", READ_PATHS, ids=[p for p, _ in READ_PATHS])
async def test_public_read_paths_return_contract_status(client, path, expected):
    response = await client.get(path)
    assert response.status_code == expected, response.text
    assert response.headers.get("x-request-id")


RESOURCE_PATHS = [
    "/api/v1/projects/{id}",
    "/api/v1/test-suites/{id}",
    "/api/v1/test-cases/{id}",
    "/api/v1/test-case-snapshots/{id}",
    "/api/v1/test-plans/{id}",
    "/api/v1/executions/{id}",
    "/api/v1/defects/{id}",
]


@pytest.mark.parametrize("path", RESOURCE_PATHS)
async def test_missing_resource_paths_return_404(client, path):
    response = await client.get(path.format(id=uuid4()))
    assert response.status_code == 404, response.text
    body = response.json()
    assert body["code"] == 40400 and body["data"] is None


@pytest.mark.parametrize("path", RESOURCE_PATHS)
async def test_malformed_resource_ids_return_unified_422(client, path):
    response = await client.get(path.format(id="not-a-uuid"))
    assert response.status_code == 422, response.text
    body = response.json()
    assert body["code"] == 42200 and body["data"]["errors"]


CREATE_PATHS = [
    "/api/v1/projects", "/api/v1/test-suites", "/api/v1/test-cases",
    "/api/v1/test-case-snapshots", "/api/v1/test-plans", "/api/v1/executions",
    "/api/v1/defects", "/api/v1/comments", "/api/v1/attachments",
    "/api/v1/pipelines",
]


@pytest.mark.parametrize("path", CREATE_PATHS)
async def test_empty_create_payloads_are_rejected_without_500(client, path):
    response = await client.post(path, json={})
    assert response.status_code in {400, 404, 409, 422}, response.text
    assert response.status_code < 500


@pytest.mark.parametrize("path", [p for p, _ in READ_PATHS if p != "/metrics"])
async def test_post_is_not_silently_accepted_on_read_paths(client, path):
    response = await client.post(path)
    assert response.status_code in {404, 405, 409, 422}, response.text


@pytest.mark.parametrize("path", ["/api/v2/projects", "/api/v1/unknown", "/unknown", "/api/v1/projects/not-a-uuid/extra"])
async def test_unknown_paths_return_404(client, path):
    response = await client.get(path)
    assert response.status_code == 404


@pytest.mark.parametrize("status", ["active", "archived", "invalid-status", "", "ACTIVE"])
async def test_project_status_filter_validates_enum(client, status):
    response = await client.get("/api/v1/projects", params={"status": status})
    assert response.status_code == (200 if status in {"active", "archived"} else 422)


@pytest.mark.parametrize("entity", ["test-cases", "defects"])
@pytest.mark.parametrize("fmt", ["csv", "xlsx"])
async def test_downloadable_templates_have_safe_headers(client, entity, fmt):
    response = await client.get(f"/api/v1/data-transfer/templates/{entity}", params={"format": fmt})
    assert response.status_code == 200, response.text
    assert "attachment" in response.headers["content-disposition"]
    assert response.headers["x-content-type-options"] == "nosniff"
    assert int(response.headers["content-length"]) == len(response.content)


@pytest.mark.parametrize("entity", ["unknown", "projects", "test_cases"])
async def test_template_entity_enum_rejects_unknown_values(client, entity):
    response = await client.get(f"/api/v1/data-transfer/templates/{entity}", params={"format": "csv"})
    assert response.status_code == 422


@pytest.mark.parametrize("fmt", ["json", "xls", "", "CSV"])
async def test_template_format_enum_is_strict(client, fmt):
    response = await client.get("/api/v1/data-transfer/templates/test-cases", params={"format": fmt})
    assert response.status_code == 422
