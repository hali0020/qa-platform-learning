from __future__ import annotations

import base64
import re
from urllib.parse import quote, urljoin, urlparse

import httpx

from app.pipeline.models import PipelineStatus
from app.pipeline.providers.common import ExternalHttpProvider
from app.pipeline.providers.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
    ProviderSecurityError,
)
from app.pipeline.providers.http import SafeHttpClient
from app.pipeline.providers.models import ProviderKind, ProviderRun, ProviderTriggerRequest
from app.pipeline.providers.security import AddressResolver, OutboundPolicy, default_resolver

_QUEUE_HANDLE = re.compile(r"queue:(\d+)\Z")
_BUILD_HANDLE = re.compile(r"build:(\d+)\Z")


def _job_path(job_name: str) -> str:
    segments = job_name.split("/")
    if not segments or any(not segment or segment in {".", ".."} for segment in segments):
        raise ProviderConfigurationError("Jenkins job name is invalid")
    return "".join(f"/job/{quote(segment, safe='')}" for segment in segments)


def _jenkins_status(result: object, building: bool) -> PipelineStatus:
    if building:
        return PipelineStatus.RUNNING
    normalized = str(result or "").upper()
    if normalized == "SUCCESS":
        return PipelineStatus.SUCCEEDED
    if normalized in {"ABORTED", "CANCELLED", "CANCELED"}:
        return PipelineStatus.CANCELLED
    if normalized in {"FAILURE", "UNSTABLE", "NOT_BUILT"}:
        return PipelineStatus.FAILED
    return PipelineStatus.QUEUED


class JenkinsPipelineProvider(ExternalHttpProvider):
    """Jenkins Remote Access API adapter.

    Official Jenkins API tokens are used with HTTP Basic authentication and
    are exempt from crumb requirements. No password/crumb downgrade is
    attempted. Jenkins has no universal signed build callback, so status is
    queried through its queue/build APIs.
    """

    kind = ProviderKind.JENKINS

    def __init__(
        self,
        *,
        base_url: str,
        job_name: str,
        username: str,
        api_token: str,
        policy: OutboundPolicy,
        enabled: bool = False,
        resolver: AddressResolver = default_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not username or ":" in username or not api_token:
            raise ProviderConfigurationError("Jenkins API credentials are invalid")
        client = SafeHttpClient(
            base_url,
            policy,
            resolver=resolver,
            transport=transport,
        )
        super().__init__(client, enabled=enabled)
        self._base_url = client.base_url
        self._job_name = job_name
        self._job_path = _job_path(job_name)
        encoded = base64.b64encode(f"{username}:{api_token}".encode("utf-8")).decode("ascii")
        self._headers = {"Authorization": f"Basic {encoded}", "Accept": "application/json"}

    async def trigger(self, request: ProviderTriggerRequest) -> ProviderRun:
        self._require_enabled()
        self._require_definition(request.definition_ref, self._job_name)
        endpoint = "buildWithParameters" if request.variables else "build"
        response = await self._client.request(
            "POST",
            f"{self._job_path}/{endpoint}",
            headers=self._headers,
            form_data=request.variables or None,
        )
        self._expect_status(response, {200, 201, 202})
        location = response.headers.get("location")
        if not location:
            raise ProviderResponseError("Jenkins did not return a queue location")
        absolute = urljoin(f"{self._base_url}/", location)
        expected = urlparse(self._base_url)
        parsed = urlparse(absolute)
        if (parsed.scheme, parsed.hostname, parsed.port) != (
            expected.scheme,
            expected.hostname,
            expected.port,
        ):
            raise ProviderSecurityError("Jenkins returned a cross-origin queue location")
        match = re.search(r"/queue/item/(\d+)/?\Z", parsed.path)
        if match is None:
            raise ProviderResponseError("Jenkins returned an invalid queue location")
        queue_id = match.group(1)
        return ProviderRun(
            provider=self.kind,
            external_id=f"queue:{queue_id}",
            status=PipelineStatus.QUEUED,
            raw_status="queued",
        )

    async def get(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        queue_match = _QUEUE_HANDLE.fullmatch(external_id)
        if queue_match is not None:
            return await self._get_queue(external_id, queue_match.group(1))
        build_match = _BUILD_HANDLE.fullmatch(external_id)
        if build_match is not None:
            return await self._get_build(external_id, build_match.group(1))
        raise ProviderConfigurationError("Jenkins run handle is invalid")

    async def _get_queue(self, external_id: str, queue_id: str) -> ProviderRun:
        response = await self._client.request(
            "GET",
            f"/queue/item/{queue_id}/api/json",
            headers=self._headers,
        )
        payload = self._json_object(response)
        if payload.get("cancelled") is True:
            return ProviderRun(
                provider=self.kind,
                external_id=external_id,
                status=PipelineStatus.CANCELLED,
                raw_status="cancelled",
            )
        executable = payload.get("executable")
        if isinstance(executable, dict) and isinstance(executable.get("number"), int):
            return await self._get_build(
                external_id,
                str(executable["number"]),
            )
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.QUEUED,
            raw_status="queued",
            message=str(payload.get("why"))[:500] if payload.get("why") else None,
        )

    async def _get_build(self, external_id: str, build_number: str) -> ProviderRun:
        response = await self._client.request(
            "GET",
            f"{self._job_path}/{build_number}/api/json",
            headers=self._headers,
        )
        payload = self._json_object(response)
        building = payload.get("building") is True
        raw_status = "RUNNING" if building else str(payload.get("result") or "QUEUED")
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=_jenkins_status(payload.get("result"), building),
            raw_status=raw_status,
            web_url=self._safe_web_url(payload.get("url")),
            metadata={"build_number": int(build_number)},
        )

    async def cancel(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        queue_match = _QUEUE_HANDLE.fullmatch(external_id)
        if queue_match is not None:
            current = await self._get_queue(external_id, queue_match.group(1))
            build_number = current.metadata.get("build_number")
            if isinstance(build_number, int) and not current.status.is_terminal:
                await self._stop_build(str(build_number))
            elif not current.status.is_terminal:
                response = await self._client.request(
                    "POST",
                    "/queue/cancelItem",
                    headers=self._headers,
                    params={"id": queue_match.group(1)},
                )
                self._expect_status(response, {200, 302})
            else:
                return current
        else:
            build_match = _BUILD_HANDLE.fullmatch(external_id)
            if build_match is None:
                raise ProviderConfigurationError("Jenkins run handle is invalid")
            current = await self._get_build(external_id, build_match.group(1))
            if current.status.is_terminal:
                return current
            await self._stop_build(build_match.group(1))
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.CANCELLED,
            raw_status="cancel_requested",
            message="Jenkins cancellation was requested",
        )

    async def _stop_build(self, build_number: str) -> None:
        response = await self._client.request(
            "POST",
            f"{self._job_path}/{build_number}/stop",
            headers=self._headers,
        )
        self._expect_status(response, {200, 302})


__all__ = ["JenkinsPipelineProvider"]
