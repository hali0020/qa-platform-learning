from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


@pytest.fixture
def application(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'case-snapshots.db').as_posix()}"
    return create_app(
        Settings(app_env="test", auth_enabled=False, database_url=database_url)
    )


async def create_project(client: AsyncClient, key: str = "SNAPSHOT") -> dict:
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


async def create_case(
    client: AsyncClient,
    project_id: str,
    title: str,
    suite_id: str | None = None,
) -> dict:
    response = await client.post(
        "/api/v1/test-cases",
        json={
            "project_id": project_id,
            "suite_id": suite_id,
            "title": title,
            "preconditions": "local account exists",
            "steps": [{"action": "open", "expected_result": "visible"}],
            "tags": ["Snapshot"],
        },
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]


@pytest.mark.asyncio
async def test_suite_snapshot_is_recursive_versioned_and_immutable(application) -> None:
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
        direct_case = await create_case(
            client,
            project["id"],
            "Direct case",
            root["id"],
        )
        child_case = await create_case(
            client,
            project["id"],
            "Child case",
            child["id"],
        )
        await create_case(client, project["id"], "Unassigned case")

        first_response = await client.post(
            "/api/v1/test-case-snapshots",
            json={
                "project_id": project["id"],
                "suite_id": root["id"],
                "label": " 1.0 baseline ",
            },
        )
        assert first_response.status_code == 201, first_response.text
        first = first_response.json()["data"]
        assert first["scope_type"] == "suite"
        assert first["scope_id"] == root["id"]
        assert first["scope_name"] == "Regression"
        assert first["version"] == 1
        assert first["label"] == "1.0 baseline"
        assert first["case_count"] == 2
        items_by_id = {item["source_case_id"]: item for item in first["items"]}
        assert items_by_id[direct_case["id"]]["suite_path"] == ["Regression"]
        assert items_by_id[child_case["id"]]["suite_path"] == [
            "Regression",
            "Login",
        ]

        changed = await client.patch(
            f"/api/v1/test-cases/{child_case['id']}",
            json={"title": "Child case changed"},
        )
        assert changed.status_code == 200
        second_response = await client.post(
            "/api/v1/test-case-snapshots",
            json={
                "project_id": project["id"],
                "suite_id": root["id"],
                "label": "1.1 baseline",
            },
        )
        second = second_response.json()["data"]
        assert second_response.status_code == 201
        assert second["version"] == 2
        assert any(
            item["title"] == "Child case changed" for item in second["items"]
        )

        restored_first = await client.get(
            f"/api/v1/test-case-snapshots/{first['id']}"
        )
        assert restored_first.status_code == 200
        assert any(
            item["title"] == "Child case"
            for item in restored_first.json()["data"]["items"]
        )

        filtered = await client.get(
            "/api/v1/test-case-snapshots",
            params={"scope_type": "suite", "scope_id": root["id"]},
        )
        assert filtered.status_code == 200
        assert [item["version"] for item in filtered.json()["data"]] == [2, 1]

        audit = await client.get(
            "/api/v1/audit-events",
            params={
                "entity_type": "test_case_snapshot",
                "entity_id": first["id"],
            },
        )
        assert audit.status_code == 200
        assert [event["action"] for event in audit.json()["data"]] == [
            "snapshot_created"
        ]


@pytest.mark.asyncio
async def test_project_and_non_recursive_suite_snapshots_have_independent_versions(
    application,
) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client, "SCOPES")
        root = await create_suite(client, project["id"], "Root")
        child = await create_suite(client, project["id"], "Child", root["id"])
        await create_case(client, project["id"], "Root case", root["id"])
        await create_case(client, project["id"], "Child case", child["id"])
        await create_case(client, project["id"], "Loose case")

        exact = await client.post(
            "/api/v1/test-case-snapshots",
            json={
                "project_id": project["id"],
                "suite_id": root["id"],
                "label": "root only",
                "include_descendants": False,
            },
        )
        assert exact.status_code == 201
        assert exact.json()["data"]["case_count"] == 1
        assert exact.json()["data"]["version"] == 1

        whole_project = await client.post(
            "/api/v1/test-case-snapshots",
            json={"project_id": project["id"], "label": "all cases"},
        )
        assert whole_project.status_code == 201
        assert whole_project.json()["data"]["scope_type"] == "project"
        assert whole_project.json()["data"]["case_count"] == 3
        assert whole_project.json()["data"]["version"] == 1


@pytest.mark.asyncio
async def test_snapshot_rejects_empty_scope_and_cross_project_suite(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        first_project = await create_project(client, "EMPTY")
        second_project = await create_project(client, "CROSS")
        empty_suite = await create_suite(client, first_project["id"], "Empty")

        empty = await client.post(
            "/api/v1/test-case-snapshots",
            json={
                "project_id": first_project["id"],
                "suite_id": empty_suite["id"],
                "label": "empty",
            },
        )
        assert empty.status_code == 409
        assert "没有可归档" in empty.json()["message"]

        cross_project = await client.post(
            "/api/v1/test-case-snapshots",
            json={
                "project_id": second_project["id"],
                "suite_id": empty_suite["id"],
                "label": "invalid",
            },
        )
        assert cross_project.status_code == 409
        assert "快照项目" in cross_project.json()["message"]


@pytest.mark.asyncio
async def test_archived_project_cannot_create_new_snapshot(application) -> None:
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        project = await create_project(client, "ARCHIVED-SNAPSHOT")
        await create_case(client, project["id"], "Historical case")
        archived = await client.post(
            f"/api/v1/projects/{project['id']}/transition",
            json={"status": "archived"},
        )
        assert archived.status_code == 200

        blocked = await client.post(
            "/api/v1/test-case-snapshots",
            json={"project_id": project["id"], "label": "too late"},
        )

    assert blocked.status_code == 409
    assert "已归档项目" in blocked.json()["message"]
