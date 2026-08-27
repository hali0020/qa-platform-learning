from __future__ import annotations

from datetime import date
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Request

from app.schemas.quality import TrendGranularity
from app.schemas.response import ApiResponse
from app.services.quality import QualityService

router = APIRouter(prefix="/quality", tags=["quality"])


def get_quality_service(request: Request) -> QualityService:
    """Wiring hook: application startup must assign this read service."""

    service = getattr(request.app.state, "quality_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="质量报表服务尚未接线")
    return service


Service = Annotated[QualityService, Depends(get_quality_service)]


@router.get("/report", response_model=ApiResponse)
async def quality_report(
    project_id: str,
    date_from: date,
    date_to: date,
    service: Service,
    granularity: TrendGranularity = TrendGranularity.DAY,
    timezone: str = "Asia/Shanghai",
) -> ApiResponse:
    report = await service.report(
        project_id=project_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        timezone_name=timezone,
    )
    return ApiResponse(data=report)


@router.get("/summary", response_model=ApiResponse)
async def quality_summary(
    project_id: str,
    date_from: date,
    date_to: date,
    service: Service,
    timezone: str = "Asia/Shanghai",
) -> ApiResponse:
    report = await service.report(
        project_id=project_id,
        date_from=date_from,
        date_to=date_to,
        timezone_name=timezone,
    )
    return ApiResponse(data=report.summary)


@router.get("/trends", response_model=ApiResponse)
async def quality_trends(
    project_id: str,
    date_from: date,
    date_to: date,
    service: Service,
    granularity: TrendGranularity = TrendGranularity.DAY,
    timezone: str = "Asia/Shanghai",
) -> ApiResponse:
    report = await service.report(
        project_id=project_id,
        date_from=date_from,
        date_to=date_to,
        granularity=granularity,
        timezone_name=timezone,
    )
    return ApiResponse(
        data={"granularity": report.granularity, "items": report.trends}
    )


@router.get("/coverage", response_model=ApiResponse)
async def quality_coverage(
    project_id: str,
    date_from: date,
    date_to: date,
    service: Service,
    timezone: str = "Asia/Shanghai",
) -> ApiResponse:
    report = await service.report(
        project_id=project_id,
        date_from=date_from,
        date_to=date_to,
        timezone_name=timezone,
    )
    return ApiResponse(data=report.coverage_by_suite)
