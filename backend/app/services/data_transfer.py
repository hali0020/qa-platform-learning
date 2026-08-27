from __future__ import annotations

import asyncio
import json
from collections import Counter, defaultdict
from dataclasses import dataclass
from typing import Any
from uuid import UUID

from pydantic import ValidationError

from app.core.errors import ConflictError, DomainError, InvalidStateError, NotFoundError
from app.data_transfer.exporters import (
    TransferArtifact,
    export_defects,
    export_test_cases,
)
from app.data_transfer.models import ParsedImport, RawImportRow
from app.data_transfer.parsers import parse_import_file
from app.data_transfer.security import exposed_value
from app.data_transfer.templates import (
    CSV_HEADERS,
    XLSX_CHILD_HEADERS,
    XLSX_MAIN_HEADERS,
    build_import_template,
)
from app.domain.models import (
    Defect,
    ExecutionStatus,
    Project,
    ProjectStatus,
    TestCase,
    TestExecution,
    TestSuite,
    TestSuiteStatus,
)
from app.repositories.base import AsyncRepository
from app.schemas.data_transfer import (
    DefectImportPayload,
    ImportCommitResult,
    ImportIssue,
    ImportIssueSeverity,
    ImportMode,
    ImportPreview,
    ImportRowPreview,
    ImportRowResult,
    ImportRowStatus,
    TestCaseImportPayload,
    TransferEntity,
    TransferFormat,
)
from app.schemas.defects import DefectCreate
from app.schemas.test_cases import TestCaseCreate
from app.services.common import parse_uuid
from app.services.defects import DefectService
from app.services.test_cases import TestCaseService

MAX_INLINE_ISSUES = 200


@dataclass(slots=True)
class _PreparedRow:
    source: RawImportRow
    payload: TestCaseImportPayload | DefectImportPayload | None
    issues: list[ImportIssue]


@dataclass(slots=True)
class _PreparedImport:
    parsed: ParsedImport
    rows: list[_PreparedRow]
    global_issues: list[ImportIssue]
    preview: ImportPreview


