from datetime import datetime, timezone
from enum import Enum
from typing import Any
from uuid import UUID, uuid4

from pydantic import BaseModel, Field


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class ProjectStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class TestCaseStatus(str, Enum):
    DRAFT = "draft"
    ACTIVE = "active"
    DISABLED = "disabled"


class TestCasePriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class TestCaseType(str, Enum):
    MANUAL = "manual"
    AUTOMATED = "automated"


class TestSuiteStatus(str, Enum):
    ACTIVE = "active"
    ARCHIVED = "archived"


class SnapshotScopeType(str, Enum):
    PROJECT = "project"
    SUITE = "suite"


class TestPlanStatus(str, Enum):
    DRAFT = "draft"
    READY = "ready"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class ExecutionStatus(str, Enum):
    CREATED = "created"
    RUNNING = "running"
    COMPLETED = "completed"
    CANCELLED = "cancelled"


class CaseResultStatus(str, Enum):
    NOT_RUN = "not_run"
    PASSED = "passed"
    FAILED = "failed"
    BLOCKED = "blocked"
    SKIPPED = "skipped"


class DefectStatus(str, Enum):
    OPEN = "open"
    IN_PROGRESS = "in_progress"
    RESOLVED = "resolved"
    VERIFIED = "verified"
    CLOSED = "closed"
    REOPENED = "reopened"


class DefectSeverity(str, Enum):
    BLOCKER = "blocker"
    CRITICAL = "critical"
    MAJOR = "major"
    MINOR = "minor"
    TRIVIAL = "trivial"


class DefectPriority(str, Enum):
    P0 = "P0"
    P1 = "P1"
    P2 = "P2"
    P3 = "P3"


class AuditAction(str, Enum):
    CREATED = "created"
    UPDATED = "updated"
    STATUS_CHANGED = "status_changed"
    DELETED = "deleted"
    SNAPSHOT_CREATED = "snapshot_created"


class TestStep(BaseModel):
    action: str
    expected_result: str


class Project(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    key: str
    name: str
    description: str = ""
    status: ProjectStatus = ProjectStatus.ACTIVE
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TestCase(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    suite_id: UUID | None = None
    title: str
    preconditions: str = ""
    steps: list[TestStep] = Field(default_factory=list)
    priority: TestCasePriority = TestCasePriority.P2
    case_type: TestCaseType = TestCaseType.MANUAL
    status: TestCaseStatus = TestCaseStatus.DRAFT
    tags: list[str] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TestPlan(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    name: str
    description: str = ""
    case_ids: list[UUID] = Field(default_factory=list)
    status: TestPlanStatus = TestPlanStatus.DRAFT
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class CaseExecutionResult(BaseModel):
    case_id: UUID
    case_title: str
    status: CaseResultStatus = CaseResultStatus.NOT_RUN
    actual_result: str = ""
    comment: str = ""
    executed_at: datetime | None = None


class TestExecution(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    plan_id: UUID
    project_id: UUID
    status: ExecutionStatus = ExecutionStatus.CREATED
    results: list[CaseExecutionResult] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    started_at: datetime | None = None
    completed_at: datetime | None = None


class Defect(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    case_id: UUID | None = None
    execution_id: UUID | None = None
    title: str
    description: str = ""
    severity: DefectSeverity = DefectSeverity.MAJOR
    priority: DefectPriority = DefectPriority.P2
    status: DefectStatus = DefectStatus.OPEN
    reporter: str = "local-user"
    assignee: str = ""
    environment: str = ""
    reproduction_steps: list[str] = Field(default_factory=list)
    expected_result: str = ""
    actual_result: str = ""
    resolution: str = ""
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    resolved_at: datetime | None = None
    closed_at: datetime | None = None


class AuditChange(BaseModel):
    before: Any = None
    after: Any = None


class AuditEvent(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID | None = None
    entity_type: str
    entity_id: str
    action: AuditAction
    actor: str = "local-user"
    actor_user_id: UUID | None = None
    changes: dict[str, AuditChange] = Field(default_factory=dict)
    comment: str = ""
    created_at: datetime = Field(default_factory=utc_now)


class TestSuite(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    parent_id: UUID | None = None
    name: str
    description: str = ""
    status: TestSuiteStatus = TestSuiteStatus.ACTIVE
    position: int = 0
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class TestCaseSnapshotItem(BaseModel):
    source_case_id: UUID
    source_suite_id: UUID | None = None
    suite_path: list[str] = Field(default_factory=list)
    position: int
    title: str
    preconditions: str = ""
    steps: list[TestStep] = Field(default_factory=list)
    priority: TestCasePriority
    case_type: TestCaseType
    status: TestCaseStatus
    tags: list[str] = Field(default_factory=list)
    source_created_at: datetime
    source_updated_at: datetime


class TestCaseSnapshot(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    scope_type: SnapshotScopeType
    scope_id: UUID
    scope_name: str
    version: int
    label: str
    description: str = ""
    case_count: int
    items: list[TestCaseSnapshotItem] = Field(default_factory=list)
    created_at: datetime = Field(default_factory=utc_now)
