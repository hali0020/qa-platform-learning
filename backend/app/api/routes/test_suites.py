from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query, status

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import TestSuiteStatus
from app.schemas.response import ApiResponse
from app.schemas.test_suites import (
    TestSuiteCreate,
    TestSuiteTransition,
    TestSuiteUpdate,
)

router = APIRouter(prefix="/test-suites", tags=["test suites"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_test_suite(
    payload: TestSuiteCreate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.test_suites.create(payload))


@router.get("", response_model=ApiResponse)
async def list_test_suites(
    container: Container,
    project_id: UUID | None = None,
    suite_status: TestSuiteStatus | None = Query(default=None, alias="status"),
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_suites.list(project_id, suite_status)
    )


@router.get("/{suite_id}", response_model=ApiResponse)
async def get_test_suite(suite_id: UUID, container: Container) -> ApiResponse:
    return ApiResponse(data=await container.test_suites.get(suite_id))


@router.patch("/{suite_id}", response_model=ApiResponse)
async def update_test_suite(
    suite_id: UUID,
    payload: TestSuiteUpdate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(data=await container.test_suites.update(suite_id, payload))


@router.post("/{suite_id}/transition", response_model=ApiResponse)
async def transition_test_suite(
    suite_id: UUID,
    payload: TestSuiteTransition,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_suites.transition(suite_id, payload.status)
    )


@router.delete("/{suite_id}", response_model=ApiResponse)
async def delete_test_suite(suite_id: UUID, container: Container) -> ApiResponse:
    deleted_id = await container.test_suites.delete(suite_id)
    return ApiResponse(data={"deleted_id": str(deleted_id)})