class DataTransferService:
    """Preview and explicit partial create-only import.

    Existing repositories commit independently, so this service never labels a
    batch atomic.  It reparses and revalidates the same bytes at commit time,
    calls the existing domain services row by row, and returns every row's
    outcome.  A future Unit of Work can replace this implementation without
    changing the parser or response schemas.
    """

    def __init__(
        self,
        *,
        projects: AsyncRepository[Project],
        test_cases: AsyncRepository[TestCase],
        test_suites: AsyncRepository[TestSuite],
        executions: AsyncRepository[TestExecution],
        defects: AsyncRepository[Defect],
        test_case_writer: TestCaseService,
        defect_writer: DefectService,
    ) -> None:
        self._projects = projects
        self._test_cases = test_cases
        self._test_suites = test_suites
        self._executions = executions
        self._defects = defects
        self._test_case_writer = test_case_writer
        self._defect_writer = defect_writer
        self._parse_slots = asyncio.Semaphore(2)

    @staticmethod
    def template(
        entity: TransferEntity,
        transfer_format: TransferFormat,
    ) -> TransferArtifact:
        return build_import_template(entity, transfer_format)

    async def preview(
        self,
        *,
        entity: TransferEntity,
        project_id: str | UUID,
        filename: str,
        content: bytes,
    ) -> ImportPreview:
        prepared = await self._prepare(
            entity=entity,
            project_id=project_id,
            filename=filename,
            content=content,
        )
        return prepared.preview

    async def commit_partial_create_only(
        self,
        *,
        entity: TransferEntity,
        project_id: str | UUID,
        filename: str,
        content: bytes,
        expected_sha256: str,
        require_clean_preview: bool = True,
    ) -> ImportCommitResult:
        prepared = await self._prepare(
            entity=entity,
            project_id=project_id,
            filename=filename,
            content=content,
        )
        if prepared.parsed.sha256 != expected_sha256.casefold():
            raise ConflictError("文件内容与预检 SHA-256 不一致，请重新预检")

        has_errors = prepared.preview.error_count > 0
        if require_clean_preview and has_errors:
            skipped = [
                ImportRowResult(
                    sheet=row.source.sheet,
                    row=row.source.row,
                    row_key=_row_key(row.source),
                    status=ImportRowStatus.SKIPPED,
                    issues=(
                        row.issues
                        or [
                            _issue(
                                row.source,
                                code="batch_validation_failed",
                                message="整批预检存在错误，clean gate 未写入任何行",
                            )
                        ]
                    ),
                )
                for row in prepared.rows
            ]
            return ImportCommitResult(
                entity=entity,
                filename=filename,
                sha256=prepared.parsed.sha256,
                clean_preview_required=True,
                committed=False,
                total_rows=len(prepared.rows),
                created_rows=0,
                failed_rows=0,
                skipped_rows=len(skipped),
                rows=skipped,
            )

        parsed_project_id = parse_uuid(project_id, "project_id")
        results: list[ImportRowResult] = []
        for row in prepared.rows:
            row_key = _row_key(row.source)
            if row.payload is None or _has_errors(row.issues):
                results.append(
                    ImportRowResult(
                        sheet=row.source.sheet,
                        row=row.source.row,
                        row_key=row_key,
                        status=ImportRowStatus.SKIPPED,
                        issues=row.issues,
                    )
                )
                continue
            try:
                if entity == TransferEntity.TEST_CASES:
                    payload = row.payload
                    assert isinstance(payload, TestCaseImportPayload)
                    created = await self._test_case_writer.create(
                        TestCaseCreate(
                            project_id=str(parsed_project_id),
                            suite_id=(str(payload.suite_id) if payload.suite_id else None),
                            title=payload.title,
                            preconditions=payload.preconditions,
                            steps=payload.steps,
                            priority=payload.priority,
                            case_type=payload.case_type,
                            tags=payload.tags,
                        )
                    )
                else:
                    payload = row.payload
                    assert isinstance(payload, DefectImportPayload)
                    created = await self._defect_writer.create(
                        DefectCreate(
                            project_id=str(parsed_project_id),
                            case_id=str(payload.case_id) if payload.case_id else None,
                            execution_id=(
                                str(payload.execution_id)
                                if payload.execution_id
                                else None
                            ),
                            title=payload.title,
                            description=payload.description,
                            severity=payload.severity,
                            priority=payload.priority,
                            reporter=payload.reporter,
                            assignee=payload.assignee,
                            environment=payload.environment,
                            reproduction_steps=payload.reproduction_steps,
                            expected_result=payload.expected_result,
                            actual_result=payload.actual_result,
                        )
                    )
            except DomainError as exc:
                issue = _issue(
                    row.source,
                    code="commit_business_error",
                    message=exc.message,
                )
                results.append(
                    ImportRowResult(
                        sheet=row.source.sheet,
                        row=row.source.row,
                        row_key=row_key,
                        status=ImportRowStatus.FAILED,
                        issues=[*row.issues, issue],
                    )
                )
            else:
                results.append(
                    ImportRowResult(
                        sheet=row.source.sheet,
                        row=row.source.row,
                        row_key=row_key,
                        status=ImportRowStatus.CREATED,
                        entity_id=created.id,
                        issues=row.issues,
                    )
                )

        statuses = Counter(result.status for result in results)
        return ImportCommitResult(
            entity=entity,
            filename=filename,
            sha256=prepared.parsed.sha256,
            mode=ImportMode.PARTIAL_CREATE_ONLY,
            atomic=False,
            clean_preview_required=require_clean_preview,
            committed=statuses[ImportRowStatus.CREATED] > 0,
            total_rows=len(results),
            created_rows=statuses[ImportRowStatus.CREATED],
            failed_rows=statuses[ImportRowStatus.FAILED],
            skipped_rows=statuses[ImportRowStatus.SKIPPED],
            rows=results,
        )

    async def export_test_cases(
        self,
        *,
        project_id: str | UUID,
        transfer_format: TransferFormat,
    ) -> TransferArtifact:
        parsed_project_id = parse_uuid(project_id, "project_id")
        await self._require_project(parsed_project_id)
        cases, suites = await asyncio.gather(
            self._test_cases.list(),
            self._test_suites.list(),
        )
        project_cases = [item for item in cases if item.project_id == parsed_project_id]
        suite_paths, _ = _suite_indexes(
            [item for item in suites if item.project_id == parsed_project_id]
        )
        path_by_id = {
            suite_id: paths[0]
            for suite_id, paths in suite_paths.items()
            if paths
        }
        return await asyncio.to_thread(
            export_test_cases,
            project_cases,
            transfer_format,
            suite_paths=path_by_id,
        )

    async def export_defects(
        self,
        *,
        project_id: str | UUID,
        transfer_format: TransferFormat,
    ) -> TransferArtifact:
        parsed_project_id = parse_uuid(project_id, "project_id")
        await self._require_project(parsed_project_id)
        defects = await self._defects.list()
        project_defects = [
            item for item in defects if item.project_id == parsed_project_id
        ]
        return await asyncio.to_thread(
            export_defects,
            project_defects,
            transfer_format,
        )

    async def _prepare(
        self,
        *,
        entity: TransferEntity,
        project_id: str | UUID,
        filename: str,
        content: bytes,
    ) -> _PreparedImport:
        parsed_project_id = parse_uuid(project_id, "project_id")
        await self._require_active_project(parsed_project_id)
        async with self._parse_slots:
            parsed = await asyncio.to_thread(
                parse_import_file,
                entity=entity,
                filename=filename,
                content=content,
            )
        _require_expected_headers(parsed)
        cases, suites, executions = await asyncio.gather(
            self._test_cases.list(),
            self._test_suites.list(),
            self._executions.list(),
        )
        project_cases = {
            item.id: item for item in cases if item.project_id == parsed_project_id
        }
        project_suites = [
            item for item in suites if item.project_id == parsed_project_id
        ]
        project_executions = {
            item.id: item
            for item in executions
            if item.project_id == parsed_project_id
        }
        if entity == TransferEntity.TEST_CASES:
            prepared_rows, global_issues = _prepare_test_case_rows(
                parsed,
                project_suites,
            )
        else:
            prepared_rows, global_issues = _prepare_defect_rows(
                parsed,
                project_cases,
                project_executions,
            )
        _apply_duplicate_row_key_issues(prepared_rows)
        preview = _build_preview(parsed, prepared_rows, global_issues)
        return _PreparedImport(parsed, prepared_rows, global_issues, preview)

    async def _require_project(self, project_id: UUID) -> Project:
        project = await self._projects.get(project_id)
        if project is None:
            raise NotFoundError("项目", project_id)
        return project

    async def _require_active_project(self, project_id: UUID) -> Project:
        project = await self._require_project(project_id)
        if project.status != ProjectStatus.ACTIVE:
            raise InvalidStateError("已归档项目不能导入业务数据")
        return project


