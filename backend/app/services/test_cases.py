from __future__ import annotations

from asyncio import Lock
from uuid import UUID

from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.domain.models import (
    Defect,
    Project,
    ProjectStatus,
    TestCase,
    TestCaseStatus,
    TestPlan,
    TestSuite,
    TestSuiteStatus,
    utc_now,
)
from app.repositories.base import AsyncRepository
from app.schemas.test_cases import TestCaseCreate, TestCaseUpdate
from app.services.common import parse_uuid


class TestCaseService:
    _TRANSITIONS = {
        TestCaseStatus.DRAFT: {TestCaseStatus.ACTIVE},
        TestCaseStatus.ACTIVE: {TestCaseStatus.DISABLED},
        TestCaseStatus.DISABLED: {TestCaseStatus.ACTIVE},
    }

    def __init__(
        self,
        test_cases: AsyncRepository[TestCase],
        projects: AsyncRepository[Project],
        test_plans: AsyncRepository[TestPlan],
        test_suites: AsyncRepository[TestSuite],
        defects: AsyncRepository[Defect],
        business_lock: Lock,
    ) -> None:
        self._test_cases = test_cases
        self._projects = projects
        self._test_plans = test_plans
        self._test_suites = test_suites
        self._defects = defects
        self._business_lock = business_lock

    async def create(self, payload: TestCaseCreate) -> TestCase:
        async with self._business_lock:
            project_id = parse_uuid(payload.project_id, "project_id")
            await self._require_active_project(project_id)
            suite_id = (
                parse_uuid(payload.suite_id, "suite_id")
                if payload.suite_id is not None
                else None
            )
            if suite_id is not None:
                await self._require_active_suite(project_id, suite_id)
            test_case = TestCase(
                project_id=project_id,
                suite_id=suite_id,
                title=payload.title,
                preconditions=payload.preconditions,
                steps=payload.steps,
                priority=payload.priority,
                case_type=payload.case_type,
                tags=self._normalize_tags(payload.tags),
            )
            return await self._test_cases.create(test_case)

    async def list(
        self,
        project_id: str | UUID | None = None,
        status: TestCaseStatus | None = None,
        suite_id: str | UUID | None = None,
        unassigned: bool = False,
    ) -> list[TestCase]:
        async with self._business_lock:
            return await self._list_unlocked(
                project_id,
                status,
                suite_id,
                unassigned,
            )

    async def _list_unlocked(
        self,
        project_id: str | UUID | None = None,
        status: TestCaseStatus | None = None,
        suite_id: str | UUID | None = None,
        unassigned: bool = False,
    ) -> list[TestCase]:
        items = await self._test_cases.list()
        if project_id is not None:
            parsed_project_id = parse_uuid(project_id, "project_id")
            items = [item for item in items if item.project_id == parsed_project_id]
        if status is not None:
            items = [item for item in items if item.status == status]
        if suite_id is not None:
            parsed_suite_id = parse_uuid(suite_id, "suite_id")
            items = [item for item in items if item.suite_id == parsed_suite_id]
        elif unassigned:
            items = [item for item in items if item.suite_id is None]
        return sorted(items, key=lambda item: item.created_at)

    async def get(self, case_id: str | UUID) -> TestCase:
        async with self._business_lock:
            return await self._get_unlocked(case_id)

    async def _get_unlocked(self, case_id: str | UUID) -> TestCase:
        parsed_id = parse_uuid(case_id, "case_id")
        test_case = await self._test_cases.get(parsed_id)
        if test_case is None:
            raise NotFoundError("测试用例", parsed_id)
        return test_case

    async def update(
        self,
        case_id: str | UUID,
        payload: TestCaseUpdate,
    ) -> TestCase:
        async with self._business_lock:
            test_case = await self._get_unlocked(case_id)
            changes = {
                field: getattr(payload, field)
                for field in payload.model_fields_set
                if field == "suite_id" or getattr(payload, field) is not None
            }
            if "suite_id" in changes:
                suite_id = (
                    parse_uuid(changes["suite_id"], "suite_id")
                    if changes["suite_id"] is not None
                    else None
                )
                if suite_id is not None:
                    await self._require_active_suite(test_case.project_id, suite_id)
                changes["suite_id"] = suite_id
            if "tags" in changes:
                changes["tags"] = self._normalize_tags(changes["tags"])
            changes["updated_at"] = utc_now()
            updated = test_case.model_copy(update=changes)
            if updated.status == TestCaseStatus.ACTIVE and not updated.steps:
                raise InvalidStateError("启用状态的测试用例必须至少包含一个步骤")
            return await self._test_cases.update(updated)

    async def transition(
        self,
        case_id: str | UUID,
        target: TestCaseStatus,
    ) -> TestCase:
        async with self._business_lock:
            test_case = await self._get_unlocked(case_id)
            if test_case.status == target:
                return test_case
            if target not in self._TRANSITIONS[test_case.status]:
                raise InvalidStateError(
                    f"测试用例不能从 {test_case.status.value} 变为 {target.value}"
                )
            if target == TestCaseStatus.ACTIVE:
                await self._require_active_project(test_case.project_id)
                if not test_case.steps:
                    raise InvalidStateError("没有步骤的测试用例不能启用")
            updated = test_case.model_copy(
                update={"status": target, "updated_at": utc_now()}
            )
            return await self._test_cases.update(updated)

    async def delete(self, case_id: str | UUID) -> UUID:
        async with self._business_lock:
            test_case = await self._get_unlocked(case_id)
            plans = await self._test_plans.list()
            if any(test_case.id in plan.case_ids for plan in plans):
                raise ConflictError("测试用例已被测试计划引用，不能删除")
            defects = await self._defects.list()
            if any(defect.case_id == test_case.id for defect in defects):
                raise ConflictError("测试用例已被缺陷引用，不能删除")
            await self._test_cases.delete(test_case.id)
            return test_case.id

    async def _require_active_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError("项目", project_id)
        if project.status != ProjectStatus.ACTIVE:
            raise InvalidStateError("已归档项目不能新增或启用测试用例")
        return project

    async def _require_active_suite(
        self,
        project_id: UUID,
        suite_id: UUID,
    ) -> TestSuite:
        suite = await self._test_suites.get(suite_id)
        if suite is None:
            raise NotFoundError("测试套件", suite_id)
        if suite.project_id != project_id:
            raise ConflictError("测试用例只能归入同一项目的套件")
        visited: set[UUID] = set()
        current = suite
        while True:
            if current.id in visited:
                raise ConflictError("测试套件层级存在循环")
            visited.add(current.id)
            if current.status != TestSuiteStatus.ACTIVE:
                raise InvalidStateError("已归档套件不能接收测试用例")
            if current.parent_id is None:
                break
            parent = await self._test_suites.get(current.parent_id)
            if parent is None:
                raise NotFoundError("父测试套件", current.parent_id)
            current = parent
        return suite

    @staticmethod
    def _normalize_tags(tags: list[str]) -> list[str]:
        return sorted({tag.strip().lower() for tag in tags if tag.strip()})
