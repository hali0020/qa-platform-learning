from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from app.pipeline.models import PipelineStatus
from app.pipeline.providers.common import ExternalHttpProvider
from app.pipeline.providers.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
)
from app.pipeline.providers.http import SafeHttpClient
from app.pipeline.providers.models import ProviderKind, ProviderRun, ProviderTriggerRequest
from app.pipeline.providers.security import (
    AddressResolver,
    OutboundPolicy,
    default_resolver,
    validate_relative_path,
)

_BUILD_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")


def _bkci_status(value: object) -> PipelineStatus:
    normalized = str(value or "").upper()
    if normalized in {
        "QUEUE",
        "QUEUE_CACHE",
        "REVIEWING",
        "READY_TO_RUN",
        "DEPENDENT_WAITING",
    }:
        return PipelineStatus.QUEUED
    if normalized in {
        "RUNNING",
        "PREPARE_ENV",
        "LOOP_WAITING",
        "CALL_WAITING",
    }:
        return PipelineStatus.RUNNING
    if normalized in {"SUCCEED", "SUCCESS"}:
        return PipelineStatus.SUCCEEDED
    if normalized in {"CANCELED", "CANCELLED", "TERMINATE"}:
        return PipelineStatus.CANCELLED
    if normalized in {
        "FAILED",
        "HEARTBEAT_TIMEOUT",
        "PREPARE_ENV_FAILED",
        "EXEC_TIMEOUT",
        "QUEUE_TIMEOUT",
        "QUALITY_CHECK_FAIL",
        "DEPENDENT_FAILED",
    }:
        return PipelineStatus.FAILED
    raise ProviderResponseError("BK-CI returned an unknown build status")


class BkCiPipelineProvider(ExternalHttpProvider):
    """BK-CI (蓝盾) adapter based on its open-source UserBuildResource.

    BK-CI gateway prefixes and authentication differ between editions and
    deployments. The default prefix matches the open-source process service;
    an operator must review it and supply an approved gateway credential
    before explicitly enabling this client.
    """

    kind = ProviderKind.BK_CI

    def __init__(
        self,
        *,
        base_url: str,
        project_id: str,
        pipeline_id: str,
        user_id: str,
        policy: OutboundPolicy,
        bearer_token: str | None = None,
        api_prefix: str = "/ms/process/api/user/builds",
        enabled: bool = False,
        resolver: AddressResolver = default_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not all((project_id, pipeline_id, user_id)):
            raise ProviderConfigurationError("BK-CI project, pipeline and user are required")
        validate_relative_path(api_prefix)
        client = SafeHttpClient(
            base_url,
            policy,
            resolver=resolver,
            transport=transport,
        )
        super().__init__(client, enabled=enabled)
        self._project_id = project_id
        self._pipeline_id = pipeline_id
        self._api_prefix = api_prefix.rstrip("/")
        self._headers = {
            "X-DEVOPS-UID": user_id,
            "Accept": "application/json",
        }
        if bearer_token is not None:
            if not bearer_token:
                raise ProviderConfigurationError("BK-CI bearer token is empty")
            self._headers["Authorization"] = f"Bearer {bearer_token}"

    @property
    def _pipeline_path(self) -> str:
        project = quote(self._project_id, safe="")
        pipeline = quote(self._pipeline_id, safe="")
        return f"{self._api_prefix}/projects/{project}/pipelines/{pipeline}"

    async def trigger(self, request: ProviderTriggerRequest) -> ProviderRun:
        self._require_enabled()
        self._require_definition(request.definition_ref, self._pipeline_id)
        response = await self._client.request(
            "POST",
            f"{self._pipeline_path}/start",
            headers=self._headers,
            json_body=request.variables,
        )
        payload = self._unwrap(self._json_object(response))
        if isinstance(payload, dict):
            build_id = payload.get("id") or payload.get("buildId")
        else:
            build_id = payload
        external_id = str(build_id or "")
        self._validate_build_id(external_id)
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.QUEUED,
            raw_status="QUEUE",
        )

    async def get(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        self._validate_build_id(external_id)
        response = await self._client.request(
            "GET",
            f"{self._pipeline_path}/builds/{quote(external_id, safe='')}/status",
            headers=self._headers,
        )
        payload = self._unwrap(self._json_object(response))
        if not isinstance(payload, dict):
            raise ProviderResponseError("BK-CI returned an unexpected build status")
        raw_status = str(payload.get("status") or payload.get("buildStatus") or "")
        if not raw_status:
            raise ProviderResponseError("BK-CI response is missing build status")
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=_bkci_status(raw_status),
            raw_status=raw_status,
            web_url=self._safe_web_url(payload.get("webUrl") or payload.get("url")),
            message=(
                str(payload.get("errorInfo"))[:500]
                if payload.get("errorInfo") is not None
                else None
            ),
        )

    async def cancel(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        self._validate_build_id(external_id)
        response = await self._client.request(
            "DELETE",
            f"{self._pipeline_path}/builds/{quote(external_id, safe='')}/stop",
            headers=self._headers,
        )
        payload = self._json_object(response)
        self._unwrap(payload)
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.CANCELLED,
            raw_status="cancel_requested",
            message="BK-CI cancellation was requested",
        )

    @staticmethod
    def _unwrap(payload: dict[str, object]) -> object:
        # Current open-source deployments use Result<T> with data. Preserve a
        # narrow compatibility fallback for gateways that return T directly.
        if "data" in payload:
            success = payload.get("status")
            if success not in (None, 0, "0", True):
                raise ProviderResponseError("BK-CI reported an unsuccessful operation")
            return payload["data"]
        return payload

    @staticmethod
    def _validate_build_id(value: str) -> None:
        if _BUILD_ID.fullmatch(value) is None:
            raise ProviderResponseError("BK-CI build id is invalid")


__all__ = ["BkCiPipelineProvider"]
