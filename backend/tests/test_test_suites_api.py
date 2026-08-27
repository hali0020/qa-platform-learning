from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def application(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'test-suites.db').as_posix()}"
    return create_app(
        Settings(app_env="test", auth_enabled=False, database_url=database_url)
    )


async def create_project(client: AsyncClient, key: str = "SUITES") -> dict:
    response = await client.post(
        "/api/v1/projects",
        json={"key": key, "name": f"{key} project"},
    )
    assert response.status_code == 201
    return response.json()["data"]


async def create_suite(
    client: AsyncClient,
    project_id: str,
    name: str,
    parent_id: str | None = None,
) -> dict:
    response = await client.post(
        "/api/v1/test-suites",
        json={
            "project_id": project_id,
            "parent_id": parent_id,
            "name": name,
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_suite_hierarchy_lifecycle_delete_guards_and_audit(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client)
        root = await create_suite(client, project["id"], "Regression")
        child = await create_suite(
            client,
            project["id"],
            "Login",
            root["id"],
        )

        listed = await client.get(
            "/api/v1/test-suites",
            params={"project_id": project["id"]},
        )
        assert listed.status_code == 200
        assert {item["id"] for item in listed.json()["data"]} == {
            root["id"],
            child["id"],
        }

        updated = await client.patch(
            f"/api/v1/test-suites/{child['id']}",
            json={"name": "Account login", "position": 20},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["name"] == "Account login"
        assert updated.json()["data"]["position"] == 20

        archived = await client.post(
            f"/api/v1/test-suites/{root['id']}/transition",
            json={"status": "archived"},
        )
        assert archived.status_code == 200
        blocked_create = await client.post(
            "/api/v1/test-suites",
            json={
                "project_id": project["id"],
                "parent_id": root["id"],
                "name": "Blocked child",
            },
        )
        assert blocked_create.status_code == 409

        restored = await client.post(
            f"/api/v1/test-suites/{root['id']}/transition",
            json={"status": "active"},
        )
        assert restored.status_code == 200

        non_empty_delete = await client.delete(
            f"/api/v1/test-suites/{root['id']}"
        )
        assert non_empty_delete.status_code == 409
        assert "子套件" in non_empty_delete.json()["message"]

        assert (
            await client.delete(f"/api/v1/test-suites/{child['id']}")
        ).status_code == 200
        assert (
            await client.delete(f"/api/v1/test-suites/{root['id']}")
        ).status_code == 200

        audit = await client.get(
            "/api/v1/audit-events",
            params={"entity_type": "test_suite", "entity_id": root["id"]},
        )
        assert audit.status_code == 200
        assert {event["action"] for event in audit.json()["data"]} == {
            "created",
            "status_changed",
            "deleted",
        }


@pytest.mark.asyncio
async def test_suite_rejects_duplicate_sibling_cycle_and_cross_project_parent(
    application,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        first_project = await create_project(client, "TREE")
        second_project = await create_project(client, "OTHER")
        root = await create_suite(client, first_project["id"], "Smoke")
        child = await create_suite(
            client,
            first_project["id"],
            "Web",
            root["id"],
        )

        duplicate = await client.post(
            "/api/v1/test-suites",
            json={"project_id": first_project["id"], "name": " smoke "},
        )
        assert duplicate.status_code == 409

        cycle = await client.patch(
            f"/api/v1/test-suites/{root['id']}",
            json={"parent_id": child["id"]},
        )
        assert cycle.status_code == 409
        assert "循环" in cycle.json()["message"]

        cross_project = await client.post(
            "/api/v1/test-suites",
            json={
                "project_id": second_project["id"],
                "parent_id": root["id"],
                "name": "Invalid",
            },
        )
        assert cross_project.status_code == 409
        assert "同一项目" in cross_project.json()["message"]


@pytest.mark.asyncio
async def test_suite_with_assigned_case_cannot_be_deleted(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client, "CASES")
        suite = await create_suite(client, project["id"], "Cases")
        case_response = await client.post(
            "/api/v1/test-cases",
            json={
                "project_id": project["id"],
                "suite_id": suite["id"],
                "title": "Assigned case",
            },
        )
        assert case_response.status_code == 201

        blocked = await client.delete(f"/api/v1/test-suites/{suite['id']}")
        assert blocked.status_code == 409
        assert "测试用例" in blocked.json()["message"]


@pytest.mark.asyncio
async def test_case_can_be_assigned_filtered_and_unassigned(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client, "ORGANIZE")
        suite = await create_suite(client, project["id"], "Regression")
        created = await client.post(
            "/api/v1/test-cases",
            json={"project_id": project["id"], "title": "Movable case"},
        )
        case_id = created.json()["data"]["id"]

        assigned = await client.patch(
            f"/api/v1/test-cases/{case_id}",
            json={"suite_id": suite["id"]},
        )
        assert assigned.status_code == 200
        assert assigned.json()["data"]["suite_id"] == suite["id"]

        by_suite = await client.get(
            "/api/v1/test-cases",
            params={"project_id": project["id"], "suite_id": suite["id"]},
        )
        assert [item["id"] for item in by_suite.json()["data"]] == [case_id]

        unassigned = await client.patch(
            f"/api/v1/test-cases/{case_id}",
            json={"suite_id": None},
        )
        assert unassigned.status_code == 200
        assert unassigned.json()["data"]["suite_id"] is None

        loose_cases = await client.get(
            "/api/v1/test-cases",
            params={"project_id": project["id"], "unassigned": "true"},
        )

    assert [item["id"] for item in loose_cases.json()["data"]] == [case_id]
