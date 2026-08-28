from __future__ import annotations

import copy
import hashlib
from asyncio import Lock
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from datetime import datetime, timezone
from io import BytesIO
from types import SimpleNamespace
from typing import Any
from uuid import UUID

import pytest
from fastapi import UploadFile
from starlette.datastructures import Headers

from app.core.errors import BusinessValidationError, InvalidStateError
from app.domain.identity import Principal
from app.domain.models import AuditAction
from app.runtime.artifacts import (
    ArtifactKind,
    ArtifactStatus,
    ProviderRunArtifactService,
)
from app.services.attachment_storage import (
    AttachmentStorageIntegrityError,
    AttachmentStorageUnavailableError,
    AttachmentValidationProfile,
    QuarantineReceipt,
    StoredContent,
    StoredUpload,
    quarantine_key,
)


RUN_ID = "run-artifact-1"
CONNECTION_ID = "connection-artifact-1"
PRINCIPAL = Principal(
    user_id=UUID("00000000-0000-0000-0000-000000000123"),
    username="artifact-admin",
    display_name="Artifact Admin",
    roles=("system_admin",),
    permissions=frozenset({"*"}),
)


class FakeRunRecord:
    def __init__(self, *, id: str, connection_id: str) -> None:
        self.id = id
        self.connection_id = connection_id


class FakeArtifactRecord:
    def __init__(self, **values: Any) -> None:
        for key, value in values.items():
            setattr(self, key, value)


class FakeSession:
    def __init__(self, repository: "FakeRuntimeRepository") -> None:
        self.repository = repository

    async def get(self, record_type: type[Any], record_id: str) -> Any | None:
        return self.repository.records.get((record_type, str(record_id)))

    def add(self, record: Any) -> None:
        self.repository.records[(type(record), str(record.id))] = record

    async def flush(self) -> None:
        artifacts = [
            record
            for (record_type, _), record in self.repository.records.items()
            if record_type is FakeArtifactRecord
        ]
        if self.repository.fail_ready_flush_once and any(
            record.status == ArtifactStatus.READY.value for record in artifacts
        ):
            self.repository.fail_ready_flush_once = False
            raise RuntimeError("simulated ready metadata commit failure")
        if self.repository.fail_deleted_flush_once and any(
            record.status == ArtifactStatus.DELETED.value for record in artifacts
        ):
            self.repository.fail_deleted_flush_once = False
            raise RuntimeError("simulated deleted metadata commit failure")


class FakeRuntimeRepository:
    def __init__(self) -> None:
        self.records: dict[tuple[type[Any], str], Any] = {
            (FakeRunRecord, RUN_ID): FakeRunRecord(
                id=RUN_ID,
                connection_id=CONNECTION_ID,
            )
        }
        self.fail_ready_flush_once = False
        self.fail_deleted_flush_once = False

    @asynccontextmanager
    async def transaction(self):
        snapshot = copy.deepcopy(self.records)
        try:
            yield FakeSession(self)
        except BaseException:
            self.records = snapshot
            raise

    def artifacts(self) -> list[FakeArtifactRecord]:
        return [
            record
            for (record_type, _), record in self.records.items()
            if record_type is FakeArtifactRecord
        ]


class FakeAuditService:
    def __init__(self) -> None:
        self.events: list[dict[str, Any]] = []

    async def _record_unlocked(self, **values: Any) -> SimpleNamespace:
        self.events.append(values)
        return SimpleNamespace(**values)


