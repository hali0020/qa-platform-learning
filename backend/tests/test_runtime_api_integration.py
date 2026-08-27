from __future__ import annotations

from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


ADMIN_PASSWORD = "LocalAdmin!12345"
VIEWER_PASSWORD = "LocalViewer!1234"


def runtime_settings(
    tmp_path: Path,
    *,
    auth_enabled: bool,
    database_name: str,
) -> Settings:
    return Settings(
        app_env="test",
        auth_enabled=auth_enabled,
        database_url=(
            "sqlite+aiosqlite:///"
            + (tmp_path / database_name).as_posix()
        ),
        upload_root=str(tmp_path / f"{database_name}-uploads"),
        password_time_cost=1,
        password_memory_cost_kib=1024,
        password_parallelism=1,
    )


async def shutdown_app(application) -> None:
    await application.state.container.shutdown()


@pytest.mark.asyncio
async def test_runtime_routes_work_together_and_survive_app_restart(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(
        tmp_path,
        auth_enabled=False,
        database_name="runtime-api.db",
    )
    application = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            runtime_status = await client.get(
                "/api/v1/integrations/connections/runtime-status"
            )
            assert runtime_status.status_code == 200, runtime_status.text
            assert runtime_status.json()["data"] == {
                "mode": "local_lab",
                "network_providers_allowed": False,
                "target_scope": "loopback_only",
                "external_public_mode_supported": False,
            }

            created_connection = await client.post(
                "/api/v1/integrations/connections",
                json={
                    "name": "Local API lesson",
                    "kind": "local",
                    "definition_ref": "qa-learning-pipeline",
                    "enabled": True,
                },
            )
            assert created_connection.status_code == 201, created_connection.text
            connection = created_connection.json()["data"]

            tested = await client.post(
                f"/api/v1/integrations/connections/{connection['id']}/test"
            )
            assert tested.status_code == 200, tested.text
            assert tested.json()["data"]["network_probe_performed"] is False

            triggered = await client.post(
                f"/api/v1/integrations/connections/{connection['id']}/trigger",
                json={
                    "correlation_id": "runtime-api-lesson",
                    "variables": {"SUITE": "smoke"},
                },
            )
            assert triggered.status_code == 202, triggered.text
            assert triggered.json()["data"]["status"] == "queued"

            enqueued = await client.post(
                "/api/v1/automation/tasks",
                json={
                    "task_type": "qa.device.execute",
                    "payload": {"case_id": "CASE-API-1"},
                    "idempotency_key": "runtime-api-task",
                },
            )
            assert enqueued.status_code == 202, enqueued.text
            task = enqueued.json()["data"]["task"]

            claimed = await client.post(
                "/api/v1/automation/tasks/claim",
                json={
                    "worker_id": "worker-api-1",
                    "queues": ["default"],
                    "lease_seconds": 60,
                },
            )
            assert claimed.status_code == 200, claimed.text
            claimed_data = claimed.json()["data"]
            assert claimed_data["task"]["id"] == task["id"]

            created_device = await client.post(
                "/api/v1/automation/devices",
                json={
                    "name": "Android API lesson",
                    "agent_id": "agent-api-1",
                    "platform": "android",
                    "capabilities": ["android", "api-35"],
                },
            )
            assert created_device.status_code == 201, created_device.text
            device = created_device.json()["data"]

            heartbeat = await client.post(
                f"/api/v1/automation/devices/{device['id']}/heartbeat",
                json={"agent_id": "agent-api-1"},
            )
            assert heartbeat.status_code == 200, heartbeat.text
            assert heartbeat.json()["data"]["status"] == "idle"

            acquired = await client.post(
                "/api/v1/automation/devices/acquire",
                json={
                    "task_id": task["id"],
                    "owner": "worker-api-1",
                    "task_lease_token": claimed_data["lease_token"],
                    "required_capabilities": ["android"],
                    "lease_seconds": 60,
                },
            )
            assert acquired.status_code == 200, acquired.text
            assert acquired.json()["data"]["device"]["status"] == "reserved"

            created_schedule = await client.post(
                "/api/v1/automation/schedules",
                json={
                    "name": "API quality report",
                    "task_type": "qa.quality.generate",
                    "payload": {"project_id": "demo"},
                    "cron": "* * * * *",
                    "timezone": "UTC",
                },
            )
            assert created_schedule.status_code == 201, created_schedule.text
            schedule = created_schedule.json()["data"]

            fired = await client.post(
                f"/api/v1/automation/schedules/{schedule['id']}/run-now"
            )
            assert fired.status_code == 200, fired.text
            assert fired.json()["data"]["status"] == "enqueued"

            metrics = await client.get("/metrics")
            assert metrics.status_code == 200, metrics.text
            assert 'qa_automation_tasks{state="running"} 1.0' in metrics.text
            assert 'qa_automation_tasks{state="queued"} 1.0' in metrics.text
            assert 'qa_devices{state="leased"} 1.0' in metrics.text
            assert (
                'qa_provider_requests_total{operation="test_connection",outcome="succeeded",provider="local"} 1.0'
                in metrics.text
            )
            assert (
                'qa_provider_requests_total{operation="trigger",outcome="succeeded",provider="local"} 1.0'
                in metrics.text
            )
    finally:
        await shutdown_app(application)

    restarted = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=restarted),
            base_url="http://testserver",
        ) as client:
            connections = await client.get("/api/v1/integrations/connections")
            tasks = await client.get("/api/v1/automation/tasks")
            devices = await client.get("/api/v1/automation/devices")
            schedules = await client.get("/api/v1/automation/schedules")

        assert connections.status_code == 200
        assert tasks.status_code == 200
        assert devices.status_code == 200
        assert schedules.status_code == 200
        assert len(connections.json()["data"]) == 1
        assert len(tasks.json()["data"]) == 2
        assert len(devices.json()["data"]) == 1
        assert len(schedules.json()["data"]) == 1
    finally:
        await shutdown_app(restarted)


