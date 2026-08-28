"""FastAPI routes for persistent integrations and automation lessons."""

from __future__ import annotations

from typing import Annotated, Any
from urllib.parse import quote

from fastapi import (
    APIRouter,
    Depends,
    File,
    Form,
    HTTPException,
    Request,
    UploadFile,
    status,
)
from fastapi.responses import StreamingResponse
from starlette.background import BackgroundTask

from app.api.dependencies import (
    get_container,
    get_current_principal,
    require_method_permission,
    require_permission,
)
from app.container import ApplicationContainer
from app.domain.identity import PermissionCode, Principal
from app.runtime.schemas import (
    DeviceAcquire,
    DeviceCreate,
    DeviceHeartbeat,
    DeviceLeaseAction,
    DeviceLeaseRenew,
    DevicePatch,
    ProviderConnectionCreate,
    ProviderConnectionPatch,
    ProviderRunApprovalPayload,
    ProviderTriggerDispatch,
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
from app.runtime.artifacts import ArtifactKind, ProviderRunArtifactService
from app.runtime.webhook_security import RawBodyBuffer, WebhookSecurityError
from app.schemas.response import ApiResponse


def get_runtime_service(request: Request) -> PersistentRuntimeService:
    return request.app.state.runtime_service


RuntimeService = Annotated[PersistentRuntimeService, Depends(get_runtime_service)]
Container = Annotated[ApplicationContainer, Depends(get_container)]
CurrentPrincipal = Annotated[Principal, Depends(get_current_principal)]


def get_provider_artifact_service(
    container: Container,
) -> ProviderRunArtifactService:
    if container.provider_artifacts is None:
        raise RuntimeError("Provider Artifact 服务未初始化")
    return container.provider_artifacts


ProviderArtifacts = Annotated[
    ProviderRunArtifactService,
    Depends(get_provider_artifact_service),
]


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


@integrations_router.get("/trigger-intents/all", response_model=ApiResponse)
async def list_trigger_intents(service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_provider_trigger_intents()))


@integrations_router.post("/trigger-intents/dispatch-one", response_model=ApiResponse)
async def dispatch_trigger_intent(
    payload: ProviderTriggerDispatch,
    service: RuntimeService,
) -> ApiResponse:
    return ApiResponse(
        data=_data(
            await service.dispatch_provider_trigger_once(
                payload.worker_id,
                payload.lease_seconds,
            )
        )
    )


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


quality_gate_router = APIRouter(
    prefix="/integrations/connections",
    tags=["integration-quality-gates"],
    dependencies=[Depends(require_permission(PermissionCode.PIPELINE_APPROVE))],
)


@quality_gate_router.post(
    "/{connection_id}/runs/{run_id}/gate-decisions",
    response_model=ApiResponse,
)
async def decide_quality_gate(
    connection_id: str,
    run_id: str,
    payload: ProviderRunApprovalPayload,
    service: RuntimeService,
) -> ApiResponse:
    return ApiResponse(
        data=_data(
            await service.decide_provider_quality_gate(
                connection_id,
                run_id,
                payload,
            )
        )
    )


webhooks_router = APIRouter(prefix="/webhooks", tags=["machine-webhooks"])


@webhooks_router.post(
    "/learning-ci/{connection_id}",
    response_model=ApiResponse,
)
async def receive_learning_ci_webhook(
    connection_id: str,
    request: Request,
    service: RuntimeService,
) -> ApiResponse:
    buffer = RawBodyBuffer()
    try:
        async for chunk in request.stream():
            buffer.append(chunk)
    except WebhookSecurityError:
        raise HTTPException(
            status_code=status.HTTP_413_CONTENT_TOO_LARGE,
            detail="Webhook 请求体超过 16 KiB",
        ) from None
    result = await service.process_learning_ci_webhook(
        connection_id,
        raw_body=buffer.finish(),
        raw_headers=list(request.scope.get("headers", ())),
    )
    return ApiResponse(data=_data(result))


