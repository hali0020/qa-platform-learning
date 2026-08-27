from typing import Annotated
from urllib.parse import quote
from uuid import UUID

from fastapi import APIRouter, Depends, File, Form, UploadFile, status
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.dependencies import get_container, get_current_principal
from app.container import ApplicationContainer
from app.domain.collaboration import CollaborationTargetType
from app.domain.identity import Principal
from app.schemas.response import ApiResponse


router = APIRouter(prefix="/attachments", tags=["attachments"])
Container = Annotated[ApplicationContainer, Depends(get_container)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@router.get("", response_model=ApiResponse)
async def list_attachments(
    entity_type: CollaborationTargetType,
    entity_id: UUID,
    _: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.attachments.list(entity_type, entity_id)
    )


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_attachment(
    project_id: Annotated[UUID, Form()],
    entity_type: Annotated[CollaborationTargetType, Form()],
    entity_id: Annotated[UUID, Form()],
    file: Annotated[UploadFile, File()],
    principal: CurrentPrincipal,
    container: Container,
    comment_id: Annotated[UUID | None, Form()] = None,
) -> ApiResponse:
    return ApiResponse(
        data=await container.attachments.create(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            comment_id=comment_id,
            upload=file,
            principal=principal,
        )
    )


@router.get("/{attachment_id}/content")
async def get_attachment_content(
    attachment_id: UUID,
    _: CurrentPrincipal,
    container: Container,
    inline: bool = False,
) -> StreamingResponse:
    attachment, content = await container.attachments.content(attachment_id)
    disposition = "inline" if inline and attachment.is_image else "attachment"
    encoded_filename = quote(attachment.original_filename)
    if encoded_filename != attachment.original_filename:
        content_disposition = (
            f"{disposition}; filename*=utf-8''{encoded_filename}"
        )
    else:
        content_disposition = (
            f'{disposition}; filename="{attachment.original_filename}"'
        )
    return StreamingResponse(
        content,
        media_type=attachment.media_type,
        background=BackgroundTask(content.aclose),
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Content-Disposition": content_disposition,
            "Content-Length": str(content.size_bytes),
        },
    )


@router.delete("/{attachment_id}", response_model=ApiResponse)
async def delete_attachment(
    attachment_id: UUID,
    principal: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.attachments.delete(attachment_id, principal)
    )
