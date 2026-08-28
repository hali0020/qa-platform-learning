"""Provider-run artifact metadata and storage compensation service.

The relational row and the object storage backend cannot participate in one
transaction.  This module therefore makes the gap explicit with a durable
``pending`` row, a verified ``ready`` transition, a safe ``failed`` state, and
recoverable deletion.  It reuses the attachment storage port so neither the
runtime service nor an HTTP route learns filesystem paths, S3 buckets, or
client details.
"""

from __future__ import annotations

import hmac
from asyncio import Lock
from collections.abc import Mapping
from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from fastapi import UploadFile
from pydantic import BaseModel
from sqlalchemy import select

from app.core.errors import (
    BusinessValidationError,
    InvalidStateError,
    NotFoundError,
)
from app.domain.identity import Principal
from app.domain.models import AuditAction, AuditChange, utc_now
from app.runtime.orm import ProviderRunRecord
from app.runtime.repository import RuntimeRepository
from app.services.attachment_storage import (
    AttachmentStorage,
    AttachmentStorageIntegrityError,
    AttachmentStorageUnavailableError,
    AttachmentValidationProfile,
    StoredContent,
    validate_sha256,
    validate_storage_key,
)
from app.services.audit import AuditService
from app.services.local_attachment_storage import LocalAttachmentStorage


class ArtifactKind(str, Enum):
    TEST_REPORT = "test_report"
    ARTIFACT = "artifact"


class ArtifactStatus(str, Enum):
    PENDING = "pending"
    READY = "ready"
    FAILED = "failed"
    DELETED = "deleted"


class ProviderRunArtifactView(BaseModel):
    """Public metadata; storage routing is deliberately private."""

    id: UUID
    run_id: str
    kind: ArtifactKind
    status: ArtifactStatus
    original_filename: str
    media_type: str | None
    size_bytes: int | None
    sha256: str | None
    error_code: str | None
    created_by_user_id: UUID | None
    created_by_name: str
    created_at: datetime
    updated_at: datetime
    ready_at: datetime | None
    failed_at: datetime | None
    deleted_at: datetime | None


def _aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _default_artifact_record_type() -> type[Any]:
    # Kept lazy so the application service and its compensation tests can be
    # developed independently from the ordered Alembic/ORM change.
    from app.runtime.orm import ProviderRunArtifactRecord

    return ProviderRunArtifactRecord


