from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, status

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import SnapshotScopeType
from app.schemas.response import ApiResponse
from app.schemas.test_case_snapshots import TestCaseSnapshotCreate

router = APIRouter(prefix="/test-case-snapshots", tags=["test case snapshots"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_test_case_snapshot(
    payload: TestCaseSnapshotCreate,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_case_snapshots.create(payload)
    )


@router.get("", response_model=ApiResponse)
async def list_test_case_snapshots(
    container: Container,
    project_id: UUID | None = None,
    scope_type: SnapshotScopeType | None = None,
    scope_id: UUID | None = None,
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_case_snapshots.list(
            project_id,
            scope_type,
            scope_id,
        )
    )


@router.get("/{snapshot_id}", response_model=ApiResponse)
async def get_test_case_snapshot(
    snapshot_id: UUID,
    container: Container,
) -> ApiResponse:
    return ApiResponse(
        data=await container.test_case_snapshots.get(snapshot_id)
    )