artifacts_router = APIRouter(
    prefix="/integrations/connections",
    tags=["integration-artifacts"],
    dependencies=[
        Depends(require_method_permission("integrations.read", "integrations.manage"))
    ],
)


@artifacts_router.get(
    "/{connection_id}/runs/{run_id}/artifacts",
    response_model=ApiResponse,
)
async def list_provider_artifacts(
    connection_id: str,
    run_id: str,
    _: CurrentPrincipal,
    service: ProviderArtifacts,
) -> ApiResponse:
    return ApiResponse(
        data=_data(
            await service.list(connection_id=connection_id, run_id=run_id)
        )
    )


@artifacts_router.post(
    "/{connection_id}/runs/{run_id}/artifacts",
    response_model=ApiResponse,
    status_code=status.HTTP_201_CREATED,
)
async def create_provider_artifact(
    connection_id: str,
    run_id: str,
    kind: Annotated[ArtifactKind, Form()],
    file: Annotated[UploadFile, File()],
    principal: CurrentPrincipal,
    service: ProviderArtifacts,
) -> ApiResponse:
    artifact = await service.create(
        connection_id=connection_id,
        run_id=run_id,
        kind=kind,
        upload=file,
        principal=principal,
    )
    return ApiResponse(data=_data(artifact))


@artifacts_router.get(
    "/{connection_id}/runs/{run_id}/artifacts/{artifact_id}/content"
)
async def get_provider_artifact_content(
    connection_id: str,
    run_id: str,
    artifact_id: str,
    _: CurrentPrincipal,
    service: ProviderArtifacts,
) -> StreamingResponse:
    artifact, content = await service.content(
        connection_id=connection_id,
        run_id=run_id,
        artifact_id=artifact_id,
    )
    encoded_filename = quote(artifact.original_filename)
    disposition = (
        f"attachment; filename*=utf-8''{encoded_filename}"
        if encoded_filename != artifact.original_filename
        else f'attachment; filename="{artifact.original_filename}"'
    )
    return StreamingResponse(
        content,
        media_type=artifact.media_type or "application/octet-stream",
        background=BackgroundTask(content.aclose),
        headers={
            "Cache-Control": "private, no-store",
            "X-Content-Type-Options": "nosniff",
            "Content-Security-Policy": "sandbox; default-src 'none'",
            "Content-Disposition": disposition,
            "Content-Length": str(content.size_bytes),
            "X-Artifact-SHA256": artifact.sha256 or "",
        },
    )


@artifacts_router.delete(
    "/{connection_id}/runs/{run_id}/artifacts/{artifact_id}",
    response_model=ApiResponse,
)
async def delete_provider_artifact(
    connection_id: str,
    run_id: str,
    artifact_id: str,
    principal: CurrentPrincipal,
    service: ProviderArtifacts,
) -> ApiResponse:
    artifact = await service.delete(
        connection_id=connection_id,
        run_id=run_id,
        artifact_id=artifact_id,
        principal=principal,
    )
    return ApiResponse(data=_data(artifact))


tasks_router = APIRouter(
    prefix="/automation/tasks",
    tags=["automation-tasks"],
    dependencies=[Depends(require_method_permission("schedules.read", "schedules.manage"))],
)


@tasks_router.get("", response_model=ApiResponse)
async def list_tasks(service: RuntimeService) -> ApiResponse:
    return ApiResponse(data=_data(await service.list_tasks()))


@tasks_router.get("/wakeup-outbox", response_model=ApiResponse)
async def list_task_wakeup_outbox(service: RuntimeService) -> ApiResponse:
    """Expose only safe delivery metadata; no task payload or lease digest."""

    return ApiResponse(data=_data(await service.list_task_wakeup_outbox()))


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
runtime_router.include_router(quality_gate_router)
runtime_router.include_router(webhooks_router)
runtime_router.include_router(artifacts_router)
runtime_router.include_router(tasks_router)
runtime_router.include_router(devices_router)
runtime_router.include_router(schedules_router)


__all__ = ["get_runtime_service", "runtime_router"]
