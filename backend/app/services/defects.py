from __future__ import annotations

from asyncio import Lock
from datetime import datetime
from enum import Enum
from typing import Any
from uuid import UUID

from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.core.actor import get_current_actor
from app.domain.models import (
    AuditAction,
    AuditChange,
    Defect,
    DefectPriority,
    DefectSeverity,
    DefectStatus,
    ExecutionStatus,
    Project,
    ProjectStatus,
    TestCase,
    TestExecution,
    utc_now,
)
from app.repositories.base import AsyncRepository
from app.schemas.defects import DefectCreate, DefectTransition, DefectUpdate
from app.services.audit import AuditService
from app.services.common import parse_uuid


class DefectService:
    _TRANSITIONS = {
        DefectStatus.OPEN: {DefectStatus.IN_PROGRESS, DefectStatus.RESOLVED},
        DefectStatus.IN_PROGRESS: {DefectStatus.OPEN, DefectStatus.RESOLVED},
        DefectStatus.RESOLVED: {DefectStatus.VERIFIED, DefectStatus.REOPENED},
        DefectStatus.VERIFIED: {DefectStatus.CLOSED, DefectStatus.REOPENED},
        DefectStatus.CLOSED: {DefectStatus.REOPENED},
        DefectStatus.REOPENED: {
            DefectStatus.IN_PROGRESS,
            DefectStatus.RESOLVED,
        },
    }
    _AUDITED_FIELDS = (
        "project_id",
        "case_id",
        "execution_id",
        "title",
        "description",
        "severity",
        "priority",
        "status",
        "reporter",
        "assignee",
        "environment",
        "reproduction_steps",
        "expected_result",
        "actual_result",
        "resolution",
        "resolved_at",
        "closed_at",
    )

    def __init__(
        self,
        defects: AsyncRepository[Defect],
        projects: AsyncRepository[Project],
        test_cases: AsyncRepository[TestCase],
        executions: AsyncRepository[TestExecution],
        audits: AuditService,
        business_lock: Lock,
    ) -> None:
        self._defects = defects
        self._projects = projects
        self._test_cases = test_cases
        self._executions = executions
        self._audits = audits
        self._business_lock = business_lock

    async def create(self, payload: DefectCreate) -> Defect:
        async with self._business_lock:
            project_id = parse_uuid(payload.project_id, "project_id")
            await self._require_active_project(project_id)
            case_id = (
                parse_uuid(payload.case_id, "case_id")
                if payload.case_id is not None
                else None
            )
            execution_id = (
                parse_uuid(payload.execution_id, "execution_id")
                if payload.execution_id is not None
                else None
            )
            await self._validate_associations(
                project_id=project_id,
                case_id=case_id,
                execution_id=execution_id,
            )
            actor = get_current_actor()
            defect = Defect(
                project_id=project_id,
                case_id=case_id,
                execution_id=execution_id,
                title=payload.title,
                description=payload.description,
                severity=payload.severity,
                priority=payload.priority,
                reporter=actor.username if actor is not None else payload.reporter,
                assignee=payload.assignee,
                environment=payload.environment,
                reproduction_steps=list(payload.reproduction_steps),
                expected_result=payload.expected_result,
                actual_result=payload.actual_result,
            )
            created = await self._defects.create(defect)
            await self._audits._record_unlocked(
                project_id=created.project_id,
                entity_type="defect",
                entity_id=created.id,
                action=AuditAction.CREATED,
                changes=self._creation_changes(created),
            )
            return created

    async def list(
        self,
        project_id: str | UUID | None = None,
        status: DefectStatus | None = None,
        severity: DefectSeverity | None = None,
        assignee: str | None = None,
        case_id: str | UUID | None = None,
        execution_id: str | UUID | None = None,
    ) -> list[Defect]:
        async with self._business_lock:
            return await self._list_unlocked(
                project_id=project_id,
                status=status,
                severity=severity,
                assignee=assignee,
                case_id=case_id,
                execution_id=execution_id,
            )

    async def _list_unlocked(
        self,
        project_id: str | UUID | None = None,
        status: DefectStatus | None = None,
        severity: DefectSeverity | None = None,
        assignee: str | None = None,
        case_id: str | UUID | None = None,
        execution_id: str | UUID | None = None,
    ) -> list[Defect]:
        items = await self._defects.list()
        if project_id is not None:
            parsed_project_id = parse_uuid(project_id, "project_id")
            items = [
                item for item in items if item.project_id == parsed_project_id
            ]
        if status is not None:
            items = [item for item in items if item.status == status]
        if severity is not None:
            items = [item for item in items if item.severity == severity]
        if assignee is not None:
            normalized_assignee = assignee.strip().casefold()
            items = [
                item
                for item in items
                if item.assignee.casefold() == normalized_assignee
            ]
        if case_id is not None:
            parsed_case_id = parse_uuid(case_id, "case_id")
            items = [item for item in items if item.case_id == parsed_case_id]
        if execution_id is not None:
            parsed_execution_id = parse_uuid(execution_id, "execution_id")
            items = [
                item
                for item in items
                if item.execution_id == parsed_execution_id
            ]
        return sorted(items, key=lambda item: item.created_at)

    async def get(self, defect_id: str | UUID) -> Defect:
        async with self._business_lock:
            return await self._get_unlocked(defect_id)

    async def _get_unlocked(self, defect_id: str | UUID) -> Defect:
        parsed_id = parse_uuid(defect_id, "defect_id")
        defect = await self._defects.get(parsed_id)
        if defect is None:
            raise NotFoundError("缺陷", parsed_id)
        return defect

    async def update(
        self,
        defect_id: str | UUID,
        payload: DefectUpdate,
    ) -> Defect:
        async with self._business_lock:
            defect = await self._get_unlocked(defect_id)
            if defect.status == DefectStatus.CLOSED:
                raise InvalidStateError("已关闭缺陷只能重新打开，不能编辑")
            changes = payload.model_dump(exclude_unset=True, exclude_none=True)
            if "reproduction_steps" in changes:
                changes["reproduction_steps"] = list(changes["reproduction_steps"])
            changed_fields = [
                field
                for field, value in changes.items()
                if getattr(defect, field) != value
            ]
            if not changed_fields:
                return defect

            effective_changes = {
                field: changes[field] for field in changed_fields
            }
            effective_changes["updated_at"] = utc_now()
            updated = defect.model_copy(update=effective_changes)
            if (
                updated.status == DefectStatus.IN_PROGRESS
                and not updated.assignee.strip()
            ):
                raise InvalidStateError("处理中缺陷必须保留负责人")
            saved = await self._defects.update(updated)
            await self._audits._record_unlocked(
                project_id=saved.project_id,
                entity_type="defect",
                entity_id=saved.id,
                action=AuditAction.UPDATED,
                changes=self._changes_between(defect, saved, changed_fields),
            )
            return saved

    async def transition(
        self,
        defect_id: str | UUID,
        payload: DefectTransition,
    ) -> Defect:
        async with self._business_lock:
            defect = await self._get_unlocked(defect_id)
            target = payload.status
            if defect.status == target:
                return defect
            if target not in self._TRANSITIONS[defect.status]:
                raise InvalidStateError(
                    f"缺陷不能从 {defect.status.value} 变为 {target.value}"
                )
            await self._require_active_project(defect.project_id)

            now = utc_now()
            updates: dict[str, Any] = {"status": target, "updated_at": now}
            audited_fields = ["status"]
            if target == DefectStatus.IN_PROGRESS and not defect.assignee.strip():
                raise InvalidStateError("缺陷进入处理中状态前必须指定负责人")
            if target == DefectStatus.RESOLVED:
                resolution = payload.resolution or ""
                if not resolution:
                    raise InvalidStateError("解决缺陷时必须填写解决说明")
                updates.update(
                    {
                        "resolution": resolution,
                        "resolved_at": now,
                        "closed_at": None,
                    }
                )
                audited_fields.extend(["resolution", "resolved_at", "closed_at"])
            elif target == DefectStatus.CLOSED:
                updates["closed_at"] = now
                audited_fields.append("closed_at")
            elif target == DefectStatus.REOPENED:
                if not payload.comment:
                    raise InvalidStateError("重新打开缺陷时必须填写原因")
                updates.update(
                    {
                        "resolution": "",
                        "resolved_at": None,
                        "closed_at": None,
                    }
                )
                audited_fields.extend(["resolution", "resolved_at", "closed_at"])

            updated = defect.model_copy(update=updates)
            saved = await self._defects.update(updated)
            await self._audits._record_unlocked(
                project_id=saved.project_id,
                entity_type="defect",
                entity_id=saved.id,
                action=AuditAction.STATUS_CHANGED,
                changes=self._changes_between(defect, saved, audited_fields),
                comment=payload.comment,
            )
            return saved

    async def _require_active_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError("项目", project_id)
        if project.status != ProjectStatus.ACTIVE:
            raise InvalidStateError("已归档项目不能新增或变更缺陷")
        return project

    async def _validate_associations(
        self,
        *,
        project_id: UUID,
        case_id: UUID | None,
        execution_id: UUID | None,
    ) -> None:
        test_case: TestCase | None = None
        if case_id is not None:
            test_case = await self._test_cases.get(case_id)
            if test_case is None:
                raise NotFoundError("测试用例", case_id)
            if test_case.project_id != project_id:
                raise ConflictError("缺陷只能关联同一项目中的测试用例")

        execution: TestExecution | None = None
        if execution_id is not None:
            execution = await self._executions.get(execution_id)
            if execution is None:
                raise NotFoundError("测试执行", execution_id)
            if execution.project_id != project_id:
                raise ConflictError("缺陷只能关联同一项目中的测试执行")
            if execution.status == ExecutionStatus.CREATED:
                raise InvalidStateError("尚未开始的测试执行不能关联缺陷")

        if test_case is not None and execution is not None:
            if not any(
                result.case_id == test_case.id for result in execution.results
            ):
                raise ConflictError("关联测试用例不属于指定测试执行")

    @classmethod
    def _creation_changes(cls, defect: Defect) -> dict[str, AuditChange]:
        return {
            field: AuditChange(
                before=None,
                after=cls._audit_value(getattr(defect, field)),
            )
            for field in cls._AUDITED_FIELDS
        }

    @classmethod
    def _changes_between(
        cls,
        before: Defect,
        after: Defect,
        fields: list[str],
    ) -> dict[str, AuditChange]:
        return {
            field: AuditChange(
                before=cls._audit_value(getattr(before, field)),
                after=cls._audit_value(getattr(after, field)),
            )
            for field in fields
            if getattr(before, field) != getattr(after, field)
        }

    @classmethod
    def _audit_value(cls, value: Any) -> Any:
        if isinstance(value, Enum):
            return value.value
        if isinstance(value, UUID):
            return str(value)
        if isinstance(value, datetime):
            return value.isoformat()
        if isinstance(value, list):
            return [cls._audit_value(item) for item in value]
        if isinstance(value, dict):
            return {key: cls._audit_value(item) for key, item in value.items()}
        return value
