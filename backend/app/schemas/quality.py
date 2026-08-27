from __future__ import annotations

from datetime import date, datetime
from enum import Enum
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import TestSuiteStatus


class TrendGranularity(str, Enum):
    DAY = "day"
    WEEK = "week"


class QualityPeriod(BaseModel):
    model_config = ConfigDict(extra="forbid")

    date_from: date
    date_to: date
    timezone: str


class CountAndRate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    numerator: int = Field(ge=0)
    denominator: int = Field(ge=0)
    percent: float | None


class TestCaseQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    total_current: int = Field(ge=0)
    active_current: int = Field(ge=0)
    automated_active_current: int = Field(ge=0)
    automation_coverage: CountAndRate
    execution_reach: CountAndRate


class ExecutionQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    completed_executions: int = Field(ge=0)
    total_results: int = Field(ge=0)
    executed_results: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    skipped: int = Field(ge=0)
    not_run: int = Field(ge=0)
    completion_rate: CountAndRate
    pass_rate: CountAndRate
    failure_defect_coverage: CountAndRate


class DefectQualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    created_in_period: int = Field(ge=0)
    resolved_in_period: int = Field(ge=0)
    closed_in_period: int = Field(ge=0)
    reopened_in_period: int = Field(ge=0)
    not_closed_current: int = Field(ge=0)
    unresolved_current: int = Field(ge=0)
    high_severity_not_closed_current: int = Field(ge=0)


class QualitySummary(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    period: QualityPeriod
    test_cases: TestCaseQualitySummary
    executions: ExecutionQualitySummary
    defects: DefectQualitySummary
    generated_at: datetime


class QualityTrendPoint(BaseModel):
    model_config = ConfigDict(extra="forbid")

    bucket_start: date
    completed_executions: int = Field(ge=0)
    passed: int = Field(ge=0)
    failed: int = Field(ge=0)
    blocked: int = Field(ge=0)
    skipped: int = Field(ge=0)
    not_run: int = Field(ge=0)
    pass_rate: CountAndRate
    defects_created: int = Field(ge=0)
    defects_resolved: int = Field(ge=0)
    defects_closed: int = Field(ge=0)
    defects_reopened: int = Field(ge=0)


class SuiteCoverage(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: UUID | None
    suite_path: str
    suite_status: TestSuiteStatus | None = None
    active_cases: int = Field(ge=0)
    automated_cases: int = Field(ge=0)
    automation_coverage: CountAndRate
    executed_cases: int = Field(ge=0)
    execution_reach: CountAndRate
    failed_or_blocked_results: int = Field(ge=0)
    linked_failed_or_blocked_results: int = Field(ge=0)
    failure_defect_coverage: CountAndRate


class QualityReport(BaseModel):
    model_config = ConfigDict(extra="forbid")

    summary: QualitySummary
    granularity: TrendGranularity
    trends: list[QualityTrendPoint]
    coverage_by_suite: list[SuiteCoverage]
