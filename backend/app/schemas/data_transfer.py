from __future__ import annotations

from enum import Enum
from typing import Annotated, Any
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, StringConstraints

from app.domain.models import (
    DefectPriority,
    DefectSeverity,
    TestCasePriority,
    TestCaseType,
    TestStep,
)


class TransferEntity(str, Enum):
    TEST_CASES = "test-cases"
    DEFECTS = "defects"


class TransferFormat(str, Enum):
    CSV = "csv"
    XLSX = "xlsx"


class ImportMode(str, Enum):
    """The only honest mode before the application owns a shared Unit of Work."""

    PARTIAL_CREATE_ONLY = "partial_create_only"


class ImportRowStatus(str, Enum):
    VALID = "valid"
    INVALID = "invalid"
    CREATED = "created"
    FAILED = "failed"
    SKIPPED = "skipped"


class ImportIssueSeverity(str, Enum):
    ERROR = "error"
    WARNING = "warning"


class ImportIssue(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet: str
    row: int = Field(ge=0)
    row_key: str = ""
    field: str = ""
    code: str
    message: str
    severity: ImportIssueSeverity = ImportIssueSeverity.ERROR
    value: Any | None = None


class ImportRowPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet: str
    row: int = Field(ge=1)
    row_key: str
    status: ImportRowStatus
    issues: list[ImportIssue] = Field(default_factory=list)


class ImportPreview(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: TransferEntity
    filename: str
    sha256: str = Field(pattern=r"^[0-9a-f]{64}$")
    template_version: str
    total_rows: int = Field(ge=0)
    valid_rows: int = Field(ge=0)
    invalid_rows: int = Field(ge=0)
    error_count: int = Field(ge=0)
    warning_count: int = Field(ge=0)
    can_commit_clean: bool
    can_commit_partial: bool
    atomic_commit: bool = False
    rows: list[ImportRowPreview] = Field(default_factory=list)
    issues: list[ImportIssue] = Field(default_factory=list)
    omitted_issue_count: int = Field(default=0, ge=0)


class ImportRowResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sheet: str
    row: int = Field(ge=1)
    row_key: str
    status: ImportRowStatus
    entity_id: UUID | None = None
    issues: list[ImportIssue] = Field(default_factory=list)


class ImportCommitResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    entity: TransferEntity
    filename: str
    sha256: str
    mode: ImportMode = ImportMode.PARTIAL_CREATE_ONLY
    atomic: bool = False
    clean_preview_required: bool
    committed: bool
    total_rows: int = Field(ge=0)
    created_rows: int = Field(ge=0)
    failed_rows: int = Field(ge=0)
    skipped_rows: int = Field(ge=0)
    rows: list[ImportRowResult] = Field(default_factory=list)


class TestCaseImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_key: str = Field(min_length=1, max_length=100)
    suite_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    preconditions: str = Field(default="", max_length=1000)
    steps: list[TestStep] = Field(default_factory=list, max_length=100)
    priority: TestCasePriority = TestCasePriority.P2
    case_type: TestCaseType = TestCaseType.MANUAL
    tags: list[str] = Field(default_factory=list, max_length=20)


class DefectImportPayload(BaseModel):
    model_config = ConfigDict(extra="forbid")

    row_key: str = Field(min_length=1, max_length=100)
    case_id: UUID | None = None
    execution_id: UUID | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    severity: DefectSeverity = DefectSeverity.MAJOR
    priority: DefectPriority = DefectPriority.P2
    reporter: str = Field(default="local-user", min_length=1, max_length=100)
    assignee: str = Field(default="", max_length=100)
    environment: str = Field(default="", max_length=200)
    reproduction_steps: list[
        Annotated[
            str,
            StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
        ]
    ] = Field(default_factory=list, max_length=50)
    expected_result: str = Field(default="", max_length=2000)
    actual_result: str = Field(default="", max_length=2000)
