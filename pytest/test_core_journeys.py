from __future__ import annotations

import pytest

from helpers import create_active_case, create_project, create_ready_plan


pytestmark = [pytest.mark.asyncio, pytest.mark.area("核心业务旅程")]


async def test_project_case_plan_execution_quality_end_to_end(client):
    project = await create_project(client, "E2E")
    case = await create_active_case(client, project["id"])
    assert set(case["tags"]) == {"smoke", "learning-site"}
    plan = await create_ready_plan(client, project["id"], [case["id"]])

    created = await client.post("/api/v1/executions", json={"plan_id": plan["id"]})
    assert created.status_code == 201, created.text
    execution = created.json()["data"]
    assert execution["results"][0]["case_title"] == case["title"]

    running = await client.post(f"/api/v1/executions/{execution['id']}/transition", json={"status": "running"})
    assert running.status_code == 200
    result = await client.put(
        f"/api/v1/executions/{execution['id']}/results/{case['id']}",
        json={"status": "passed", "actual_result": "登录成功"},
    )
    assert result.status_code == 200
    completed = await client.post(f"/api/v1/executions/{execution['id']}/transition", json={"status": "completed"})
    assert completed.status_code == 200

    summary = await client.get("/api/v1/quality/summary", params={
        "project_id": project["id"], "date_from": "2026-01-01", "date_to": "2026-12-31",
    })
    assert summary.status_code == 200, summary.text
    data = summary.json()["data"]
    assert data["executions"]["pass_rate"]["numerator"] == 1
    assert data["executions"]["pass_rate"]["denominator"] == 1
    assert data["executions"]["pass_rate"]["percent"] == 100.0


async def test_lifecycle_rejects_shortcuts_and_backwards_transitions(client):
    project = await create_project(client, "LIFE")
    created = await client.post("/api/v1/test-cases", json={
        "project_id": project["id"], "title": "缺步骤用例",
    })
    case = created.json()["data"]
    active = await client.post(f"/api/v1/test-cases/{case['id']}/transition", json={"status": "active"})
    assert active.status_code == 409
    patched = await client.patch(f"/api/v1/test-cases/{case['id']}", json={
        "steps": [{"action": "执行", "expected_result": "成功"}],
    })
    assert patched.status_code == 200
    active = await client.post(f"/api/v1/test-cases/{case['id']}/transition", json={"status": "active"})
    assert active.status_code == 200
    backwards = await client.post(f"/api/v1/test-cases/{case['id']}/transition", json={"status": "draft"})
    assert backwards.status_code == 409


async def test_duplicate_project_key_and_referenced_delete_are_conflicts(client):
    project = await create_project(client, "UNIQ")
    duplicate = await client.post("/api/v1/projects", json={"key": "uniq", "name": "重复"})
    assert duplicate.status_code == 409
    case = await create_active_case(client, project["id"], "引用保护")
    await create_ready_plan(client, project["id"], [case["id"]])
    assert (await client.delete(f"/api/v1/test-cases/{case['id']}")).status_code == 409
    assert (await client.delete(f"/api/v1/projects/{project['id']}")).status_code == 409


@pytest.mark.parametrize("result_status", ["passed", "failed", "blocked", "skipped", "not_run"])
async def test_execution_result_status_transition_contract(client, result_status):
    project = await create_project(client, f"R{result_status[:3]}".upper())
    case = await create_active_case(client, project["id"], result_status)
    plan = await create_ready_plan(client, project["id"], [case["id"]])
    execution = (await client.post("/api/v1/executions", json={"plan_id": plan["id"]})).json()["data"]
    await client.post(f"/api/v1/executions/{execution['id']}/transition", json={"status": "running"})
    response = await client.put(
        f"/api/v1/executions/{execution['id']}/results/{case['id']}",
        json={"status": result_status, "actual_result": f"result={result_status}"},
    )
    if result_status == "not_run":
        assert response.status_code == 409
        assert response.json()["code"] == 40901
    else:
        assert response.status_code == 200, response.text
        assert response.json()["data"]["results"][0]["status"] == result_status
