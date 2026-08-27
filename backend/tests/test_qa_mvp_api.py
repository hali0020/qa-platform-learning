from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def application(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'qa-mvp.db').as_posix()}"
    return create_app(
        Settings(app_env="test", auth_enabled=False, database_url=database_url)
    )


async def create_project(client: AsyncClient, key: str = "SHOP") -> dict:
    response = await client.post(
        "/api/v1/projects",
        json={"key": key, "name": f"{key} project"},
    )
    assert response.status_code == 201
    return response.json()["data"]


async def create_active_case(client: AsyncClient, project_id: str) -> dict:
    response = await client.post(
        "/api/v1/test-cases",
        json={
            "project_id": project_id,
            "title": "用户可以登录",
            "steps": [
                {"action": "提交正确账号密码", "expected_result": "进入首页"}
            ],
            "tags": [" Smoke ", "smoke", "Login"],
        },
    )
    assert response.status_code == 201
    test_case = response.json()["data"]
    transitioned = await client.post(
        f"/api/v1/test-cases/{test_case['id']}/transition",
        json={"status": "active"},
    )
    assert transitioned.status_code == 200
    return transitioned.json()["data"]


async def create_ready_plan(
    client: AsyncClient,
    project_id: str,
    case_ids: list[str],
) -> dict:
    response = await client.post(
        "/api/v1/test-plans",
        json={
            "project_id": project_id,
            "name": "冒烟测试",
            "case_ids": case_ids,
        },
    )
    assert response.status_code == 201
    plan = response.json()["data"]
    transitioned = await client.post(
        f"/api/v1/test-plans/{plan['id']}/transition",
        json={"status": "ready"},
    )
    assert transitioned.status_code == 200
    return transitioned.json()["data"]


