from __future__ import annotations

import hashlib
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.main import create_app


ADMIN_PASSWORD = "LocalAdmin!12345"
VIEWER_PASSWORD = "LocalViewer!1234"


def artifact_settings(tmp_path: Path) -> Settings:
    return Settings(
        app_env="test",
        auth_enabled=True,
        database_url=(
            "sqlite+aiosqlite:///"
            + (tmp_path / "runtime-artifact-api.db").as_posix()
        ),
        upload_root=str(tmp_path / "artifact-uploads"),
        password_time_cost=1,
        password_memory_cost_kib=1024,
        password_parallelism=1,
    )


async def _create_dispatched_local_run(
    client: AsyncClient,
    csrf_token: str,
) -> tuple[str, str]:
    connection_response = await client.post(
        "/api/v1/integrations/connections",
        headers={"X-CSRF-Token": csrf_token},
        json={
            "name": "Local artifact API lesson",
            "kind": "local",
            "definition_ref": "artifact-api-pipeline",
            "enabled": True,
        },
    )
    assert connection_response.status_code == 201, connection_response.text
    connection_id = connection_response.json()["data"]["id"]

    trigger_response = await client.post(
        f"/api/v1/integrations/connections/{connection_id}/trigger",
        headers={"X-CSRF-Token": csrf_token},
        json={"correlation_id": "artifact-api-run", "variables": {}},
    )
    assert trigger_response.status_code == 202, trigger_response.text
    run = trigger_response.json()["data"]
    assert run["dispatch_status"] == "pending"

    dispatch_response = await client.post(
        "/api/v1/integrations/connections/trigger-intents/dispatch-one",
        headers={"X-CSRF-Token": csrf_token},
        json={"worker_id": "artifact-api-dispatcher", "lease_seconds": 30},
    )
    assert dispatch_response.status_code == 200, dispatch_response.text
    dispatched = dispatch_response.json()["data"]
    assert dispatched["id"] == run["id"]
    assert dispatched["dispatch_status"] == "dispatched"
    return connection_id, run["id"]


async def _upload(
    client: AsyncClient,
    *,
    url: str,
    csrf_token: str,
    filename: str,
    content: bytes,
    media_type: str,
    kind: str = "test_report",
):
    return await client.post(
        url,
        headers={"X-CSRF-Token": csrf_token},
        data={"kind": kind},
        files={"file": (filename, content, media_type)},
    )


