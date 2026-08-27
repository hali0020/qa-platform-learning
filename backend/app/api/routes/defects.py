from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import DefectSeverity, DefectStatus
from app.schemas.defects import DefectCreate, DefectTransition, DefectUpdate
from app.schemas.response import ApiResponse

router = APIRouter(prefix="/defects", tags=["defects"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_defect(
    payload: DefectCreate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.defects.create(payload))


@router.get("", response_model=ApiResponse)
async def list_defects(
    container: Container,
    project_id: UUID | None = None,
    defect_status: DefectStatus | None = Query(default=None, alias="status"),
    severity: DefectSeverity | None = None,
    assignee: str | None = Query(default=None, max_length=100),
    case_id: UUID | None = None,
    execution_id: UUID | None = None,
) -> ApiResponse:
    return ApiResponse(
        data=await container.defects.list(
            project_id=project_id,
            status=defect_status,
            severity=severity,
            assignee=assignee,
            case_id=case_id,
            execution_id=execution_id,
        )
    )


@router.get("/{defect_id}", response_model=ApiResponse)
async def get_defect(defect_id: UUID, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.defects.get(defect_id))


@router.patch("/{defect_id}", response_model=ApiResponse)
async def update_defect(
    defect_id: UUID,
    payload: DefectUpdate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.defects.update(defect_id, payload))


@router.post("/{defect_id}/transition", response_model=ApiResponse)
async def transition_defect(
    defect_id: UUID,
    payload: DefectTransition,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.defects.transition(defect_id, payload)
    )
