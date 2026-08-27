from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_container, require_permission
from app.container import ApplicationContainer
from app.domain.identity import PermissionCode, Principal
from app.schemas.auth import (
    OidcBindingRequest,
    PasswordResetRequest,
    UserCreate,
    UserUpdate,
)
from app.schemas.response import ApiResponse


router = APIRouter(tags=["users and roles"])
Container = Annotated[ApplicationContainer, Depends(get_container)]
CanReadUsers = Annotated[
    Principal,
    Depends(require_permission(PermissionCode.USERS_READ)),
]
CanManageUsers = Annotated[
    Principal,
    Depends(require_permission(PermissionCode.USERS_MANAGE)),
]


@router.get("/roles", response_model=ApiResponse)
async def list_roles(_: CanReadUsers, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.identity.list_roles())


@router.get("/users", response_model=ApiResponse)
async def list_users(_: CanReadUsers, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.identity.list_users())


@router.post("/users", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_user(
    payload: UserCreate,
    _: CanManageUsers,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.identity.create_user(payload))


@router.patch("/users/{user_id}", response_model=ApiResponse)
async def update_user(
    user_id: UUID,
    payload: UserUpdate,
    principal: CanManageUsers,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.identity.update_user(user_id, payload, principal)
    )


@router.post("/users/{user_id}/reset-password", response_model=ApiResponse)
async def reset_password(
    user_id: UUID,
    payload: PasswordResetRequest,
    _: CanManageUsers,
    container: Container,
) -> ApiResponse:
    await container.identity.reset_password(user_id, payload)
    return ApiResponse(data={"reset": True, "sessions_revoked": True})


@router.post("/users/{user_id}/revoke-sessions", response_model=ApiResponse)
async def revoke_sessions(
    user_id: UUID,
    _: CanManageUsers,
    container: Container,
) -> ApiResponse:
    revoked = await container.identity.revoke_sessions(user_id)
    return ApiResponse(data={"revoked_sessions": revoked})


@router.post("/users/{user_id}/oidc-binding", response_model=ApiResponse)
async def bind_oidc_identity(
    user_id: UUID,
    payload: OidcBindingRequest,
    _: CanManageUsers,
    container: Container,
) -> ApiResponse:
    await container.identity.bind_oidc_identity(
        user_id=user_id,
        subject=payload.subject,
    )
    return ApiResponse(data={"bound": True})
