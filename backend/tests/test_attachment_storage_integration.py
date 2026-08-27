from __future__ import annotations

import hashlib
from collections.abc import AsyncIterator
from pathlib import Path
from uuid import UUID

import pytest
from fastapi import UploadFile
from httpx import ASGITransport, AsyncClient

from app.core.config import Settings
from app.core.errors import NotFoundError
from app.main import create_app
from app.services.attachment_storage import (
    AttachmentStorageIntegrityError,
    QuarantineReceipt,
    StoredContent,
    StoredUpload,
    quarantine_key,
)


PASSWORD = "LocalStorage!12345"


class FakeObjectStorage:
    """In-memory storage port; it never resolves DNS or opens a socket."""

    backend_name = "s3_local_container"
    namespace = "qa-artifacts"

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.trash: dict[str, bytes] = {}
        self.open_count = 0
        self.closed_streams = 0
        self.closed = False

    async def save(self, upload: UploadFile, attachment_id: UUID) -> StoredUpload:
        data = await upload.read()
        await upload.close()
        key = f"{attachment_id.hex[:2]}/{attachment_id.hex}"
        digest = hashlib.sha256(data).hexdigest()
        self.objects[key] = data
        return StoredUpload(
            original_filename=upload.filename or "attachment.txt",
            storage_key=key,
            media_type=(upload.content_type or "text/plain"),
            size_bytes=len(data),
            sha256=digest,
            is_image=False,
        )

    async def open(
        self,
        storage_key: str,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredContent:
        self.open_count += 1
        try:
            data = self.objects[storage_key]
        except KeyError as error:
            raise NotFoundError("附件内容", "missing") from error
        digest = hashlib.sha256(data).hexdigest()
        if expected_size is not None and expected_size != len(data):
            raise AttachmentStorageIntegrityError()
        if expected_sha256 is not None and expected_sha256 != digest:
            raise AttachmentStorageIntegrityError()

        async def body() -> AsyncIterator[bytes]:
            yield data

        async def closed() -> None:
            self.closed_streams += 1

        return StoredContent(
            body=body(),
            size_bytes=len(data),
            sha256=digest,
            close=closed,
        )

    async def discard(self, storage_key: str) -> None:
        self.objects.pop(storage_key, None)

    async def quarantine(
        self,
        storage_key: str,
        attachment_id: UUID,
    ) -> QuarantineReceipt:
        try:
            data = self.objects.pop(storage_key)
        except KeyError as error:
            raise NotFoundError("附件内容", "missing") from error
        isolated_key = quarantine_key(attachment_id)
        self.trash[isolated_key] = data
        return QuarantineReceipt(
            _backend_name=self.backend_name,
            _namespace=self.namespace,
            _original_key=storage_key,
            _quarantine_key=isolated_key,
        )

    async def restore(self, receipt: QuarantineReceipt) -> None:
        self.objects[receipt._original_key] = self.trash.pop(
            receipt._quarantine_key
        )

    async def aclose(self) -> None:
        self.closed = True


def settings(
    tmp_path: Path,
    *,
    object_storage: bool,
) -> Settings:
    values: dict[str, object] = {
        "app_env": "local-container" if object_storage else "test",
        "database_url": (
            f"sqlite+aiosqlite:///{(tmp_path / 'attachments.db').as_posix()}"
        ),
        "upload_root": str(tmp_path / "uploads"),
        "password_time_cost": 2 if object_storage else 1,
        "password_memory_cost_kib": 19_456 if object_storage else 1_024,
        "password_parallelism": 1,
    }
    if object_storage:
        values.update(
            {
                "object_storage_runtime_mode": "s3_local_container",
                "object_storage_endpoint_url": "http://seaweedfs:8333",
                "object_storage_bucket": "qa-artifacts",
                "object_storage_region": "us-east-1",
                "object_storage_access_key": "qa-integration-access",
                "object_storage_secret_key": "qa-integration-secret",
            }
        )
    return Settings(**values)


async def setup_and_upload(client: AsyncClient) -> dict:
    setup = await client.post(
        "/api/v1/auth/setup",
        json={
            "username": "storage-admin",
            "display_name": "Storage Admin",
            "password": PASSWORD,
        },
    )
    assert setup.status_code == 200, setup.text
    csrf = setup.json()["data"]["csrf_token"]
    headers = {"X-CSRF-Token": csrf}
    project_response = await client.post(
        "/api/v1/projects",
        headers=headers,
        json={"key": "STORAGE", "name": "Storage lab"},
    )
    project = project_response.json()["data"]
    defect_response = await client.post(
        "/api/v1/defects",
        headers=headers,
        json={
            "project_id": project["id"],
            "title": "Object storage lesson",
        },
    )
    defect = defect_response.json()["data"]
    upload_response = await client.post(
        "/api/v1/attachments",
        headers=headers,
        data={
            "project_id": project["id"],
            "entity_type": "defect",
            "entity_id": defect["id"],
        },
        files={"file": ("lesson.log", b"local S3 lesson\n", "text/plain")},
    )
    assert upload_response.status_code == 201, upload_response.text
    return {
        "attachment": upload_response.json()["data"],
        "csrf": csrf,
    }


@pytest.mark.asyncio
async def test_s3_mode_proxies_content_and_keeps_routing_private(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    fake = FakeObjectStorage()
    import app.services.s3_attachment_storage as s3_module

    monkeypatch.setattr(
        s3_module,
        "S3AttachmentStorage",
        lambda **_kwargs: fake,
    )
    application = create_app(settings(tmp_path, object_storage=True))
    async with application.router.lifespan_context(application):
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            created = await setup_and_upload(client)
            attachment = created["attachment"]
            assert "storage_key" not in attachment
            assert "storage_backend" not in attachment
            assert "storage_namespace" not in attachment
            assert len(fake.objects) == 1

            response = await client.get(
                f"/api/v1/attachments/{attachment['id']}/content"
            )
            assert response.status_code == 200
            assert response.content == b"local S3 lesson\n"
            assert response.headers["content-length"] == str(len(response.content))
            assert fake.open_count == 1
            assert fake.closed_streams == 1

            deleted = await client.delete(
                f"/api/v1/attachments/{attachment['id']}",
                headers={"X-CSRF-Token": created["csrf"]},
            )
            assert deleted.status_code == 200
            assert fake.objects == {}
            assert len(fake.trash) == 1
    assert fake.closed is True


@pytest.mark.asyncio
async def test_s3_mode_can_read_legacy_local_attachment(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    local_application = create_app(settings(tmp_path, object_storage=False))
    async with local_application.router.lifespan_context(local_application):
        async with AsyncClient(
            transport=ASGITransport(app=local_application),
            base_url="http://testserver",
        ) as client:
            created = await setup_and_upload(client)
            attachment_id = created["attachment"]["id"]

    fake = FakeObjectStorage()
    import app.services.s3_attachment_storage as s3_module

    monkeypatch.setattr(
        s3_module,
        "S3AttachmentStorage",
        lambda **_kwargs: fake,
    )
    object_application = create_app(settings(tmp_path, object_storage=True))
    async with object_application.router.lifespan_context(object_application):
        async with AsyncClient(
            transport=ASGITransport(app=object_application),
            base_url="http://testserver",
        ) as client:
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "storage-admin", "password": PASSWORD},
            )
            assert login.status_code == 200, login.text
            response = await client.get(
                f"/api/v1/attachments/{attachment_id}/content"
            )
            assert response.status_code == 200
            assert response.content == b"local S3 lesson\n"
            assert fake.open_count == 0
