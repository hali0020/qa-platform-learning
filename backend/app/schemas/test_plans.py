from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models import TestPlanStatus


class TestPlanCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    name: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=1000)
    case_ids: list[str] = Field(default_factory=list, max_length=500)

    @field_validator("case_ids")
    @classmethod
    def reject_duplicate_cases(cls, values: list[str]) -> list[str]:
        if len(values) != len(set(values)):
            raise ValueError("case_ids 不能包含重复项")
        return values


class TestPlanUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=1000)
    case_ids: list[str] | None = Field(default=None, max_length=500)

    @field_validator("case_ids")
    @classmethod
    def reject_duplicate_cases(cls, values: list[str] | None) -> list[str] | None:
        if values is not None and len(values) != len(set(values)):
            raise ValueError("case_ids 不能包含重复项")
        return values


class TestPlanTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TestPlanStatus
