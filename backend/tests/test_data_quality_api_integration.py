from __future__ import annotations

import csv
import json
import sqlite3
from contextlib import asynccontextmanager
from io import StringIO
from pathlib import Path

import pytest
from alembic import command
from alembic.config import Config
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.data_transfer.templates import CSV_HEADERS
from app.main import create_app
from app.schemas.data_transfer import TransferEntity


BACKEND_ROOT = Path(__file__).resolve().parents[1]
ADMIN_PASSWORD = "LocalAdmin!12345"
LIMITED_PASSWORD = "LocalLimited!1234"


def _migrated_settings(
    tmp_path: Path,
    *,
    database_name: str,
    auth_enabled: bool,
) -> Settings:
    database_path = tmp_path / database_name
    config = Config(str(BACKEND_ROOT / "alembic.ini"))
    config.set_main_option("script_location", str(BACKEND_ROOT / "alembic"))
    config.attributes["database_url"] = (
        f"sqlite+aiosqlite:///{database_path.as_posix()}"
    )
    command.upgrade(config, "head")
    return Settings(
        app_env="test",
        auth_enabled=auth_enabled,
        database_url=f"sqlite+aiosqlite:///{database_path.as_posix()}",
        upload_root=str(tmp_path / f"{database_path.stem}-uploads"),
        password_time_cost=1,
        password_memory_cost_kib=1024,
        password_parallelism=1,
    )


@pytest.fixture
def local_settings(tmp_path: Path) -> Settings:
    return _migrated_settings(
        tmp_path,
        database_name="data-quality-local.db",
        auth_enabled=False,
    )


@pytest.fixture
def rbac_settings(tmp_path: Path) -> Settings:
    settings = _migrated_settings(
        tmp_path,
        database_name="data-quality-rbac.db",
        auth_enabled=True,
    )
    # Built-in roles deliberately all have reports.read.  This test-only role
    # proves that both route dependencies reject a genuinely low-privilege
    # principal instead of accidentally relying on a role-name convention.
    with sqlite3.connect(settings.database_path) as connection:
        connection.execute(
            """
            INSERT INTO roles (key, name, description, is_builtin)
            VALUES (?, ?, ?, ?)
            """,
            (
                "limited",
                "受限测试角色",
                "没有导入或报表权限，仅用于 API 集成测试",
                False,
            ),
        )
        connection.commit()
    return settings


@asynccontextmanager
async def _client_for(settings: Settings):
    application = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            yield application, client
    finally:
        await application.state.container.shutdown()


