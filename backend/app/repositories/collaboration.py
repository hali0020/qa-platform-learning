from __future__ import annotations

from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError
from app.database.models import AttachmentRecord, CommentRecord
from app.database.session import Database
from app.domain.collaboration import Attachment, CollaborationTargetType, Comment


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class CommentRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, comment: Comment) -> Comment:
        async with self._database.session() as session:
            session.add(self._to_record(comment))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("评论关联对象无效") from exc
        return comment.model_copy(deep=True)

    async def get(self, comment_id: UUID) -> Comment | None:
        async with self._database.session() as session:
            record = await session.get(CommentRecord, str(comment_id))
            return self._to_entity(record) if record is not None else None

    async def list_for_target(
        self,
        entity_type: CollaborationTargetType,
        entity_id: UUID,
    ) -> list[Comment]:
        statement = (
            select(CommentRecord)
            .where(
                CommentRecord.entity_type == entity_type.value,
                CommentRecord.entity_id == str(entity_id),
            )
            .order_by(CommentRecord.created_at, CommentRecord.id)
        )
        async with self._database.session() as session:
            records = (await session.scalars(statement)).all()
            return [self._to_entity(record) for record in records]

    async def update(self, comment: Comment) -> Comment:
        async with self._database.session() as session:
            record = await session.get(CommentRecord, str(comment.id))
            if record is None:
                raise NotFoundError("评论", comment.id)
            record.body = comment.body
            record.updated_at = comment.updated_at
            record.edited_at = comment.edited_at
            record.deleted_at = comment.deleted_at
            record.deleted_by_id = (
                str(comment.deleted_by_id) if comment.deleted_by_id else None
            )
            await session.commit()
        return comment.model_copy(deep=True)

    @staticmethod
    def _to_record(comment: Comment) -> CommentRecord:
        return CommentRecord(
            id=str(comment.id),
            project_id=str(comment.project_id),
            entity_type=comment.entity_type.value,
            entity_id=str(comment.entity_id),
            parent_id=str(comment.parent_id) if comment.parent_id else None,
            author_id=str(comment.author_id),
            author_name=comment.author_name,
            body=comment.body,
            created_at=comment.created_at,
            updated_at=comment.updated_at,
            edited_at=comment.edited_at,
            deleted_at=comment.deleted_at,
            deleted_by_id=(str(comment.deleted_by_id) if comment.deleted_by_id else None),
        )

    @staticmethod
    def _to_entity(record: CommentRecord) -> Comment:
        return Comment(
            id=UUID(record.id),
            project_id=UUID(record.project_id),
            entity_type=CollaborationTargetType(record.entity_type),
            entity_id=UUID(record.entity_id),
            parent_id=UUID(record.parent_id) if record.parent_id else None,
            author_id=UUID(record.author_id),
            author_name=record.author_name,
            body=record.body,
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
            edited_at=_as_utc(record.edited_at),
            deleted_at=_as_utc(record.deleted_at),
            deleted_by_id=(UUID(record.deleted_by_id) if record.deleted_by_id else None),
        )


class AttachmentRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def create(self, attachment: Attachment) -> Attachment:
        async with self._database.session() as session:
            session.add(self._to_record(attachment))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("附件元数据关联无效或存储键重复") from exc
        return attachment.model_copy(deep=True)

    async def get(self, attachment_id: UUID) -> Attachment | None:
        async with self._database.session() as session:
            record = await session.get(AttachmentRecord, str(attachment_id))
            return self._to_entity(record) if record is not None else None

    async def list_for_target(
        self,
        entity_type: CollaborationTargetType,
        entity_id: UUID,
    ) -> list[Attachment]:
        statement = (
            select(AttachmentRecord)
            .where(
                AttachmentRecord.entity_type == entity_type.value,
                AttachmentRecord.entity_id == str(entity_id),
                AttachmentRecord.deleted_at.is_(None),
            )
            .order_by(AttachmentRecord.created_at, AttachmentRecord.id)
        )
        async with self._database.session() as session:
            records = (await session.scalars(statement)).all()
            return [self._to_entity(record) for record in records]

    async def update(self, attachment: Attachment) -> Attachment:
        async with self._database.session() as session:
            record = await session.get(AttachmentRecord, str(attachment.id))
            if record is None:
                raise NotFoundError("附件", attachment.id)
            record.deleted_at = attachment.deleted_at
            record.deleted_by_id = (
                str(attachment.deleted_by_id) if attachment.deleted_by_id else None
            )
            await session.commit()
        return attachment.model_copy(deep=True)

    @staticmethod
    def _to_record(attachment: Attachment) -> AttachmentRecord:
        return AttachmentRecord(
            id=str(attachment.id),
            project_id=str(attachment.project_id),
            entity_type=attachment.entity_type.value,
            entity_id=str(attachment.entity_id),
            comment_id=str(attachment.comment_id) if attachment.comment_id else None,
            uploader_id=str(attachment.uploader_id),
            uploader_name=attachment.uploader_name,
            original_filename=attachment.original_filename,
            storage_backend=attachment.storage_backend,
            storage_namespace=attachment.storage_namespace,
            storage_key=attachment.storage_key,
            media_type=attachment.media_type,
            size_bytes=attachment.size_bytes,
            sha256=attachment.sha256,
            is_image=attachment.is_image,
            created_at=attachment.created_at,
            deleted_at=attachment.deleted_at,
            deleted_by_id=(
                str(attachment.deleted_by_id) if attachment.deleted_by_id else None
            ),
        )

    @staticmethod
    def _to_entity(record: AttachmentRecord) -> Attachment:
        return Attachment(
            id=UUID(record.id),
            project_id=UUID(record.project_id),
            entity_type=CollaborationTargetType(record.entity_type),
            entity_id=UUID(record.entity_id),
            comment_id=UUID(record.comment_id) if record.comment_id else None,
            uploader_id=UUID(record.uploader_id),
            uploader_name=record.uploader_name,
            original_filename=record.original_filename,
            storage_key=record.storage_key,
            storage_backend=record.storage_backend,
            storage_namespace=record.storage_namespace,
            media_type=record.media_type,
            size_bytes=record.size_bytes,
            sha256=record.sha256,
            is_image=record.is_image,
            created_at=_as_utc(record.created_at),
            deleted_at=_as_utc(record.deleted_at),
            deleted_by_id=(UUID(record.deleted_by_id) if record.deleted_by_id else None),
        )
