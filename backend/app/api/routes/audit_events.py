from typing import Annotated
from uuid import UUID

from fastapi import APIRouter, Depends, Query

from app.api.dependencies import get_container
from app.container import ApplicationContainer
from app.domain.models import AuditAction
from app.schemas.response import ApiResponse

router = APIRouter(prefix="/audit-events", tags=["audit events"])
Container = Annotated[ApplicationContainer, Depends(get_container)]


@router.get("", response_model=ApiResponse)
async def list_audit_events(
    container: Container,
    project_id: UUID | None = None,
    entity_type: str | None = Query(
        default=None,
        min_length=1,
        max_length=50,
    ),
    entity_id: str | None = Query(
        default=None,
        min_length=1,
        max_length=64,
    ),
    action: AuditAction | None = None,
    limit: int = Query(default=100, ge=1, le=200),
) -> ApiResponse:
    return ApiResponse(
        data=await container.audit.list(
            project_id=project_id,
            entity_type=entity_type,
            entity_id=entity_id,
            action=action,
            limit=limit,
        )
    )
