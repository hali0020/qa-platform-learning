from dataclasses import dataclass
from uuid import UUID

from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.domain.collaboration import CollaborationTargetType
from app.domain.models import (
    Defect,
    Project,
    ProjectStatus,
    TestCase,
    TestCaseSnapshot,
    TestExecution,
    TestPlan,
    TestSuite,
)
from app.repositories.base import AsyncRepository


@dataclass(frozen=True, slots=True)
class ResolvedTarget:
    project: Project
    entity_type: CollaborationTargetType
    entity_id: UUID


class CollaborationTargetResolver:
    def __init__(
        self,
        projects: AsyncRepository[Project],
        test_cases: AsyncRepository[TestCase],
        test_suites: AsyncRepository[TestSuite],
        snapshots: AsyncRepository[TestCaseSnapshot],
        test_plans: AsyncRepository[TestPlan],
        executions: AsyncRepository[TestExecution],
        defects: AsyncRepository[Defect],
    ) -> None:
        self._projects = projects
        self._repositories = {
            CollaborationTargetType.TEST_CASE: test_cases,
            CollaborationTargetType.TEST_SUITE: test_suites,
            CollaborationTargetType.SNAPSHOT: snapshots,
            CollaborationTargetType.TEST_PLAN: test_plans,
            CollaborationTargetType.EXECUTION: executions,
            CollaborationTargetType.DEFECT: defects,
        }

    async def resolve(
        self,
        entity_type: CollaborationTargetType,
        entity_id: UUID,
        *,
        requested_project_id: UUID | None = None,
        require_writable: bool = False,
    ) -> ResolvedTarget:
        if entity_type == CollaborationTargetType.PROJECT:
            project = await self._projects.get(entity_id)
            if project is None:
                raise NotFoundError("协作目标", entity_id)
        else:
            repository = self._repositories.get(entity_type)
            if repository is None:
                raise NotFoundError("协作目标类型", entity_type.value)
            entity = await repository.get(entity_id)
            if entity is None:
                raise NotFoundError("协作目标", entity_id)
            project_id = getattr(entity, "project_id")
            project = await self._projects.get(project_id)
            if project is None:
                raise NotFoundError("项目", project_id)
        if requested_project_id is not None and requested_project_id != project.id:
            raise ConflictError("project_id 与协作目标所属项目不一致")
        if require_writable and project.status != ProjectStatus.ACTIVE:
            raise InvalidStateError("已归档项目不能新增或变更评论和附件")
        return ResolvedTarget(
            project=project,
            entity_type=entity_type,
            entity_id=entity_id,
        )
