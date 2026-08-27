from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.domain.collaboration import CollaborationTargetType


class CommentCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID
    entity_type: CollaborationTargetType
    entity_id: UUID
    parent_id: UUID | None = None
    body: str = Field(min_length=1, max_length=5000)

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("评论内容不能为空")
        return normalized


class CommentUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    body: str = Field(min_length=1, max_length=5000)

    @field_validator("body")
    @classmethod
    def strip_body(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("评论内容不能为空")
        return normalized
