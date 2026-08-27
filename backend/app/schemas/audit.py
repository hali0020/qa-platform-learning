from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.models import AuditAction


class AuditEventQuery(BaseModel):
    """通用审计只读接口的过滤条件。"""

    model_config = ConfigDict(extra="forbid")

    project_id: str | None = None
    entity_type: str | None = Field(default=None, min_length=1, max_length=50)
    entity_id: str | None = Field(default=None, min_length=1, max_length=64)
    action: AuditAction | None = None
    limit: int = Field(default=100, ge=1, le=200)

    @field_validator("entity_type", "entity_id")
    @classmethod
    def strip_filter(cls, value: str | None) -> str | None:
        if value is None:
            return None
        stripped = value.strip()
        if not stripped:
            raise ValueError("过滤条件不能为空白字符串")
        return stripped
