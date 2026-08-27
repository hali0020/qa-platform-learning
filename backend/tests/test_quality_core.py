from __future__ import annotations

from datetime import date, datetime, timedelta, timezone

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import BusinessValidationError
from app.domain.models import (
    AuditAction,
    AuditChange,
    AuditEvent,
    CaseExecutionResult,
    CaseResultStatus,
    Defect,
    DefectSeverity,
    DefectStatus,
    ExecutionStatus,
    Project,
    TestCase as DomainCase,
    TestCaseStatus as CaseStatus,
    TestCaseType as CaseType,
    TestExecution as DomainExecution,
    TestSuite as DomainSuite,
)
from app.repositories.memory import InMemoryRepository
from app.schemas.quality import TrendGranularity
from app.services.quality import QualityService
from app.api.routes.quality import router as quality_router


async def _quality_service(
    *,
    project: Project,
    cases=(),
    suites=(),
    executions=(),
    defects=(),
    audits=(),
) -> QualityService:
    repositories = [InMemoryRepository() for _ in range(6)]
    projects, case_repo, suite_repo, execution_repo, defect_repo, audit_repo = repositories
    await projects.create(project)
    for repo, items in (
        (case_repo, cases),
        (suite_repo, suites),
        (execution_repo, executions),
        (defect_repo, defects),
        (audit_repo, audits),
    ):
        for item in items:
            await repo.create(item)
    return QualityService(
        projects=projects,
        test_cases=case_repo,
        test_suites=suite_repo,
        executions=execution_repo,
        defects=defect_repo,
        audit_events=audit_repo,
    )


@pytest.mark.asyncio
async def test_quality_report_uses_completed_results_and_documented_denominators() -> None:
    project = Project(key="QUAL", name="Quality")
    root = DomainSuite(project_id=project.id, name="账号")
    child = DomainSuite(project_id=project.id, parent_id=root.id, name="登录")
    active_cases = [
        DomainCase(
            project_id=project.id,
            suite_id=child.id,
            title=f"case-{index}",
            status=CaseStatus.ACTIVE,
            case_type=(CaseType.AUTOMATED if index == 0 else CaseType.MANUAL),
        )
        for index in range(4)
    ]
    disabled = DomainCase(
        project_id=project.id,
        title="disabled",
        status=CaseStatus.DISABLED,
    )
    completed_at = datetime(2026, 8, 20, 16, 30, tzinfo=timezone.utc)
    statuses = [
        CaseResultStatus.PASSED,
        CaseResultStatus.FAILED,
        CaseResultStatus.BLOCKED,
        CaseResultStatus.SKIPPED,
    ]
    completed = DomainExecution(
        plan_id=project.id,
        project_id=project.id,
        status=ExecutionStatus.COMPLETED,
        results=[
            CaseExecutionResult(
                case_id=test_case.id,
                case_title=test_case.title,
                status=status,
                executed_at=completed_at - timedelta(minutes=index),
            )
            for index, (test_case, status) in enumerate(zip(active_cases, statuses))
        ],
        completed_at=completed_at,
    )
    running = DomainExecution(
        plan_id=disabled.id,
        project_id=project.id,
        status=ExecutionStatus.RUNNING,
        results=[
            CaseExecutionResult(
                case_id=active_cases[0].id,
                case_title=active_cases[0].title,
                status=CaseResultStatus.FAILED,
                executed_at=completed_at,
            )
        ],
    )
    linked = Defect(
        project_id=project.id,
        case_id=active_cases[1].id,
        execution_id=completed.id,
        title="失败已提单",
        severity=DefectSeverity.CRITICAL,
        created_at=completed_at,
    )
    closed = Defect(
        project_id=project.id,
        title="已关闭",
        status=DefectStatus.CLOSED,
        created_at=completed_at,
        resolved_at=completed_at + timedelta(hours=1),
        closed_at=completed_at + timedelta(hours=2),
    )
    reopen = AuditEvent(
        project_id=project.id,
        entity_type="defect",
        entity_id=str(closed.id),
        action=AuditAction.STATUS_CHANGED,
        changes={
            "status": AuditChange(before="closed", after="reopened")
        },
        created_at=completed_at + timedelta(hours=3),
    )
    service = await _quality_service(
        project=project,
        cases=[*active_cases, disabled],
        suites=[root, child],
        executions=[completed, running],
        defects=[linked, closed],
        audits=[reopen],
    )

    report = await service.report(
        project_id=project.id,
        date_from=date(2026, 8, 21),
        date_to=date(2026, 8, 21),
        timezone_name="Asia/Shanghai",
        generated_at=datetime(2026, 8, 21, tzinfo=timezone.utc),
    )

    assert report.summary.test_cases.total_current == 5
    assert report.summary.test_cases.automation_coverage.model_dump() == {
        "numerator": 1,
        "denominator": 4,
        "percent": 25.0,
    }
    assert report.summary.test_cases.execution_reach.percent == 100.0
    assert report.summary.executions.completed_executions == 1
    assert report.summary.executions.failed == 1  # running failure is excluded
    assert report.summary.executions.pass_rate.model_dump() == {
        "numerator": 1,
        "denominator": 2,
        "percent": 50.0,
    }
    assert report.summary.executions.failure_defect_coverage.model_dump() == {
        "numerator": 1,
        "denominator": 2,
        "percent": 50.0,
    }
    assert report.summary.defects.reopened_in_period == 1
    assert report.summary.defects.not_closed_current == 1
    assert report.summary.defects.high_severity_not_closed_current == 1
    assert report.trends[0].bucket_start == date(2026, 8, 21)
    assert report.trends[0].completed_executions == 1
    assert report.trends[0].pass_rate.percent == 50.0
    root_coverage = next(
        item for item in report.coverage_by_suite if item.suite_id == root.id
    )
    assert root_coverage.active_cases == 4
    assert root_coverage.executed_cases == 4