class FakeStorage:
    backend_name = "local_filesystem"
    namespace = ""

    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.trash: dict[str, bytes] = {}
        self.discarded: list[str] = []
        self.restored: list[str] = []
        self.closed_streams = 0
        self.save_error: BaseException | None = None
        self.reported_sha256: str | None = None
        self.validation_profiles: list[AttachmentValidationProfile] = []

    async def save(
        self,
        upload: UploadFile,
        artifact_id: UUID,
        *,
        validation_profile: AttachmentValidationProfile = (
            AttachmentValidationProfile.GENERIC
        ),
    ) -> StoredUpload:
        self.validation_profiles.append(validation_profile)
        try:
            data = await upload.read()
            if self.save_error is not None:
                raise self.save_error
        finally:
            await upload.close()
        key = f"{artifact_id.hex[:2]}/{artifact_id.hex}"
        digest = hashlib.sha256(data).hexdigest()
        self.objects[key] = data
        return StoredUpload(
            original_filename=upload.filename or "report.json",
            storage_key=key,
            media_type=upload.content_type or "application/json",
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
        data = self.objects[storage_key]

        async def body() -> AsyncIterator[bytes]:
            yield data

        async def close() -> None:
            self.closed_streams += 1

        return StoredContent(
            body=body(),
            size_bytes=len(data),
            sha256=self.reported_sha256 or hashlib.sha256(data).hexdigest(),
            close=close,
        )

    async def discard(self, storage_key: str) -> None:
        self.discarded.append(storage_key)
        self.objects.pop(storage_key, None)

    async def quarantine(
        self,
        storage_key: str,
        artifact_id: UUID,
    ) -> QuarantineReceipt:
        isolated_key = quarantine_key(artifact_id)
        self.trash[isolated_key] = self.objects.pop(storage_key)
        return QuarantineReceipt(
            _backend_name=self.backend_name,
            _namespace=self.namespace,
            _original_key=storage_key,
            _quarantine_key=isolated_key,
        )

    async def restore(self, receipt: QuarantineReceipt) -> None:
        self.restored.append(receipt._original_key)
        self.objects[receipt._original_key] = self.trash.pop(
            receipt._quarantine_key
        )

    async def aclose(self) -> None:
        return None


def upload(
    data: bytes = b'{"passed": 12, "failed": 0}',
    *,
    filename: str = "quality-report.json",
    media_type: str = "application/json",
) -> UploadFile:
    return UploadFile(
        file=BytesIO(data),
        filename=filename,
        headers=Headers({"content-type": media_type}),
    )


def build_service() -> tuple[
    ProviderRunArtifactService,
    FakeRuntimeRepository,
    FakeStorage,
    FakeAuditService,
]:
    repository = FakeRuntimeRepository()
    storage = FakeStorage()
    audits = FakeAuditService()
    service = ProviderRunArtifactService(
        repository,  # type: ignore[arg-type]
        {storage.backend_name: storage},
        storage.backend_name,
        audits,  # type: ignore[arg-type]
        Lock(),
        artifact_record_type=FakeArtifactRecord,
        run_record_type=FakeRunRecord,
    )
    return service, repository, storage, audits


@pytest.mark.asyncio
async def test_artifact_pending_upload_ready_content_and_deleted_lifecycle() -> None:
    service, repository, storage, audits = build_service()

    ready = await service.create(
        connection_id=CONNECTION_ID,
        run_id=RUN_ID,
        kind=ArtifactKind.TEST_REPORT,
        upload=upload(),
        principal=PRINCIPAL,
    )

    assert ready.status == ArtifactStatus.READY
    assert ready.kind == ArtifactKind.TEST_REPORT
    assert ready.sha256 == hashlib.sha256(
        b'{"passed": 12, "failed": 0}'
    ).hexdigest()
    assert ready.size_bytes == len(b'{"passed": 12, "failed": 0}')
    assert storage.validation_profiles == [
        AttachmentValidationProfile.TEST_REPORT
    ]
    assert "storage_key" not in ready.model_dump()
    assert [event["action"] for event in audits.events] == [
        AuditAction.CREATED,
        AuditAction.STATUS_CHANGED,
    ]
    assert audits.events[-1]["changes"]["status"].model_dump() == {
        "before": "pending",
        "after": "ready",
    }

    metadata, content = await service.content(
        connection_id=CONNECTION_ID,
        run_id=RUN_ID,
        artifact_id=str(ready.id),
    )
    downloaded = b"".join([chunk async for chunk in content])
    assert metadata.id == ready.id
    assert downloaded == b'{"passed": 12, "failed": 0}'
    assert storage.closed_streams == 1

    deleted = await service.delete(
        connection_id=CONNECTION_ID,
        run_id=RUN_ID,
        artifact_id=str(ready.id),
        principal=PRINCIPAL,
    )
    assert deleted.status == ArtifactStatus.DELETED
    assert repository.artifacts()[0].status == ArtifactStatus.DELETED.value
    assert storage.objects == {}
    assert len(storage.trash) == 1
    assert audits.events[-1]["action"] == AuditAction.DELETED


@pytest.mark.asyncio
async def test_artifact_kind_selects_content_contract_and_rejects_non_report_media() -> None:
    service, repository, storage, _ = build_service()

    ordinary = await service.create(
        connection_id=CONNECTION_ID,
        run_id=RUN_ID,
        kind=ArtifactKind.ARTIFACT,
        upload=upload(
            b'<report name="ordinary"/>',
            filename="ordinary.xml",
            media_type="application/xml",
        ),
        principal=PRINCIPAL,
    )
    rejected = upload(
        b"plain diagnostic output",
        filename="report.txt",
        media_type="text/plain",
    )

    with pytest.raises(BusinessValidationError, match="JSON.*JUnit XML"):
        await service.create(
            connection_id=CONNECTION_ID,
            run_id=RUN_ID,
            kind=ArtifactKind.TEST_REPORT,
            upload=rejected,
            principal=PRINCIPAL,
        )

    assert ordinary.status == ArtifactStatus.READY
    assert storage.validation_profiles == [AttachmentValidationProfile.GENERIC]
    assert len(repository.artifacts()) == 1
    assert rejected.file.closed


@pytest.mark.asyncio
async def test_storage_failure_is_visible_as_failed_without_leaking_error_text() -> None:
    service, repository, storage, audits = build_service()
    storage.save_error = AttachmentStorageUnavailableError(
        "private endpoint and credential details must not be persisted"
    )

    with pytest.raises(AttachmentStorageUnavailableError):
        await service.create(
            connection_id=CONNECTION_ID,
            run_id=RUN_ID,
            kind=ArtifactKind.ARTIFACT,
            upload=upload(b"build log"),
            principal=PRINCIPAL,
        )

    [failed] = repository.artifacts()
    assert failed.status == ArtifactStatus.FAILED.value
    assert failed.error_code == "storage_unavailable"
    assert "private endpoint" not in repr(failed.__dict__)
    assert len(storage.discarded) == 1
    assert audits.events[-1]["changes"]["error_code"].after == (
        "storage_unavailable"
    )


@pytest.mark.asyncio
async def test_ready_finalize_failure_discards_object_then_marks_failed() -> None:
    service, repository, storage, audits = build_service()
    repository.fail_ready_flush_once = True

    with pytest.raises(RuntimeError, match="ready metadata commit failure"):
        await service.create(
            connection_id=CONNECTION_ID,
            run_id=RUN_ID,
            kind=ArtifactKind.TEST_REPORT,
            upload=upload(),
            principal=PRINCIPAL,
        )

    [failed] = repository.artifacts()
    assert failed.status == ArtifactStatus.FAILED.value
    assert failed.error_code == "finalize_failed"
    assert storage.objects == {}
    assert storage.discarded == [failed.storage_key]
    assert [event["action"] for event in audits.events] == [
        AuditAction.CREATED,
        AuditAction.STATUS_CHANGED,
    ]


@pytest.mark.asyncio
async def test_content_metadata_mismatch_closes_stream_and_rejects_download() -> None:
    service, _, storage, _ = build_service()
    ready = await service.create(
        connection_id=CONNECTION_ID,
        run_id=RUN_ID,
        kind=ArtifactKind.TEST_REPORT,
        upload=upload(),
        principal=PRINCIPAL,
    )
    storage.reported_sha256 = "0" * 64

    with pytest.raises(AttachmentStorageIntegrityError):
        await service.content(
            connection_id=CONNECTION_ID,
            run_id=RUN_ID,
            artifact_id=str(ready.id),
        )

    assert storage.closed_streams == 1


@pytest.mark.asyncio
async def test_delete_metadata_failure_restores_quarantined_ready_object() -> None:
    service, repository, storage, audits = build_service()
    ready = await service.create(
        connection_id=CONNECTION_ID,
        run_id=RUN_ID,
        kind=ArtifactKind.ARTIFACT,
        upload=upload(b"artifact body"),
        principal=PRINCIPAL,
    )
    original_key = repository.artifacts()[0].storage_key
    repository.fail_deleted_flush_once = True

    with pytest.raises(RuntimeError, match="deleted metadata commit failure"):
        await service.delete(
            connection_id=CONNECTION_ID,
            run_id=RUN_ID,
            artifact_id=str(ready.id),
            principal=PRINCIPAL,
        )

    [restored] = repository.artifacts()
    assert restored.status == ArtifactStatus.READY.value
    assert original_key in storage.objects
    assert storage.trash == {}
    assert storage.restored == [original_key]
    assert all(event["action"] != AuditAction.DELETED for event in audits.events)


@pytest.mark.asyncio
async def test_pending_artifact_cannot_be_deleted() -> None:
    service, repository, _, _ = build_service()
    pending_upload = upload()
    artifact_id = UUID("00000000-0000-0000-0000-000000000999")
    repository.records[(FakeArtifactRecord, str(artifact_id))] = FakeArtifactRecord(
        id=str(artifact_id),
        run_id=RUN_ID,
        kind=ArtifactKind.ARTIFACT.value,
        status=ArtifactStatus.PENDING.value,
        original_filename="pending.log",
        storage_backend="local_filesystem",
        storage_namespace="",
        storage_key=f"{artifact_id.hex[:2]}/{artifact_id.hex}",
        media_type=None,
        size_bytes=None,
        sha256=None,
        error_code=None,
        created_by_user_id=str(PRINCIPAL.user_id),
        created_by_name=PRINCIPAL.display_name,
        created_at=datetime.now(timezone.utc),
        updated_at=datetime.now(timezone.utc),
        ready_at=None,
        failed_at=None,
        deleted_at=None,
    )
    await pending_upload.close()

    with pytest.raises(InvalidStateError, match="pending"):
        await service.delete(
            connection_id=CONNECTION_ID,
            run_id=RUN_ID,
            artifact_id=str(artifact_id),
            principal=PRINCIPAL,
        )
