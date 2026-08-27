from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import TestPlanStatus
from app.schemas.response import ApiResponse
from app.schemas.test_plans import TestPlanCreate, TestPlanTransition, TestPlanUpdate

router = APIRouter(prefix="/test-plans", tags=["test plans"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_test_plan(
    payload: TestPlanCreate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.test_plans.create(payload))


@router.get("", response_model=ApiResponse)
async def list_test_plans(
    container: Container,
    project_id: UUID | None = None,
    plan_status: TestPlanStatus | None = Query(default=None, alias="status"),
) -> ApiResponse:
    return ApiResponse(data=await container.test_plans.list(project_id, plan_status))


@router.get("/{plan_id}", response_model=ApiResponse)
async def get_test_plan(plan_id: UUID, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.test_plans.get(plan_id))


@router.patch("/{plan_id}", response_model=ApiResponse)
async def update_test_plan(
    plan_id: UUID,
    payload: TestPlanUpdate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.test_plans.update(plan_id, payload))


@router.post("/{plan_id}/transition", response_model=ApiResponse)
async def transition_test_plan(
    plan_id: UUID,
    payload: TestPlanTransition,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_plans.transition(plan_id, payload.status)
    )


@router.delete("/{plan_id}", response_model=ApiResponse)
async def delete_test_plan(plan_id: UUID, container: Container) -> ApiResponse:
    deleted_id = await container.test_plans.delete(plan_id)
    return ApiResponse(data={"deleted_id": str(deleted_id)})
