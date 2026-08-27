from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field, StringConstraints, field_validator

from app.domain.models import DefectPriority, DefectSeverity, DefectStatus


ReproductionStep = Annotated[
    str,
    StringConstraints(strip_whitespace=True, min_length=1, max_length=500),
]


class DefectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    case_id: str | None = None
    execution_id: str | None = None
    title: str = Field(min_length=1, max_length=200)
    description: str = Field(default="", max_length=2000)
    severity: DefectSeverity = DefectSeverity.MAJOR
    priority: DefectPriority = DefectPriority.P2
    reporter: str = Field(default="local-user", min_length=1, max_length=100)
    assignee: str = Field(default="", max_length=100)
    environment: str = Field(default="", max_length=200)
    reproduction_steps: list[ReproductionStep] = Field(
        default_factory=list,
        max_length=50,
    )
    expected_result: str = Field(default="", max_length=2000)
    actual_result: str = Field(default="", max_length=2000)

    @field_validator("title", "reporter")
    @classmethod
    def strip_required_text(cls, value: str) -> str:
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空白字符串")
        return stripped

    @field_validator(
        "description",
        "assignee",
        "environment",
        "expected_result",
        "actual_result",
    )
    @classmethod
    def strip_optional_text(cls, value: str) -> str:
        return value.strip()


class DefectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    title: str | None = Field(default=None, min_length=1, max_length=200)
    description: str | None = Field(default=None, max_length=2000)
    severity: DefectSeverity | None = None
    priority: DefectPriority | None = None
    assignee: str | None = Field(default=None, max_length=100)
    environment: str | None = Field(default=None, max_length=200)
    reproduction_steps: list[ReproductionStep] | None = Field(
        default=None,
        max_length=50,
    )
    expected_result: str | None = Field(default=None, max_length=2000)
    actual_result: str | None = Field(default=None, max_length=2000)

    @field_validator("title")
    @classmethod
    def strip_title(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("字段不能为空白字符串")
        return stripped

    @field_validator(
        "description",
        "assignee",
        "environment",
        "expected_result",
        "actual_result",
    )
    @classmethod
    def strip_optional_text(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None


class DefectTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: DefectStatus
    resolution: str | None = Field(default=None, max_length=2000)
    comment: str = Field(default="", max_length=1000)

    @field_validator("resolution")
    @classmethod
    def strip_resolution(cls, value: str | None) -> str | None:
        return value.strip() if value is not None else None

    @field_validator("comment")
    @classmethod
    def strip_comment(cls, value: str) -> str:
        return value.strip()