@pytest.mark.asyncio
async def test_empty_quality_report_uses_null_rates_and_zero_filled_weeks() -> None:
    project = Project(key="EMPTY", name="Empty")
    service = await _quality_service(project=project)
    report = await service.report(
        project_id=project.id,
        date_from=date(2026, 8, 1),
        date_to=date(2026, 8, 10),
        granularity=TrendGranularity.WEEK,
        timezone_name="UTC",
    )
    assert report.summary.test_cases.automation_coverage.percent is None
    assert report.summary.executions.pass_rate.percent is None
    assert [point.bucket_start for point in report.trends] == [
        date(2026, 7, 27),
        date(2026, 8, 3),
        date(2026, 8, 10),
    ]
    assert all(point.pass_rate.percent is None for point in report.trends)


@pytest.mark.asyncio
async def test_quality_range_and_timezone_are_bounded() -> None:
    project = Project(key="BOUND", name="Bounds")
    service = await _quality_service(project=project)
    with pytest.raises(BusinessValidationError, match="不能早于"):
        await service.report(
            project_id=project.id,
            date_from=date(2026, 8, 2),
            date_to=date(2026, 8, 1),
        )
    with pytest.raises(BusinessValidationError, match="366"):
        await service.report(
            project_id=project.id,
            date_from=date(2025, 1, 1),
            date_to=date(2026, 8, 1),
        )
    with pytest.raises(BusinessValidationError, match="timezone"):
        await service.report(
            project_id=project.id,
            date_from=date(2026, 8, 1),
            date_to=date(2026, 8, 1),
            timezone_name="Europe/London",
        )


@pytest.mark.asyncio
async def test_quality_routes_expose_separate_summary_trends_and_coverage() -> None:
    project = Project(key="QHTTP", name="Quality HTTP")
    service = await _quality_service(project=project)
    application = FastAPI()
    application.state.quality_service = service
    application.include_router(quality_router, prefix="/api/v1")
    params = {
        "project_id": str(project.id),
        "date_from": "2026-08-01",
        "date_to": "2026-08-02",
        "timezone": "UTC",
    }
    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        summary = await client.get("/api/v1/quality/summary", params=params)
        combined = await client.get("/api/v1/quality/report", params=params)
        trends = await client.get("/api/v1/quality/trends", params=params)
        coverage = await client.get("/api/v1/quality/coverage", params=params)

    assert summary.status_code == 200
    assert combined.status_code == 200
    assert combined.json()["data"]["granularity"] == "day"
    assert summary.json()["data"]["executions"]["pass_rate"]["percent"] is None
    assert trends.json()["data"]["granularity"] == "day"
    assert len(trends.json()["data"]["items"]) == 2
    assert coverage.json()["data"] == []