class ProviderRunArtifactService:
    """Own artifact state transitions without hiding cross-store failures."""

    def __init__(
        self,
        repository: RuntimeRepository,
        storages: Mapping[str, AttachmentStorage],
        write_backend: str,
        audits: AuditService,
        business_lock: Lock,
        *,
        artifact_record_type: type[Any] | None = None,
        run_record_type: type[Any] = ProviderRunRecord,
    ) -> None:
        self._repository = repository
        self._storages = dict(storages)
        if not self._storages:
            raise ValueError("artifact storage registry cannot be empty")
        if any(
            name != storage.backend_name
            for name, storage in self._storages.items()
        ):
            raise ValueError("artifact storage registry name mismatch")
        try:
            self._write_storage = self._storages[write_backend]
        except KeyError as error:
            raise ValueError("artifact write backend is not registered") from error
        self._audits = audits
        self._business_lock = business_lock
        self._record_type = artifact_record_type or _default_artifact_record_type()
        self._run_record_type = run_record_type

    async def create(
        self,
        *,
        connection_id: str,
        run_id: str,
        kind: ArtifactKind,
        upload: UploadFile,
        principal: Principal,
    ) -> ProviderRunArtifactView:
        artifact_id = uuid4()
        try:
            filename = LocalAttachmentStorage.validate_filename(upload.filename or "")
            declared_type = (upload.content_type or "").split(";", maxsplit=1)[
                0
            ].lower()
            validation_profile = (
                AttachmentValidationProfile.TEST_REPORT
                if kind == ArtifactKind.TEST_REPORT
                else AttachmentValidationProfile.GENERIC
            )
            if (
                validation_profile == AttachmentValidationProfile.TEST_REPORT
                and declared_type
                not in {"application/json", "application/xml", "text/xml"}
            ):
                raise BusinessValidationError(
                    "测试报告只允许 UTF-8 JSON 或 JUnit XML"
                )
        except BaseException:
            await upload.close()
            raise
        storage = self._write_storage
        expected_key = validate_storage_key(
            f"{artifact_id.hex[:2]}/{artifact_id.hex}"
        )
        now = utc_now()
        pending = self._record_type(
            id=str(artifact_id),
            run_id=run_id,
            kind=kind.value,
            status=ArtifactStatus.PENDING.value,
            original_filename=filename,
            storage_backend=storage.backend_name,
            storage_namespace=storage.namespace,
            storage_key=expected_key,
            media_type=None,
            # The ordered migration keeps this column non-null; zero is the
            # explicit "content not finalized" sentinel for pending/failed.
            size_bytes=0,
            sha256=None,
            error_code=None,
            created_by_user_id=str(principal.user_id),
            created_by_name=principal.display_name,
            created_at=now,
            updated_at=now,
            ready_at=None,
            failed_at=None,
            deleted_at=None,
        )

        try:
            await self._create_pending(connection_id, pending)
            await self._audit_created(pending, principal)
        except BaseException:
            await upload.close()
            raise

        try:
            stored = await storage.save(
                upload,
                artifact_id,
                validation_profile=validation_profile,
            )
        except BaseException as error:
            await self._best_effort_discard(storage, expected_key)
            await self._best_effort_fail(
                connection_id=connection_id,
                run_id=run_id,
                artifact_id=str(artifact_id),
                error_code=self._safe_error_code(error, phase="upload"),
                principal=principal,
            )
            raise

        if stored.storage_key != expected_key:
            error = AttachmentStorageIntegrityError("制品存储键与预留元数据不一致")
            await self._best_effort_discard(storage, stored.storage_key)
            await self._best_effort_discard(storage, expected_key)
            await self._best_effort_fail(
                connection_id=connection_id,
                run_id=run_id,
                artifact_id=str(artifact_id),
                error_code="storage_contract_mismatch",
                principal=principal,
            )
            raise error

        try:
            validate_sha256(stored.sha256)
            if stored.size_bytes <= 0:
                raise AttachmentStorageIntegrityError("制品大小无效")
            ready = await self._finalize_ready(
                connection_id=connection_id,
                run_id=run_id,
                artifact_id=str(artifact_id),
                original_filename=stored.original_filename,
                media_type=stored.media_type,
                size_bytes=stored.size_bytes,
                sha256=stored.sha256,
            )
        except BaseException as error:
            await self._best_effort_discard(storage, expected_key)
            await self._best_effort_fail(
                connection_id=connection_id,
                run_id=run_id,
                artifact_id=str(artifact_id),
                error_code=self._safe_error_code(error, phase="finalize"),
                principal=principal,
            )
            raise

        # Once the ready row commits, the object is authoritative data. An
        # audit failure must not trigger object compensation.
        await self._audit_status(
            ready,
            before=ArtifactStatus.PENDING,
            after=ArtifactStatus.READY,
            principal=principal,
            extra={
                "sha256": AuditChange(before=None, after=ready.sha256),
                "size_bytes": AuditChange(before=None, after=ready.size_bytes),
            },
        )
        return ready

    async def list(
        self,
        *,
        connection_id: str,
        run_id: str,
    ) -> list[ProviderRunArtifactView]:
        async with self._business_lock:
            async with self._repository.transaction() as session:
                await self._require_run(session, connection_id, run_id)
                statement = (
                    select(self._record_type)
                    .where(self._record_type.run_id == run_id)
                    .order_by(self._record_type.created_at, self._record_type.id)
                )
                records = list((await session.scalars(statement)).all())
                return [self._view(record) for record in records]

    async def content(
        self,
        *,
        connection_id: str,
        run_id: str,
        artifact_id: str,
    ) -> tuple[ProviderRunArtifactView, StoredContent]:
        async with self._business_lock:
            async with self._repository.transaction() as session:
                await self._require_run(session, connection_id, run_id)
                record = await self._require_artifact(session, run_id, artifact_id)
                status = ArtifactStatus(record.status)
                if status == ArtifactStatus.DELETED:
                    raise NotFoundError("流水线制品", artifact_id)
                if status != ArtifactStatus.READY:
                    raise InvalidStateError("只有 ready 制品可以下载")
                if (
                    record.size_bytes is None
                    or record.sha256 is None
                    or record.media_type is None
                ):
                    raise AttachmentStorageIntegrityError("制品元数据不完整")
                storage = self._storage_for(record)
                view = self._view(record)
                size_bytes = int(record.size_bytes)
                sha256 = validate_sha256(str(record.sha256))

        content = await storage.open(
            record.storage_key,
            expected_size=size_bytes,
            expected_sha256=sha256,
        )
        if content.size_bytes != size_bytes or (
            content.sha256 is not None
            and not hmac.compare_digest(content.sha256, sha256)
        ):
            await content.aclose()
            raise AttachmentStorageIntegrityError("制品内容与元数据不一致")
        return view, content

    async def delete(
        self,
        *,
        connection_id: str,
        run_id: str,
        artifact_id: str,
        principal: Principal,
    ) -> ProviderRunArtifactView:
        async with self._business_lock:
            async with self._repository.transaction() as session:
                await self._require_run(session, connection_id, run_id)
                record = await self._require_artifact(session, run_id, artifact_id)
                status = ArtifactStatus(record.status)
                if status == ArtifactStatus.DELETED:
                    return self._view(record)
                if status == ArtifactStatus.PENDING:
                    raise InvalidStateError("pending 制品不能在上传过程中删除")
                storage = self._storage_for(record)
                storage_key = record.storage_key
                artifact_uuid = UUID(str(record.id))

        if status == ArtifactStatus.FAILED:
            # A failed upload should not own an object, but an idempotent
            # discard also removes a possible uncertain completion.
            await storage.discard(storage_key)
            deleted = await self._mark_deleted(
                connection_id=connection_id,
                run_id=run_id,
                artifact_id=artifact_id,
                expected_status=ArtifactStatus.FAILED,
            )
            await self._audit_deleted(deleted, ArtifactStatus.FAILED, principal)
            return deleted

        receipt = await storage.quarantine(storage_key, artifact_uuid)
        deleted_persisted = False
        try:
            deleted = await self._mark_deleted(
                connection_id=connection_id,
                run_id=run_id,
                artifact_id=artifact_id,
                expected_status=ArtifactStatus.READY,
            )
            deleted_persisted = True
            await self._audit_deleted(deleted, ArtifactStatus.READY, principal)
            return deleted
        except BaseException:
            if not deleted_persisted:
                try:
                    await storage.restore(receipt)
                except BaseException:
                    # Keep the original database/cancellation failure. The
                    # live relational row remains the reconciliation source.
                    pass
            raise

    async def _create_pending(self, connection_id: str, record: Any) -> None:
        async with self._business_lock:
            async with self._repository.transaction() as session:
                await self._require_run(session, connection_id, record.run_id)
                session.add(record)
                await session.flush()

    async def _finalize_ready(
        self,
        *,
        connection_id: str,
        run_id: str,
        artifact_id: str,
        original_filename: str,
        media_type: str,
        size_bytes: int,
        sha256: str,
    ) -> ProviderRunArtifactView:
        async with self._business_lock:
            async with self._repository.transaction() as session:
                await self._require_run(session, connection_id, run_id)
                record = await self._require_artifact(session, run_id, artifact_id)
                if ArtifactStatus(record.status) != ArtifactStatus.PENDING:
                    raise InvalidStateError("制品只能从 pending 转为 ready")
                now = utc_now()
                record.status = ArtifactStatus.READY.value
                record.original_filename = LocalAttachmentStorage.validate_filename(
                    original_filename
                )
                record.media_type = media_type
                record.size_bytes = size_bytes
                record.sha256 = validate_sha256(sha256)
                record.error_code = None
                record.updated_at = now
                record.ready_at = now
                record.failed_at = None
                await session.flush()
                return self._view(record)

    async def _mark_failed(
        self,
        *,
        connection_id: str,
        run_id: str,
        artifact_id: str,
        error_code: str,
    ) -> ProviderRunArtifactView | None:
        async with self._business_lock:
            async with self._repository.transaction() as session:
                await self._require_run(session, connection_id, run_id)
                record = await self._require_artifact(session, run_id, artifact_id)
                if ArtifactStatus(record.status) != ArtifactStatus.PENDING:
                    return None
                now = utc_now()
                record.status = ArtifactStatus.FAILED.value
                record.error_code = error_code
                record.updated_at = now
                record.failed_at = now
                await session.flush()
                return self._view(record)

    async def _mark_deleted(
        self,
        *,
        connection_id: str,
        run_id: str,
        artifact_id: str,
        expected_status: ArtifactStatus,
    ) -> ProviderRunArtifactView:
        async with self._business_lock:
            async with self._repository.transaction() as session:
                await self._require_run(session, connection_id, run_id)
                record = await self._require_artifact(session, run_id, artifact_id)
                current = ArtifactStatus(record.status)
                if current == ArtifactStatus.DELETED:
                    return self._view(record)
                if current != expected_status:
                    raise InvalidStateError("制品状态在存储操作期间发生变化")
                now = utc_now()
                record.status = ArtifactStatus.DELETED.value
                record.updated_at = now
                record.deleted_at = now
                await session.flush()
                return self._view(record)

    async def _require_run(
        self,
        session: Any,
        connection_id: str,
        run_id: str,
    ) -> Any:
        run = await session.get(self._run_record_type, run_id)
        if run is None or run.connection_id != connection_id:
            raise NotFoundError("集成运行", run_id)
        return run

    async def _require_artifact(
        self,
        session: Any,
        run_id: str,
        artifact_id: str,
    ) -> Any:
        record = await session.get(self._record_type, artifact_id)
        if record is None or record.run_id != run_id:
            raise NotFoundError("流水线制品", artifact_id)
        return record

    def _storage_for(self, record: Any) -> AttachmentStorage:
        storage = self._storages.get(record.storage_backend)
        if storage is None or storage.namespace != record.storage_namespace:
            raise AttachmentStorageUnavailableError("制品存储暂不可用")
        return storage

    async def _best_effort_fail(
        self,
        *,
        connection_id: str,
        run_id: str,
        artifact_id: str,
        error_code: str,
        principal: Principal,
    ) -> None:
        try:
            failed = await self._mark_failed(
                connection_id=connection_id,
                run_id=run_id,
                artifact_id=artifact_id,
                error_code=error_code,
            )
            if failed is not None:
                await self._audit_status(
                    failed,
                    before=ArtifactStatus.PENDING,
                    after=ArtifactStatus.FAILED,
                    principal=principal,
                    extra={
                        "error_code": AuditChange(before=None, after=error_code)
                    },
                )
        except BaseException:
            # Failure settlement must never replace the original storage or
            # metadata error. A persistent pending row remains observable.
            pass

    @staticmethod
    async def _best_effort_discard(
        storage: AttachmentStorage,
        storage_key: str,
    ) -> None:
        try:
            await storage.discard(storage_key)
        except BaseException:
            pass

    async def _audit_created(self, record: Any, principal: Principal) -> None:
        async with self._business_lock:
            await self._audits._record_unlocked(
                project_id=None,
                entity_type="provider_run_artifact",
                entity_id=record.id,
                action=AuditAction.CREATED,
                actor=principal.username,
                actor_user_id=principal.user_id,
                changes={
                    "run_id": AuditChange(before=None, after=record.run_id),
                    "kind": AuditChange(before=None, after=record.kind),
                    "filename": AuditChange(
                        before=None, after=record.original_filename
                    ),
                    "status": AuditChange(
                        before=None, after=ArtifactStatus.PENDING.value
                    ),
                },
            )

    async def _audit_status(
        self,
        artifact: ProviderRunArtifactView,
        *,
        before: ArtifactStatus,
        after: ArtifactStatus,
        principal: Principal,
        extra: dict[str, AuditChange] | None = None,
    ) -> None:
        changes = {
            "status": AuditChange(before=before.value, after=after.value),
            **(extra or {}),
        }
        async with self._business_lock:
            await self._audits._record_unlocked(
                project_id=None,
                entity_type="provider_run_artifact",
                entity_id=artifact.id,
                action=AuditAction.STATUS_CHANGED,
                actor=principal.username,
                actor_user_id=principal.user_id,
                changes=changes,
            )

    async def _audit_deleted(
        self,
        artifact: ProviderRunArtifactView,
        before: ArtifactStatus,
        principal: Principal,
    ) -> None:
        async with self._business_lock:
            await self._audits._record_unlocked(
                project_id=None,
                entity_type="provider_run_artifact",
                entity_id=artifact.id,
                action=AuditAction.DELETED,
                actor=principal.username,
                actor_user_id=principal.user_id,
                changes={
                    "status": AuditChange(
                        before=before.value,
                        after=ArtifactStatus.DELETED.value,
                    ),
                    "deleted_at": AuditChange(
                        before=None,
                        after=(
                            artifact.deleted_at.isoformat()
                            if artifact.deleted_at is not None
                            else None
                        ),
                    ),
                },
            )

    @staticmethod
    def _safe_error_code(error: BaseException, *, phase: str) -> str:
        if isinstance(error, AttachmentStorageUnavailableError):
            return "storage_unavailable"
        if isinstance(error, AttachmentStorageIntegrityError):
            return "storage_integrity"
        if isinstance(error, BusinessValidationError):
            return "artifact_rejected"
        return f"{phase}_failed"

    @staticmethod
    def _view(record: Any) -> ProviderRunArtifactView:
        return ProviderRunArtifactView(
            id=UUID(str(record.id)),
            run_id=str(record.run_id),
            kind=ArtifactKind(record.kind),
            status=ArtifactStatus(record.status),
            original_filename=record.original_filename,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            error_code=record.error_code,
            created_by_user_id=(
                UUID(str(record.created_by_user_id))
                if record.created_by_user_id is not None
                else None
            ),
            created_by_name=record.created_by_name,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
            ready_at=_aware(record.ready_at),
            failed_at=_aware(record.failed_at),
            deleted_at=_aware(record.deleted_at),
        )


__all__ = [
    "ArtifactKind",
    "ArtifactStatus",
    "ProviderRunArtifactService",
    "ProviderRunArtifactView",
]
