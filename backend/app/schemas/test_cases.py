from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import (
    TestCasePriority,
    TestCaseStatus,
    TestCaseType,
    TestStep,
)


class TestCaseCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    suite_id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    preconditions: str = Field(default="", max_length=1000)
    steps: list[TestStep] = Field(default_factory=list, max_length=100)
    priority: TestCasePriority = TestCasePriority.P2
    case_type: TestCaseType = TestCaseType.MANUAL
    tags: list[str] = Field(default_factory=list, max_length=20)


class TestCaseUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    suite_id: str | None = None
    title: str | None = Field(default=None, min_length=1, max_length=200)
    preconditions: str | None = Field(default=None, max_length=1000)
    steps: list[TestStep] | None = Field(default=None, max_length=100)
    priority: TestCasePriority | None = None
    case_type: TestCaseType | None = None
    tags: list[str] | None = Field(default=None, max_length=20)


class TestCaseTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TestCaseStatus
