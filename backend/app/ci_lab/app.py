"""FastAPI factory for the isolated CI Lab machine API."""

from __future__ import annotations

import hmac
import json
from contextlib import asynccontextmanager
from pathlib import Path
from uuid import UUID

from fastapi import (
    Depends,
    FastAPI,
    Header,
    HTTPException,
    Path as ApiPath,
    Query,
    status,
)
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse

from app.ci_lab.database import CiLabDatabase
from app.ci_lab.models import (
    GateDecisionRequest,
    RunView,
    TriggerRunRequest,
    WebhookDeliveryStatus,
    WebhookDeliveryView,
)
from app.ci_lab.registry import DEFAULT_DEFINITION_REGISTRY, DefinitionRegistry
from app.ci_lab.service import CiLabError, CiLabService, Clock, utc_now


_MAX_REQUEST_BODY_BYTES = 16 * 1024
_MIN_MACHINE_TOKEN_LENGTH = 32
_MAX_MACHINE_TOKEN_LENGTH = 512
_MAX_AUTHORIZATION_HEADER_LENGTH = len("Bearer ") + _MAX_MACHINE_TOKEN_LENGTH


class _BoundedRequestBodyMiddleware:
    """Reject oversized POST bodies before Pydantic or auth can buffer them."""

    def __init__(self, app, *, max_bytes: int) -> None:
        self.app = app
        self.max_bytes = max_bytes

    async def __call__(self, scope, receive, send) -> None:
        if scope.get("type") != "http":
            await self.app(scope, receive, send)
            return

        for name, value in scope.get("headers", ()):
            if name.lower() != b"content-length":
                continue
            try:
                declared = int(value)
            except ValueError:
                await self._reject(send, status_code=400, code="invalid_content_length")
                return
            if declared < 0 or declared > self.max_bytes:
                await self._reject(send, status_code=413, code="request_body_too_large")
                return

        chunks: list[bytes] = []
        total = 0
        while True:
            message = await receive()
            if message["type"] == "http.disconnect":
                return
            body = message.get("body", b"")
            total += len(body)
            if total > self.max_bytes:
                await self._reject(send, status_code=413, code="request_body_too_large")
                return
            chunks.append(body)
            if not message.get("more_body", False):
                break

        replayed = False

        async def replay_receive():
            nonlocal replayed
            if replayed:
                return {"type": "http.disconnect"}
            replayed = True
            return {
                "type": "http.request",
                "body": b"".join(chunks),
                "more_body": False,
            }

        await self.app(scope, replay_receive, send)

    @staticmethod
    async def _reject(send, *, status_code: int, code: str) -> None:
        body = json.dumps(
            {"code": code, "detail": "request body was rejected"},
            separators=(",", ":"),
        ).encode("utf-8")
        await send(
            {
                "type": "http.response.start",
                "status": status_code,
                "headers": [
                    (b"content-type", b"application/json"),
                    (b"content-length", str(len(body)).encode("ascii")),
                ],
            }
        )
        await send({"type": "http.response.body", "body": body})


def _validate_machine_token(value: str) -> str:
    if (
        not _MIN_MACHINE_TOKEN_LENGTH <= len(value) <= _MAX_MACHINE_TOKEN_LENGTH
        or not value.isascii()
        or value != value.strip()
        or any(ord(character) < 33 or ord(character) == 127 for character in value)
    ):
        raise ValueError(
            "CI Lab machine token must contain 32-512 visible, non-whitespace characters"
        )
    return value


