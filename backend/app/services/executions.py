from __future__ import annotations

from asyncio import Lock
from uuid import UUID

from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.domain.models import (
    CaseExecutionResult,
    CaseResultStatus,
    Defect,
    ExecutionStatus,
    Project,
    ProjectStatus,
    TestCase,
    TestExecution,
    TestPlanStatus,
    utc_now,
)
from app.repositories.base import AsyncRepository
from app.services.common import parse_uuid
from app.services.test_plans import TestPlanService


class ExecutionService:
    def __init__(
        self,
        executions: AsyncRepository[TestExecution],
        test_cases: AsyncRepository[TestCase],
        projects: AsyncRepository[Project],
        plans: TestPlanService,
        defects: AsyncRepository[Defect],
        business_lock: Lock,
    ) -> None:
        self._executions = executions
        self._test_cases = test_cases
        self._projects = projects
        self._plans = plans
        self._defects = defects
        self._business_lock = business_lock

    async def create(self, plan_id: str | UUID) -> TestExecution:
        async with self._business_lock:
            plan = await self._plans._get_unlocked(plan_id)
            await self._require_active_project(plan.project_id)
            if plan.status != TestPlanStatus.READY:
                raise InvalidStateError("只有就绪测试计划可以创建执行")
            existing = await self._executions.list()
            if any(item.plan_id == plan.id for item in existing):
                raise ConflictError("该测试计划已经存在执行记录")
            results: list[CaseExecutionResult] = []
            for case_id in plan.case_ids:
                test_case = await self._test_cases.get(case_id)
                if test_case is None:
                    raise NotFoundError("测试用例", case_id)
                results.append(
                    CaseExecutionResult(
                        case_id=test_case.id,
                        case_title=test_case.title,
                    )
                )
            execution = TestExecution(
                plan_id=plan.id,
                project_id=plan.project_id,
                results=results,
            )
            return await self._executions.create(execution)

    async def list(
        self,
        plan_id: str | UUID | None = None,
        status: ExecutionStatus | None = None,
    ) -> list[TestExecution]:
        async with self._business_lock:
            return await self._list_unlocked(plan_id, status)

    async def _list_unlocked(
        self,
        plan_id: str | UUID | None = None,
        status: ExecutionStatus | None = None,
    ) -> list[TestExecution]:
        items = await self._executions.list()
        if plan_id is not None:
            parsed_plan_id = parse_uuid(plan_id, "plan_id")
            items = [item for item in items if item.plan_id == parsed_plan_id]
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at)

    async def get(self, execution_id: str | UUID) -> TestExecution:
        async with self._business_lock:
            return await self._get_unlocked(execution_id)

    async def _get_unlocked(
        self,
        execution_id: str | UUID,
    ) -> TestExecution:
        parsed_id = parse_uuid(execution_id, "execution_id")
        execution = await self._executions.get(parsed_id)
        if execution is None:
            raise NotFoundError("测试执行", parsed_id)
        return execution

    async def transition(
        self,
        execution_id: str | UUID,
        target: ExecutionStatus,
    ) -> TestExecution:
        async with self._business_lock:
            execution = await self._get_unlocked(execution_id)
            if execution.status == target:
                return execution
            if target == ExecutionStatus.RUNNING:
                if execution.status != ExecutionStatus.CREATED:
                    raise InvalidStateError("只有新建执行可以开始")
                await self._require_active_project(execution.project_id)
                plan = await self._plans._get_unlocked(execution.plan_id)
                if plan.status != TestPlanStatus.READY:
                    raise InvalidStateError("关联测试计划已不在就绪状态")
                now = utc_now()
                updated = execution.model_copy(
                    update={"status": target, "started_at": now, "updated_at": now}
                )
                await self._plans._set_status_unlocked(
                    execution.plan_id,
                    TestPlanStatus.RUNNING,
                )
            elif target == ExecutionStatus.COMPLETED:
                if execution.status != ExecutionStatus.RUNNING:
                    raise InvalidStateError("只有运行中的执行可以完成")
                unfinished = [
                    result
                    for result in execution.results
                    if result.status == CaseResultStatus.NOT_RUN
                ]
                if unfinished:
                    raise InvalidStateError("仍有未执行用例，不能完成测试执行")
                now = utc_now()
                updated = execution.model_copy(
                    update={"status": target, "completed_at": now, "updated_at": now}
                )
                await self._plans._set_status_unlocked(
                    execution.plan_id,
                    TestPlanStatus.COMPLETED,
                )
            elif target == ExecutionStatus.CANCELLED:
                if execution.status not in {
                    ExecutionStatus.CREATED,
                    ExecutionStatus.RUNNING,
                }:
                    raise InvalidStateError("当前测试执行不能取消")
                now = utc_now()
                updated = execution.model_copy(
                    update={"status": target, "completed_at": now, "updated_at": now}
                )
                await self._plans._set_status_unlocked(
                    execution.plan_id,
                    TestPlanStatus.CANCELLED,
                )
            else:
                raise InvalidStateError(f"不支持转换到状态: {target.value}")
            return await self._executions.update(updated)

    async def update_case_result(
        self,
        execution_id: str | UUID,
        case_id: str | UUID,
        status: CaseResultStatus,
        actual_result: str,
        comment: str,
    ) -> TestExecution:
        async with self._business_lock:
            execution = await self._get_unlocked(execution_id)
            if execution.status != ExecutionStatus.RUNNING:
                raise InvalidStateError("只有运行中的执行可以记录用例结果")
            if status == CaseResultStatus.NOT_RUN:
                raise InvalidStateError("不能把执行结果更新为 not_run")
            parsed_case_id = parse_uuid(case_id, "case_id")
            matched = False
            results: list[CaseExecutionResult] = []
            for result in execution.results:
                if result.case_id == parsed_case_id:
                    matched = True
                    result = result.model_copy(
                        update={
                            "status": status,
                            "actual_result": actual_result,
                            "comment": comment,
                            "executed_at": utc_now(),
                        }
                    )
                results.append(result)
            if not matched:
                raise NotFoundError("执行中的测试用例", parsed_case_id)
            updated = execution.model_copy(
                update={"results": results, "updated_at": utc_now()}
            )
            return await self._executions.update(updated)

    async def delete(self, execution_id: str | UUID) -> UUID:
        async with self._business_lock:
            execution = await self._get_unlocked(execution_id)
            if execution.status != ExecutionStatus.CREATED:
                raise InvalidStateError("只有尚未开始的测试执行可以删除")
            defects = await self._defects.list()
            if any(defect.execution_id == execution.id for defect in defects):
                raise ConflictError("测试执行已被缺陷引用，不能删除")
            await self._executions.delete(execution.id)
            return execution.id

    async def _require_active_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError("项目", project_id)
        if project.status != ProjectStatus.ACTIVE:
            raise InvalidStateError("已归档项目不能创建或开始测试执行")
        return project
