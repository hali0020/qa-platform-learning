from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models import TestSuiteStatus


class TestSuiteCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    parent_id: str | None = None
    name: str = Field(min_length=1, max_length=150)
    description: str = Field(default="", max_length=1000)
    position: int = Field(default=0, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("套件名称不能为空")
        return normalized


class TestSuiteUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    parent_id: str | None = None
    name: str | None = Field(default=None, min_length=1, max_length=150)
    description: str | None = Field(default=None, max_length=1000)
    position: int | None = Field(default=None, ge=0, le=1_000_000)

    @field_validator("name")
    @classmethod
    def normalize_name(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("套件名称不能为空")
        return normalized


class TestSuiteTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: TestSuiteStatus