@pytest.mark.asyncio
async def test_runtime_routes_enforce_viewer_read_only_permissions(
    tmp_path: Path,
) -> None:
    settings = runtime_settings(
        tmp_path,
        auth_enabled=True,
        database_name="runtime-rbac.db",
    )
    application = create_app(settings)
    transport = ASGITransport(app=application)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://testserver",
        ) as admin:
            setup = await admin.post(
                "/api/v1/auth/setup",
                json={
                    "username": "admin",
                    "display_name": "Local Admin",
                    "password": ADMIN_PASSWORD,
                },
            )
            assert setup.status_code == 200, setup.text
            admin_csrf = setup.json()["data"]["csrf_token"]
            created = await admin.post(
                "/api/v1/users",
                headers={"X-CSRF-Token": admin_csrf},
                json={
                    "username": "viewer",
                    "display_name": "Read Only",
                    "password": VIEWER_PASSWORD,
                    "role": "viewer",
                },
            )
            assert created.status_code == 201, created.text

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as viewer:
                login = await viewer.post(
                    "/api/v1/auth/login",
                    json={"username": "viewer", "password": VIEWER_PASSWORD},
                )
                assert login.status_code == 200, login.text
                viewer_csrf = login.json()["data"]["csrf_token"]

                assert (
                    await viewer.get("/api/v1/integrations/connections")
                ).status_code == 200
                assert (
                    await viewer.get("/api/v1/automation/devices")
                ).status_code == 200
                assert (
                    await viewer.get("/api/v1/automation/schedules")
                ).status_code == 200

                denied_connection = await viewer.post(
                    "/api/v1/integrations/connections",
                    headers={"X-CSRF-Token": viewer_csrf},
                    json={
                        "name": "Must not be created",
                        "kind": "local",
                        "definition_ref": "denied",
                    },
                )
                denied_device = await viewer.post(
                    "/api/v1/automation/devices",
                    headers={"X-CSRF-Token": viewer_csrf},
                    json={"name": "Denied", "agent_id": "denied-agent"},
                )
                denied_schedule = await viewer.post(
                    "/api/v1/automation/schedules",
                    headers={"X-CSRF-Token": viewer_csrf},
                    json={
                        "name": "Denied",
                        "task_type": "qa.quality.generate",
                        "cron": "* * * * *",
                    },
                )

                assert denied_connection.status_code == 403
                assert denied_device.status_code == 403
                assert denied_schedule.status_code == 403
    finally:
        await shutdown_app(application)
