from __future__ import annotations

import asyncio
from collections import Counter, defaultdict
from datetime import date, datetime, time, timedelta, timezone, tzinfo
from uuid import UUID

from app.core.errors import BusinessValidationError, NotFoundError
from app.domain.models import (
    AuditAction,
    AuditEvent,
    CaseResultStatus,
    Defect,
    DefectSeverity,
    DefectStatus,
    ExecutionStatus,
    Project,
    TestCase,
    TestCaseStatus,
    TestCaseType,
    TestExecution,
    TestSuite,
)
from app.repositories.base import AsyncRepository
from app.schemas.quality import (
    CountAndRate,
    DefectQualitySummary,
    ExecutionQualitySummary,
    QualityPeriod,
    QualityReport,
    QualitySummary,
    QualityTrendPoint,
    SuiteCoverage,
    TestCaseQualitySummary,
    TrendGranularity,
)
from app.services.common import parse_uuid

_SUPPORTED_TIMEZONES: dict[str, tzinfo] = {
    "UTC": timezone.utc,
    # A fixed offset is deliberate: Python 3.10 on Windows does not ship the
    # IANA database.  The main application may replace this with zoneinfo once
    # it installs the pinned tzdata package.
    "Asia/Shanghai": timezone(timedelta(hours=8), name="Asia/Shanghai"),
}
_MAX_PERIOD_DAYS = 366


