from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models import ProjectStatus


class ProjectCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    key: str = Field(min_length=2, max_length=20, pattern=r"^[A-Za-z][A-Za-z0-9_-]*$")
    name: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=500)

    @field_validator("key")
    @classmethod
    def normalize_key(cls, value: str) -> str:
        return value.upper()


class ProjectUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    name: str | None = Field(default=None, min_length=1, max_length=100)
    description: str | None = Field(default=None, max_length=500)


class ProjectTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ProjectStatus
