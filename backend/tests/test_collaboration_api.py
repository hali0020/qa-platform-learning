import sqlite3
from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from PIL import Image
from pydantic import ValidationError

from app.core.config import Settings
from app.domain.collaboration import Attachment, CollaborationTargetType
from app.main import create_app


PASSWORD = "LocalAdmin!12345"


def make_settings(tmp_path: Path, **overrides) -> Settings:
    values = {
        "app_env": "test",
        "database_url": f"sqlite+aiosqlite:///{(tmp_path / 'collab.db').as_posix()}",
        "upload_root": str(tmp_path / "uploads"),
        "password_time_cost": 1,
        "password_memory_cost_kib": 1024,
        "password_parallelism": 1,
    }
    values.update(overrides)
    return Settings(**values)


async def setup_workspace(client: AsyncClient) -> tuple[dict, dict, str]:
    setup = await client.post(
        "/api/v1/auth/setup",
        json={
            "username": "admin",
            "display_name": "Local Admin",
            "password": PASSWORD,
        },
    )
    assert setup.status_code == 200, setup.text
    csrf = setup.json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    project_response = await client.post(
        "/api/v1/projects",
        headers=headers,
        json={"key": "COLLAB", "name": "Collaboration"},
    )
    assert project_response.status_code == 201
    project = project_response.json()["data"]
    defect_response = await client.post(
        "/api/v1/defects",
        headers=headers,
        json={
            "project_id": project["id"],
            "title": "本地协作缺陷",
            "reporter": "spoofed-client-user",
        },
    )
    assert defect_response.status_code == 201, defect_response.text
    defect = defect_response.json()["data"]
    assert defect["reporter"] == "admin"
    return project, defect, csrf


def png_bytes() -> bytes:
    output = BytesIO()
    Image.new("RGB", (8, 6), color=(35, 140, 100)).save(output, format="PNG")
    return output.getvalue()


def test_attachment_domain_accepts_only_fixed_internal_storage_routes() -> None:
    values = {
        "project_id": uuid4(),
        "entity_type": CollaborationTargetType.PROJECT,
        "entity_id": uuid4(),
        "uploader_id": uuid4(),
        "uploader_name": "Local User",
        "original_filename": "result.log",
        "storage_key": "local-storage-key",
        "media_type": "text/plain",
        "size_bytes": 3,
        "sha256": "a" * 64,
    }

    local = Attachment(**values)
    assert (local.storage_backend, local.storage_namespace) == (
        "local_filesystem",
        "",
    )
    s3 = Attachment(
        **values,
        storage_backend="s3_local_container",
        storage_namespace="qa-artifacts",
    )
    assert s3.storage_namespace == "qa-artifacts"
    with pytest.raises(ValidationError):
        Attachment(
            **values,
            storage_backend="s3_local_container",
            storage_namespace="",
        )
    with pytest.raises(ValidationError):
        Attachment(
            **values,
            storage_backend="other",
            storage_namespace="",
        )


