from __future__ import annotations

import hmac
from asyncio import Lock
from collections.abc import Mapping
from uuid import UUID, uuid4

from fastapi import UploadFile

from app.core.errors import AuthorizationError, InvalidStateError, NotFoundError
from app.domain.collaboration import Attachment, CollaborationTargetType
from app.domain.identity import PermissionCode, Principal
from app.domain.models import AuditAction, AuditChange, utc_now
from app.repositories.collaboration import AttachmentRepository, CommentRepository
from app.schemas.attachments import AttachmentView
from app.services.audit import AuditService
from app.services.attachment_storage import (
    AttachmentStorage,
    AttachmentStorageIntegrityError,
    AttachmentStorageUnavailableError,
    StoredContent,
)
from app.services.collaboration_targets import CollaborationTargetResolver


class AttachmentService:
    def __init__(
        self,
        attachments: AttachmentRepository,
        comments: CommentRepository,
        targets: CollaborationTargetResolver,
        storages: Mapping[str, AttachmentStorage],
        write_backend: str,
        audits: AuditService,
        business_lock: Lock,
    ) -> None:
        self._attachments = attachments
        self._comments = comments
        self._targets = targets
        self._storages = dict(storages)
        if not self._storages:
            raise ValueError("attachment storage registry cannot be empty")
        if any(
            registered_name != storage.backend_name
            for registered_name, storage in self._storages.items()
        ):
            raise ValueError("attachment storage registry name mismatch")
        try:
            self._write_storage = self._storages[write_backend]
        except KeyError as error:
            raise ValueError("attachment write backend is not registered") from error
        self._audits = audits
        self._business_lock = business_lock

    async def list(
        self,
        entity_type: CollaborationTargetType,
        entity_id: UUID,
    ) -> list[AttachmentView]:
        async with self._business_lock:
            await self._targets.resolve(entity_type, entity_id)
            items = await self._attachments.list_for_target(entity_type, entity_id)
            return [self._view(item) for item in items]

    async def create(
        self,
        *,
        project_id: UUID,
        entity_type: CollaborationTargetType,
        entity_id: UUID,
        comment_id: UUID | None,
        upload: UploadFile,
        principal: Principal,
    ) -> AttachmentView:
        # Validate once before accepting a potentially slow upload, then again
        # after storage I/O so a concurrently deleted/changed target cannot be
        # linked using a stale authorization decision.
        async with self._business_lock:
            await self._validate_create_target(
                project_id=project_id,
                entity_type=entity_type,
                entity_id=entity_id,
                comment_id=comment_id,
            )

        attachment_id = uuid4()
        storage = self._write_storage
        stored = await storage.save(upload, attachment_id)
        created: Attachment | None = None
        try:
            async with self._business_lock:
                target = await self._validate_create_target(
                    project_id=project_id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    comment_id=comment_id,
                )
                attachment = Attachment(
                    id=attachment_id,
                    project_id=target.project.id,
                    entity_type=entity_type,
                    entity_id=entity_id,
                    comment_id=comment_id,
                    uploader_id=principal.user_id,
                    uploader_name=principal.display_name,
                    original_filename=stored.original_filename,
                    storage_backend=storage.backend_name,
                    storage_namespace=storage.namespace,
                    storage_key=stored.storage_key,
                    media_type=stored.media_type,
                    size_bytes=stored.size_bytes,
                    sha256=stored.sha256,
                    is_image=stored.is_image,
                )
                created = await self._attachments.create(attachment)
                await self._audits._record_unlocked(
                    project_id=created.project_id,
                    entity_type="attachment",
                    entity_id=created.id,
                    action=AuditAction.CREATED,
                    changes={
                        "filename": AuditChange(
                            before=None,
                            after=created.original_filename,
                        ),
                        "size_bytes": AuditChange(
                            before=None, after=created.size_bytes
                        ),
                        "sha256": AuditChange(before=None, after=created.sha256),
                    },
                )
                return self._view(created)
        except BaseException:
            # Once metadata exists the blob is authoritative attachment data;
            # an audit failure must not turn it into a dangling DB row.
            if created is None:
                try:
                    await storage.discard(stored.storage_key)
                except BaseException:
                    # Preserve the original business/cancellation error. A
                    # later orphan reconciliation job owns residual cleanup.
                    pass
            raise

    async def content(
        self,
        attachment_id: UUID,
    ) -> tuple[AttachmentView, StoredContent]:
        async with self._business_lock:
            attachment = await self._require_attachment(attachment_id)
            if attachment.deleted_at is not None:
                raise NotFoundError("附件", attachment_id)
            await self._targets.resolve(
                attachment.entity_type,
                attachment.entity_id,
                requested_project_id=attachment.project_id,
            )
            storage = self._storage_for(attachment)
            view = self._view(attachment)

        content = await storage.open(
            attachment.storage_key,
            expected_size=attachment.size_bytes,
            expected_sha256=attachment.sha256,
        )
        if content.size_bytes != attachment.size_bytes or (
            content.sha256 is not None
            and not hmac.compare_digest(content.sha256, attachment.sha256)
        ):
            await content.aclose()
            raise AttachmentStorageIntegrityError()
        return view, content

    async def delete(
        self,
        attachment_id: UUID,
        principal: Principal,
    ) -> AttachmentView:
        async with self._business_lock:
            attachment = await self._require_attachment(attachment_id)
            await self._validate_delete(attachment, principal)
            if attachment.deleted_at is not None:
                return self._view(attachment)
            storage = self._storage_for(attachment)

        receipt = await storage.quarantine(attachment.storage_key, attachment.id)
        saved: Attachment | None = None
        try:
            async with self._business_lock:
                # Re-read and re-authorize after slow storage I/O. Routing
                # metadata is immutable; a mismatch is treated as unavailable
                # rather than allowing cross-backend restoration.
                current = await self._require_attachment(attachment_id)
                await self._validate_delete(current, principal)
                if current.deleted_at is not None:
                    return self._view(current)
                if (
                    current.storage_backend != attachment.storage_backend
                    or current.storage_namespace != attachment.storage_namespace
                    or current.storage_key != attachment.storage_key
                ):
                    raise InvalidStateError("附件存储路由已发生变化")
                now = utc_now()
                deleted = current.model_copy(
                    update={
                        "deleted_at": now,
                        "deleted_by_id": principal.user_id,
                    }
                )
                saved = await self._attachments.update(deleted)
                await self._audits._record_unlocked(
                    project_id=saved.project_id,
                    entity_type="attachment",
                    entity_id=saved.id,
                    action=AuditAction.DELETED,
                    changes={
                        "deleted_at": AuditChange(
                            before=None, after=now.isoformat()
                        )
                    },
                )
                return self._view(saved)
        except BaseException:
            if saved is None:
                try:
                    await storage.restore(receipt)
                except BaseException:
                    # The relational row is still live. A repair job must
                    # reconcile a failed compensation without masking the
                    # original error.
                    pass
            raise

    async def _validate_create_target(
        self,
        *,
        project_id: UUID,
        entity_type: CollaborationTargetType,
        entity_id: UUID,
        comment_id: UUID | None,
    ):
        target = await self._targets.resolve(
            entity_type,
            entity_id,
            requested_project_id=project_id,
            require_writable=True,
        )
        if comment_id is not None:
            comment = await self._comments.get(comment_id)
            if comment is None:
                raise NotFoundError("评论", comment_id)
            if (
                comment.project_id != project_id
                or comment.entity_type != entity_type
                or comment.entity_id != entity_id
                or comment.deleted_at is not None
            ):
                raise InvalidStateError("附件评论必须属于同一个协作目标")
        return target

    async def _validate_delete(
        self,
        attachment: Attachment,
        principal: Principal,
    ) -> None:
        await self._targets.resolve(
            attachment.entity_type,
            attachment.entity_id,
            requested_project_id=attachment.project_id,
            require_writable=True,
        )
        if attachment.uploader_id != principal.user_id and not principal.has_permission(
            PermissionCode.ATTACHMENT_MODERATE
        ):
            raise AuthorizationError("只能删除自己上传的附件")

    def _storage_for(self, attachment: Attachment) -> AttachmentStorage:
        storage = self._storages.get(attachment.storage_backend)
        if storage is None or storage.namespace != attachment.storage_namespace:
            raise AttachmentStorageUnavailableError()
        return storage

    async def _require_attachment(self, attachment_id: UUID) -> Attachment:
        attachment = await self._attachments.get(attachment_id)
        if attachment is None:
            raise NotFoundError("附件", attachment_id)
        return attachment

    @staticmethod
    def _view(attachment: Attachment) -> AttachmentView:
        return AttachmentView(
            id=attachment.id,
            project_id=attachment.project_id,
            entity_type=attachment.entity_type,
            entity_id=attachment.entity_id,
            comment_id=attachment.comment_id,
            uploader_id=attachment.uploader_id,
            uploader_name=attachment.uploader_name,
            original_filename=attachment.original_filename,
            media_type=attachment.media_type,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
            is_image=attachment.is_image,
            created_at=attachment.created_at.isoformat(),
            deleted_at=(
                attachment.deleted_at.isoformat() if attachment.deleted_at else None
            ),
        )
