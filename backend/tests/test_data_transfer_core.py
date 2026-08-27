from __future__ import annotations

import csv
import json
import zipfile
from datetime import datetime, timezone
from io import BytesIO, StringIO
from uuid import UUID

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient

from app.core.errors import BusinessValidationError, ConflictError
from app.data_transfer.exporters import export_test_cases
from app.data_transfer.models import DataTransferFileError
from app.data_transfer.parsers import parse_import_file
from app.data_transfer.security import (
    MAX_PRIMARY_ROWS,
    MAX_XLSX_ARCHIVE_MEMBERS,
    sha256_hex,
)
from app.data_transfer.templates import CSV_HEADERS, build_import_template
from app.domain.models import (
    Defect,
    Project,
    TestCase as DomainCase,
    TestStep as DomainStep,
    TestSuite as DomainSuite,
)
from app.repositories.memory import InMemoryRepository
from app.schemas.data_transfer import (
    ImportRowStatus,
    TransferEntity,
    TransferFormat,
)
from app.services.data_transfer import DataTransferService
from app.api.routes.data_transfer import router as data_transfer_router


class _CaseWriter:
    def __init__(self) -> None:
        self.payloads = []

    async def create(self, payload):
        self.payloads.append(payload)
        if payload.title == "提交阶段失败":
            raise BusinessValidationError("模拟逐行业务失败")
        return DomainCase(
            project_id=UUID(payload.project_id),
            suite_id=UUID(payload.suite_id) if payload.suite_id else None,
            title=payload.title,
            preconditions=payload.preconditions,
            steps=payload.steps,
            priority=payload.priority,
            case_type=payload.case_type,
            tags=payload.tags,
        )