@pytest.mark.asyncio
async def test_comment_lifecycle_parent_validation_and_authenticated_audit(
    tmp_path: Path,
) -> None:
    application = create_app(make_settings(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project, defect, csrf = await setup_workspace(client)
        headers = {"X-CSRF-Token": csrf}
        created = await client.post(
            "/api/v1/comments",
            headers=headers,
            json={
                "project_id": project["id"],
                "entity_type": "defect",
                "entity_id": defect["id"],
                "body": "请检查本机复现日志",
            },
        )
        assert created.status_code == 201, created.text
        comment = created.json()["data"]
        assert comment["author_name"] == "Local Admin"

        reply = await client.post(
            "/api/v1/comments",
            headers=headers,
            json={
                "project_id": project["id"],
                "entity_type": "defect",
                "entity_id": defect["id"],
                "parent_id": comment["id"],
                "body": "已检查",
            },
        )
        assert reply.status_code == 201

        updated = await client.patch(
            f"/api/v1/comments/{comment['id']}",
            headers=headers,
            json={"body": "请检查本机复现日志与截图"},
        )
        assert updated.status_code == 200
        assert updated.json()["data"]["edited_at"] is not None

        listed = await client.get(
            "/api/v1/comments",
            params={"entity_type": "defect", "entity_id": defect["id"]},
        )
        assert [item["id"] for item in listed.json()["data"]] == [
            comment["id"],
            reply.json()["data"]["id"],
        ]

        deleted = await client.delete(
            f"/api/v1/comments/{comment['id']}",
            headers=headers,
        )
        assert deleted.status_code == 200
        assert deleted.json()["data"]["body"] == ""
        assert deleted.json()["data"]["deleted_at"] is not None

        audit = await client.get(
            "/api/v1/audit-events",
            params={"entity_type": "comment", "entity_id": comment["id"]},
        )
        events = audit.json()["data"]
        assert [event["action"] for event in events] == [
            "deleted",
            "updated",
            "created",
        ]
        assert all(event["actor"] == "admin" for event in events)
        assert all(event["actor_user_id"] == comment["author_id"] for event in events)


@pytest.mark.asyncio
async def test_attachment_upload_download_image_normalization_and_recoverable_delete(
    tmp_path: Path,
) -> None:
    application = create_app(make_settings(tmp_path))
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project, defect, csrf = await setup_workspace(client)
        headers = {"X-CSRF-Token": csrf}
        fields = {
            "project_id": project["id"],
            "entity_type": "defect",
            "entity_id": defect["id"],
        }
        text_upload = await client.post(
            "/api/v1/attachments",
            headers=headers,
            data=fields,
            files={"file": ("local.log", b"local-only log\n", "text/plain")},
        )
        assert text_upload.status_code == 201, text_upload.text
        text_attachment = text_upload.json()["data"]
        assert {
            "storage_key",
            "storage_backend",
            "storage_namespace",
        }.isdisjoint(text_attachment)
        assert text_attachment["sha256"]
        with sqlite3.connect(tmp_path / "collab.db") as connection:
            internal_storage = connection.execute(
                """
                SELECT storage_backend, storage_namespace
                FROM attachments WHERE id = ?
                """,
                (text_attachment["id"],),
            ).fetchone()
        assert internal_storage == ("local_filesystem", "")

        downloaded = await client.get(
            f"/api/v1/attachments/{text_attachment['id']}/content"
        )
        assert downloaded.status_code == 200
        assert downloaded.content == b"local-only log\n"
        assert downloaded.headers["x-content-type-options"] == "nosniff"
        assert "attachment" in downloaded.headers["content-disposition"]

        image_upload = await client.post(
            "/api/v1/attachments",
            headers=headers,
            data=fields,
            files={"file": ("screen.png", png_bytes(), "image/png")},
        )
        assert image_upload.status_code == 201, image_upload.text
        image_attachment = image_upload.json()["data"]
        assert image_attachment["is_image"] is True
        preview = await client.get(
            f"/api/v1/attachments/{image_attachment['id']}/content",
            params={"inline": "true"},
        )
        assert preview.status_code == 200
        assert preview.headers["content-type"].startswith("image/png")
        assert "inline" in preview.headers["content-disposition"]

        listed = await client.get(
            "/api/v1/attachments",
            params={"entity_type": "defect", "entity_id": defect["id"]},
        )
        assert len(listed.json()["data"]) == 2

        deleted = await client.delete(
            f"/api/v1/attachments/{text_attachment['id']}",
            headers=headers,
        )
        assert deleted.status_code == 200
        assert deleted.json()["data"]["deleted_at"] is not None
        assert (
            await client.get(
                f"/api/v1/attachments/{text_attachment['id']}/content"
            )
        ).status_code == 404
        assert any((tmp_path / "uploads" / ".trash").iterdir())


@pytest.mark.asyncio
async def test_attachment_rejects_path_mime_spoof_oversize_and_cross_project(
    tmp_path: Path,
) -> None:
    application = create_app(make_settings(tmp_path, upload_max_bytes=1024))
    async with AsyncClient(
        transport=ASGITransport(app=application), base_url="http://testserver"
    ) as client:
        project, defect, csrf = await setup_workspace(client)
        headers = {"X-CSRF-Token": csrf}
        fields = {
            "project_id": project["id"],
            "entity_type": "defect",
            "entity_id": defect["id"],
        }
        traversal = await client.post(
            "/api/v1/attachments",
            headers=headers,
            data=fields,
            files={"file": ("../escape.txt", b"safe", "text/plain")},
        )
        assert traversal.status_code == 400
        assert not (tmp_path / "escape.txt").exists()

        active_content = await client.post(
            "/api/v1/attachments",
            headers=headers,
            data=fields,
            files={"file": ("payload.html", b"<script>x</script>", "text/html")},
        )
        assert active_content.status_code == 400

        fake_pdf = await client.post(
            "/api/v1/attachments",
            headers=headers,
            data=fields,
            files={"file": ("fake.pdf", b"not a pdf", "application/pdf")},
        )
        assert fake_pdf.status_code == 400

        oversize = await client.post(
            "/api/v1/attachments",
            headers={**headers, "Content-Length": "1"},
            data=fields,
            files={"file": ("large.log", b"x" * 1025, "text/plain")},
        )
        assert oversize.status_code == 400

        other_project = await client.post(
            "/api/v1/projects",
            headers=headers,
            json={"key": "OTHER", "name": "Other"},
        )
        cross_comment = await client.post(
            "/api/v1/comments",
            headers=headers,
            json={
                "project_id": other_project.json()["data"]["id"],
                "entity_type": "defect",
                "entity_id": defect["id"],
                "body": "错误项目关联",
            },
        )
        assert cross_comment.status_code == 409

        temp_parts = list((tmp_path / "uploads" / ".tmp").glob("*.part"))
        assert temp_parts == []
