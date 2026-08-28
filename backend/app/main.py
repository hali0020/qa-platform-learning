from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.responses import JSONResponse
from starlette.middleware.cors import CORSMiddleware

from app.api.router import api_router
from app.container import build_container
from app.core.config import Settings, get_settings
from app.core.errors import DomainError
from app.database import Database
from app.pipeline.service import create_pipeline_service, get_pipeline_service
from app.observability import install_observability, observability_router
from app.schemas.response import ApiResponse
from app.runtime.service import create_runtime_service
from app.secrets import build_secret_store


@asynccontextmanager
async def lifespan(application: FastAPI):
    application.state.settings.validate_local_safety()
    pipeline_initialized = False
    try:
        await application.state.container.initialize()
        await application.state.runtime_service.initialize()
        # Alembic is the only schema owner. The pipeline store may load only
        # after migrations have completed successfully.
        await application.state.pipeline_service.initialize()
        pipeline_initialized = True
        yield
    finally:
        try:
            if pipeline_initialized:
                await application.state.pipeline_service.shutdown()
        finally:
            try:
                await application.state.runtime_service.shutdown()
            finally:
                # Dispose a partially initialized SQLAlchemy engine too,
                # while preserving the original startup exception.
                await application.state.container.shutdown()


def create_app(settings: Settings | None = None) -> FastAPI:
    current_settings = settings or get_settings()
    current_settings.validate_local_safety()

    application = FastAPI(
        title=current_settings.app_name,
        debug=current_settings.debug,
        version="0.3.0",
        lifespan=lifespan,
    )
    application.state.settings = current_settings
    database = Database(
        current_settings.database_url,
        runtime_mode=current_settings.database_runtime_mode,
        app_env=current_settings.app_env,
    )
    install_observability(
        application,
        database=database,
        settings=current_settings,
    )
    application.state.container = build_container(database, current_settings)
    secret_store = build_secret_store(current_settings)
    application.state.runtime_service = create_runtime_service(
        database,
        current_settings,
        provider_metrics=application.state.observability.metrics.business,
        secret_store=secret_store,
    )
    application.state.data_transfer_service = (
        application.state.container.data_transfer
    )
    application.state.quality_service = application.state.container.quality
    application.state.pipeline_service = create_pipeline_service(
        database=None if database.is_memory else database,
    )
    application.dependency_overrides[get_pipeline_service] = (
        lambda: application.state.pipeline_service
    )
    application.add_middleware(
        CORSMiddleware,
        allow_origins=list(current_settings.cors_origins),
        allow_credentials=True,
        allow_methods=["GET", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"],
        allow_headers=[
            "Content-Type",
            "Idempotency-Key",
            "X-CSRF-Token",
            "X-Request-ID",
            "X-QA-Webhook-Event-ID",
            "X-QA-Webhook-Timestamp",
            "X-QA-Webhook-Signature",
        ],
        expose_headers=[
            "Content-Disposition",
            "X-Export-Count",
            "X-Request-ID",
            "X-Artifact-SHA256",
        ],
    )

    @application.exception_handler(DomainError)
    async def handle_domain_error(_: Request, exc: DomainError) -> JSONResponse:
        response = ApiResponse(code=exc.code, message=exc.message, data=None)
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(response.model_dump()),
        )

    @application.exception_handler(RequestValidationError)
    async def handle_request_validation(
        _: Request,
        exc: RequestValidationError,
    ) -> JSONResponse:
        response = ApiResponse(
            code=42200,
            message="请求参数校验失败",
            data={"errors": exc.errors()},
        )
        return JSONResponse(
            status_code=422,
            content=jsonable_encoder(response.model_dump()),
        )

    @application.exception_handler(HTTPException)
    async def handle_http_exception(_: Request, exc: HTTPException) -> JSONResponse:
        message = exc.detail if isinstance(exc.detail, str) else "HTTP 请求失败"
        response = ApiResponse(
            code=exc.status_code * 100,
            message=message,
            data=None,
        )
        return JSONResponse(
            status_code=exc.status_code,
            content=jsonable_encoder(response.model_dump()),
            headers=exc.headers,
        )

    application.include_router(api_router, prefix="/api/v1")
    application.include_router(observability_router)
    return application


app = create_app()
