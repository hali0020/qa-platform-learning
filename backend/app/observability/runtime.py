from __future__ import annotations

import logging
from dataclasses import dataclass

from fastapi import FastAPI
from prometheus_client import CollectorRegistry

from app.database import Database
from app.observability.config import ObservabilityOptions
from app.observability.health import DatabaseReadinessProbe, ReadinessProbe
from app.observability.json_logging import configure_json_logger
from app.observability.metrics import ObservabilityMetrics
from app.observability.middleware import (
    PrometheusMiddleware,
    RequestContextMiddleware,
)


@dataclass(slots=True)
class ObservabilityRuntime:
    options: ObservabilityOptions
    metrics: ObservabilityMetrics
    readiness_probe: ReadinessProbe


def install_observability(
    application: FastAPI,
    *,
    database: Database,
    settings: object,
    registry: CollectorRegistry | None = None,
    readiness_probe: ReadinessProbe | None = None,
    logger: logging.Logger | None = None,
    configure_logger: bool = True,
) -> ObservabilityRuntime:
    """Install middleware and state; routes remain an explicit include step."""

    options = ObservabilityOptions.from_settings(settings)
    metrics = ObservabilityMetrics(registry)
    request_logger = logger or logging.getLogger("qa_platform.http")
    if configure_logger:
        configure_json_logger(request_logger)

    runtime = ObservabilityRuntime(
        options=options,
        metrics=metrics,
        readiness_probe=readiness_probe or DatabaseReadinessProbe(database),
    )
    application.state.observability = runtime
    application.add_middleware(
        PrometheusMiddleware,
        metrics=metrics,
        enabled=options.metrics_enabled,
        excluded_paths=("/metrics",),
    )
    application.add_middleware(
        RequestContextMiddleware,
        logger=request_logger,
        logging_enabled=options.request_logging_enabled,
    )
    return runtime
