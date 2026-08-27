from __future__ import annotations

import json
import time
from collections import Counter

from fastapi import APIRouter, HTTPException, Request, Response
from prometheus_client import CONTENT_TYPE_LATEST, generate_latest

from app.observability.runtime import ObservabilityRuntime


router = APIRouter(tags=["observability"])


def _runtime(request: Request) -> ObservabilityRuntime:
    runtime = getattr(request.app.state, "observability", None)
    if not isinstance(runtime, ObservabilityRuntime):
        raise HTTPException(status_code=503, detail="observability_not_installed")
    return runtime


@router.get("/health/live", include_in_schema=False)
async def liveness(request: Request) -> dict[str, object]:
    runtime = _runtime(request)
    return {
        "status": "alive",
        "service": runtime.options.service_name,
        "environment": runtime.options.environment,
        "checked_at_unix": int(time.time()),
    }


@router.get("/health/ready", include_in_schema=False)
async def readiness(request: Request) -> Response:
    runtime = _runtime(request)
    result = await runtime.readiness_probe.check()
    status_code = 200 if result.ready else 503
    payload = {
        "status": "ready" if result.ready else "not_ready",
        "checks": {
            "database": {
                "status": "up" if result.ready else "down",
                "reason": result.reason,
            }
        },
    }
    # Explicit JSON keeps this standalone router independent from the app's
    # custom API envelope and exception handlers.
    return Response(
        content=json.dumps(payload, ensure_ascii=False),
        status_code=status_code,
        media_type="application/json",
    )


@router.get("/metrics", include_in_schema=False)
async def metrics(request: Request) -> Response:
    runtime = _runtime(request)
    if not runtime.options.metrics_enabled:
        raise HTTPException(status_code=404, detail="metrics_disabled")
    await _refresh_runtime_snapshots(request, runtime)
    return Response(
        content=generate_latest(runtime.metrics.registry),
        media_type=CONTENT_TYPE_LATEST,
    )


async def _refresh_runtime_snapshots(
    request: Request,
    runtime: ObservabilityRuntime,
) -> None:
    """Refresh bounded task/device gauges from the optional local runtime.

    Scraping remains read-only and never probes a CI provider.  The runtime is
    duck-typed here so observability stays usable without the automation
    learning module.
    """

    service = getattr(request.app.state, "runtime_service", None)
    snapshot = getattr(service, "observability_snapshot", None)
    if snapshot is None:
        return
    task_counts, raw_device_counts = await snapshot()
    device_counts: Counter[str] = Counter()
    for status, count in raw_device_counts.items():
        mapped = {
            "reserved": "leased",
            "maintenance": "unhealthy",
        }.get(status, status)
        device_counts[mapped] += count
    runtime.metrics.business.set_task_snapshot(task_counts)
    runtime.metrics.business.set_device_snapshot(device_counts)
