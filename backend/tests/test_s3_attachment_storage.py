from __future__ import annotations

import asyncio
import builtins
import socket
from contextlib import AbstractAsyncContextManager
from dataclasses import dataclass
from io import BytesIO
from pathlib import Path
from typing import Any
from uuid import uuid4

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.services.attachment_storage import (
    AttachmentStorageIntegrityError,
    AttachmentStorageUnavailableError,
)
from app.services.s3_attachment_storage import S3AttachmentStorage


class FakeClientError(Exception):
    def __init__(self, code: str = "NoSuchKey") -> None:
        self.response = {
            "Error": {"Code": code},
            "ResponseMetadata": {"HTTPStatusCode": 404},
        }
        super().__init__(code)


@dataclass
class FakeObject:
    body: bytes
    metadata: dict[str, str]
    content_type: str


class FakeBody:
    def __init__(self, content: bytes) -> None:
        self._content = content
        self._offset = 0
        self.closed = False

    async def read(self, amount: int = -1) -> bytes:
        if amount < 0:
            amount = len(self._content)
        chunk = self._content[self._offset : self._offset + amount]
        self._offset += len(chunk)
        return chunk

    def close(self) -> None:
        self.closed = True


class BlockingCloseBody(FakeBody):
    def __init__(self, content: bytes) -> None:
        super().__init__(content)
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()

    async def close(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.closed = True


class FakeS3Client:
    def __init__(self) -> None:
        self.objects: dict[tuple[str, str], FakeObject] = {}
        self.uploads: dict[tuple[str, str, str], dict[str, Any]] = {}
        self.calls: list[tuple[str, str]] = []
        self.bodies: list[FakeBody] = []
        self.body_factory = FakeBody
        self.block_head = False
        self.head_started = asyncio.Event()
        self.fail_part_number: int | None = None
        self.fail_complete = False
        self.fail_abort = False
        self.fail_delete_keys: set[str] = set()
        self._upload_sequence = 0

    async def create_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        self.calls.append(("create_multipart", key))
        self._upload_sequence += 1
        upload_id = f"upload-{self._upload_sequence}"
        self.uploads[(bucket, key, upload_id)] = {
            "parts": {},
            "metadata": dict(kwargs["Metadata"]),
            "content_type": kwargs["ContentType"],
        }
        return {"UploadId": upload_id}

    async def upload_part(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key, upload_id = kwargs["Bucket"], kwargs["Key"], kwargs["UploadId"]
        part_number = kwargs["PartNumber"]
        self.calls.append(("upload_part", key))
        if part_number == self.fail_part_number:
            raise RuntimeError(
                "https://seaweedfs:8333 leaked-secret leaked-storage-key"
            )
        body = bytes(kwargs["Body"])
        assert kwargs["ContentLength"] == len(body)
        self.uploads[(bucket, key, upload_id)]["parts"][part_number] = body
        return {"ETag": f'"etag-{part_number}"'}

    async def complete_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key, upload_id = kwargs["Bucket"], kwargs["Key"], kwargs["UploadId"]
        self.calls.append(("complete_multipart", key))
        if self.fail_complete:
            raise RuntimeError("complete leaked-secret leaked-storage-key")
        state = self.uploads.pop((bucket, key, upload_id))
        ordered = kwargs["MultipartUpload"]["Parts"]
        body = b"".join(state["parts"][part["PartNumber"]] for part in ordered)
        self.objects[(bucket, key)] = FakeObject(
            body=body,
            metadata=dict(state["metadata"]),
            content_type=state["content_type"],
        )
        return {}

    async def abort_multipart_upload(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key, upload_id = kwargs["Bucket"], kwargs["Key"], kwargs["UploadId"]
        self.calls.append(("abort_multipart", key))
        if self.fail_abort:
            raise RuntimeError("abort leaked-secret leaked-storage-key")
        self.uploads.pop((bucket, key, upload_id), None)
        return {}

    async def get_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        self.calls.append(("get", key))
        item = self._require(bucket, key)
        body = self.body_factory(item.body)
        self.bodies.append(body)
        return {
            "Body": body,
            "ContentLength": len(item.body),
            "Metadata": dict(item.metadata),
        }

    async def head_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        self.calls.append(("head", key))
        if self.block_head and not key.startswith(".trash/"):
            self.head_started.set()
            await asyncio.Event().wait()
        item = self._require(bucket, key)
        return {
            "ContentLength": len(item.body),
            "Metadata": dict(item.metadata),
        }

    async def copy_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        source = kwargs["CopySource"]
        self.calls.append(("copy", key))
        item = self._require(source["Bucket"], source["Key"])
        self.objects[(bucket, key)] = FakeObject(
            body=item.body,
            metadata=dict(item.metadata),
            content_type=item.content_type,
        )
        return {}

    async def delete_object(self, **kwargs: Any) -> dict[str, Any]:
        bucket, key = kwargs["Bucket"], kwargs["Key"]
        self.calls.append(("delete", key))
        if key in self.fail_delete_keys:
            raise RuntimeError("delete leaked-secret leaked-storage-key")
        self.objects.pop((bucket, key), None)
        return {}

    def _require(self, bucket: str, key: str) -> FakeObject:
        try:
            return self.objects[(bucket, key)]
        except KeyError as error:
            raise FakeClientError() from error


class FakeClientContext(AbstractAsyncContextManager[FakeS3Client]):
    def __init__(self, client: FakeS3Client) -> None:
        self.client = client
        self.entered = 0
        self.exited = 0

    async def __aenter__(self) -> FakeS3Client:
        self.entered += 1
        return self.client

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1


class BlockingEnterContext(AbstractAsyncContextManager[FakeS3Client]):
    def __init__(self, client: FakeS3Client) -> None:
        self.client = client
        self.enter_started = asyncio.Event()
        self.release_enter = asyncio.Event()
        self.exited = 0

    async def __aenter__(self) -> FakeS3Client:
        self.enter_started.set()
        await self.release_enter.wait()
        return self.client

    async def __aexit__(self, *_args: object) -> None:
        self.exited += 1


def upload(content: bytes = b"self-hosted object\n") -> UploadFile:
    return UploadFile(
        file=BytesIO(content),
        filename="artifact.log",
        headers=Headers({"content-type": "text/plain"}),
    )


def storage(
    tmp_path: Path,
    client: FakeS3Client,
    *,
    operation_timeout_seconds: float = 1,
) -> tuple[S3AttachmentStorage, FakeClientContext]:
    context = FakeClientContext(client)
    adapter = S3AttachmentStorage(
        app_env="local-container",
        endpoint_url="http://seaweedfs:8333",
        bucket="qa-artifacts",
        region="us-east-1",
        access_key="qa-storage-access",
        secret_key="qa-storage-secret",
        staging_root=tmp_path / "staging",
        max_bytes=1024,
        max_image_pixels=1_000_000,
        max_concurrency=2,
        operation_timeout_seconds=operation_timeout_seconds,
        client_factory=lambda: context,
    )
    return adapter, context


@pytest.mark.asyncio
async def test_fake_s3_save_stream_quarantine_restore_and_discard_never_networks(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_network(*_args: object, **_kwargs: object):
        raise AssertionError("S3 unit tests must not use DNS or sockets")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    client = FakeS3Client()
    adapter, context = storage(tmp_path, client)
    attachment_id = uuid4()

    stored = await adapter.save(upload(), attachment_id)
    assert adapter.backend_name == "s3_local_container"
    assert adapter.namespace == "qa-artifacts"
    assert context.entered == 1
    assert client.objects[("qa-artifacts", stored.storage_key)].metadata == {
        "sha256": stored.sha256
    }

    content = await adapter.open(
        stored.storage_key,
        expected_size=stored.size_bytes,
        expected_sha256=stored.sha256,
    )
    assert b"".join([chunk async for chunk in content]) == b"self-hosted object\n"
    assert client.bodies[-1].closed is True

    receipt = await adapter.quarantine(stored.storage_key, attachment_id)
    assert ("qa-artifacts", stored.storage_key) not in client.objects
    assert ("qa-artifacts", f".trash/{attachment_id.hex}") in client.objects
    await adapter.restore(receipt)
    assert ("qa-artifacts", stored.storage_key) in client.objects
    assert ("qa-artifacts", f".trash/{attachment_id.hex}") not in client.objects

    await adapter.discard(stored.storage_key)
    assert client.objects == {}
    assert not [path for path in (tmp_path / "staging").rglob("*") if path.is_file()]
    assert {operation for operation, _ in client.calls} <= {
        "create_multipart",
        "upload_part",
        "complete_multipart",
        "abort_multipart",
        "get",
        "head",
        "copy",
        "delete",
    }
    await adapter.aclose()
    assert context.exited == 1


@pytest.mark.asyncio
async def test_s3_open_rejects_remote_metadata_mismatch_and_closes_body(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    adapter, _ = storage(tmp_path, client)
    stored = await adapter.save(upload(b"trusted"), uuid4())
    client.objects[("qa-artifacts", stored.storage_key)].metadata["sha256"] = "0" * 64

    with pytest.raises(AttachmentStorageIntegrityError):
        await adapter.open(
            stored.storage_key,
            expected_size=stored.size_bytes,
            expected_sha256=stored.sha256,
        )
    assert client.bodies[-1].closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_s3_stream_rejects_body_that_no_longer_matches_trusted_digest(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    adapter, _ = storage(tmp_path, client)
    stored = await adapter.save(upload(b"trusted"), uuid4())
    remote = client.objects[("qa-artifacts", stored.storage_key)]
    remote.body = b"changed"

    content = await adapter.open(
        stored.storage_key,
        expected_size=stored.size_bytes,
        expected_sha256=stored.sha256,
    )
    with pytest.raises(AttachmentStorageIntegrityError):
        _ = b"".join([chunk async for chunk in content])
    assert client.bodies[-1].closed is True
    await adapter.aclose()


@pytest.mark.asyncio
async def test_cancelled_s3_save_removes_committed_object_and_staging_file(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    client.block_head = True
    adapter, _ = storage(tmp_path, client, operation_timeout_seconds=5)
    operation = asyncio.create_task(adapter.save(upload(b"cancel me"), uuid4()))
    await asyncio.wait_for(client.head_started.wait(), timeout=1)

    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert client.objects == {}
    assert not [path for path in (tmp_path / "staging").rglob("*") if path.is_file()]
    assert any(operation == "abort_multipart" for operation, _ in client.calls)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_multipart_part_failure_aborts_and_preserves_safe_error(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    client.fail_part_number = 1
    adapter, _ = storage(tmp_path, client)

    with pytest.raises(AttachmentStorageUnavailableError) as captured:
        await adapter.save(upload(b"part failure"), uuid4())

    rendered = repr(captured.value)
    assert "seaweedfs" not in rendered
    assert "leaked-secret" not in rendered
    assert "leaked-storage-key" not in rendered
    assert captured.value.__cause__ is None
    assert captured.value.__context__ is None
    assert client.objects == {}
    assert client.uploads == {}
    assert any(operation == "abort_multipart" for operation, _ in client.calls)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_complete_and_abort_failures_do_not_replace_original_error(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    client.fail_complete = True
    client.fail_abort = True
    adapter, _ = storage(tmp_path, client)

    with pytest.raises(AttachmentStorageUnavailableError) as captured:
        await adapter.save(upload(b"complete failure"), uuid4())

    assert "complete multipart upload" in str(captured.value)
    assert "leaked-secret" not in repr(captured.value)
    assert any(operation == "abort_multipart" for operation, _ in client.calls)
    assert not [path for path in (tmp_path / "staging").rglob("*") if path.is_file()]
    await adapter.aclose()


@pytest.mark.asyncio
async def test_quarantine_and_restore_tolerate_uncertain_source_cleanup(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    adapter, _ = storage(tmp_path, client)
    attachment_id = uuid4()
    stored = await adapter.save(upload(b"keep both copies"), attachment_id)
    trash_key = f".trash/{attachment_id.hex}"
    client.fail_delete_keys.add(stored.storage_key)

    receipt = await adapter.quarantine(stored.storage_key, attachment_id)
    assert ("qa-artifacts", stored.storage_key) in client.objects
    assert ("qa-artifacts", trash_key) in client.objects

    client.fail_delete_keys.add(trash_key)
    await adapter.restore(receipt)
    assert ("qa-artifacts", stored.storage_key) in client.objects
    assert ("qa-artifacts", trash_key) in client.objects
    await adapter.aclose()


@pytest.mark.asyncio
async def test_missing_object_returns_generic_integrity_error_without_key(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    adapter, _ = storage(tmp_path, client)
    missing_key = "ab/" + "0" * 32

    with pytest.raises(AttachmentStorageIntegrityError) as captured:
        await adapter.open(missing_key)

    assert missing_key not in str(captured.value)
    assert "seaweedfs" not in repr(captured.value)
    await adapter.aclose()


@pytest.mark.asyncio
async def test_cancelled_stream_close_finishes_body_cleanup_and_releases_permit(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    client.body_factory = BlockingCloseBody
    adapter, _ = storage(tmp_path, client)
    stored = await adapter.save(upload(b"stream close"), uuid4())
    content = await adapter.open(
        stored.storage_key,
        expected_size=stored.size_bytes,
        expected_sha256=stored.sha256,
    )
    body = client.bodies[-1]
    assert isinstance(body, BlockingCloseBody)
    closing = asyncio.create_task(content.aclose())
    await asyncio.wait_for(body.close_started.wait(), timeout=1)

    closing.cancel()
    body.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert body.closed is True
    assert adapter._semaphore._value == 2
    await adapter.aclose()


@pytest.mark.asyncio
async def test_cancelled_client_enter_closes_partially_entered_context(
    tmp_path: Path,
) -> None:
    client = FakeS3Client()
    context = BlockingEnterContext(client)
    adapter = S3AttachmentStorage(
        app_env="local-container",
        endpoint_url="http://seaweedfs:8333",
        bucket="qa-artifacts",
        region="us-east-1",
        access_key="qa-storage-access",
        secret_key="qa-storage-secret",
        staging_root=tmp_path / "staging",
        max_bytes=1024,
        max_image_pixels=1_000_000,
        operation_timeout_seconds=1,
        client_factory=lambda: context,
    )
    operation = asyncio.create_task(adapter.discard("ab/" + "0" * 32))
    await asyncio.wait_for(context.enter_started.wait(), timeout=1)

    operation.cancel()
    context.release_enter.set()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert context.exited == 1
    assert adapter._semaphore._value == 4
    await adapter.aclose()


def test_s3_adapter_is_lazy_and_rechecks_fixed_topology(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object):
        if name.startswith(("aiobotocore", "botocore")):
            raise AssertionError("constructing the adapter must not import its SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    S3AttachmentStorage(
        app_env="local-container",
        endpoint_url="http://seaweedfs:8333",
        bucket="qa-artifacts",
        region="us-east-1",
        access_key="qa-storage-access",
        secret_key="qa-storage-secret",
        staging_root=tmp_path / "staging",
        max_bytes=1024,
        max_image_pixels=1_000_000,
    )

    with pytest.raises(RuntimeError, match="APP_ENV"):
        S3AttachmentStorage(
            app_env="test",
            endpoint_url="http://seaweedfs:8333",
            bucket="qa-artifacts",
            region="us-east-1",
            access_key="qa-storage-access",
            secret_key="qa-storage-secret",
            staging_root=tmp_path / "other",
            max_bytes=1024,
            max_image_pixels=1_000_000,
        )