@pytest.mark.asyncio
async def test_artifact_api_local_storage_lifecycle_and_viewer_read_only(
    tmp_path: Path,
) -> None:
    application = create_app(artifact_settings(tmp_path))
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

            viewer_created = await admin.post(
                "/api/v1/users",
                headers={"X-CSRF-Token": admin_csrf},
                json={
                    "username": "artifact-viewer",
                    "display_name": "Artifact Viewer",
                    "password": VIEWER_PASSWORD,
                    "role": "viewer",
                },
            )
            assert viewer_created.status_code == 201, viewer_created.text

            connection_id, run_id = await _create_dispatched_local_run(
                admin,
                admin_csrf,
            )
            artifacts_url = (
                f"/api/v1/integrations/connections/{connection_id}"
                f"/runs/{run_id}/artifacts"
            )

            json_report = b'{"tests": 4, "passed": 3, "failed": 1}'
            json_upload = await _upload(
                admin,
                url=artifacts_url,
                csrf_token=admin_csrf,
                filename="summary.json",
                content=json_report,
                media_type="application/json",
            )
            assert json_upload.status_code == 201, json_upload.text
            json_artifact = json_upload.json()["data"]
            assert json_artifact["status"] == "ready"
            assert json_artifact["kind"] == "test_report"
            assert json_artifact["size_bytes"] == len(json_report)
            assert json_artifact["sha256"] == hashlib.sha256(json_report).hexdigest()

            junit_report = (
                b'<?xml version="1.0" encoding="UTF-8"?>'
                b'<testsuite name="smoke" tests="1" failures="0">'
                b'<testcase classname="qa.Login" name="valid login"/>'
                b"</testsuite>"
            )
            xml_upload = await _upload(
                admin,
                url=artifacts_url,
                csrf_token=admin_csrf,
                filename="junit.xml",
                content=junit_report,
                media_type="application/xml",
            )
            assert xml_upload.status_code == 201, xml_upload.text
            xml_artifact = xml_upload.json()["data"]
            assert xml_artifact["status"] == "ready"
            assert xml_artifact["sha256"] == hashlib.sha256(junit_report).hexdigest()

            listed = await admin.get(artifacts_url)
            assert listed.status_code == 200, listed.text
            assert [item["id"] for item in listed.json()["data"]] == [
                json_artifact["id"],
                xml_artifact["id"],
            ]

            content_url = f"{artifacts_url}/{json_artifact['id']}/content"
            downloaded = await admin.get(content_url)
            assert downloaded.status_code == 200, downloaded.text
            assert downloaded.content == json_report
            assert downloaded.headers["content-type"].startswith("application/json")
            assert downloaded.headers["content-length"] == str(len(json_report))
            assert downloaded.headers["x-content-type-options"] == "nosniff"
            assert downloaded.headers["cache-control"] == "private, no-store"
            assert downloaded.headers["content-security-policy"] == (
                "sandbox; default-src 'none'"
            )
            assert downloaded.headers["content-disposition"] == (
                'attachment; filename="summary.json"'
            )
            assert downloaded.headers["x-artifact-sha256"] == hashlib.sha256(
                json_report
            ).hexdigest()

            dtd_report = (
                b'<?xml version="1.0"?>'
                b'<!DOCTYPE testsuite [<!ENTITY xxe SYSTEM "file:///etc/passwd">]>'
                b'<testsuite name="unsafe"><testcase name="&xxe;"/></testsuite>'
            )
            dtd_rejected = await _upload(
                admin,
                url=artifacts_url,
                csrf_token=admin_csrf,
                filename="unsafe.xml",
                content=dtd_report,
                media_type="application/xml",
            )
            assert dtd_rejected.status_code == 400, dtd_rejected.text
            assert "DTD" in dtd_rejected.json()["message"]

            wrong_root = await _upload(
                admin,
                url=artifacts_url,
                csrf_token=admin_csrf,
                filename="not-junit.xml",
                content=b'<report name="not-junit"/>',
                media_type="application/xml",
            )
            assert wrong_root.status_code == 400, wrong_root.text
            assert "JUnit" in wrong_root.json()["message"]

            unsupported_report = await _upload(
                admin,
                url=artifacts_url,
                csrf_token=admin_csrf,
                filename="report.txt",
                content=b"plain test output",
                media_type="text/plain",
            )
            assert unsupported_report.status_code == 400, unsupported_report.text
            assert "JSON" in unsupported_report.json()["message"]
            assert "JUnit XML" in unsupported_report.json()["message"]

            ordinary_xml = b'<report name="ordinary-artifact"/>'
            ordinary_upload = await _upload(
                admin,
                url=artifacts_url,
                csrf_token=admin_csrf,
                filename="ordinary.xml",
                content=ordinary_xml,
                media_type="application/xml",
                kind="artifact",
            )
            assert ordinary_upload.status_code == 201, ordinary_upload.text
            ordinary_artifact = ordinary_upload.json()["data"]
            assert ordinary_artifact["kind"] == "artifact"
            assert ordinary_artifact["status"] == "ready"
            ordinary_content = await admin.get(
                f"{artifacts_url}/{ordinary_artifact['id']}/content"
            )
            assert ordinary_content.status_code == 200, ordinary_content.text
            assert ordinary_content.content == ordinary_xml

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as viewer:
                login = await viewer.post(
                    "/api/v1/auth/login",
                    json={
                        "username": "artifact-viewer",
                        "password": VIEWER_PASSWORD,
                    },
                )
                assert login.status_code == 200, login.text
                viewer_csrf = login.json()["data"]["csrf_token"]

                viewer_list = await viewer.get(artifacts_url)
                assert viewer_list.status_code == 200, viewer_list.text
                viewer_download = await viewer.get(content_url)
                assert viewer_download.status_code == 200, viewer_download.text
                assert viewer_download.content == json_report

                denied_upload = await _upload(
                    viewer,
                    url=artifacts_url,
                    csrf_token=viewer_csrf,
                    filename="viewer.json",
                    content=b'{"must": "not be stored"}',
                    media_type="application/json",
                )
                assert denied_upload.status_code == 403, denied_upload.text

                denied_delete = await viewer.delete(
                    f"{artifacts_url}/{json_artifact['id']}",
                    headers={"X-CSRF-Token": viewer_csrf},
                )
                assert denied_delete.status_code == 403, denied_delete.text

            deleted = await admin.delete(
                f"{artifacts_url}/{json_artifact['id']}",
                headers={"X-CSRF-Token": admin_csrf},
            )
            assert deleted.status_code == 200, deleted.text
            assert deleted.json()["data"]["status"] == "deleted"
            assert deleted.json()["data"]["deleted_at"] is not None

            deleted_content = await admin.get(content_url)
            assert deleted_content.status_code == 404, deleted_content.text

            final_list = await admin.get(artifacts_url)
            assert final_list.status_code == 200, final_list.text
            states = {
                item["id"]: item["status"] for item in final_list.json()["data"]
            }
            assert states[json_artifact["id"]] == "deleted"
            assert states[xml_artifact["id"]] == "ready"
    finally:
        await application.state.container.shutdown()