def _prepare_test_case_rows(
    parsed: ParsedImport,
    suites: list[TestSuite],
) -> tuple[list[_PreparedRow], list[ImportIssue]]:
    child_map, child_issues = _test_step_map(parsed.child_rows)
    suite_paths, suites_by_path = _suite_indexes(suites)
    suites_by_id = {item.id: item for item in suites}
    rows: list[_PreparedRow] = []
    for source in parsed.main_rows:
        issues = list(child_issues.pop(_row_key(source), []))
        values = source.values
        try:
            if parsed.transfer_format == TransferFormat.CSV:
                steps = _json_list(values.get("steps_json", ""), "steps_json")
            else:
                steps = child_map.get(_row_key(source), [])
            suite_id = _resolve_suite(
                source,
                suites_by_id,
                suite_paths,
                suites_by_path,
            )
            tags = sorted(
                {
                    tag.strip().casefold()
                    for tag in str(values.get("tags", "")).split(";")
                    if tag.strip()
                }
            )
            payload = TestCaseImportPayload.model_validate(
                {
                    "row_key": _row_key(source),
                    "suite_id": suite_id,
                    "title": str(values.get("title", "")).strip(),
                    "preconditions": str(values.get("preconditions", "")).strip(),
                    "steps": steps,
                    "priority": str(values.get("priority", "") or "P2").strip(),
                    "case_type": str(
                        values.get("case_type", "") or "manual"
                    ).strip(),
                    "tags": tags,
                }
            )
        except _RowDataError as exc:
            payload = None
            issues.append(
                _issue(
                    source,
                    field=exc.field,
                    code=exc.code,
                    message=exc.message,
                    value=exc.value,
                )
            )
        except ValidationError as exc:
            payload = None
            issues.extend(_pydantic_issues(source, exc))
        rows.append(_PreparedRow(source=source, payload=payload, issues=issues))

    main_keys = {_row_key(row.source) for row in rows}
    for orphan_key in set(child_map) - main_keys:
        orphan_source = next(
            item for item in parsed.child_rows if _row_key(item) == orphan_key
        )
        child_issues[orphan_key].append(
            _issue(
                orphan_source,
                field="row_key",
                code="orphan_child_row",
                message="步骤 row_key 在主表中不存在",
                value=orphan_key,
            )
        )
    global_issues = [
        issue
        for orphan_issues in child_issues.values()
        for issue in orphan_issues
    ]
    return rows, global_issues


