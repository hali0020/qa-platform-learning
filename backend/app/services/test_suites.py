from __future__ import annotations

from asyncio import Lock
from uuid import UUID

from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.domain.models import (
    AuditAction,
    AuditChange,
    Project,
    ProjectStatus,
    TestCase,
    TestSuite,
    TestSuiteStatus,
    utc_now,
)
from app.repositories.base import AsyncRepository
from app.schemas.test_suites import TestSuiteCreate, TestSuiteUpdate
from app.services.audit import AuditService
from app.services.common import parse_uuid


class TestSuiteService:
    def __init__(
        self,
        test_suites: AsyncRepository[TestSuite],
        projects: AsyncRepository[Project],
        test_cases: AsyncRepository[TestCase],
        audit: AuditService,
        business_lock: Lock,
    ) -> None:
        self._test_suites = test_suites
        self._projects = projects
        self._test_cases = test_cases
        self._audit = audit
        self._business_lock = business_lock

    async def create(self, payload: TestSuiteCreate) -> TestSuite:
        async with self._business_lock:
            project_id = parse_uuid(payload.project_id, "project_id")
            await self._require_active_project(project_id)
            parent_id = (
                parse_uuid(payload.parent_id, "parent_id")
                if payload.parent_id is not None
                else None
            )
            await self._validate_parent(
                project_id=project_id,
                suite_id=None,
                parent_id=parent_id,
            )
            await self._ensure_unique_name(
                project_id=project_id,
                parent_id=parent_id,
                name=payload.name,
            )
            suite = TestSuite(
                project_id=project_id,
                parent_id=parent_id,
                name=payload.name,
                description=payload.description,
                position=payload.position,
            )
            created = await self._test_suites.create(suite)
            await self._audit._record_unlocked(
                project_id=project_id,
                entity_type="test_suite",
                entity_id=created.id,
                action=AuditAction.CREATED,
                changes={
                    "name": AuditChange(before=None, after=created.name),
                    "parent_id": AuditChange(
                        before=None,
                        after=self._uuid_value(created.parent_id),
                    ),
                    "status": AuditChange(
                        before=None,
                        after=created.status.value,
                    ),
                },
                comment="创建测试套件",
            )
            return created

    async def list(
        self,
        project_id: str | UUID | None = None,
        status: TestSuiteStatus | None = None,
    ) -> list[TestSuite]:
        async with self._business_lock:
            items = await self._test_suites.list()
            if project_id is not None:
                parsed_project_id = parse_uuid(project_id, "project_id")
                items = [
                    item for item in items if item.project_id == parsed_project_id
                ]
            if status is not None:
                items = [item for item in items if item.status == status]
            return sorted(
                items,
                key=lambda item: (
                    str(item.project_id),
                    str(item.parent_id or ""),
                    item.position,
                    item.name.casefold(),
                    item.created_at,
                ),
            )

    async def get(self, suite_id: str | UUID) -> TestSuite:
        async with self._business_lock:
            return await self._get_unlocked(suite_id)

    async def _get_unlocked(self, suite_id: str | UUID) -> TestSuite:
        parsed_id = parse_uuid(suite_id, "suite_id")
        suite = await self._test_suites.get(parsed_id)
        if suite is None:
            raise NotFoundError("测试套件", parsed_id)
        return suite

    async def update(
        self,
        suite_id: str | UUID,
        payload: TestSuiteUpdate,
    ) -> TestSuite:
        async with self._business_lock:
            suite = await self._get_unlocked(suite_id)
            await self._require_active_project(suite.project_id)
            if suite.status != TestSuiteStatus.ACTIVE:
                raise InvalidStateError("已归档测试套件不能编辑")

            raw_changes = payload.model_dump(exclude_unset=True)
            candidate = suite
            if "parent_id" in raw_changes:
                parent_id = (
                    parse_uuid(raw_changes["parent_id"], "parent_id")
                    if raw_changes["parent_id"] is not None
                    else None
                )
                await self._validate_parent(
                    project_id=suite.project_id,
                    suite_id=suite.id,
                    parent_id=parent_id,
                )
                candidate = candidate.model_copy(update={"parent_id": parent_id})

            simple_changes = {
                field: value
                for field, value in raw_changes.items()
                if field != "parent_id" and value is not None
            }
            if simple_changes:
                candidate = candidate.model_copy(update=simple_changes)

            await self._ensure_unique_name(
                project_id=suite.project_id,
                parent_id=candidate.parent_id,
                name=candidate.name,
                exclude_id=suite.id,
            )
            audit_changes = self._suite_changes(suite, candidate)
            if not audit_changes:
                return suite

            candidate = candidate.model_copy(update={"updated_at": utc_now()})
            updated = await self._test_suites.update(candidate)
            await self._audit._record_unlocked(
                project_id=suite.project_id,
                entity_type="test_suite",
                entity_id=suite.id,
                action=AuditAction.UPDATED,
                changes=audit_changes,
                comment="更新测试套件",
            )
            return updated

    async def transition(
        self,
        suite_id: str | UUID,
        target: TestSuiteStatus,
    ) -> TestSuite:
        async with self._business_lock:
            suite = await self._get_unlocked(suite_id)
            if suite.status == target:
                return suite
            await self._require_active_project(suite.project_id)
            if target == TestSuiteStatus.ACTIVE:
                await self._validate_parent(
                    project_id=suite.project_id,
                    suite_id=suite.id,
                    parent_id=suite.parent_id,
                )

            updated = suite.model_copy(
                update={"status": target, "updated_at": utc_now()}
            )
            updated = await self._test_suites.update(updated)
            await self._audit._record_unlocked(
                project_id=suite.project_id,
                entity_type="test_suite",
                entity_id=suite.id,
                action=AuditAction.STATUS_CHANGED,
                changes={
                    "status": AuditChange(
                        before=suite.status.value,
                        after=target.value,
                    )
                },
                comment="变更测试套件状态",
            )
            return updated

    async def delete(self, suite_id: str | UUID) -> UUID:
        async with self._business_lock:
            suite = await self._get_unlocked(suite_id)
            await self._require_active_project(suite.project_id)
            suites = await self._test_suites.list()
            if any(item.parent_id == suite.id for item in suites):
                raise ConflictError("测试套件仍有子套件，不能删除")
            cases = await self._test_cases.list()
            if any(test_case.suite_id == suite.id for test_case in cases):
                raise ConflictError("测试套件仍有关联测试用例，不能删除")

            await self._test_suites.delete(suite.id)
            await self._audit._record_unlocked(
                project_id=suite.project_id,
                entity_type="test_suite",
                entity_id=suite.id,
                action=AuditAction.DELETED,
                changes={
                    "name": AuditChange(before=suite.name, after=None),
                    "parent_id": AuditChange(
                        before=self._uuid_value(suite.parent_id),
                        after=None,
                    ),
                },
                comment="删除测试套件",
            )
            return suite.id

    async def _require_active_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError("项目", project_id)
        if project.status != ProjectStatus.ACTIVE:
            raise InvalidStateError("已归档项目不能修改测试套件")
        return project

    async def _validate_parent(
        self,
        *,
        project_id: UUID,
        suite_id: UUID | None,
        parent_id: UUID | None,
    ) -> None:
        if parent_id is None:
            return
        current_id: UUID | None = parent_id
        visited: set[UUID] = set()
        while current_id is not None:
            if current_id in visited or current_id == suite_id:
                raise ConflictError("测试套件层级不能形成循环")
            visited.add(current_id)
            current = await self._test_suites.get(current_id)
            if current is None:
                raise NotFoundError("父测试套件", current_id)
            if current.project_id != project_id:
                raise ConflictError("父测试套件必须属于同一项目")
            if current.status != TestSuiteStatus.ACTIVE:
                raise InvalidStateError("不能放入已归档的测试套件")
            current_id = current.parent_id

    async def _ensure_unique_name(
        self,
        *,
        project_id: UUID,
        parent_id: UUID | None,
        name: str,
        exclude_id: UUID | None = None,
    ) -> None:
        normalized = name.casefold()
        for existing in await self._test_suites.list():
            if existing.id == exclude_id:
                continue
            if (
                existing.project_id == project_id
                and existing.parent_id == parent_id
                and existing.name.casefold() == normalized
            ):
                raise ConflictError(f"同级测试套件名称已存在: {name}")

    @classmethod
    def _suite_changes(
        cls,
        before: TestSuite,
        after: TestSuite,
    ) -> dict[str, AuditChange]:
        changes: dict[str, AuditChange] = {}
        fields = ("parent_id", "name", "description", "position")
        for field in fields:
            before_value = getattr(before, field)
            after_value = getattr(after, field)
            if before_value == after_value:
                continue
            changes[field] = AuditChange(
                before=cls._uuid_value(before_value),
                after=cls._uuid_value(after_value),
            )
        return changes

    @staticmethod
    def _uuid_value(value: object) -> object:
        return str(value) if isinstance(value, UUID) else value