class QualityService:
    """Read-only, deterministic quality calculations over current repositories.

    Official result metrics include only completed executions, because a
    running execution's CaseExecutionResult is mutable and is not an append-only
    attempt history.  The current adapter reads repositories independently; a
    production-sized deployment should wire the same formulas to one SQL read
    transaction and aggregate query.
    """

    def __init__(
        self,
        *,
        projects: AsyncRepository[Project],
        test_cases: AsyncRepository[TestCase],
        test_suites: AsyncRepository[TestSuite],
        executions: AsyncRepository[TestExecution],
        defects: AsyncRepository[Defect],
        audit_events: AsyncRepository[AuditEvent],
    ) -> None:
        self._projects = projects
        self._test_cases = test_cases
        self._test_suites = test_suites
        self._executions = executions
        self._defects = defects
        self._audit_events = audit_events

    async def report(
        self,
        *,
        project_id: str | UUID,
        date_from: date,
        date_to: date,
        granularity: TrendGranularity = TrendGranularity.DAY,
        timezone_name: str = "Asia/Shanghai",
        generated_at: datetime | None = None,
    ) -> QualityReport:
        parsed_project_id = parse_uuid(project_id, "project_id")
        project = await self._projects.get(parsed_project_id)
        if project is None:
            raise NotFoundError("项目", parsed_project_id)
        if isinstance(granularity, str):
            granularity = TrendGranularity(granularity)
        local_timezone = _timezone_for(timezone_name)
        start_utc, end_utc = _period_bounds(
            date_from,
            date_to,
            local_timezone,
        )

        cases, suites, executions, defects, audits = await asyncio.gather(
            self._test_cases.list(),
            self._test_suites.list(),
            self._executions.list(),
            self._defects.list(),
            self._audit_events.list(),
        )
        project_cases = [item for item in cases if item.project_id == parsed_project_id]
        project_suites = [item for item in suites if item.project_id == parsed_project_id]
        project_executions = [
            item for item in executions if item.project_id == parsed_project_id
        ]
        project_defects = [
            item for item in defects if item.project_id == parsed_project_id
        ]
        project_audits = [
            item for item in audits if item.project_id == parsed_project_id
        ]
        selected_executions = [
            item
            for item in project_executions
            if item.status == ExecutionStatus.COMPLETED
            and _within(item.completed_at, start_utc, end_utc)
        ]

        result_counts, reached_case_ids, failure_pairs = _execution_facts(
            selected_executions
        )
        linked_failure_pairs = {
            (defect.execution_id, defect.case_id)
            for defect in project_defects
            if defect.execution_id is not None
            and defect.case_id is not None
            and (defect.execution_id, defect.case_id) in failure_pairs
        }
        reopened_audits = [
            event
            for event in project_audits
            if _is_reopen_event(event)
            and _within(event.created_at, start_utc, end_utc)
        ]

        active_cases = [
            item for item in project_cases if item.status == TestCaseStatus.ACTIVE
        ]
        active_case_ids = {item.id for item in active_cases}
        reached_active_ids = reached_case_ids & active_case_ids
        automated_active = [
            item for item in active_cases if item.case_type == TestCaseType.AUTOMATED
        ]
        total_results = sum(result_counts.values())
        not_run = result_counts[CaseResultStatus.NOT_RUN]
        executed_results = total_results - not_run
        passed = result_counts[CaseResultStatus.PASSED]
        failed = result_counts[CaseResultStatus.FAILED]
        blocked = result_counts[CaseResultStatus.BLOCKED]
        skipped = result_counts[CaseResultStatus.SKIPPED]

        not_closed = [
            item for item in project_defects if item.status != DefectStatus.CLOSED
        ]
        unresolved = [
            item
            for item in project_defects
            if item.status
            in {DefectStatus.OPEN, DefectStatus.IN_PROGRESS, DefectStatus.REOPENED}
        ]
        high_severity_not_closed = [
            item
            for item in not_closed
            if item.severity in {DefectSeverity.BLOCKER, DefectSeverity.CRITICAL}
        ]
        current_time = _as_utc(generated_at or datetime.now(timezone.utc))
        summary = QualitySummary(
            project_id=parsed_project_id,
            period=QualityPeriod(
                date_from=date_from,
                date_to=date_to,
                timezone=timezone_name,
            ),
            test_cases=TestCaseQualitySummary(
                total_current=len(project_cases),
                active_current=len(active_cases),
                automated_active_current=len(automated_active),
                automation_coverage=_rate(len(automated_active), len(active_cases)),
                execution_reach=_rate(len(reached_active_ids), len(active_cases)),
            ),
            executions=ExecutionQualitySummary(
                completed_executions=len(selected_executions),
                total_results=total_results,
                executed_results=executed_results,
                passed=passed,
                failed=failed,
                blocked=blocked,
                skipped=skipped,
                not_run=not_run,
                completion_rate=_rate(executed_results, total_results),
                pass_rate=_rate(passed, passed + failed),
                failure_defect_coverage=_rate(
                    len(linked_failure_pairs),
                    len(failure_pairs),
                ),
            ),
            defects=DefectQualitySummary(
                created_in_period=sum(
                    _within(item.created_at, start_utc, end_utc)
                    for item in project_defects
                ),
                resolved_in_period=sum(
                    _within(item.resolved_at, start_utc, end_utc)
                    for item in project_defects
                ),
                closed_in_period=sum(
                    _within(item.closed_at, start_utc, end_utc)
                    for item in project_defects
                ),
                reopened_in_period=len(reopened_audits),
                not_closed_current=len(not_closed),
                unresolved_current=len(unresolved),
                high_severity_not_closed_current=len(high_severity_not_closed),
            ),
            generated_at=current_time,
        )
        trends = _build_trends(
            date_from=date_from,
            date_to=date_to,
            granularity=granularity,
            local_timezone=local_timezone,
            executions=selected_executions,
            defects=project_defects,
            reopen_events=reopened_audits,
        )
        coverage = _build_suite_coverage(
            cases=active_cases,
            suites=project_suites,
            reached_case_ids=reached_case_ids,
            failure_pairs=failure_pairs,
            linked_failure_pairs=linked_failure_pairs,
        )
        return QualityReport(
            summary=summary,
            granularity=granularity,
            trends=trends,
            coverage_by_suite=coverage,
        )


def _timezone_for(name: str) -> tzinfo:
    selected = _SUPPORTED_TIMEZONES.get(name)
    if selected is None:
        raise BusinessValidationError(
            "timezone 当前只支持 UTC 或 Asia/Shanghai"
        )
    return selected


