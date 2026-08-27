from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_container, get_current_principal
from app.container import ApplicationContainer
from app.domain.collaboration import CollaborationTargetType
from app.domain.identity import Principal
from app.schemas.comments import CommentCreate, CommentUpdate
from app.schemas.response import ApiResponse


router = APIRouter(prefix="/comments", tags=["comments"])
Container = Annotated[ApplicationContainer, Depends(get_container)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


@router.get("", response_model=ApiResponse)
async def list_comments(
    entity_type: CollaborationTargetType,
    entity_id: UUID,
    _: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.comments.list(entity_type, entity_id))


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_comment(
    payload: CommentCreate,
    principal: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.comments.create(payload, principal))


@router.patch("/{comment_id}", response_model=ApiResponse)
async def update_comment(
    comment_id: UUID,
    payload: CommentUpdate,
    principal: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.comments.update(comment_id, payload, principal)
    )


@router.delete("/{comment_id}", response_model=ApiResponse)
async def delete_comment(
    comment_id: UUID,
    principal: CurrentPrincipal,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.comments.delete(comment_id, principal))
