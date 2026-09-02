from __future__ import annotations

from asyncio import Lock
from typing import Protocol
from uuid import UUID

from app.core.errors import ConflictError, NotFoundError
from app.domain.models import (
    Defect,
    DefectStatus,
    ExecutionStatus,
    Project,
    ProjectStatus,
    TestCase,
    TestExecution,
    TestPlan,
    TestPlanStatus,
    TestCaseSnapshot,
    TestSuite,
    utc_now,
)
from app.repositories.base import AsyncRepository
from app.schemas.projects import ProjectCreate, ProjectUpdate
from app.services.common import parse_uuid


class ProjectCacheInvalidator(Protocol):
    async def invalidate(self, project_id: UUID) -> None: ...


class ProjectService:
    _ACTIVE_PLAN_STATUSES = {
        TestPlanStatus.DRAFT,
        TestPlanStatus.READY,
        TestPlanStatus.RUNNING,
    }
    _ACTIVE_EXECUTION_STATUSES = {
        ExecutionStatus.CREATED,
        ExecutionStatus.RUNNING,
    }

    def __init__(
        self,
        projects: AsyncRepository[Project],
        test_cases: AsyncRepository[TestCase],
        test_plans: AsyncRepository[TestPlan],
        executions: AsyncRepository[TestExecution],
        defects: AsyncRepository[Defect],
        test_suites: AsyncRepository[TestSuite],
        snapshots: AsyncRepository[TestCaseSnapshot],
        business_lock: Lock,
        project_queries: AsyncRepository[Project] | None = None,
        cache_invalidator: ProjectCacheInvalidator | None = None,
    ) -> None:
        self._projects = projects
        self._project_queries = project_queries or projects
        self._cache_invalidator = cache_invalidator
        self._test_cases = test_cases
        self._test_plans = test_plans
        self._executions = executions
        self._defects = defects
        self._test_suites = test_suites
        self._snapshots = snapshots
        self._business_lock = business_lock

    async def create(self, payload: ProjectCreate) -> Project:
        async with self._business_lock:
            project = Project(
                key=payload.key.upper(),
                name=payload.name,
                description=payload.description,
            )
            created = await self._projects.create(project, unique_fields=("key",))
            await self._invalidate_cache(created.id)
            return created

    async def list(self, status: ProjectStatus | None = None) -> list[Project]:
        async with self._business_lock:
            return await self._list_unlocked(status)

    async def _list_unlocked(
        self,
        status: ProjectStatus | None = None,
    ) -> list[Project]:
        items = await self._project_queries.list()
        if status is not None:
            items = [item for item in items if item.status == status]
        return sorted(items, key=lambda item: item.created_at)

    async def get(self, project_id: str | UUID) -> Project:
        async with self._business_lock:
            parsed_id = parse_uuid(project_id, "project_id")
            project = await self._project_queries.get(parsed_id)
            if project is None:
                raise NotFoundError("项目", parsed_id)
            return project

    async def _get_unlocked(self, project_id: str | UUID) -> Project:
        parsed_id = parse_uuid(project_id, "project_id")
        project = await self._projects.get(parsed_id)
        if project is None:
            raise NotFoundError("项目", parsed_id)
        return project

    async def update(
        self,
        project_id: str | UUID,
        payload: ProjectUpdate,
    ) -> Project:
        async with self._business_lock:
            project = await self._get_unlocked(project_id)
            changes = payload.model_dump(exclude_unset=True, exclude_none=True)
            changes["updated_at"] = utc_now()
            updated = project.model_copy(update=changes)
            saved = await self._projects.update(updated, unique_fields=("key",))
            await self._invalidate_cache(saved.id)
            return saved

    async def transition(
        self,
        project_id: str | UUID,
        target: ProjectStatus,
    ) -> Project:
        async with self._business_lock:
            project = await self._get_unlocked(project_id)
            if project.status == target:
                return project
            if target == ProjectStatus.ARCHIVED:
                await self._ensure_project_has_no_active_work(project.id)
            updated = project.model_copy(
                update={"status": target, "updated_at": utc_now()}
            )
            saved = await self._projects.update(updated, unique_fields=("key",))
            await self._invalidate_cache(saved.id)
            return saved

    async def delete(self, project_id: str | UUID) -> UUID:
        async with self._business_lock:
            project = await self._get_unlocked(project_id)
            cases = await self._test_cases.list()
            plans = await self._test_plans.list()
            if any(case.project_id == project.id for case in cases):
                raise ConflictError("项目仍有关联测试用例，不能删除")
            if any(plan.project_id == project.id for plan in plans):
                raise ConflictError("项目仍有关联测试计划，不能删除")
            defects = await self._defects.list()
            if any(defect.project_id == project.id for defect in defects):
                raise ConflictError("项目仍有关联缺陷，不能删除")
            suites = await self._test_suites.list()
            if any(suite.project_id == project.id for suite in suites):
                raise ConflictError("项目仍有关联测试套件，不能删除")
            snapshots = await self._snapshots.list()
            if any(snapshot.project_id == project.id for snapshot in snapshots):
                raise ConflictError("项目仍有测试用例快照，不能删除")
            await self._projects.delete(project.id)
            await self._invalidate_cache(project.id)
            return project.id

    async def _invalidate_cache(self, project_id: UUID) -> None:
        if self._cache_invalidator is not None:
            await self._cache_invalidator.invalidate(project_id)

    async def _ensure_project_has_no_active_work(self, project_id: UUID) -> None:
        plans = await self._test_plans.list()
        if any(
            plan.project_id == project_id
            and plan.status in self._ACTIVE_PLAN_STATUSES
            for plan in plans
        ):
            raise ConflictError("项目仍有未结束的测试计划，不能归档")

        executions = await self._executions.list()
        if any(
            execution.project_id == project_id
            and execution.status in self._ACTIVE_EXECUTION_STATUSES
            for execution in executions
        ):
            raise ConflictError("项目仍有未结束的测试执行，不能归档")

        defects = await self._defects.list()
        if any(
            defect.project_id == project_id
            and defect.status != DefectStatus.CLOSED
            for defect in defects
        ):
            raise ConflictError("项目仍有未关闭缺陷，不能归档")
