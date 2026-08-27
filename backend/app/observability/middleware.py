from __future__ import annotations

import logging
import re
import time
import uuid
from collections.abc import Awaitable, Callable, Iterable
from typing import Any

from starlette.datastructures import Headers, MutableHeaders
from starlette.types import ASGIApp, Message, Receive, Scope, Send

from app.observability.json_logging import request_id_context
from app.observability.metrics import ObservabilityMetrics, route_template


_REQUEST_ID_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")


def normalized_request_id(value: str | None) -> str:
    if value and _REQUEST_ID_PATTERN.fullmatch(value):
        return value
    return uuid.uuid4().hex


class RequestContextMiddleware:
    """Attach/return a safe request ID and emit one completion log per request."""

    def __init__(
        self,
        app: ASGIApp,
        *,
        logger: logging.Logger,
        logging_enabled: bool = True,
    ) -> None:
        self.app = app
        self.logger = logger
        self.logging_enabled = logging_enabled

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if scope["type"] != "http":
            await self.app(scope, receive, send)
            return

        headers = Headers(scope=scope)
        request_id = normalized_request_id(headers.get("x-request-id"))
        scope.setdefault("state", {})["request_id"] = request_id
        status_code = 500
        started_at = time.perf_counter()
        token = request_id_context.set(request_id)

        async def send_with_request_id(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
                MutableHeaders(scope=message)["X-Request-ID"] = request_id
            await send(message)

        error_type: str | None = None
        try:
            await self.app(scope, receive, send_with_request_id)
        except BaseException as exc:
            error_type = type(exc).__name__
            raise
        finally:
            elapsed_ms = (time.perf_counter() - started_at) * 1000
            if self.logging_enabled:
                extra: dict[str, Any] = {
                    "event": "http_request_completed",
                    "request_id": request_id,
                    "method": scope.get("method", ""),
                    "route": route_template(scope),
                    "status_code": status_code,
                    "duration_ms": round(elapsed_ms, 3),
                }
                if error_type:
                    extra["error_type"] = error_type
                self.logger.info("HTTP request completed", extra=extra)
            request_id_context.reset(token)


class PrometheusMiddleware:
    def __init__(
        self,
        app: ASGIApp,
        *,
        metrics: ObservabilityMetrics,
        enabled: bool = True,
        excluded_paths: Iterable[str] = ("/metrics",),
    ) -> None:
        self.app = app
        self.metrics = metrics
        self.enabled = enabled
        self.excluded_paths = frozenset(excluded_paths)

    async def __call__(self, scope: Scope, receive: Receive, send: Send) -> None:
        if (
            scope["type"] != "http"
            or not self.enabled
            or scope.get("path") in self.excluded_paths
        ):
            await self.app(scope, receive, send)
            return

        status_code = 500
        started_at = time.perf_counter()
        self.metrics.http.started()

        async def capture_status(message: Message) -> None:
            nonlocal status_code
            if message["type"] == "http.response.start":
                status_code = int(message["status"])
            await send(message)

        try:
            await self.app(scope, receive, capture_status)
        finally:
            self.metrics.http.completed(
                method=str(scope.get("method", "OTHER")),
                route=route_template(scope),
                status_code=status_code,
                duration_seconds=time.perf_counter() - started_at,
            )
