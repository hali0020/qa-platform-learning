from __future__ import annotations

import re
from urllib.parse import quote

import httpx

from app.pipeline.models import PipelineStatus
from app.pipeline.providers.common import ExternalHttpProvider
from app.pipeline.providers.errors import ProviderConfigurationError, ProviderResponseError
from app.pipeline.providers.http import SafeHttpClient
from app.pipeline.providers.models import ProviderKind, ProviderRun, ProviderTriggerRequest
from app.pipeline.providers.security import AddressResolver, OutboundPolicy, default_resolver

_PIPELINE_ID = re.compile(r"[1-9]\d*\Z")


def _gitlab_status(value: object) -> PipelineStatus:
    normalized = str(value or "").lower()
    if normalized in {
        "created",
        "waiting_for_resource",
        "preparing",
        "pending",
        "manual",
        "scheduled",
    }:
        return PipelineStatus.QUEUED
    if normalized in {"running", "waiting_for_callback"}:
        return PipelineStatus.RUNNING
    if normalized == "success":
        return PipelineStatus.SUCCEEDED
    if normalized in {"canceled", "cancelled", "canceling", "skipped"}:
        return PipelineStatus.CANCELLED
    if normalized == "failed":
        return PipelineStatus.FAILED
    raise ProviderResponseError("GitLab returned an unknown pipeline status")


class GitLabPipelineProvider(ExternalHttpProvider):
    """Adapter for GitLab's documented v4 Pipelines API."""

    kind = ProviderKind.GITLAB

    def __init__(
        self,
        *,
        base_url: str,
        project_id: str,
        private_token: str,
        policy: OutboundPolicy,
        enabled: bool = False,
        resolver: AddressResolver = default_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not project_id or not private_token:
            raise ProviderConfigurationError("GitLab project or token is empty")
        client = SafeHttpClient(
            base_url,
            policy,
            resolver=resolver,
            transport=transport,
        )
        super().__init__(client, enabled=enabled)
        self._project_id = project_id
        self._project_path = quote(project_id, safe="")
        self._headers = {
            "PRIVATE-TOKEN": private_token,
            "Accept": "application/json",
        }

    @property
    def _pipelines_path(self) -> str:
        return f"/api/v4/projects/{self._project_path}/pipelines"

    async def trigger(self, request: ProviderTriggerRequest) -> ProviderRun:
        self._require_enabled()
        self._require_definition(request.definition_ref, self._project_id)
        if request.ref is None:
            raise ProviderConfigurationError("GitLab pipeline triggers require a branch or tag ref")
        variables = [
            {"key": key, "value": value, "variable_type": "env_var"}
            for key, value in sorted(request.variables.items())
        ]
        response = await self._client.request(
            "POST",
            self._pipelines_path,
            headers=self._headers,
            params={"ref": request.ref},
            json_body={"variables": variables} if variables else {},
        )
        return self._normalize(self._json_object(response, {200, 201}))

    async def get(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        self._validate_pipeline_id(external_id)
        response = await self._client.request(
            "GET",
            f"{self._pipelines_path}/{external_id}",
            headers=self._headers,
        )
        return self._normalize(self._json_object(response))

    async def cancel(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        self._validate_pipeline_id(external_id)
        response = await self._client.request(
            "POST",
            f"{self._pipelines_path}/{external_id}/cancel",
            headers=self._headers,
        )
        return self._normalize(self._json_object(response))

    @staticmethod
    def _validate_pipeline_id(value: str) -> None:
        if _PIPELINE_ID.fullmatch(value) is None:
            raise ProviderConfigurationError("GitLab pipeline id is invalid")

    def _normalize(self, payload: dict[str, object]) -> ProviderRun:
        external_id = self._string_field(payload, "id")
        self._validate_pipeline_id(external_id)
        raw_status = self._string_field(payload, "status")
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=_gitlab_status(raw_status),
            raw_status=raw_status,
            web_url=self._safe_web_url(payload.get("web_url")),
            metadata={
                "ref": str(payload["ref"])[:300] if payload.get("ref") is not None else None,
                "sha": str(payload["sha"])[:64] if payload.get("sha") is not None else None,
            },
        )


__all__ = ["GitLabPipelineProvider"]
