from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, status

from app.pipeline.errors import PipelineError, map_pipeline_error
from app.pipeline.models import PipelineCallbackRequest, PipelineTriggerRequest
from app.pipeline.service import (
    InMemoryPipelineService,
    get_pipeline_service,
)
from app.schemas.response import ApiResponse

router = APIRouter(prefix="/pipelines", tags=["pipelines"])


def _data(model: object) -> object:
    if hasattr(model, "model_dump"):
        return model.model_dump(mode="json")  # type: ignore[union-attr]
    return model


PipelineServiceDependency = Annotated[
    InMemoryPipelineService, Depends(get_pipeline_service)
]


def _http_error(error: PipelineError) -> HTTPException:
    mapped = map_pipeline_error(error)
    return HTTPException(status_code=mapped.status_code, detail=mapped.message)


@router.post("", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def trigger_pipeline(
    payload: PipelineTriggerRequest,
    service: PipelineServiceDependency,
) -> ApiResponse:
    try:
        result = await service.trigger(payload)
    except PipelineError as error:
        raise _http_error(error) from error
    return ApiResponse(data=_data(result))


@router.get("", response_model=ApiResponse)
async def list_pipelines(service: PipelineServiceDependency) -> ApiResponse:
    runs = await service.list_runs()
    return ApiResponse(data=[_data(run) for run in runs])


@router.get("/{run_id}", response_model=ApiResponse)
async def get_pipeline(
    run_id: str,
    service: PipelineServiceDependency,
) -> ApiResponse:
    try:
        run = await service.get(run_id)
    except PipelineError as error:
        raise _http_error(error) from error
    return ApiResponse(data=_data(run))


@router.post("/{run_id}/cancel", response_model=ApiResponse)
async def cancel_pipeline(
    run_id: str,
    service: PipelineServiceDependency,
) -> ApiResponse:
    try:
        result = await service.cancel(run_id)
    except PipelineError as error:
        raise _http_error(error) from error
    return ApiResponse(data=_data(result))


@router.post("/{run_id}/callbacks", response_model=ApiResponse)
async def receive_pipeline_callback(
    run_id: str,
    payload: PipelineCallbackRequest,
    service: PipelineServiceDependency,
) -> ApiResponse:
    try:
        result = await service.apply_callback(run_id, payload)
    except PipelineError as error:
        raise _http_error(error) from error
    return ApiResponse(data=_data(result))
