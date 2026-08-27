from pathlib import Path
from uuid import uuid4

import pytest

from app.container import build_container
from app.core.errors import ConflictError
from app.database.session import Database
from app.domain.models import (
    CaseResultStatus,
    ExecutionStatus,
    Project,
    TestCase as DomainTestCase,
    TestCaseStatus as CaseStatus,
    TestPlanStatus as PlanStatus,
    TestStep as Step,
)
from app.repositories.sqlalchemy import (
    ProjectRepository,
    TestCaseRepository as CaseRepository,
)
from app.schemas.projects import ProjectCreate
from app.schemas.test_cases import TestCaseCreate as CaseCreateSchema
from app.schemas.test_plans import TestPlanCreate as PlanCreateSchema


def sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


@pytest.mark.asyncio
async def test_complete_qa_flow_survives_new_database_instance(tmp_path: Path) -> None:
    url = sqlite_url(tmp_path / "persistent-qa.db")
    first_database = Database(url)
    first = build_container(first_database)

    project = await first.projects.create(
        ProjectCreate(key="PERSIST", name="持久化项目")
    )
    test_case = await first.test_cases.create(
        CaseCreateSchema(
            project_id=str(project.id),
            title="重启后仍可读取",
            steps=[Step(action="重启服务", expected_result="数据仍存在")],
            tags=["SQLite", " persistence "],
        )
    )
    test_case = await first.test_cases.transition(
        test_case.id,
        CaseStatus.ACTIVE,
    )
    plan = await first.test_plans.create(
        PlanCreateSchema(
            project_id=str(project.id),
            name="持久化回归",
            case_ids=[str(test_case.id)],
        )
    )
    plan = await first.test_plans.transition(plan.id, PlanStatus.READY)
    execution = await first.executions.create(plan.id)
    execution = await first.executions.transition(
        execution.id,
        ExecutionStatus.RUNNING,
    )
    execution = await first.executions.update_case_result(
        execution.id,
        test_case.id,
        CaseResultStatus.PASSED,
        "数据恢复成功",
        "本机 SQLite",
    )
    await first.executions.transition(execution.id, ExecutionStatus.COMPLETED)
    await first.shutdown()

    second_database = Database(url)
    second = build_container(second_database)
    try:
        restored_project = await second.projects.get(project.id)
        restored_case = await second.test_cases.get(test_case.id)
        restored_plan = await second.test_plans.get(plan.id)
        restored_execution = await second.executions.get(execution.id)

        assert restored_project.name == "持久化项目"
        assert restored_case.tags == ["persistence", "sqlite"]
        assert restored_case.steps == test_case.steps
        assert restored_plan.case_ids == [test_case.id]
        assert restored_plan.status == PlanStatus.COMPLETED
        assert restored_execution.status == ExecutionStatus.COMPLETED
        assert restored_execution.results[0].case_id == test_case.id
        assert restored_execution.results[0].status == CaseResultStatus.PASSED
        assert restored_execution.results[0].actual_result == "数据恢复成功"
        assert restored_execution.results[0].comment == "本机 SQLite"
        assert restored_execution.results[0].executed_at is not None
    finally:
        await second.shutdown()


@pytest.mark.asyncio
async def test_database_constraints_become_domain_conflicts(tmp_path: Path) -> None:
    database = Database(sqlite_url(tmp_path / "constraints.db"))
    projects = ProjectRepository(database)
    cases = CaseRepository(database)
    project = Project(key="UNIQUE", name="唯一项目")
    await projects.create(project, unique_fields=("key",))

    try:
        with pytest.raises(ConflictError, match="字段 key 的值已存在"):
            await projects.create(
                Project(key="UNIQUE", name="重复项目"),
                unique_fields=("key",),
            )

        orphan = DomainTestCase(
            project_id=uuid4(),
            title="孤立用例",
            steps=[Step(action="a", expected_result="b")],
        )
        with pytest.raises(ConflictError, match="关联约束"):
            await cases.create(orphan)
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_in_memory_database_uses_the_application_engine() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    projects = ProjectRepository(database)
    project = Project(key="MEMORY", name="内存测试项目")

    try:
        await projects.create(project, unique_fields=("key",))
        restored = await projects.get(project.id)
        assert restored == project
    finally:
        await database.shutdown()
