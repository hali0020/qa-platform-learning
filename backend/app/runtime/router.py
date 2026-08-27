"""FastAPI routes for persistent integrations and automation lessons."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Depends, Request, status

from app.api.dependencies import require_method_permission
from app.runtime.schemas import (
    DeviceAcquire,
    DeviceCreate,
    DeviceHeartbeat,
    DeviceLeaseAction,
    DeviceLeaseRenew,
    DevicePatch,
    ProviderConnectionCreate,
    ProviderConnectionPatch,
    ProviderTriggerPayload,
    ScheduleCreate,
    SchedulePatch,
    ScheduleTick,
    TaskClaim,
    TaskComplete,
    TaskDeadLetter,
    TaskEnqueue,
    TaskFail,
    TaskHeartbeat,
)
from app.runtime.service import PersistentRuntimeService
from app.schemas.response import ApiResponse


def get_runtime_service(request: Request) -> PersistentRuntimeService:
    return request.app.state.runtime_service


RuntimeService = Annotated[PersistentRuntimeService, Depends(get_runtime_service)]


def _data(value: Any) -> Any:
    if hasattr(value, "model_dump"):
        return value.model_dump(mode="json")
    if isinstance(value, list):
        return [_data(item) for item in value]
    return value


integrations_router = APIRouter(
    prefix="/integrations/connections",
    tags=["integrations"],
    dependencies=[
        Depends(require_method_permission("integrations.read", "integrations.manage"))
    ],
)


@integrations_router.get("/runtime-status", response_model=ApiResponse)
async def provider_runtime_status(service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=service.provider_runtime_status())


@integrations_router.get("", response_model=ApiResponse)
async def list_connections(service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_connections()))


@integrations_router.post(
    "", response_model=ApiResponse, status_code=status.HTTP_201_CREATED
)
async def create_connection(
    payload: ProviderConnectionCreate, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.create_connection(payload)))


@integrations_router.get("/{connection_id}", response_model=ApiResponse)
async def get_connection(connection_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.get_connection(connection_id)))


@integrations_router.patch("/{connection_id}", response_model=ApiResponse)
async def update_connection(
    connection_id: str,
    payload: ProviderConnectionPatch,
    service: RuntimeService,
) -> ApiResponse:
    return ApiResponse(data=_data(await service.update_connection(connection_id, payload)))


@integrations_router.delete("/{connection_id}", response_model=ApiResponse)
async def delete_connection(connection_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data={"deleted": await service.delete_connection(connection_id)})


@integrations_router.post("/{connection_id}/test", response_model=ApiResponse)
async def test_connection(connection_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.test_connection(connection_id)))


@integrations_router.post(
    "/{connection_id}/trigger",
    response_model=ApiResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def trigger_connection(
    connection_id: str,
    payload: ProviderTriggerPayload,
    service: RuntimeService,
) -> ApiResponse:
    return ApiResponse(data=_data(await service.trigger_provider(connection_id, payload)))


@integrations_router.get("/{connection_id}/runs", response_model=ApiResponse)
async def list_connection_runs(
    connection_id: str, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_provider_runs(connection_id)))


@integrations_router.get(
    "/{connection_id}/runs/{run_id}", response_model=ApiResponse
)
async def get_connection_run(
    connection_id: str, run_id: str, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.get_provider_run(connection_id, run_id)))


@integrations_router.post(
    "/{connection_id}/runs/{run_id}/cancel", response_model=ApiResponse
)
async def cancel_connection_run(
    connection_id: str, run_id: str, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.cancel_provider_run(connection_id, run_id)))


tasks_router = APIRouter(
    prefix="/automation/tasks",
    tags=["automation-tasks"],
    dependencies=[Depends(require_method_permission("schedules.read", "schedules.manage"))],
)


@tasks_router.get("", response_model=ApiResponse)
async def list_tasks(service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_tasks()))


@tasks_router.post("", response_model=ApiResponse, status_code=status.HTTP_202_ACCEPTED)
async def enqueue_task(payload: TaskEnqueue, service: RuntimeService) -> ApiResponse:
    task, replayed = await service.enqueue_task(payload)
    return ApiResponse(data={"task": _data(task), "replayed": replayed})


@tasks_router.post("/claim", response_model=ApiResponse)
async def claim_task(payload: TaskClaim, service: RuntimeService) -> ApiResponse:
    claimed = await service.claim_task(
        payload.worker_id, payload.queues, payload.lease_seconds
    )
    return ApiResponse(data=_data(claimed) if claimed is not None else None)


@tasks_router.get("/{task_id}", response_model=ApiResponse)
async def get_task(task_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.get_task(task_id)))


@tasks_router.post("/{task_id}/heartbeat", response_model=ApiResponse)
async def heartbeat_task(
    task_id: str, payload: TaskHeartbeat, service: RuntimeService
) -> ApiResponse:
    task = await service.heartbeat_task(
        task_id, payload.worker_id, payload.lease_token, payload.lease_seconds
    )
    return ApiResponse(data=_data(task))


@tasks_router.post("/{task_id}/complete", response_model=ApiResponse)
async def complete_task(
    task_id: str, payload: TaskComplete, service: RuntimeService
) -> ApiResponse:
    task = await service.complete_task(
        task_id, payload.worker_id, payload.lease_token, payload.result
    )
    return ApiResponse(data=_data(task))


@tasks_router.post("/{task_id}/fail", response_model=ApiResponse)
async def fail_task(
    task_id: str, payload: TaskFail, service: RuntimeService
) -> ApiResponse:
    task = await service.fail_task(
        task_id,
        payload.worker_id,
        payload.lease_token,
        payload.error_code,
        payload.retryable,
    )
    return ApiResponse(data=_data(task))


@tasks_router.post("/{task_id}/cancel", response_model=ApiResponse)
async def cancel_task(task_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.cancel_task(task_id)))


@tasks_router.post("/{task_id}/retry", response_model=ApiResponse)
async def retry_task(task_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.retry_task(task_id)))


@tasks_router.post("/{task_id}/dead-letter", response_model=ApiResponse)
async def dead_letter_task(
    task_id: str, payload: TaskDeadLetter, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(
        data=_data(await service.dead_letter_task(task_id, payload.error_code))
    )


devices_router = APIRouter(
    prefix="/automation/devices",
    tags=["automation-devices"],
    dependencies=[Depends(require_method_permission("devices.read", "devices.manage"))],
)


@devices_router.get("", response_model=ApiResponse)
async def list_devices(service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_devices()))


@devices_router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_device(payload: DeviceCreate, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.create_device(payload)))


@devices_router.post("/acquire", response_model=ApiResponse)
async def acquire_device(payload: DeviceAcquire, service: RuntimeService) -> ApiResponse:
    claimed = await service.acquire_device(payload)
    return ApiResponse(data=_data(claimed) if claimed is not None else None)


@devices_router.post("/leases/{lease_id}/start", response_model=ApiResponse)
async def start_device_work(
    lease_id: str, payload: DeviceLeaseAction, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(
        data=_data(
            await service.start_device_work(
                lease_id, payload.owner, payload.lease_token
            )
        )
    )


@devices_router.post("/leases/{lease_id}/renew", response_model=ApiResponse)
async def renew_device_lease(
    lease_id: str, payload: DeviceLeaseRenew, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(
        data=_data(
            await service.renew_device_lease(
                lease_id,
                payload.owner,
                payload.lease_token,
                payload.task_lease_token,
                payload.lease_seconds,
            )
        )
    )


@devices_router.post("/leases/{lease_id}/release", response_model=ApiResponse)
async def release_device_lease(
    lease_id: str, payload: DeviceLeaseAction, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(
        data=_data(
            await service.release_device_lease(
                lease_id, payload.owner, payload.lease_token
            )
        )
    )


@devices_router.get("/{device_id}", response_model=ApiResponse)
async def get_device(device_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.get_device(device_id)))


@devices_router.patch("/{device_id}", response_model=ApiResponse)
async def update_device(
    device_id: str, payload: DevicePatch, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.update_device(device_id, payload)))


@devices_router.delete("/{device_id}", response_model=ApiResponse)
async def delete_device(device_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data={"deleted": await service.delete_device(device_id)})


@devices_router.post("/{device_id}/heartbeat", response_model=ApiResponse)
async def heartbeat_device(
    device_id: str, payload: DeviceHeartbeat, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(
        data=_data(await service.heartbeat_device(device_id, payload.agent_id))
    )


schedules_router = APIRouter(
    prefix="/automation/schedules",
    tags=["automation-schedules"],
    dependencies=[Depends(require_method_permission("schedules.read", "schedules.manage"))],
)


@schedules_router.get("", response_model=ApiResponse)
async def list_schedules(service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_schedules()))


@schedules_router.post("", response_model=ApiResponse, status_code=status.HTTP_201_CREATED)
async def create_schedule(
    payload: ScheduleCreate, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.create_schedule(payload)))


@schedules_router.post("/tick", response_model=ApiResponse)
async def tick_schedules(
    service: RuntimeService, payload: ScheduleTick | None = None
) -> ApiResponse:
    return ApiResponse(
        data=_data(await service.tick_schedules(payload.now if payload else None))
    )


@schedules_router.get("/{schedule_id}", response_model=ApiResponse)
async def get_schedule(schedule_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.get_schedule(schedule_id)))


@schedules_router.patch("/{schedule_id}", response_model=ApiResponse)
async def update_schedule(
    schedule_id: str, payload: SchedulePatch, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.update_schedule(schedule_id, payload)))


@schedules_router.delete("/{schedule_id}", response_model=ApiResponse)
async def delete_schedule(schedule_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data={"deleted": await service.delete_schedule(schedule_id)})


@schedules_router.post("/{schedule_id}/run-now", response_model=ApiResponse)
async def run_schedule_now(schedule_id: str, service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.run_schedule_now(schedule_id)))


@schedules_router.get("/{schedule_id}/fires", response_model=ApiResponse)
async def list_schedule_fires(
    schedule_id: str, service: RuntimeService
) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_schedule_fires(schedule_id)))


runtime_router = APIRouter()
runtime_router.include_router(integrations_router)
runtime_router.include_router(tasks_router)
runtime_router.include_router(devices_router)
runtime_router.include_router(schedules_router)


__all__ = ["get_runtime_service", "runtime_router"]
