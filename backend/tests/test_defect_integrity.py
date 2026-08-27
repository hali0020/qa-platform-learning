import asyncio

import pytest

from app.container import build_container
from app.core.errors import ConflictError, InvalidStateError
from app.domain.models import (
    AuditAction,
    DefectStatus,
    ExecutionStatus,
    ProjectStatus,
    TestCaseStatus as CaseStatus,
    TestPlanStatus as PlanStatus,
    TestStep as Step,
)
from app.schemas.defects import DefectCreate, DefectTransition, DefectUpdate
from app.schemas.projects import ProjectCreate
from app.schemas.test_cases import TestCaseCreate as CaseCreateSchema
from app.schemas.test_plans import TestPlanCreate as PlanCreateSchema


@pytest.mark.asyncio
async def test_defect_and_audit_services_share_business_lock() -> None:
    container = build_container()

    assert container.defects._business_lock is container.projects._business_lock
    assert container.audit._business_lock is container.projects._business_lock


@pytest.mark.asyncio
async def test_concurrent_defect_transitions_remain_consistent() -> None:
    container = build_container()
    project = await container.projects.create(
        ProjectCreate(key="DEFECT-RACE", name="Defect race")
    )
    defect = await container.defects.create(
        DefectCreate(
            project_id=str(project.id),
            title="并发状态变更",
            assignee="developer-local",
        )
    )

    results = await asyncio.gather(
        container.defects.transition(
            defect.id,
            DefectTransition(status=DefectStatus.IN_PROGRESS),
        ),
        container.defects.transition(
            defect.id,
            DefectTransition(status=DefectStatus.VERIFIED),
        ),
        return_exceptions=True,
    )

    assert sum(not isinstance(result, Exception) for result in results) == 1
    assert sum(isinstance(result, InvalidStateError) for result in results) == 1
    restored = await container.defects.get(defect.id)
    assert restored.status == DefectStatus.IN_PROGRESS
    transitions = await container.audit.list(
        entity_type="defect",
        entity_id=defect.id,
        action=AuditAction.STATUS_CHANGED,
    )
    assert len(transitions) == 1
    assert transitions[0].changes["status"].after == "in_progress"

    with pytest.raises(InvalidStateError, match="保留负责人"):
        await container.defects.update(
            defect.id,
            DefectUpdate(assignee=""),
        )
    unchanged = await container.defects.get(defect.id)
    assert unchanged.assignee == "developer-local"


@pytest.mark.asyncio
async def test_open_defect_blocks_project_archive_until_closed() -> None:
    container = build_container()
    project = await container.projects.create(
        ProjectCreate(key="QUALITY", name="Quality gate")
    )
    defect = await container.defects.create(
        DefectCreate(
            project_id=str(project.id),
            title="阻断归档",
            assignee="developer-local",
        )
    )

    with pytest.raises(ConflictError, match="缺陷"):
        await container.projects.transition(project.id, ProjectStatus.ARCHIVED)

    await container.defects.transition(
        defect.id,
        DefectTransition(
            status=DefectStatus.RESOLVED,
            resolution="已修复",
        ),
    )
    await container.defects.transition(
        defect.id,
        DefectTransition(status=DefectStatus.VERIFIED),
    )
    await container.defects.transition(
        defect.id,
        DefectTransition(status=DefectStatus.CLOSED),
    )

    archived = await container.projects.transition(
        project.id,
        ProjectStatus.ARCHIVED,
    )
    assert archived.status == ProjectStatus.ARCHIVED


@pytest.mark.asyncio
async def test_defect_reference_protects_test_case_deletion() -> None:
    container = build_container()
    project = await container.projects.create(
        ProjectCreate(key="TRACE", name="Traceability")
    )
    test_case = await container.test_cases.create(
        CaseCreateSchema(project_id=str(project.id), title="被缺陷引用的用例")
    )
    await container.defects.create(
        DefectCreate(
            project_id=str(project.id),
            case_id=str(test_case.id),
            title="关联用例缺陷",
        )
    )

    with pytest.raises(ConflictError, match="缺陷"):
        await container.test_cases.delete(test_case.id)


@pytest.mark.asyncio
async def test_execution_and_case_links_must_describe_the_same_run() -> None:
    container = build_container()
    project = await container.projects.create(
        ProjectCreate(key="EXEC-BUG", name="Execution defects")
    )
    included = await container.test_cases.create(
        CaseCreateSchema(
            project_id=str(project.id),
            title="执行内用例",
            steps=[Step(action="执行", expected_result="成功")],
        )
    )
    included = await container.test_cases.transition(
        included.id,
        CaseStatus.ACTIVE,
    )
    outside = await container.test_cases.create(
        CaseCreateSchema(project_id=str(project.id), title="执行外用例")
    )
    plan = await container.test_plans.create(
        PlanCreateSchema(
            project_id=str(project.id),
            name="缺陷关联计划",
            case_ids=[str(included.id)],
        )
    )
    plan = await container.test_plans.transition(plan.id, PlanStatus.READY)
    execution = await container.executions.create(plan.id)

    with pytest.raises(InvalidStateError, match="尚未开始"):
        await container.defects.create(
            DefectCreate(
                project_id=str(project.id),
                case_id=str(included.id),
                execution_id=str(execution.id),
                title="过早关联",
            )
        )

    execution = await container.executions.transition(
        execution.id,
        ExecutionStatus.RUNNING,
    )
    with pytest.raises(ConflictError, match="不属于指定测试执行"):
        await container.defects.create(
            DefectCreate(
                project_id=str(project.id),
                case_id=str(outside.id),
                execution_id=str(execution.id),
                title="错误执行关联",
            )
        )

    linked = await container.defects.create(
        DefectCreate(
            project_id=str(project.id),
            case_id=str(included.id),
            execution_id=str(execution.id),
            title="正确执行关联",
        )
    )
    assert linked.case_id == included.id
    assert linked.execution_id == execution.id
