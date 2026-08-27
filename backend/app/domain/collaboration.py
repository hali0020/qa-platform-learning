from __future__ import annotations

from datetime import datetime
from enum import Enum
from typing import Literal
from uuid import UUID, uuid4

from pydantic import BaseModel, Field, model_validator

from app.domain.models import utc_now


class CollaborationTargetType(str, Enum):
    PROJECT = "project"
    TEST_CASE = "test_case"
    TEST_SUITE = "test_suite"
    TEST_PLAN = "test_plan"
    EXECUTION = "execution"
    DEFECT = "defect"
    SNAPSHOT = "snapshot"


class Comment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    entity_type: CollaborationTargetType
    entity_id: UUID
    parent_id: UUID | None = None
    author_id: UUID
    author_name: str
    body: str
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)
    edited_at: datetime | None = None
    deleted_at: datetime | None = None
    deleted_by_id: UUID | None = None


class Attachment(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    project_id: UUID
    entity_type: CollaborationTargetType
    entity_id: UUID
    comment_id: UUID | None = None
    uploader_id: UUID
    uploader_name: str
    original_filename: str
    storage_key: str
    storage_backend: Literal["local_filesystem", "s3_local_container"] = (
        "local_filesystem"
    )
    storage_namespace: str = Field(default="", max_length=200)
    media_type: str
    size_bytes: int
    sha256: str
    is_image: bool = False
    created_at: datetime = Field(default_factory=utc_now)
    deleted_at: datetime | None = None
    deleted_by_id: UUID | None = None

    @model_validator(mode="after")
    def validate_storage_route(self) -> Attachment:
        expected_namespace = (
            "" if self.storage_backend == "local_filesystem" else "qa-artifacts"
        )
        if self.storage_namespace != expected_namespace:
            raise ValueError("附件存储 backend 与 namespace 组合无效")
        return self