@pytest.mark.asyncio
async def test_project_crud_filter_and_transition(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project = await create_project(client, "web")
        assert project["key"] == "WEB"

        updated = await client.patch(
            f"/api/v1/projects/{project['id']}",
            json={"name": "Web QA"},
        )
        assert updated.json()["data"]["name"] == "Web QA"

        archived = await client.post(
            f"/api/v1/projects/{project['id']}/transition",
            json={"status": "archived"},
        )
        assert archived.json()["data"]["status"] == "archived"

        listed = await client.get("/api/v1/projects", params={"status": "archived"})
        assert [item["id"] for item in listed.json()["data"]] == [project["id"]]

        deleted = await client.delete(f"/api/v1/projects/{project['id']}")
        assert deleted.json()["data"]["deleted_id"] == project["id"]


@pytest.mark.asyncio
async def test_duplicate_project_key_returns_unified_conflict(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        await create_project(client, "MOBILE")
        duplicate = await client.post(
            "/api/v1/projects",
            json={"key": "mobile", "name": "duplicate"},
        )

    assert duplicate.status_code == 409
    assert duplicate.json()["code"] == 40900
    assert duplicate.json()["data"] is None


@pytest.mark.asyncio
async def test_case_crud_normalizes_tags_and_enforces_lifecycle(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project = await create_project(client)
        no_steps = await client.post(
            "/api/v1/test-cases",
            json={"project_id": project["id"], "title": "empty"},
        )
        case_id = no_steps.json()["data"]["id"]

        invalid = await client.post(
            f"/api/v1/test-cases/{case_id}/transition",
            json={"status": "active"},
        )
        assert invalid.status_code == 409

        updated = await client.patch(
            f"/api/v1/test-cases/{case_id}",
            json={
                "steps": [{"action": "打开页面", "expected_result": "展示成功"}],
                "tags": [" Smoke ", "smoke", "Web"],
            },
        )
        assert updated.json()["data"]["tags"] == ["smoke", "web"]

        active = await client.post(
            f"/api/v1/test-cases/{case_id}/transition",
            json={"status": "active"},
        )
        assert active.json()["data"]["status"] == "active"

        invalid_backwards = await client.post(
            f"/api/v1/test-cases/{case_id}/transition",
            json={"status": "draft"},
        )
        assert invalid_backwards.status_code == 409


@pytest.mark.asyncio
async def test_case_requires_existing_active_project(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        missing = await client.post(
            "/api/v1/test-cases",
            json={"project_id": str(uuid4()), "title": "orphan"},
        )
        assert missing.status_code == 404

        project = await create_project(client)
        await client.post(
            f"/api/v1/projects/{project['id']}/transition",
            json={"status": "archived"},
        )
        archived = await client.post(
            "/api/v1/test-cases",
            json={"project_id": project["id"], "title": "blocked"},
        )
        assert archived.status_code == 409


@pytest.mark.asyncio
async def test_plan_only_becomes_ready_with_active_cases(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project = await create_project(client)
        draft_case = await client.post(
            "/api/v1/test-cases",
            json={
                "project_id": project["id"],
                "title": "draft case",
                "steps": [{"action": "a", "expected_result": "b"}],
            },
        )
        plan = await client.post(
            "/api/v1/test-plans",
            json={
                "project_id": project["id"],
                "name": "release",
                "case_ids": [draft_case.json()["data"]["id"]],
            },
        )
        transition = await client.post(
            f"/api/v1/test-plans/{plan.json()['data']['id']}/transition",
            json={"status": "ready"},
        )

    assert transition.status_code == 409
    assert "未启用" in transition.json()["message"]


@pytest.mark.asyncio
async def test_execution_full_status_and_result_flow(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project = await create_project(client)
        test_case = await create_active_case(client, project["id"])
        plan = await create_ready_plan(client, project["id"], [test_case["id"]])

        created = await client.post(
            "/api/v1/executions", json={"plan_id": plan["id"]}
        )
        assert created.status_code == 201
        execution = created.json()["data"]
        assert execution["results"][0]["case_title"] == test_case["title"]

        started = await client.post(
            f"/api/v1/executions/{execution['id']}/transition",
            json={"status": "running"},
        )
        assert started.json()["data"]["status"] == "running"

        premature = await client.post(
            f"/api/v1/executions/{execution['id']}/transition",
            json={"status": "completed"},
        )
        assert premature.status_code == 409

        result = await client.put(
            f"/api/v1/executions/{execution['id']}/results/{test_case['id']}",
            json={"status": "passed", "actual_result": "登录成功"},
        )
        assert result.json()["data"]["results"][0]["status"] == "passed"

        completed = await client.post(
            f"/api/v1/executions/{execution['id']}/transition",
            json={"status": "completed"},
        )
        assert completed.json()["data"]["status"] == "completed"

        refreshed_plan = await client.get(f"/api/v1/test-plans/{plan['id']}")
        assert refreshed_plan.json()["data"]["status"] == "completed"


@pytest.mark.asyncio
async def test_execution_can_cancel_and_cannot_restart(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project = await create_project(client)
        test_case = await create_active_case(client, project["id"])
        plan = await create_ready_plan(client, project["id"], [test_case["id"]])
        execution = (
            await client.post("/api/v1/executions", json={"plan_id": plan["id"]})
        ).json()["data"]

        cancelled = await client.post(
            f"/api/v1/executions/{execution['id']}/transition",
            json={"status": "cancelled"},
        )
        assert cancelled.json()["data"]["status"] == "cancelled"

        restarted = await client.post(
            f"/api/v1/executions/{execution['id']}/transition",
            json={"status": "running"},
        )
        assert restarted.status_code == 409


@pytest.mark.asyncio
async def test_referenced_entities_are_protected_from_deletion(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project = await create_project(client)
        test_case = await create_active_case(client, project["id"])
        await create_ready_plan(client, project["id"], [test_case["id"]])

        case_delete = await client.delete(f"/api/v1/test-cases/{test_case['id']}")
        project_delete = await client.delete(f"/api/v1/projects/{project['id']}")

    assert case_delete.status_code == 409
    assert project_delete.status_code == 409


@pytest.mark.asyncio
async def test_validation_errors_use_api_response_shape(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        invalid = await client.post(
            "/api/v1/projects",
            json={"key": "1", "name": ""},
        )

    body = invalid.json()
    assert invalid.status_code == 422
    assert body["code"] == 42200
    assert body["message"] == "请求参数校验失败"
    assert body["data"]["errors"]
