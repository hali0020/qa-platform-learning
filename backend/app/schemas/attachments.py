from uuid import UUID

from pydantic import BaseModel, ConfigDict

from app.domain.collaboration import CollaborationTargetType


class AttachmentQuery(BaseModel):
    model_config = ConfigDict(extra="forbid")

    project_id: UUID | None = None
    entity_type: CollaborationTargetType
    entity_id: UUID


class AttachmentView(BaseModel):
    id: UUID
    project_id: UUID
    entity_type: CollaborationTargetType
    entity_id: UUID
    comment_id: UUID | None
    uploader_id: UUID
    uploader_name: str
    original_filename: str
    media_type: str
    size_bytes: int
    sha256: str
    is_image: bool
    created_at: str
    deleted_at: str | None