def _period_bounds(
    date_from: date,
    date_to: date,
    local_timezone: tzinfo,
) -> tuple[datetime, datetime]:
    if date_to < date_from:
        raise BusinessValidationError("date_to 不能早于 date_from")
    days = (date_to - date_from).days + 1
    if days > _MAX_PERIOD_DAYS:
        raise BusinessValidationError(
            f"质量报表时间范围不能超过 {_MAX_PERIOD_DAYS} 天"
        )
    start = datetime.combine(date_from, time.min, tzinfo=local_timezone)
    end = datetime.combine(date_to + timedelta(days=1), time.min, tzinfo=local_timezone)
    return start.astimezone(timezone.utc), end.astimezone(timezone.utc)


def _within(
    value: datetime | None,
    start_utc: datetime,
    end_utc: datetime,
) -> bool:
    if value is None:
        return False
    normalized = _as_utc(value)
    return start_utc <= normalized < end_utc


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _rate(numerator: int, denominator: int) -> CountAndRate:
    return CountAndRate(
        numerator=numerator,
        denominator=denominator,
        percent=(round(numerator / denominator * 100, 1) if denominator else None),
    )


def _execution_facts(
    executions: list[TestExecution],
) -> tuple[Counter[CaseResultStatus], set[UUID], set[tuple[UUID, UUID]]]:
    counts: Counter[CaseResultStatus] = Counter()
    reached: set[UUID] = set()
    failure_pairs: set[tuple[UUID, UUID]] = set()
    for execution in executions:
        for result in execution.results:
            counts[result.status] += 1
            if result.status != CaseResultStatus.NOT_RUN:
                reached.add(result.case_id)
            if result.status in {CaseResultStatus.FAILED, CaseResultStatus.BLOCKED}:
                failure_pairs.add((execution.id, result.case_id))
    return counts, reached, failure_pairs


def _is_reopen_event(event: AuditEvent) -> bool:
    if event.entity_type != "defect" or event.action != AuditAction.STATUS_CHANGED:
        return False
    status_change = event.changes.get("status")
    return status_change is not None and status_change.after == DefectStatus.REOPENED.value


def _bucket_start(
    value: datetime,
    local_timezone: tzinfo,
    granularity: TrendGranularity,
) -> date:
    local_date = _as_utc(value).astimezone(local_timezone).date()
    if granularity == TrendGranularity.WEEK:
        return local_date - timedelta(days=local_date.weekday())
    return local_date


def _period_bucket_starts(
    date_from: date,
    date_to: date,
    granularity: TrendGranularity,
) -> list[date]:
    values: list[date] = []
    seen: set[date] = set()
    cursor = date_from
    while cursor <= date_to:
        bucket = (
            cursor - timedelta(days=cursor.weekday())
            if granularity == TrendGranularity.WEEK
            else cursor
        )
        if bucket not in seen:
            seen.add(bucket)
            values.append(bucket)
        cursor += timedelta(days=1)
    return values


def _build_trends(
    *,
    date_from: date,
    date_to: date,
    granularity: TrendGranularity,
    local_timezone: tzinfo,
    executions: list[TestExecution],
    defects: list[Defect],
    reopen_events: list[AuditEvent],
) -> list[QualityTrendPoint]:
    counters: dict[date, Counter[str]] = defaultdict(Counter)
    for execution in executions:
        assert execution.completed_at is not None
        bucket = _bucket_start(execution.completed_at, local_timezone, granularity)
        counters[bucket]["completed_executions"] += 1
        for result in execution.results:
            counters[bucket][result.status.value] += 1
    for defect in defects:
        for field, timestamp in (
            ("defects_created", defect.created_at),
            ("defects_resolved", defect.resolved_at),
            ("defects_closed", defect.closed_at),
        ):
            if timestamp is None:
                continue
            local_date = _as_utc(timestamp).astimezone(local_timezone).date()
            if not (date_from <= local_date <= date_to):
                continue
            counters[_bucket_start(timestamp, local_timezone, granularity)][field] += 1
    for event in reopen_events:
        counters[
            _bucket_start(event.created_at, local_timezone, granularity)
        ]["defects_reopened"] += 1

    points: list[QualityTrendPoint] = []
    for bucket in _period_bucket_starts(date_from, date_to, granularity):
        values = counters[bucket]
        passed = values[CaseResultStatus.PASSED.value]
        failed = values[CaseResultStatus.FAILED.value]
        points.append(
            QualityTrendPoint(
                bucket_start=bucket,
                completed_executions=values["completed_executions"],
                passed=passed,
                failed=failed,
                blocked=values[CaseResultStatus.BLOCKED.value],
                skipped=values[CaseResultStatus.SKIPPED.value],
                not_run=values[CaseResultStatus.NOT_RUN.value],
                pass_rate=_rate(passed, passed + failed),
                defects_created=values["defects_created"],
                defects_resolved=values["defects_resolved"],
                defects_closed=values["defects_closed"],
                defects_reopened=values["defects_reopened"],
            )
        )
    return points