def _prepare_defect_rows(
    parsed: ParsedImport,
    cases: dict[UUID, TestCase],
    executions: dict[UUID, TestExecution],
) -> tuple[list[_PreparedRow], list[ImportIssue]]:
    child_map, child_issues = _reproduction_step_map(parsed.child_rows)
    rows: list[_PreparedRow] = []
    for source in parsed.main_rows:
        key = _row_key(source)
        issues = list(child_issues.pop(key, []))
        values = source.values
        try:
            if parsed.transfer_format == TransferFormat.CSV:
                reproduction_steps = _json_string_list(
                    values.get("reproduction_steps_json", ""),
                    "reproduction_steps_json",
                )
            else:
                reproduction_steps = child_map.get(key, [])
            case_id = _optional_uuid(values.get("case_id", ""), "case_id")
            execution_id = _optional_uuid(
                values.get("execution_id", ""),
                "execution_id",
            )
            _validate_defect_associations(
                case_id=case_id,
                execution_id=execution_id,
                cases=cases,
                executions=executions,
            )
            payload = DefectImportPayload.model_validate(
                {
                    "row_key": key,
                    "case_id": case_id,
                    "execution_id": execution_id,
                    "title": str(values.get("title", "")).strip(),
                    "description": str(values.get("description", "")).strip(),
                    "severity": str(
                        values.get("severity", "") or "major"
                    ).strip(),
                    "priority": str(values.get("priority", "") or "P2").strip(),
                    "reporter": str(
                        values.get("reporter", "") or "local-user"
                    ).strip(),
                    "assignee": str(values.get("assignee", "")).strip(),
                    "environment": str(values.get("environment", "")).strip(),
                    "reproduction_steps": reproduction_steps,
                    "expected_result": str(
                        values.get("expected_result", "")
                    ).strip(),
                    "actual_result": str(values.get("actual_result", "")).strip(),
                }
            )
        except _RowDataError as exc:
            payload = None
            issues.append(
                _issue(
                    source,
                    field=exc.field,
                    code=exc.code,
                    message=exc.message,
                    value=exc.value,
                )
            )
        except ValidationError as exc:
            payload = None
            issues.extend(_pydantic_issues(source, exc))
        rows.append(_PreparedRow(source=source, payload=payload, issues=issues))
    main_keys = {_row_key(row.source) for row in rows}
    for orphan_key in set(child_map) - main_keys:
        orphan_source = next(
            item for item in parsed.child_rows if _row_key(item) == orphan_key
        )
        child_issues[orphan_key].append(
            _issue(
                orphan_source,
                field="row_key",
                code="orphan_child_row",
                message="复现步骤 row_key 在主表中不存在",
                value=orphan_key,
            )
        )
    global_issues = [
        issue
        for orphan_issues in child_issues.values()
        for issue in orphan_issues
    ]
    return rows, global_issues


