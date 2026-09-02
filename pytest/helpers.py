from __future__ import annotations

from httpx import AsyncClient


async def create_project(client: AsyncClient, key: str = "LEARN") -> dict:
    response = await client.post("/api/v1/projects", json={"key": key, "name": f"{key} 学习项目"})
    assert response.status_code == 201, response.text
    return response.json()["data"]


async def create_active_case(client: AsyncClient, project_id: str, title="登录主路径") -> dict:
    response = await client.post("/api/v1/test-cases", json={
        "project_id": project_id,
        "title": title,
        "priority": "P1",
        "case_type": "automated",
        "steps": [{"action": "打开登录页并提交有效账号", "expected_result": "进入首页"}],
        "tags": [" Smoke ", "smoke", "Learning-Site"],
    })
    assert response.status_code == 201, response.text
    case = response.json()["data"]
    response = await client.post(f"/api/v1/test-cases/{case['id']}/transition", json={"status": "active"})
    assert response.status_code == 200, response.text
    return response.json()["data"]


async def create_ready_plan(client: AsyncClient, project_id: str, case_ids: list[str]) -> dict:
    response = await client.post("/api/v1/test-plans", json={
        "project_id": project_id, "name": "学习网站全链路回归", "case_ids": case_ids,
    })
    assert response.status_code == 201, response.text
    plan = response.json()["data"]
    response = await client.post(f"/api/v1/test-plans/{plan['id']}/transition", json={"status": "ready"})
    assert response.status_code == 200, response.text
    return response.json()["data"]