def _test_case_csv(rows: list[dict[str, object]]) -> bytes:
    stream = StringIO(newline="")
    stream.write("\ufeff")
    writer = csv.DictWriter(
        stream,
        fieldnames=CSV_HEADERS[TransferEntity.TEST_CASES],
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


def _mixed_csv() -> bytes:
    return _test_case_csv(
        [
            {
                "template_version": "1",
                "row_key": "INVALID-1",
                "title": "",
                "steps_json": "[]",
            },
            {
                "template_version": "1",
                "row_key": "VALID-1",
                "title": "异步 HTTP 登录接口",
                "priority": "P1",
                "case_type": "automated",
                "tags": "http;smoke",
                "steps_json": json.dumps(
                    [
                        {
                            "action": "并发提交登录请求",
                            "expected_result": "请求均异步完成",
                        }
                    ],
                    ensure_ascii=False,
                ),
            },
        ]
    )


async def _create_project(
    client: AsyncClient,
    *,
    csrf_token: str | None = None,
) -> str:
    headers = {"X-CSRF-Token": csrf_token} if csrf_token else None
    response = await client.post(
        "/api/v1/projects",
        headers=headers,
        json={"key": "DQAPI", "name": "数据与质量 API"},
    )
    assert response.status_code == 201, response.text
    return response.json()["data"]["id"]


@pytest.mark.asyncio
async def test_migrated_sqlite_data_transfer_and_quality_report_end_to_end(
    local_settings: Settings,
) -> None:
    content = _mixed_csv()
    async with _client_for(local_settings) as (_, client):
        project_id = await _create_project(client)

        template = await client.get(
            "/api/v1/data-transfer/templates/test-cases",
            params={"format": "csv"},
        )
        assert template.status_code == 200, template.text
        assert template.content.startswith(b"\xef\xbb\xbf")
        assert tuple(
            next(csv.reader(StringIO(template.content.decode("utf-8-sig"))))
        ) == CSV_HEADERS[TransferEntity.TEST_CASES]
        assert template.headers["cache-control"] == "no-store"

        preview = await client.post(
            "/api/v1/data-transfer/imports/test-cases/preview",
            data={"project_id": project_id},
            files={"file": ("cases.csv", content, "text/csv")},
        )
        assert preview.status_code == 200, preview.text
        preview_data = preview.json()["data"]
        assert preview_data["total_rows"] == 2
        assert preview_data["valid_rows"] == 1
        assert preview_data["invalid_rows"] == 1
        assert preview_data["can_commit_clean"] is False
        assert preview_data["can_commit_partial"] is True

        blocked = await client.post(
            "/api/v1/data-transfer/imports/test-cases",
            data={
                "project_id": project_id,
                "expected_sha256": preview_data["sha256"],
                "require_clean": "true",
            },
            files={"file": ("cases.csv", content, "text/csv")},
        )
        assert blocked.status_code == 200, blocked.text
        blocked_data = blocked.json()["data"]
        assert blocked_data["committed"] is False
        assert blocked_data["created_rows"] == 0
        assert blocked_data["skipped_rows"] == 2
        after_block = await client.get(
            "/api/v1/test-cases",
            params={"project_id": project_id},
        )
        assert after_block.status_code == 200
        assert after_block.json()["data"] == []

        partial = await client.post(
            "/api/v1/data-transfer/imports/test-cases",
            data={
                "project_id": project_id,
                "expected_sha256": preview_data["sha256"],
                "require_clean": "false",
            },
            files={"file": ("cases.csv", content, "text/csv")},
        )
        assert partial.status_code == 201, partial.text
        partial_data = partial.json()["data"]
        assert partial_data["atomic"] is False
        assert partial_data["created_rows"] == 1
        assert partial_data["skipped_rows"] == 1
        assert [row["status"] for row in partial_data["rows"]] == [
            "skipped",
            "created",
        ]

        exported = await client.get(
            "/api/v1/data-transfer/exports/test-cases",
            params={"project_id": project_id, "format": "csv"},
        )
        assert exported.status_code == 200, exported.text
        assert exported.headers["x-export-count"] == "1"
        assert "filename*=UTF-8''test-cases-" in exported.headers[
            "content-disposition"
        ]
        rows = list(
            csv.DictReader(StringIO(exported.content.decode("utf-8-sig")))
        )
        assert len(rows) == 1
        assert rows[0]["title"] == "异步 HTTP 登录接口"
        assert rows[0]["status"] == "draft"

        report = await client.get(
            "/api/v1/quality/report",
            params={
                "project_id": project_id,
                "date_from": "2026-08-01",
                "date_to": "2026-08-02",
                "granularity": "day",
                "timezone": "UTC",
            },
        )
        assert report.status_code == 200, report.text
        report_data = report.json()["data"]
        assert report_data["summary"]["project_id"] == project_id
        assert report_data["summary"]["test_cases"]["total_current"] == 1
        assert report_data["summary"]["test_cases"]["active_current"] == 0
        assert report_data["granularity"] == "day"
        assert len(report_data["trends"]) == 2


@pytest.mark.asyncio
async def test_import_and_report_rbac_distinguishes_csrf_from_permissions(
    rbac_settings: Settings,
) -> None:
    application = create_app(rbac_settings)
    transport = ASGITransport(app=application)
    content = _test_case_csv(
        [
            {
                "template_version": "1",
                "row_key": "RBAC-1",
                "title": "RBAC 预检",
                "steps_json": "[]",
            }
        ]
    )
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
            project_id = await _create_project(admin, csrf_token=admin_csrf)

            template_allowed = await admin.get(
                "/api/v1/data-transfer/templates/test-cases",
                params={"format": "csv"},
            )
            report_allowed = await admin.get(
                "/api/v1/quality/report",
                params={
                    "project_id": project_id,
                    "date_from": "2026-08-01",
                    "date_to": "2026-08-01",
                    "timezone": "UTC",
                },
            )
            assert template_allowed.status_code == 200
            assert report_allowed.status_code == 200

            missing_csrf = await admin.post(
                "/api/v1/data-transfer/imports/test-cases/preview",
                data={"project_id": project_id},
                files={"file": ("cases.csv", content, "text/csv")},
            )
            assert missing_csrf.status_code == 403
            assert missing_csrf.json()["message"] == "CSRF 校验失败"

            preview_allowed = await admin.post(
                "/api/v1/data-transfer/imports/test-cases/preview",
                headers={"X-CSRF-Token": admin_csrf},
                data={"project_id": project_id},
                files={"file": ("cases.csv", content, "text/csv")},
            )
            assert preview_allowed.status_code == 200, preview_allowed.text

            created_limited = await admin.post(
                "/api/v1/users",
                headers={"X-CSRF-Token": admin_csrf},
                json={
                    "username": "limited",
                    "display_name": "Limited User",
                    "password": LIMITED_PASSWORD,
                    "role": "limited",
                },
            )
            assert created_limited.status_code == 201, created_limited.text

            async with AsyncClient(
                transport=transport,
                base_url="http://testserver",
            ) as limited:
                login = await limited.post(
                    "/api/v1/auth/login",
                    json={
                        "username": "limited",
                        "password": LIMITED_PASSWORD,
                    },
                )
                assert login.status_code == 200, login.text
                limited_csrf = login.json()["data"]["csrf_token"]

                template_denied = await limited.get(
                    "/api/v1/data-transfer/templates/test-cases",
                    params={"format": "csv"},
                )
                report_denied = await limited.get(
                    "/api/v1/quality/report",
                    params={
                        "project_id": project_id,
                        "date_from": "2026-08-01",
                        "date_to": "2026-08-01",
                        "timezone": "UTC",
                    },
                )
                preview_denied = await limited.post(
                    "/api/v1/data-transfer/imports/test-cases/preview",
                    headers={"X-CSRF-Token": limited_csrf},
                    data={"project_id": project_id},
                    files={"file": ("cases.csv", content, "text/csv")},
                )

                assert template_denied.status_code == 403
                assert report_denied.status_code == 403
                assert preview_denied.status_code == 403
                assert preview_denied.json()["message"] == (
                    "没有执行该操作的权限"
                )
    finally:
        await application.state.container.shutdown()
