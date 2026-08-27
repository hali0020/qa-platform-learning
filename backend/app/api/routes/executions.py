from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import ExecutionStatus
from app.schemas.executions import (
    CaseResultUpdate,
    ExecutionCreate,
    ExecutionTransition,
)
from app.schemas.response import ApiResponse

router = APIRouter(prefix="/executions", tags=["test executions"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_execution(
    payload: ExecutionCreate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.executions.create(payload.plan_id))


@router.get("", response_model=ApiResponse)
async def list_executions(
    container: Container,
    plan_id: UUID | None = None,
    execution_status: ExecutionStatus | None = Query(default=None, alias="status"),
) -> ApiResponse:
    return ApiResponse(
        data=await container.executions.list(plan_id, execution_status)
    )


@router.get("/{execution_id}", response_model=ApiResponse)
async def get_execution(execution_id: UUID, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.executions.get(execution_id))


@router.post("/{execution_id}/transition", response_model=ApiResponse)
async def transition_execution(
    execution_id: UUID,
    payload: ExecutionTransition,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.executions.transition(execution_id, payload.status)
    )


@router.put("/{execution_id}/results/{case_id}", response_model=ApiResponse)
async def update_case_result(
    execution_id: UUID,
    case_id: UUID,
    payload: CaseResultUpdate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.executions.update_case_result(
            execution_id,
            case_id,
            payload.status,
            payload.actual_result,
            payload.comment,
        )
    )


@router.delete("/{execution_id}", response_model=ApiResponse)
async def delete_execution(execution_id: UUID, container: Container) -> ApiResponse:
    deleted_id = await container.executions.delete(execution_id)
    return ApiResponse(data={"deleted_id": str(deleted_id)})
