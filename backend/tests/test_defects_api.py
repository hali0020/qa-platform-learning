from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def application(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'defects.db').as_posix()}"
    return create_app(
        Settings(app_env="test", auth_enabled=False, database_url=database_url)
    )


async def create_project(client: AsyncClient, key: str = "BUGS") -> dict:
    response = await client.post(
        "/api/v1/projects",
        json={"key": key, "name": f"{key} project"},
    )
    assert response.status_code == 201
    return response.json()["data"]


async def create_case(
    client: AsyncClient,
    project_id: str,
    title: str = "可以提交订单",
) -> dict:
    response = await client.post(
        "/api/v1/test-cases",
        json={
            "project_id": project_id,
            "title": title,
            "steps": [{"action": "提交订单", "expected_result": "提交成功"}],
        },
    )
    assert response.status_code == 201
    return response.json()["data"]


async def create_defect(
    client: AsyncClient,
    project_id: str,
    **overrides,
) -> dict:
    payload = {
        "project_id": project_id,
        "title": "结算按钮没有响应",
        "severity": "major",
        "priority": "P1",
        "reporter": "qa-local",
        "assignee": "developer-local",
        "reproduction_steps": ["进入购物车", "点击结算"],
        "expected_result": "进入支付页",
        "actual_result": "页面没有变化",
    }
    payload.update(overrides)
    response = await client.post("/api/v1/defects", json=payload)
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_defect_crud_lifecycle_and_audit_history(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client)
        defect = await create_defect(client, project["id"])

        listed = await client.get(
            "/api/v1/defects",
            params={
                "project_id": project["id"],
                "status": "open",
                "severity": "major",
                "assignee": "DEVELOPER-LOCAL",
            },
        )
        assert [item["id"] for item in listed.json()["data"]] == [defect["id"]]

        updated = await client.patch(
            f"/api/v1/defects/{defect['id']}",
            json={"description": "稳定复现", "priority": "P0"},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["priority"] == "P0"

        for target, extra in (
            ("in_progress", {}),
            ("resolved", {"resolution": "修复空指针"}),
            ("verified", {}),
            ("closed", {"comment": "本地回归通过"}),
        ):
            transitioned = await client.post(
                f"/api/v1/defects/{defect['id']}/transition",
                json={"status": target, **extra},
            )
            assert transitioned.status_code == 200, transitioned.text

        closed_edit = await client.patch(
            f"/api/v1/defects/{defect['id']}",
            json={"description": "不应被写入"},
        )
        assert closed_edit.status_code == 409

        history = await client.get(
            "/api/v1/audit-events",
            params={"entity_type": "defect", "entity_id": defect["id"]},
        )
        events = history.json()["data"]

    assert [event["action"] for event in events] == [
        "status_changed",
        "status_changed",
        "status_changed",
        "status_changed",
        "updated",
        "created",
    ]
    assert events[0]["actor"] == "local-user"
    assert events[-1]["changes"]["status"] == {
        "before": None,
        "after": "open",
    }


@pytest.mark.asyncio
async def test_defect_transition_rules_and_idempotency(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client)
        defect = await create_defect(client, project["id"], assignee="")

        missing_assignee = await client.post(
            f"/api/v1/defects/{defect['id']}/transition",
            json={"status": "in_progress"},
        )
        assert missing_assignee.status_code == 409

        invalid_jump = await client.post(
            f"/api/v1/defects/{defect['id']}/transition",
            json={"status": "verified"},
        )
        assert invalid_jump.status_code == 409

        missing_resolution = await client.post(
            f"/api/v1/defects/{defect['id']}/transition",
            json={"status": "resolved"},
        )
        assert missing_resolution.status_code == 409

        resolved = await client.post(
            f"/api/v1/defects/{defect['id']}/transition",
            json={"status": "resolved", "resolution": "修复完成"},
        )
        assert resolved.status_code == 200
        resolved_at = resolved.json()["data"]["resolved_at"]
        assert resolved_at is not None

        same_status = await client.post(
            f"/api/v1/defects/{defect['id']}/transition",
            json={"status": "resolved", "resolution": "不会重复记录"},
        )
        assert same_status.status_code == 200

        no_reason = await client.post(
            f"/api/v1/defects/{defect['id']}/transition",
            json={"status": "reopened"},
        )
        assert no_reason.status_code == 409

        reopened = await client.post(
            f"/api/v1/defects/{defect['id']}/transition",
            json={"status": "reopened", "comment": "问题仍可复现"},
        )
        assert reopened.status_code == 200
        assert reopened.json()["data"]["resolution"] == ""
        assert reopened.json()["data"]["resolved_at"] is None

        history = await client.get(
            "/api/v1/audit-events",
            params={
                "entity_type": "defect",
                "entity_id": defect["id"],
                "action": "status_changed",
            },
        )

    assert len(history.json()["data"]) == 2
    assert history.json()["data"][0]["comment"] == "问题仍可复现"


@pytest.mark.asyncio
async def test_defect_rejects_invalid_project_and_cross_project_links(
    application,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        missing = await client.post(
            "/api/v1/defects",
            json={"project_id": str(uuid4()), "title": "孤立缺陷"},
        )
        assert missing.status_code == 404

        first = await create_project(client, "FIRST")
        second = await create_project(client, "SECOND")
        second_case = await create_case(client, second["id"])
        cross_project = await client.post(
            "/api/v1/defects",
            json={
                "project_id": first["id"],
                "case_id": second_case["id"],
                "title": "错误关联",
            },
        )
        assert cross_project.status_code == 409

        archived = await create_project(client, "ARCHIVED")
        archive_response = await client.post(
            f"/api/v1/projects/{archived['id']}/transition",
            json={"status": "archived"},
        )
        assert archive_response.status_code == 200
        blocked = await client.post(
            "/api/v1/defects",
            json={"project_id": archived["id"], "title": "归档项目缺陷"},
        )

        read_only = await client.post("/api/v1/audit-events", json={})

    assert blocked.status_code == 409
    assert read_only.status_code == 405


@pytest.mark.asyncio
async def test_audit_filters_validate_limit(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client)
        defect = await create_defect(client, project["id"])

        by_project = await client.get(
            "/api/v1/audit-events",
            params={"project_id": project["id"], "limit": 1},
        )
        invalid_limit = await client.get(
            "/api/v1/audit-events",
            params={"limit": 0},
        )

    assert len(by_project.json()["data"]) == 1
    assert by_project.json()["data"][0]["entity_id"] == defect["id"]
    assert invalid_limit.status_code == 422
    assert invalid_limit.json()["code"] == 42200
