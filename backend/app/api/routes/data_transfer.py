from __future__ import annotations

import asyncio
import string
from collections.abc import Iterator
from typing import Annotated
from urllib.parse import quote

from fastapi import APIRouter, Depends, HTTPException, Query, Request, Response, status
from fastapi.responses import StreamingResponse
from starlette.datastructures import UploadFile

from app.data_transfer.exporters import TransferArtifact
from app.data_transfer.models import DataTransferFileError, SpreadsheetDependencyError
from app.data_transfer.security import MAX_UPLOAD_BYTES
from app.schemas.data_transfer import TransferEntity, TransferFormat
from app.schemas.response import ApiResponse
from app.services.data_transfer import DataTransferService

router = APIRouter(prefix="/data-transfer", tags=["data transfer"])


def get_data_transfer_service(request: Request) -> DataTransferService:
    """Wiring hook: application startup must assign this local service."""

    service = getattr(request.app.state, "data_transfer_service", None)
    if service is None:
        raise HTTPException(status_code=503, detail="数据传输服务尚未接线")
    return service


Service = Annotated[DataTransferService, Depends(get_data_transfer_service)]


@router.get("/templates/{entity}")
async def download_template(
    entity: TransferEntity,
    service: Service,
    transfer_format: Annotated[TransferFormat, Query(alias="format")],
) -> StreamingResponse:
    try:
        artifact = await asyncio.to_thread(service.template, entity, transfer_format)
    except SpreadsheetDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _download(artifact)


@router.post("/imports/{entity}/preview", response_model=ApiResponse)
async def preview_import(
    entity: TransferEntity,
    request: Request,
    service: Service,
) -> ApiResponse:
    project_id, upload, content, _ = await _multipart_payload(request)
    try:
        preview = await service.preview(
            entity=entity,
            project_id=project_id,
            filename=upload.filename or f"upload.{entity.value}",
            content=content,
        )
    except DataTransferFileError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except SpreadsheetDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return ApiResponse(data=preview)


@router.post("/imports/{entity}", response_model=ApiResponse)
async def commit_import(
    entity: TransferEntity,
    request: Request,
    response: Response,
    service: Service,
) -> ApiResponse:
    project_id, upload, content, form = await _multipart_payload(request)
    expected_sha256 = str(form.get("expected_sha256", "")).strip().casefold()
    if len(expected_sha256) != 64 or any(
        character not in string.hexdigits for character in expected_sha256
    ):
        raise HTTPException(status_code=422, detail="expected_sha256 必须是 64 位摘要")
    require_clean = _form_bool(
        form.get(
            "require_clean",
            form.get("require_clean_preview", "true"),
        )
    )
    try:
        result = await service.commit_partial_create_only(
            entity=entity,
            project_id=project_id,
            filename=upload.filename or f"upload.{entity.value}",
            content=content,
            expected_sha256=expected_sha256,
            require_clean_preview=require_clean,
        )
    except DataTransferFileError as exc:
        raise HTTPException(status_code=400, detail=exc.message) from exc
    except SpreadsheetDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    if result.created_rows:
        response.status_code = status.HTTP_201_CREATED
    return ApiResponse(data=result)


@router.get("/exports/{entity}")
async def export_data(
    entity: TransferEntity,
    project_id: str,
    service: Service,
    transfer_format: Annotated[TransferFormat, Query(alias="format")],
) -> StreamingResponse:
    try:
        if entity == TransferEntity.TEST_CASES:
            artifact = await service.export_test_cases(
                project_id=project_id,
                transfer_format=transfer_format,
            )
        else:
            artifact = await service.export_defects(
                project_id=project_id,
                transfer_format=transfer_format,
            )
    except SpreadsheetDependencyError as exc:
        raise HTTPException(status_code=503, detail=str(exc)) from exc
    return _download(artifact)


async def _multipart_payload(request: Request):
    try:
        async with request.form(
            max_files=1,
            max_fields=10,
            max_part_size=MAX_UPLOAD_BYTES,
        ) as form:
            upload = form.get("file")
            project_id = str(form.get("project_id", "")).strip()
            if not project_id:
                raise HTTPException(status_code=422, detail="project_id 不能为空")
            if not isinstance(upload, UploadFile):
                raise HTTPException(status_code=422, detail="file 必须是上传文件")
            content = await upload.read(MAX_UPLOAD_BYTES + 1)
            # Preserve primitive form values after the multipart temporary files
            # are closed by the context manager.
            values = {key: value for key, value in form.multi_items() if key != "file"}
            return project_id, upload, content, values
    except HTTPException:
        raise
    except (RuntimeError, ValueError) as exc:
        raise HTTPException(status_code=400, detail="无法解析 multipart 表单") from exc


def _form_bool(value: object) -> bool:
    normalized = str(value).strip().casefold()
    if normalized in {"true", "1", "yes", "on"}:
        return True
    if normalized in {"false", "0", "no", "off"}:
        return False
    raise HTTPException(
        status_code=422,
        detail="require_clean 必须是 true 或 false",
    )


def _download(artifact: TransferArtifact) -> StreamingResponse:
    headers = {
        "Content-Disposition": (
            "attachment; filename=qa-export; "
            f"filename*=UTF-8''{quote(artifact.filename)}"
        ),
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
        "X-Export-Count": str(artifact.row_count),
        "Content-Length": str(len(artifact.content)),
    }
    return StreamingResponse(
        _chunks(artifact.content),
        media_type=artifact.media_type,
        headers=headers,
    )


def _chunks(content: bytes, chunk_size: int = 64 * 1024) -> Iterator[bytes]:
    for offset in range(0, len(content), chunk_size):
        yield content[offset : offset + chunk_size]