class _RowDataError(ValueError):
    def __init__(self, field: str, code: str, message: str, value: Any = None):
        super().__init__(message)
        self.field = field
        self.code = code
        self.message = message
        self.value = value


def _json_list(value: Any, field: str) -> list[dict[str, str]]:
    if value in (None, ""):
        return []
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise _RowDataError(field, "invalid_json", "必须是有效 JSON 数组", value) from exc
    if not isinstance(decoded, list) or not all(isinstance(item, dict) for item in decoded):
        raise _RowDataError(field, "invalid_steps", "必须是对象组成的 JSON 数组", value)
    return decoded


def _json_string_list(value: Any, field: str) -> list[str]:
    if value in (None, ""):
        return []
    try:
        decoded = json.loads(str(value))
    except (TypeError, json.JSONDecodeError) as exc:
        raise _RowDataError(field, "invalid_json", "必须是有效 JSON 数组", value) from exc
    if not isinstance(decoded, list) or not all(isinstance(item, str) for item in decoded):
        raise _RowDataError(field, "invalid_string_list", "必须是字符串组成的 JSON 数组", value)
    return [item.strip() for item in decoded if item.strip()]


def _optional_uuid(value: Any, field: str) -> UUID | None:
    rendered = str(value or "").strip()
    if not rendered:
        return None
    try:
        return UUID(rendered)
    except ValueError as exc:
        raise _RowDataError(field, "invalid_uuid", "必须是有效 UUID", value) from exc


def _test_step_map(
    rows: tuple[RawImportRow, ...],
) -> tuple[dict[str, list[dict[str, str]]], dict[str, list[ImportIssue]]]:
    grouped, issues = _ordered_children(rows)
    result: dict[str, list[dict[str, str]]] = {}
    for row_key, children in grouped.items():
        values: list[dict[str, str]] = []
        for source in children:
            action = str(source.values.get("action", "")).strip()
            expected = str(source.values.get("expected_result", "")).strip()
            if not action or not expected:
                issues[row_key].append(
                    _issue(
                        source,
                        field="action/expected_result",
                        code="incomplete_step",
                        message="步骤 action 与 expected_result 均不能为空",
                    )
                )
            else:
                values.append({"action": action, "expected_result": expected})
        result[row_key] = values
    return result, issues


def _reproduction_step_map(
    rows: tuple[RawImportRow, ...],
) -> tuple[dict[str, list[str]], dict[str, list[ImportIssue]]]:
    grouped, issues = _ordered_children(rows)
    result: dict[str, list[str]] = {}
    for row_key, children in grouped.items():
        values: list[str] = []
        for source in children:
            value = str(source.values.get("step", "")).strip()
            if not value:
                issues[row_key].append(
                    _issue(
                        source,
                        field="step",
                        code="blank_reproduction_step",
                        message="复现步骤不能为空",
                    )
                )
            else:
                values.append(value)
        result[row_key] = values
    return result, issues


def _ordered_children(
    rows: tuple[RawImportRow, ...],
) -> tuple[dict[str, list[RawImportRow]], dict[str, list[ImportIssue]]]:
    grouped: dict[str, list[tuple[int, RawImportRow]]] = defaultdict(list)
    issues: dict[str, list[ImportIssue]] = defaultdict(list)
    positions: dict[str, set[int]] = defaultdict(set)
    for source in rows:
        key = _row_key(source)
        if not key:
            issues[f"__orphan_{source.row}"].append(
                _issue(source, field="row_key", code="blank_row_key", message="row_key 不能为空")
            )
            continue
        try:
            position = int(str(source.values.get("position", "")).strip())
        except ValueError:
            issues[key].append(
                _issue(source, field="position", code="invalid_position", message="position 必须是正整数")
            )
            continue
        if position < 1:
            issues[key].append(
                _issue(source, field="position", code="invalid_position", message="position 必须从 1 开始")
            )
            continue
        if position in positions[key]:
            issues[key].append(
                _issue(source, field="position", code="duplicate_position", message="同一 row_key 的 position 不能重复", value=position)
            )
            continue
        positions[key].add(position)
        grouped[key].append((position, source))
    return (
        {key: [item for _, item in sorted(values)] for key, values in grouped.items()},
        issues,
    )


