from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import TestCaseStatus
from app.schemas.response import ApiResponse
from app.schemas.test_cases import TestCaseCreate, TestCaseTransition, TestCaseUpdate

router = APIRouter(prefix="/test-cases", tags=["test cases"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_test_case(
    payload: TestCaseCreate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.test_cases.create(payload))


@router.get("", response_model=ApiResponse)
async def list_test_cases(
    container: Container,
    project_id: UUID | None = None,
    suite_id: UUID | None = None,
    unassigned: bool = False,
    case_status: TestCaseStatus | None = Query(default=None, alias="status"),
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_cases.list(
            project_id,
            case_status,
            suite_id,
            unassigned,
        )
    )


@router.get("/{case_id}", response_model=ApiResponse)
async def get_test_case(case_id: UUID, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.test_cases.get(case_id))


@router.patch("/{case_id}", response_model=ApiResponse)
async def update_test_case(
    case_id: UUID,
    payload: TestCaseUpdate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.test_cases.update(case_id, payload))


@router.post("/{case_id}/transition", response_model=ApiResponse)
async def transition_test_case(
    case_id: UUID,
    payload: TestCaseTransition,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_cases.transition(case_id, payload.status)
    )


@router.delete("/{case_id}", response_model=ApiResponse)
async def delete_test_case(case_id: UUID, container: Container) -> ApiResponse:
    deleted_id = await container.test_cases.delete(case_id)
    return ApiResponse(data={"deleted_id": str(deleted_id)})