def _build_suite_coverage(
    *,
    cases: list[TestCase],
    suites: list[TestSuite],
    reached_case_ids: set[UUID],
    failure_pairs: set[tuple[UUID, UUID]],
    linked_failure_pairs: set[tuple[UUID, UUID]],
) -> list[SuiteCoverage]:
    by_id = {item.id: item for item in suites}
    paths = _suite_paths(suites)
    descendant_ids: dict[UUID, set[UUID]] = {
        suite.id: {suite.id} for suite in suites
    }
    for candidate in suites:
        current = candidate
        visited: set[UUID] = set()
        while current.parent_id is not None and current.parent_id not in visited:
            visited.add(current.parent_id)
            descendant_ids.setdefault(current.parent_id, {current.parent_id}).add(
                candidate.id
            )
            parent = by_id.get(current.parent_id)
            if parent is None:
                break
            current = parent

    rows: list[SuiteCoverage] = []
    for suite in suites:
        scoped_suite_ids = descendant_ids.get(suite.id, {suite.id})
        scoped_cases = [item for item in cases if item.suite_id in scoped_suite_ids]
        rows.append(
            _coverage_row(
                suite_id=suite.id,
                suite_path=paths.get(suite.id, suite.name),
                suite_status=suite.status,
                cases=scoped_cases,
                reached_case_ids=reached_case_ids,
                failure_pairs=failure_pairs,
                linked_failure_pairs=linked_failure_pairs,
            )
        )
    unassigned = [item for item in cases if item.suite_id is None]
    if unassigned:
        rows.append(
            _coverage_row(
                suite_id=None,
                suite_path="（未归类）",
                suite_status=None,
                cases=unassigned,
                reached_case_ids=reached_case_ids,
                failure_pairs=failure_pairs,
                linked_failure_pairs=linked_failure_pairs,
            )
        )
    return sorted(rows, key=lambda item: (item.suite_id is None, item.suite_path))


def _coverage_row(
    *,
    suite_id: UUID | None,
    suite_path: str,
    suite_status,
    cases: list[TestCase],
    reached_case_ids: set[UUID],
    failure_pairs: set[tuple[UUID, UUID]],
    linked_failure_pairs: set[tuple[UUID, UUID]],
) -> SuiteCoverage:
    case_ids = {item.id for item in cases}
    automated = sum(item.case_type == TestCaseType.AUTOMATED for item in cases)
    reached = case_ids & reached_case_ids
    failures = {pair for pair in failure_pairs if pair[1] in case_ids}
    linked = failures & linked_failure_pairs
    return SuiteCoverage(
        suite_id=suite_id,
        suite_path=suite_path,
        suite_status=suite_status,
        active_cases=len(cases),
        automated_cases=automated,
        automation_coverage=_rate(automated, len(cases)),
        executed_cases=len(reached),
        execution_reach=_rate(len(reached), len(cases)),
        failed_or_blocked_results=len(failures),
        linked_failed_or_blocked_results=len(linked),
        failure_defect_coverage=_rate(len(linked), len(failures)),
    )


def _suite_paths(suites: list[TestSuite]) -> dict[UUID, str]:
    by_id = {item.id: item for item in suites}
    result: dict[UUID, str] = {}
    for suite in suites:
        names: list[str] = []
        current = suite
        visited: set[UUID] = set()
        while True:
            if current.id in visited:
                names.append("[循环]")
                break
            visited.add(current.id)
            names.append(current.name)
            if current.parent_id is None:
                break
            parent = by_id.get(current.parent_id)
            if parent is None:
                names.append("[缺失父节点]")
                break
            current = parent
        result[suite.id] = "/".join(reversed(names))
    return result