def _validate_defect_associations(
    *,
    case_id: UUID | None,
    execution_id: UUID | None,
    cases: dict[UUID, TestCase],
    executions: dict[UUID, TestExecution],
) -> None:
    if case_id is not None and case_id not in cases:
        raise _RowDataError("case_id", "case_not_in_project", "测试用例不存在或不属于目标项目", case_id)
    execution = executions.get(execution_id) if execution_id is not None else None
    if execution_id is not None and execution is None:
        raise _RowDataError("execution_id", "execution_not_in_project", "测试执行不存在或不属于目标项目", execution_id)
    if execution is not None and execution.status == ExecutionStatus.CREATED:
        raise _RowDataError("execution_id", "execution_not_started", "尚未开始的执行不能关联缺陷", execution_id)
    if case_id is not None and execution is not None:
        if not any(result.case_id == case_id for result in execution.results):
            raise _RowDataError("case_id", "case_not_in_execution", "测试用例不属于指定执行", case_id)


def _suite_indexes(
    suites: list[TestSuite],
) -> tuple[dict[UUID, list[str]], dict[str, list[TestSuite]]]:
    by_id = {item.id: item for item in suites}
    paths_by_id: dict[UUID, list[str]] = defaultdict(list)
    by_path: dict[str, list[TestSuite]] = defaultdict(list)
    for suite in suites:
        names: list[str] = []
        current = suite
        visited: set[UUID] = set()
        valid = True
        while True:
            if current.id in visited:
                valid = False
                break
            visited.add(current.id)
            names.append(current.name.strip())
            if current.parent_id is None:
                break
            parent = by_id.get(current.parent_id)
            if parent is None:
                valid = False
                break
            current = parent
        if not valid:
            continue
        path = "/".join(reversed(names))
        paths_by_id[suite.id].append(path)
        by_path[path].append(suite)
    return dict(paths_by_id), dict(by_path)


def _resolve_suite(
    source: RawImportRow,
    suites_by_id: dict[UUID, TestSuite],
    paths_by_id: dict[UUID, list[str]],
    suites_by_path: dict[str, list[TestSuite]],
) -> UUID | None:
    raw_id = str(source.values.get("suite_id", "")).strip()
    raw_path = "/".join(
        part.strip()
        for part in str(source.values.get("suite_path", "")).split("/")
        if part.strip()
    )
    suite_from_id: TestSuite | None = None
    suite_from_path: TestSuite | None = None
    if raw_id:
        suite_id = _optional_uuid(raw_id, "suite_id")
        assert suite_id is not None
        suite_from_id = suites_by_id.get(suite_id)
        if suite_from_id is None:
            raise _RowDataError("suite_id", "suite_not_in_project", "套件不存在或不属于目标项目", raw_id)
    if raw_path:
        matches = suites_by_path.get(raw_path, [])
        if not matches:
            raise _RowDataError("suite_path", "suite_path_not_found", "目标项目中不存在该套件路径", raw_path)
        if len(matches) > 1:
            raise _RowDataError("suite_path", "ambiguous_suite_path", "套件路径不唯一，请改用 suite_id", raw_path)
        suite_from_path = matches[0]
    if suite_from_id is not None and suite_from_path is not None and suite_from_id.id != suite_from_path.id:
        raise _RowDataError("suite_id", "suite_reference_mismatch", "suite_id 与 suite_path 指向不同套件")
    selected = suite_from_id or suite_from_path
    if selected is None:
        return None
    by_id = suites_by_id
    current = selected
    visited: set[UUID] = set()
    while True:
        if current.id in visited:
            raise _RowDataError("suite_id", "suite_cycle", "套件层级存在循环", selected.id)
        visited.add(current.id)
        if current.status != TestSuiteStatus.ACTIVE:
            raise _RowDataError("suite_id", "suite_archived", "归档套件或其归档祖先不能接收用例", selected.id)
        if current.parent_id is None:
            break
        parent = by_id.get(current.parent_id)
        if parent is None:
            raise _RowDataError("suite_id", "suite_parent_missing", "套件父节点不存在", current.parent_id)
        current = parent
    if selected.id not in paths_by_id:
        raise _RowDataError("suite_id", "suite_path_invalid", "无法解析套件路径", selected.id)
    return selected.id