class _DefectWriter:
    def __init__(self) -> None:
        self.payloads = []

    async def create(self, payload):
        self.payloads.append(payload)
        return Defect(
            project_id=UUID(payload.project_id),
            case_id=UUID(payload.case_id) if payload.case_id else None,
            execution_id=(UUID(payload.execution_id) if payload.execution_id else None),
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


async def _service(project: Project, suites: list[DomainSuite] | None = None):
    projects = InMemoryRepository()
    cases = InMemoryRepository()
    suite_repository = InMemoryRepository()
    executions = InMemoryRepository()
    defects = InMemoryRepository()
    await projects.create(project)
    for suite in suites or []:
        await suite_repository.create(suite)
    case_writer = _CaseWriter()
    defect_writer = _DefectWriter()
    service = DataTransferService(
        projects=projects,
        test_cases=cases,
        test_suites=suite_repository,
        executions=executions,
        defects=defects,
        test_case_writer=case_writer,
        defect_writer=defect_writer,
    )
    return service, case_writer, defect_writer


def _csv_bytes(entity: TransferEntity, rows: list[dict[str, object]]) -> bytes:
    stream = StringIO(newline="")
    stream.write("\ufeff")
    writer = csv.DictWriter(
        stream,
        fieldnames=CSV_HEADERS[entity],
        lineterminator="\r\n",
    )
    writer.writeheader()
    writer.writerows(rows)
    return stream.getvalue().encode("utf-8")


@pytest.mark.asyncio
async def test_test_case_csv_preview_and_partial_commit_are_explicit() -> None:
    project = Project(key="DATA", name="Data transfer")
    root = DomainSuite(project_id=project.id, name="账号")
    child = DomainSuite(project_id=project.id, parent_id=root.id, name="登录")
    service, case_writer, _ = await _service(project, [root, child])
    content = _csv_bytes(
        TransferEntity.TEST_CASES,
        [
            {
                "template_version": "1",
                "row_key": "TC-1",
                "suite_path": "账号/登录",
                "title": "正确密码可以登录",
                "priority": "P1",
                "case_type": "automated",
                "tags": " Smoke ;login;smoke ",
                "steps_json": json.dumps(
                    [{"action": "提交账号密码", "expected_result": "进入首页"}],
                    ensure_ascii=False,
                ),
            }
        ],
    )

    preview = await service.preview(
        entity=TransferEntity.TEST_CASES,
        project_id=project.id,
        filename="cases.csv",
        content=content,
    )
    result = await service.commit_partial_create_only(
        entity=TransferEntity.TEST_CASES,
        project_id=project.id,
        filename="cases.csv",
        content=content,
        expected_sha256=preview.sha256,
    )

    assert preview.model_dump(exclude={"rows", "issues"}) == {
        "entity": TransferEntity.TEST_CASES,
        "filename": "cases.csv",
        "sha256": sha256_hex(content),
        "template_version": "1",
        "total_rows": 1,
        "valid_rows": 1,
        "invalid_rows": 0,
        "error_count": 0,
        "warning_count": 0,
        "can_commit_clean": True,
        "can_commit_partial": True,
        "atomic_commit": False,
        "omitted_issue_count": 0,
    }
    assert result.atomic is False
    assert result.created_rows == 1
    assert result.rows[0].status == ImportRowStatus.CREATED
    assert case_writer.payloads[0].suite_id == str(child.id)
    assert case_writer.payloads[0].tags == ["login", "smoke"]


@pytest.mark.asyncio
async def test_clean_gate_writes_nothing_then_partial_mode_reports_every_row() -> None:
    project = Project(key="PART", name="Partial")
    service, case_writer, _ = await _service(project)
    rows = [
        {
            "template_version": "1",
            "row_key": "BAD",
            "title": "",
            "steps_json": "[]",
        },
        {
            "template_version": "1",
            "row_key": "OK",
            "title": "可以创建",
            "steps_json": "[]",
        },
        {
            "template_version": "1",
            "row_key": "RACE",
            "title": "提交阶段失败",
            "steps_json": "[]",
        },
    ]
    content = _csv_bytes(TransferEntity.TEST_CASES, rows)
    preview = await service.preview(
        entity=TransferEntity.TEST_CASES,
        project_id=project.id,
        filename="partial.csv",
        content=content,
    )

    blocked = await service.commit_partial_create_only(
        entity=TransferEntity.TEST_CASES,
        project_id=project.id,
        filename="partial.csv",
        content=content,
        expected_sha256=preview.sha256,
        require_clean_preview=True,
    )
    assert blocked.committed is False
    assert blocked.skipped_rows == 3
    assert case_writer.payloads == []

    partial = await service.commit_partial_create_only(
        entity=TransferEntity.TEST_CASES,
        project_id=project.id,
        filename="partial.csv",
        content=content,
        expected_sha256=preview.sha256,
        require_clean_preview=False,
    )
    assert [row.status for row in partial.rows] == [
        ImportRowStatus.SKIPPED,
        ImportRowStatus.CREATED,
        ImportRowStatus.FAILED,
    ]
    assert partial.created_rows == 1
    assert partial.failed_rows == 1
    assert partial.skipped_rows == 1


@pytest.mark.asyncio
async def test_commit_rejects_file_changed_after_preview() -> None:
    project = Project(key="HASH", name="Hash")
    service, _, _ = await _service(project)
    content = _csv_bytes(
        TransferEntity.TEST_CASES,
        [{"template_version": "1", "row_key": "A", "title": "A", "steps_json": "[]"}],
    )
    with pytest.raises(ConflictError, match="SHA-256"):
        await service.commit_partial_create_only(
            entity=TransferEntity.TEST_CASES,
            project_id=project.id,
            filename="cases.csv",
            content=content,
            expected_sha256="0" * 64,
        )


@pytest.mark.asyncio
async def test_defect_csv_is_create_only_open_data_and_preserves_steps() -> None:
    project = Project(key="BUGCSV", name="Defect CSV")
    service, _, defect_writer = await _service(project)
    content = _csv_bytes(
        TransferEntity.DEFECTS,
        [
            {
                "template_version": "1",
                "row_key": "BUG-1",
                "title": "按钮无响应",
                "severity": "critical",
                "priority": "P1",
                "reporter": "qa-local",
                "reproduction_steps_json": json.dumps(
                    ["进入页面", "点击按钮"], ensure_ascii=False
                ),
            }
        ],
    )
    preview = await service.preview(
        entity=TransferEntity.DEFECTS,
        project_id=project.id,
        filename="defects.csv",
        content=content,
    )
    result = await service.commit_partial_create_only(
        entity=TransferEntity.DEFECTS,
        project_id=project.id,
        filename="defects.csv",
        content=content,
        expected_sha256=preview.sha256,
    )
    assert result.created_rows == 1
    assert defect_writer.payloads[0].reproduction_steps == ["进入页面", "点击按钮"]
    # DefectCreate has no status field: imports cannot bypass the open state machine.
    assert "status" not in defect_writer.payloads[0].model_fields_set


def test_csv_template_is_versioned_and_export_neutralizes_formulas() -> None:
    template = build_import_template(TransferEntity.TEST_CASES, TransferFormat.CSV)
    assert template.content.startswith(b"\xef\xbb\xbf")
    assert tuple(
        next(csv.reader(StringIO(template.content.decode("utf-8-sig"))))
    ) == CSV_HEADERS[TransferEntity.TEST_CASES]

    case = DomainCase(
        project_id=Project(key="EXP", name="Export").id,
        title="=HYPERLINK(\"https://example.invalid\")",
        steps=[DomainStep(action="+danger", expected_result="safe")],
    )
    artifact = export_test_cases(
        [case],
        TransferFormat.CSV,
        generated_at=datetime(2026, 8, 27, tzinfo=timezone.utc),
    )
    decoded = artifact.content.decode("utf-8-sig")
    assert "'=HYPERLINK" in decoded
    assert artifact.row_count == 1
    assert artifact.filename == "test-cases-20260827-000000Z.csv"


def test_upload_security_rejects_unsupported_and_fake_xlsx() -> None:
    with pytest.raises(DataTransferFileError, match="只支持"):
        parse_import_file(
            entity=TransferEntity.TEST_CASES,
            filename="unsafe.xlsm",
            content=b"not-empty",
        )
    with pytest.raises(DataTransferFileError, match="签名"):
        parse_import_file(
            entity=TransferEntity.TEST_CASES,
            filename="fake.xlsx",
            content=b"not-a-zip",
        )

    archive_bytes = BytesIO()
    with zipfile.ZipFile(archive_bytes, "w") as archive:
        for index in range(MAX_XLSX_ARCHIVE_MEMBERS + 1):
            archive.writestr(f"safe/member-{index}.xml", "")
    with pytest.raises(DataTransferFileError, match="内部文件"):
        parse_import_file(
            entity=TransferEntity.TEST_CASES,
            filename="too-many-members.xlsx",
            content=archive_bytes.getvalue(),
        )


def test_xlsx_template_parses_and_formula_is_rejected_when_dependency_exists() -> None:
    openpyxl = pytest.importorskip("openpyxl")
    artifact = build_import_template(
        TransferEntity.TEST_CASES,
        TransferFormat.XLSX,
    )
    workbook = openpyxl.load_workbook(BytesIO(artifact.content))
    workbook["test_cases"].append(
        ("TC-1", "", "", "本地 XLSX", "", "P2", "manual", "smoke")
    )
    workbook["steps"].append(("TC-1", 1, "打开页面", "页面展示"))
    output = BytesIO()
    workbook.save(output)
    workbook.close()

    parsed = parse_import_file(
        entity=TransferEntity.TEST_CASES,
        filename="cases.xlsx",
        content=output.getvalue(),
    )
    assert parsed.template_version == "1"
    assert len(parsed.main_rows) == 1
    assert len(parsed.child_rows) == 1

    workbook = openpyxl.load_workbook(BytesIO(output.getvalue()))
    workbook["test_cases"]["D2"] = "=1+1"
    unsafe = BytesIO()
    workbook.save(unsafe)
    workbook.close()
    with pytest.raises(DataTransferFileError, match="公式"):
        parse_import_file(
            entity=TransferEntity.TEST_CASES,
            filename="formula.xlsx",
            content=unsafe.getvalue(),
        )

    defect_artifact = build_import_template(
        TransferEntity.DEFECTS,
        TransferFormat.XLSX,
    )
    workbook = openpyxl.load_workbook(BytesIO(defect_artifact.content))
    workbook["defects"].append(
        ("BUG-1", "本地缺陷", "", "major", "P2", "qa", "", "", "", "", "", "")
    )
    workbook["reproduction_steps"].append(("BUG-1", 1, "点击按钮"))
    defect_output = BytesIO()
    workbook.save(defect_output)
    workbook.close()
    defect_parsed = parse_import_file(
        entity=TransferEntity.DEFECTS,
        filename="defects.xlsx",
        content=defect_output.getvalue(),
    )
    assert len(defect_parsed.main_rows) == 1
    assert len(defect_parsed.child_rows) == 1

    sparse_workbook = openpyxl.load_workbook(BytesIO(artifact.content))
    sparse_workbook["test_cases"].cell(
        row=MAX_PRIMARY_ROWS + 2,
        column=1,
        value="sparse-tail",
    )
    sparse_output = BytesIO()
    sparse_workbook.save(sparse_output)
    sparse_workbook.close()
    with pytest.raises(DataTransferFileError, match="逻辑行数"):
        parse_import_file(
            entity=TransferEntity.TEST_CASES,
            filename="sparse.xlsx",
            content=sparse_output.getvalue(),
        )


@pytest.mark.asyncio
async def test_data_transfer_router_uses_format_alias_and_multipart_contract() -> None:
    project = Project(key="HTTP", name="HTTP")
    service, _, _ = await _service(project)
    application = FastAPI()
    application.state.data_transfer_service = service
    application.include_router(data_transfer_router, prefix="/api/v1")
    content = _csv_bytes(
        TransferEntity.TEST_CASES,
        [
            {
                "template_version": "1",
                "row_key": "HTTP-1",
                "title": "HTTP 导入",
                "steps_json": "[]",
            }
        ],
    )

    async with AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
    ) as client:
        template = await client.get(
            "/api/v1/data-transfer/templates/test-cases",
            params={"format": "csv"},
        )
        preview = await client.post(
            "/api/v1/data-transfer/imports/test-cases/preview",
            data={"project_id": str(project.id)},
            files={"file": ("cases.csv", content, "text/csv")},
        )

    assert template.status_code == 200
    assert template.headers["cache-control"] == "no-store"
    assert "filename*=UTF-8''" in template.headers["content-disposition"]
    assert preview.status_code == 200, preview.text
    assert preview.json()["data"]["valid_rows"] == 1
