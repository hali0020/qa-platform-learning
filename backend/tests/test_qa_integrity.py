import asyncio
from uuid import uuid4

import pytest

import app.domain.models as domain_models
from app.container import ApplicationContainer, build_container
from app.core.errors import ConflictError, InvalidStateError, NotFoundError
from app.repositories.memory import InMemoryRepository
from app.schemas.projects import ProjectCreate
from app.schemas.test_cases import TestCaseCreate as CaseCreateSchema
from app.schemas.test_plans import TestPlanCreate as PlanCreateSchema


async def create_ready_plan(
    container: ApplicationContainer,
) -> tuple[domain_models.Project, domain_models.TestPlan]:
    project = await container.projects.create(
        ProjectCreate(key="SHOP", name="Shop project")
    )
    test_case = await container.test_cases.create(
        CaseCreateSchema(
            project_id=str(project.id),
            title="用户可以登录",
            steps=[
                domain_models.TestStep(
                    action="提交正确账号密码",
                    expected_result="进入首页",
                )
            ],
        )
    )
    await container.test_cases.transition(
        test_case.id,
        domain_models.TestCaseStatus.ACTIVE,
    )
    plan = await container.test_plans.create(
        PlanCreateSchema(
            project_id=str(project.id),
            name="冒烟测试",
            case_ids=[str(test_case.id)],
        )
    )
    plan = await container.test_plans.transition(
        plan.id,
        domain_models.TestPlanStatus.READY,
    )
    return project, plan


@pytest.mark.asyncio
async def test_all_qa_services_share_one_business_lock() -> None:
    container = build_container()

    assert container.projects._business_lock is container.test_cases._business_lock
    assert container.projects._business_lock is container.test_plans._business_lock
    assert container.projects._business_lock is container.executions._business_lock


@pytest.mark.asyncio
async def test_concurrent_execution_creation_is_atomic(monkeypatch) -> None:
    container = build_container()
    _, plan = await create_ready_plan(container)
    repository = container.executions._executions
    original_create = repository.create
    first_create_entered = asyncio.Event()
    allow_first_create = asyncio.Event()
    create_calls = 0

    async def delayed_create(*args, **kwargs):
        nonlocal create_calls
        create_calls += 1
        if create_calls == 1:
            first_create_entered.set()
            await allow_first_create.wait()
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(repository, "create", delayed_create)
    first = asyncio.create_task(container.executions.create(plan.id))
    await asyncio.wait_for(first_create_entered.wait(), timeout=1)
    second = asyncio.create_task(container.executions.create(plan.id))

    try:
        await asyncio.sleep(0)
        assert create_calls == 1
    finally:
        allow_first_create.set()

    results = await asyncio.gather(first, second, return_exceptions=True)
    assert sum(isinstance(result, domain_models.TestExecution) for result in results) == 1
    assert sum(isinstance(result, ConflictError) for result in results) == 1
    assert len(await container.executions.list(plan.id)) == 1


@pytest.mark.asyncio
async def test_concurrent_plan_create_and_project_archive_stay_consistent(
    monkeypatch,
) -> None:
    container = build_container()
    project = await container.projects.create(
        ProjectCreate(key="RACE", name="Race project")
    )
    repository = container.test_plans._test_plans
    original_create = repository.create
    plan_create_entered = asyncio.Event()
    allow_plan_create = asyncio.Event()

    async def delayed_create(*args, **kwargs):
        plan_create_entered.set()
        await allow_plan_create.wait()
        return await original_create(*args, **kwargs)

    monkeypatch.setattr(repository, "create", delayed_create)
    plan_task = asyncio.create_task(
        container.test_plans.create(
            PlanCreateSchema(project_id=str(project.id), name="并发计划")
        )
    )
    await asyncio.wait_for(plan_create_entered.wait(), timeout=1)
    archive_task = asyncio.create_task(
        container.projects.transition(
            project.id,
            domain_models.ProjectStatus.ARCHIVED,
        )
    )

    try:
        await asyncio.sleep(0)
        assert not archive_task.done()
    finally:
        allow_plan_create.set()

    await plan_task
    with pytest.raises(ConflictError, match="未结束的测试计划"):
        await archive_task
    assert (await container.projects.get(project.id)).status == domain_models.ProjectStatus.ACTIVE


@pytest.mark.asyncio
async def test_project_archive_rejects_active_execution() -> None:
    container = build_container()
    project = await container.projects.create(
        ProjectCreate(key="EXEC", name="Execution project")
    )
    await container.executions._executions.create(
        domain_models.TestExecution(
            plan_id=uuid4(),
            project_id=project.id,
        )
    )

    with pytest.raises(ConflictError, match="未结束的测试执行"):
        await container.projects.transition(
            project.id,
            domain_models.ProjectStatus.ARCHIVED,
        )


@pytest.mark.asyncio
async def test_archived_project_blocks_execution_create_and_start() -> None:
    container = build_container()
    project, plan = await create_ready_plan(container)
    archived = project.model_copy(
        update={"status": domain_models.ProjectStatus.ARCHIVED}
    )
    await container.projects._projects.update(archived, unique_fields=("key",))

    with pytest.raises(InvalidStateError, match="已归档项目"):
        await container.executions.create(plan.id)

    active = archived.model_copy(
        update={"status": domain_models.ProjectStatus.ACTIVE}
    )
    await container.projects._projects.update(active, unique_fields=("key",))
    execution = await container.executions.create(plan.id)
    await container.projects._projects.update(archived, unique_fields=("key",))

    with pytest.raises(InvalidStateError, match="已归档项目"):
        await container.executions.transition(
            execution.id,
            domain_models.ExecutionStatus.RUNNING,
        )


@pytest.mark.asyncio
async def test_repository_update_missing_entity_raises_domain_not_found() -> None:
    repository: InMemoryRepository[domain_models.Project] = InMemoryRepository()
    project = domain_models.Project(key="SAFE", name="Safe project")

    with pytest.raises(NotFoundError, match="实体不存在"):
        await repository.update(project)