def _apply_duplicate_row_key_issues(rows: list[_PreparedRow]) -> None:
    counts = Counter(_row_key(row.source) for row in rows)
    for row in rows:
        key = _row_key(row.source)
        if key and counts[key] > 1:
            row.issues.append(
                _issue(
                    row.source,
                    field="row_key",
                    code="duplicate_row_key",
                    message="主表 row_key 在文件内必须唯一",
                    value=key,
                )
            )
            row.payload = None


def _build_preview(
    parsed: ParsedImport,
    rows: list[_PreparedRow],
    global_issues: list[ImportIssue],
) -> ImportPreview:
    row_previews = [
        ImportRowPreview(
            sheet=row.source.sheet,
            row=row.source.row,
            row_key=_row_key(row.source),
            status=(ImportRowStatus.INVALID if _has_errors(row.issues) else ImportRowStatus.VALID),
            issues=row.issues,
        )
        for row in rows
    ]
    all_issues = [*global_issues, *(issue for row in rows for issue in row.issues)]
    error_count = sum(issue.severity == ImportIssueSeverity.ERROR for issue in all_issues)
    warning_count = len(all_issues) - error_count
    invalid_rows = sum(item.status == ImportRowStatus.INVALID for item in row_previews)
    valid_rows = len(row_previews) - invalid_rows
    return ImportPreview(
        entity=parsed.entity,
        filename=parsed.filename,
        sha256=parsed.sha256,
        template_version=parsed.template_version,
        total_rows=len(row_previews),
        valid_rows=valid_rows,
        invalid_rows=invalid_rows,
        error_count=error_count,
        warning_count=warning_count,
        can_commit_clean=error_count == 0 and valid_rows > 0,
        can_commit_partial=valid_rows > 0,
        atomic_commit=False,
        rows=row_previews,
        issues=all_issues[:MAX_INLINE_ISSUES],
        omitted_issue_count=max(0, len(all_issues) - MAX_INLINE_ISSUES),
    )


def _require_expected_headers(parsed: ParsedImport) -> None:
    if parsed.transfer_format == TransferFormat.CSV:
        expected_main = CSV_HEADERS[parsed.entity]
        expected_child: tuple[str, ...] = ()
    else:
        expected_main = XLSX_MAIN_HEADERS[parsed.entity]
        expected_child = XLSX_CHILD_HEADERS[parsed.entity]
    if parsed.main_headers != expected_main:
        raise ConflictError(
            "主表表头必须与模板完全一致；请下载最新模板后重试"
        )
    if expected_child and parsed.child_headers != expected_child:
        raise ConflictError(
            "步骤表表头必须与模板完全一致；请下载最新模板后重试"
        )


def _row_key(source: RawImportRow) -> str:
    return str(source.values.get("row_key", "")).strip()


def _has_errors(issues: list[ImportIssue]) -> bool:
    return any(issue.severity == ImportIssueSeverity.ERROR for issue in issues)


def _issue(
    source: RawImportRow,
    *,
    code: str,
    message: str,
    field: str = "",
    value: Any = None,
) -> ImportIssue:
    return ImportIssue(
        sheet=source.sheet,
        row=source.row,
        row_key=_row_key(source),
        field=field,
        code=code,
        message=message,
        value=exposed_value(value),
    )


def _pydantic_issues(source: RawImportRow, exc: ValidationError) -> list[ImportIssue]:
    return [
        _issue(
            source,
            field=".".join(str(part) for part in error.get("loc", ())),
            code=str(error.get("type", "validation_error")),
            message=str(error.get("msg", "字段校验失败")),
            value=error.get("input"),
        )
        for error in exc.errors()
    ]
