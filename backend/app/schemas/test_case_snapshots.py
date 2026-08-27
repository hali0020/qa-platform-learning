from pydantic import BaseModel, ConfigDict, Field, model_validator

from app.domain.models import SnapshotScopeType


class TestCaseSnapshotCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str
    suite_id: str | None = None
    label: str = Field(min_length=1, max_length=100)
    description: str = Field(default="", max_length=1000)
    include_descendants: bool = True

    @model_validator(mode="after")
    def normalize_text(self) -> "TestCaseSnapshotCreate":
        self.label = self.label.strip()
        if not self.label:
            raise ValueError("快照标签不能为空")
        return self


class TestCaseSnapshotListQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    scope_type: SnapshotScopeType | None = None
    scope_id: str | None = None

    @model_validator(mode="after")
    def require_scope_pair(self) -> "TestCaseSnapshotListQuery":
        if (self.scope_type is None) != (self.scope_id is None):
            raise ValueError("scope_type 和 scope_id 必须同时提供")
        return self
