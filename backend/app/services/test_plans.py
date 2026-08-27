from __future__ import annotations

from asyncio import Lock
from uuid import UUID

from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.domain.models import (
    Project,
    ProjectStatus,
    TestCase,
    TestCaseStatus,
    TestExecution,
    TestPlan,
    TestPlanStatus,
    utc_now,
)
from app.repositories.base import AsyncRepository
from app.schemas.test_plans import TestPlanCreate, TestPlanUpdate
from app.services.common import parse_uuid


class TestPlanService:
    _USER_TRANSITIONS = {
        TestPlanStatus.DRAFT: {TestPlanStatus.READY},
        TestPlanStatus.READY: {TestPlanStatus.DRAFT},
        TestPlanStatus.RUNNING: set(),
        TestPlanStatus.COMPLETED: set(),
        TestPlanStatus.CANCELLED: set(),
    }

    def __init__(
        self,
        test_plans: AsyncRepository[TestPlan],
        projects: AsyncRepository[Project],
        test_cases: AsyncRepository[TestCase],
        executions: AsyncRepository[TestExecution],
        business_lock: Lock,
    ) -> None:
        self._test_plans = test_plans
        self._projects = projects
        self._test_cases = test_cases
        self._executions = executions
        self._business_lock = business_lock

    async def create(self, payload: TestPlanCreate) -> TestPlan:
        async with self._business_lock:
            project_id = parse_uuid(payload.project_id, "project_id")
            await self._require_active_project(project_id)
            case_ids = [parse_uuid(value, "case_id") for value in payload.case_ids]
            await self._validate_cases(project_id, case_ids, require_active=False)
            plan = TestPlan(
                project_id=project_id,
                name=payload.name,
                description=payload.description,
                case_ids=case_ids,
            )
            return await self._test_plans.create(plan)

    async def list(
        self,
        project_id: str | UUID | None = None,
        status: TestPlanStatus | None = None,
    ) -> list[TestPlan]:
        async with self._business_lock:
            return await self._list_unlocked(project_id, status)

    async def _list_unlocked(
        self,
        project_id: str | UUID | None = None,
        status: TestPlanStatus | None = None,
    ) -> list[TestPlan]:
        items = await self._test_plans.list()
        if project_id is not None:
            parsed_project_id = parse_uuid(project_id, "project_id")
            items = [item for item in items if item.project_id == parsed_project_id]
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at)

    async def get(self, plan_id: str | UUID) -> TestPlan:
        async with self._business_lock:
            return await self._get_unlocked(plan_id)

    async def _get_unlocked(self, plan_id: str | UUID) -> TestPlan:
        parsed_id = parse_uuid(plan_id, "plan_id")
        plan = await self._test_plans.get(parsed_id)
        if plan is None:
            raise NotFoundError("测试计划", parsed_id)
        return plan

    async def update(
        self,
        plan_id: str | UUID,
        payload: TestPlanUpdate,
    ) -> TestPlan:
        async with self._business_lock:
            plan = await self._get_unlocked(plan_id)
            if plan.status != TestPlanStatus.DRAFT:
                raise InvalidStateError("只有草稿测试计划可以编辑")
            changes = payload.model_dump(exclude_unset=True, exclude_none=True)
            if "case_ids" in changes:
                case_ids = [
                    parse_uuid(value, "case_id") for value in changes["case_ids"]
                ]
                await self._validate_cases(
                    plan.project_id,
                    case_ids,
                    require_active=False,
                )
                changes["case_ids"] = case_ids
            changes["updated_at"] = utc_now()
            return await self._test_plans.update(plan.model_copy(update=changes))

    async def transition(
        self,
        plan_id: str | UUID,
        target: TestPlanStatus,
    ) -> TestPlan:
        async with self._business_lock:
            plan = await self._get_unlocked(plan_id)
            if plan.status == target:
                return plan
            if target not in self._USER_TRANSITIONS[plan.status]:
                raise InvalidStateError(
                    f"测试计划不能从 {plan.status.value} 变为 {target.value}"
                )
            if target == TestPlanStatus.READY:
                await self._require_active_project(plan.project_id)
                if not plan.case_ids:
                    raise InvalidStateError("空测试计划不能进入就绪状态")
                await self._validate_cases(
                    plan.project_id,
                    plan.case_ids,
                    require_active=True,
                )
            if target == TestPlanStatus.DRAFT:
                executions = await self._executions.list()
                if any(execution.plan_id == plan.id for execution in executions):
                    raise InvalidStateError("已有执行记录的测试计划不能退回草稿")
            return await self._set_status_unlocked(plan.id, target)

    async def _set_status_unlocked(
        self,
        plan_id: str | UUID,
        target: TestPlanStatus,
    ) -> TestPlan:
        """调用方必须已持有共享业务锁。"""
        plan = await self._get_unlocked(plan_id)
        updated = plan.model_copy(update={"status": target, "updated_at": utc_now()})
        return await self._test_plans.update(updated)

    async def delete(self, plan_id: str | UUID) -> UUID:
        async with self._business_lock:
            plan = await self._get_unlocked(plan_id)
            if plan.status not in {TestPlanStatus.DRAFT, TestPlanStatus.READY}:
                raise InvalidStateError("运行过的测试计划不能删除")
            executions = await self._executions.list()
            if any(execution.plan_id == plan.id for execution in executions):
                raise ConflictError("测试计划已有执行记录，不能删除")
            await self._test_plans.delete(plan.id)
            return plan.id

    async def _require_active_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError("项目", project_id)
        if project.status != ProjectStatus.ACTIVE:
            raise InvalidStateError("已归档项目不能创建或执行测试计划")
        return project

    async def _validate_cases(
        self,
        project_id: UUID,
        case_ids: list[UUID],
        *,
        require_active: bool,
    ) -> None:
        if len(case_ids) != len(set(case_ids)):
            raise ConflictError("测试计划不能包含重复用例")
        for case_id in case_ids:
            test_case = await self._test_cases.get(case_id)
            if test_case is None:
                raise NotFoundError("测试用例", case_id)
            if test_case.project_id != project_id:
                raise ConflictError("测试计划只能引用同一项目中的测试用例")
            if require_active and test_case.status != TestCaseStatus.ACTIVE:
                raise InvalidStateError(f"测试用例未启用: {case_id}")
