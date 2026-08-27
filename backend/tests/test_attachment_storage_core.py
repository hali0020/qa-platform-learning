from __future__ import annotations

from io import BytesIO
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.services.attachment_storage import (
    AttachmentStorage,
    AttachmentStorageIntegrityError,
)
from app.services.local_attachment_storage import LocalAttachmentStorage


def upload(filename: str, content: bytes, media_type: str) -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": media_type}),
    )


@pytest.mark.asyncio
async def test_local_storage_implements_port_streams_and_compensates_delete(
    tmp_path: Path,
) -> None:
    storage = LocalAttachmentStorage(
        tmp_path / "uploads",
        max_bytes=1024,
        max_image_pixels=1_000_000,
    )
    assert isinstance(storage, AttachmentStorage)
    assert storage.backend_name == "local_filesystem"
    assert storage.namespace == ""

    attachment_id = uuid4()
    stored = await storage.save(
        upload("lesson.log", b"offline attachment\n", "text/plain"),
        attachment_id,
    )
    content = await storage.open(
        stored.storage_key,
        expected_size=stored.size_bytes,
        expected_sha256=stored.sha256,
    )
    assert content.size_bytes == stored.size_bytes
    assert content.sha256 == stored.sha256
    assert b"".join([chunk async for chunk in content]) == b"offline attachment\n"
    await content.aclose()

    receipt = await storage.quarantine(stored.storage_key, attachment_id)
    assert not storage.path_for_key(stored.storage_key, must_exist=False).exists()
    await storage.restore(receipt)
    assert storage.path_for_key(stored.storage_key).is_file()

    await storage.discard(stored.storage_key)
    assert not storage.path_for_key(stored.storage_key, must_exist=False).exists()
    await storage.aclose()


@pytest.mark.asyncio
async def test_local_storage_open_rejects_relational_metadata_mismatch(
    tmp_path: Path,
) -> None:
    storage = LocalAttachmentStorage(
        tmp_path / "uploads",
        max_bytes=1024,
        max_image_pixels=1_000_000,
    )
    stored = await storage.save(
        upload("lesson.txt", b"trusted", "text/plain"),
        uuid4(),
    )

    with pytest.raises(AttachmentStorageIntegrityError):
        await storage.open(stored.storage_key, expected_size=stored.size_bytes + 1)
    with pytest.raises(AttachmentStorageIntegrityError):
        await storage.open(stored.storage_key, expected_sha256="0" * 64)
