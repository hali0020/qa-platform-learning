from __future__ import annotations

from asyncio import Lock
from uuid import UUID

from app.core.errors import BusinessValidationError
from app.core.actor import get_current_actor
from app.domain.models import AuditAction, AuditChange, AuditEvent
from app.repositories.base import AsyncRepository
from app.services.common import parse_uuid


class AuditService:
    """追加式通用审计服务；写入由持有共享业务锁的领域服务调用。"""

    def __init__(
        self,
        audit_events: AsyncRepository[AuditEvent],
        business_lock: Lock,
    ) -> None:
        self._audit_events = audit_events
        self._business_lock = business_lock

    async def list(
        self,
        project_id: str | UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | UUID | None = None,
        action: AuditAction | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        async with self._business_lock:
            return await self._list_unlocked(
                project_id=project_id,
                entity_type=entity_type,
                entity_id=entity_id,
                action=action,
                limit=limit,
            )

    async def _list_unlocked(
        self,
        project_id: str | UUID | None = None,
        entity_type: str | None = None,
        entity_id: str | UUID | None = None,
        action: AuditAction | None = None,
        limit: int = 100,
    ) -> list[AuditEvent]:
        """调用方必须已持有共享业务锁。"""

        if not 1 <= limit <= 200:
            raise BusinessValidationError("limit 必须在 1 到 200 之间")

        parsed_project_id = (
            parse_uuid(project_id, "project_id")
            if project_id is not None
            else None
        )
        normalized_type = self._normalize_entity_type(entity_type)
        normalized_id = str(entity_id).strip() if entity_id is not None else None
        if normalized_id == "":
            raise BusinessValidationError("entity_id 不能为空")

        events = await self._audit_events.list()
        if parsed_project_id is not None:
            events = [
                event
                for event in events
                if event.project_id == parsed_project_id
            ]
        if normalized_type is not None:
            events = [
                event for event in events if event.entity_type == normalized_type
            ]
        if normalized_id is not None:
            events = [event for event in events if event.entity_id == normalized_id]
        if action is not None:
            events = [event for event in events if event.action == action]
        return sorted(events, key=lambda event: event.created_at, reverse=True)[:limit]

    async def _record_unlocked(
        self,
        *,
        project_id: str | UUID | None,
        entity_type: str,
        entity_id: str | UUID,
        action: AuditAction,
        changes: dict[str, AuditChange],
        comment: str = "",
        actor: str = "local-user",
        actor_user_id: str | UUID | None = None,
    ) -> AuditEvent:
        """追加审计事件；调用方必须已持有共享业务锁。"""

        parsed_project_id = (
            parse_uuid(project_id, "project_id")
            if project_id is not None
            else None
        )
        normalized_type = self._normalize_entity_type(entity_type)
        if normalized_type is None:
            raise BusinessValidationError("entity_type 不能为空")
        normalized_id = str(entity_id).strip()
        if not normalized_id:
            raise BusinessValidationError("entity_id 不能为空")
        if len(normalized_id) > 64:
            raise BusinessValidationError("entity_id 不能超过 64 个字符")
        current_actor = get_current_actor()
        if current_actor is not None and actor == "local-user":
            actor = current_actor.username
            actor_user_id = current_actor.user_id
        normalized_actor = actor.strip()
        if not normalized_actor:
            raise BusinessValidationError("actor 不能为空")
        if len(normalized_actor) > 100:
            raise BusinessValidationError("actor 不能超过 100 个字符")
        normalized_comment = comment.strip()
        if len(normalized_comment) > 1000:
            raise BusinessValidationError("comment 不能超过 1000 个字符")

        event = AuditEvent(
            project_id=parsed_project_id,
            entity_type=normalized_type,
            entity_id=normalized_id,
            action=action,
            actor=normalized_actor,
            actor_user_id=(
                parse_uuid(actor_user_id, "actor_user_id")
                if actor_user_id is not None
                else None
            ),
            changes=changes,
            comment=normalized_comment,
        )
        return await self._audit_events.create(event)

    @staticmethod
    def _normalize_entity_type(value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip().lower()
        if not normalized:
            raise BusinessValidationError("entity_type 不能为空")
        if len(normalized) > 64:
            raise BusinessValidationError("entity_type 不能超过 64 个字符")
        return normalized
