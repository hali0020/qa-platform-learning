from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import ProjectStatus
from app.schemas.projects import ProjectCreate, ProjectTransition, ProjectUpdate
from app.schemas.response import ApiResponse

router = APIRouter(prefix="/projects", tags=["projects"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_project(payload: ProjectCreate, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.projects.create(payload))


@router.get("", response_model=ApiResponse)
async def list_projects(
    container: Container,
    project_status: ProjectStatus | None = Query(default=None, alias="status"),
) -> ApiResponse:
    return ApiResponse(data=await container.projects.list(project_status))


@router.get("/{project_id}", response_model=ApiResponse)
async def get_project(project_id: UUID, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.projects.get(project_id))


@router.patch("/{project_id}", response_model=ApiResponse)
async def update_project(
    project_id: UUID,
    payload: ProjectUpdate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.projects.update(project_id, payload))


@router.post("/{project_id}/transition", response_model=ApiResponse)
async def transition_project(
    project_id: UUID,
    payload: ProjectTransition,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.projects.transition(project_id, payload.status)
    )


@router.delete("/{project_id}", response_model=ApiResponse)
async def delete_project(project_id: UUID, container: Container) -> ApiResponse:
    deleted_id = await container.projects.delete(project_id)
    return ApiResponse(data={"deleted_id": str(deleted_id)})
