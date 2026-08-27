from asyncio import Lock
from uuid import UUID

from app.core.errors import AuthorizationError, InvalidStateError, NotFoundError
from app.domain.collaboration import CollaborationTargetType, Comment
from app.domain.identity import PermissionCode, Principal
from app.domain.models import AuditAction, AuditChange, utc_now
from app.repositories.collaboration import CommentRepository
from app.schemas.comments import CommentCreate, CommentUpdate
from app.services.audit import AuditService
from app.services.collaboration_targets import CollaborationTargetResolver


class CommentService:
    def __init__(
        self,
        comments: CommentRepository,
        targets: CollaborationTargetResolver,
        audits: AuditService,
        business_lock: Lock,
    ) -> None:
        self._comments = comments
        self._targets = targets
        self._audits = audits
        self._business_lock = business_lock

    async def list(
        self,
        entity_type: CollaborationTargetType,
        entity_id: UUID,
    ) -> list[Comment]:
        async with self._business_lock:
            await self._targets.resolve(entity_type, entity_id)
            return await self._comments.list_for_target(entity_type, entity_id)

    async def create(
        self,
        payload: CommentCreate,
        principal: Principal,
    ) -> Comment:
        async with self._business_lock:
            target = await self._targets.resolve(
                payload.entity_type,
                payload.entity_id,
                requested_project_id=payload.project_id,
                require_writable=True,
            )
            if payload.parent_id is not None:
                parent = await self._comments.get(payload.parent_id)
                if parent is None:
                    raise NotFoundError("父评论", payload.parent_id)
                if (
                    parent.entity_type != payload.entity_type
                    or parent.entity_id != payload.entity_id
                    or parent.project_id != payload.project_id
                ):
                    raise InvalidStateError("父评论必须属于同一个协作目标")
            comment = Comment(
                project_id=target.project.id,
                entity_type=payload.entity_type,
                entity_id=payload.entity_id,
                parent_id=payload.parent_id,
                author_id=principal.user_id,
                author_name=principal.display_name,
                body=payload.body,
            )
            created = await self._comments.create(comment)
            await self._audits._record_unlocked(
                project_id=created.project_id,
                entity_type="comment",
                entity_id=created.id,
                action=AuditAction.CREATED,
                changes={
                    "target": AuditChange(
                        before=None,
                        after=f"{created.entity_type.value}:{created.entity_id}",
                    ),
                    "body_length": AuditChange(before=None, after=len(created.body)),
                },
            )
            return created

    async def update(
        self,
        comment_id: UUID,
        payload: CommentUpdate,
        principal: Principal,
    ) -> Comment:
        async with self._business_lock:
            comment = await self._require_comment(comment_id)
            await self._targets.resolve(
                comment.entity_type,
                comment.entity_id,
                requested_project_id=comment.project_id,
                require_writable=True,
            )
            self._require_owner_or_moderator(comment, principal)
            if comment.deleted_at is not None:
                raise InvalidStateError("已删除评论不能编辑")
            if comment.body == payload.body:
                return comment
            now = utc_now()
            updated = comment.model_copy(
                update={"body": payload.body, "updated_at": now, "edited_at": now}
            )
            saved = await self._comments.update(updated)
            await self._audits._record_unlocked(
                project_id=saved.project_id,
                entity_type="comment",
                entity_id=saved.id,
                action=AuditAction.UPDATED,
                changes={
                    "body_length": AuditChange(
                        before=len(comment.body),
                        after=len(saved.body),
                    )
                },
            )
            return saved

    async def delete(self, comment_id: UUID, principal: Principal) -> Comment:
        async with self._business_lock:
            comment = await self._require_comment(comment_id)
            await self._targets.resolve(
                comment.entity_type,
                comment.entity_id,
                requested_project_id=comment.project_id,
                require_writable=True,
            )
            self._require_owner_or_moderator(comment, principal)
            if comment.deleted_at is not None:
                return comment
            now = utc_now()
            deleted = comment.model_copy(
                update={
                    "body": "",
                    "deleted_at": now,
                    "deleted_by_id": principal.user_id,
                    "updated_at": now,
                }
            )
            saved = await self._comments.update(deleted)
            await self._audits._record_unlocked(
                project_id=saved.project_id,
                entity_type="comment",
                entity_id=saved.id,
                action=AuditAction.DELETED,
                changes={
                    "deleted_at": AuditChange(before=None, after=now.isoformat())
                },
            )
            return saved

    async def _require_comment(self, comment_id: UUID) -> Comment:
        comment = await self._comments.get(comment_id)
        if comment is None:
            raise NotFoundError("评论", comment_id)
        return comment

    @staticmethod
    def _require_owner_or_moderator(
        comment: Comment,
        principal: Principal,
    ) -> None:
        if comment.author_id == principal.user_id:
            return
        if principal.has_permission(PermissionCode.COMMENT_MODERATE):
            return
        raise AuthorizationError("只能修改自己的评论")