def create_ci_lab_app(
    *,
    database_path: str | Path,
    machine_token: str,
    registry: DefinitionRegistry = DEFAULT_DEFINITION_REGISTRY,
    clock: Clock = utc_now,
) -> FastAPI:
    """Build an independent app with explicitly injected local dependencies."""

    trusted_token = _validate_machine_token(machine_token)
    trusted_token_bytes = trusted_token.encode("ascii")
    database = CiLabDatabase(database_path)
    service = CiLabService(database, registry, clock=clock)

    @asynccontextmanager
    async def lifespan(_application: FastAPI):
        await service.initialize()
        try:
            yield
        finally:
            await service.close()

    application = FastAPI(
        title="QA Learning CI Lab",
        version="0.1.0",
        debug=False,
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
        lifespan=lifespan,
    )
    application.state.ci_lab_service = service
    application.state.ci_lab_database = database
    application.add_middleware(
        _BoundedRequestBodyMiddleware,
        max_bytes=_MAX_REQUEST_BODY_BYTES,
    )

    async def require_machine(
        authorization: str | None = Header(default=None),
    ) -> None:
        bounded = (
            authorization
            if authorization is not None
            and len(authorization) <= _MAX_AUTHORIZATION_HEADER_LENGTH
            and authorization.isascii()
            else ""
        )
        scheme, separator, credential = bounded.partition(" ")
        accepted = (
            separator == " "
            and scheme.casefold() == "bearer"
            and _MIN_MACHINE_TOKEN_LENGTH
            <= len(credential)
            <= _MAX_MACHINE_TOKEN_LENGTH
            and hmac.compare_digest(
                credential.encode("ascii"),
                trusted_token_bytes,
            )
        )
        if not accepted:
            raise HTTPException(
                status_code=status.HTTP_401_UNAUTHORIZED,
                detail="valid CI Lab machine authentication is required",
                headers={"WWW-Authenticate": "Bearer"},
            )

    @application.exception_handler(CiLabError)
    async def handle_ci_lab_error(_request, error: CiLabError) -> JSONResponse:
        return JSONResponse(
            status_code=error.status_code,
            content={"code": error.code, "detail": error.message},
        )

    @application.exception_handler(RequestValidationError)
    async def handle_validation_error(
        _request,
        _error: RequestValidationError,
    ) -> JSONResponse:
        # Validation errors can otherwise echo a rejected variable document.
        return JSONResponse(
            status_code=status.HTTP_422_UNPROCESSABLE_CONTENT,
            content={
                "code": "ci_lab_request_validation_error",
                "detail": "request validation failed",
            },
        )

    @application.get("/health/live")
    async def live() -> dict[str, str]:
        return {"service": "ci-lab", "status": "ok"}

    @application.get(
        "/api/v1/definitions",
        dependencies=[Depends(require_machine)],
    )
    async def list_definitions() -> list[dict[str, object]]:
        return [item.model_dump(mode="json") for item in service.list_definitions()]

    @application.post(
        "/api/v1/definitions/{definition}/runs",
        response_model=RunView,
        status_code=status.HTTP_202_ACCEPTED,
        dependencies=[Depends(require_machine)],
    )
    async def trigger_run(
        payload: TriggerRunRequest,
        definition: str = ApiPath(
            min_length=1,
            max_length=100,
            pattern=r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}",
        ),
        idempotency_key: str = Header(
            alias="Idempotency-Key",
            min_length=1,
            max_length=200,
        ),
    ) -> RunView:
        return await service.trigger(definition, payload, idempotency_key)

    @application.get(
        "/api/v1/runs/{run_id}",
        response_model=RunView,
        dependencies=[Depends(require_machine)],
    )
    async def get_run(run_id: UUID) -> RunView:
        return await service.get(run_id)

    @application.post(
        "/api/v1/runs/{run_id}/cancel",
        response_model=RunView,
        dependencies=[Depends(require_machine)],
    )
    async def cancel_run(run_id: UUID) -> RunView:
        return await service.cancel(run_id)

    @application.post(
        "/api/v1/runs/{run_id}/gate-decisions",
        response_model=RunView,
        dependencies=[Depends(require_machine)],
    )
    async def decide_run_gate(
        payload: GateDecisionRequest,
        run_id: UUID,
    ) -> RunView:
        return await service.decide_gate(run_id, payload)

    @application.get(
        "/api/v1/webhook-deliveries",
        response_model=list[WebhookDeliveryView],
        dependencies=[Depends(require_machine)],
    )
    async def list_webhook_deliveries(
        delivery_status: WebhookDeliveryStatus | None = Query(
            default=None,
            alias="status",
        ),
        run_id: UUID | None = None,
        limit: int = Query(default=100, ge=1, le=500),
    ) -> list[WebhookDeliveryView]:
        return await service.list_webhook_deliveries(
            status=delivery_status,
            run_id=run_id,
            limit=limit,
        )

    @application.post(
        "/api/v1/webhook-deliveries/{delivery_id}/retry",
        response_model=WebhookDeliveryView,
        dependencies=[Depends(require_machine)],
    )
    async def retry_webhook_delivery(delivery_id: UUID) -> WebhookDeliveryView:
        return await service.retry_webhook_delivery(delivery_id)

    return application


create_app = create_ci_lab_app


__all__ = ["create_app", "create_ci_lab_app"]
