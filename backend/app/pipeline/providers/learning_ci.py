from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any
from urllib.parse import quote

import httpx

from app.pipeline.models import PipelineStatus
from app.pipeline.providers.common import ExternalHttpProvider
from app.pipeline.providers.errors import (
    ProviderConfigurationError,
    ProviderResponseError,
)
from app.pipeline.providers.http import SafeHttpClient
from app.pipeline.providers.models import (
    ProviderKind,
    ProviderRun,
    ProviderTriggerRequest,
)
from app.pipeline.providers.security import (
    AddressResolver,
    OutboundPolicy,
    default_resolver,
)

_RUN_ID = re.compile(r"[A-Za-z0-9_-]{1,200}\Z")
_DEFINITION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._/-]{0,299}\Z")
_IDEMPOTENCY_KEY = re.compile(
    r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z"
)
_MIN_BEARER_TOKEN_LENGTH = 32
_MAX_BEARER_TOKEN_LENGTH = 512


def _learning_ci_status(value: object) -> PipelineStatus:
    normalized = str(value or "").lower()
    if normalized in {"queued", "waiting_approval"}:
        return PipelineStatus.QUEUED
    if normalized == "running":
        return PipelineStatus.RUNNING
    if normalized == "succeeded":
        return PipelineStatus.SUCCEEDED
    if normalized == "failed":
        return PipelineStatus.FAILED
    if normalized == "cancelled":
        return PipelineStatus.CANCELLED
    raise ProviderResponseError("Learning CI returned an unknown run status")


class LearningCiPipelineProvider(ExternalHttpProvider):
    """Adapter for the project-owned Learning CI HTTP lab.

    The adapter exposes only the lab's three fixed run operations.  A stored
    definition binding is percent-encoded into the path, while every trigger
    requires the orchestration correlation id as its HTTP idempotency key.
    """

    kind = ProviderKind.LEARNING_CI

    def __init__(
        self,
        *,
        base_url: str,
        definition_id: str,
        bearer_token: str,
        policy: OutboundPolicy,
        enabled: bool = False,
        resolver: AddressResolver = default_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if (
            _DEFINITION_ID.fullmatch(definition_id) is None
            or any(
                segment in {"", ".", ".."}
                for segment in definition_id.split("/")
            )
        ):
            raise ProviderConfigurationError(
                "Learning CI definition id is invalid"
            )
        if (
            not bearer_token
            or bearer_token != bearer_token.strip()
            or not bearer_token.isascii()
            or not _MIN_BEARER_TOKEN_LENGTH
            <= len(bearer_token)
            <= _MAX_BEARER_TOKEN_LENGTH
            or any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in bearer_token
            )
        ):
            raise ProviderConfigurationError(
                "Learning CI bearer token must be 32-512 visible ASCII characters"
            )
        client = SafeHttpClient(
            base_url,
            policy,
            resolver=resolver,
            transport=transport,
        )
        super().__init__(client, enabled=enabled)
        self._definition_id = definition_id
        self._definition_path = quote(definition_id, safe="")
        self._headers = {
            "Authorization": f"Bearer {bearer_token}",
            "Accept": "application/json",
        }

    async def trigger(self, request: ProviderTriggerRequest) -> ProviderRun:
        self._require_enabled()
        self._require_definition(request.definition_ref, self._definition_id)
        if (
            request.correlation_id is None
            or _IDEMPOTENCY_KEY.fullmatch(request.correlation_id) is None
        ):
            raise ProviderConfigurationError(
                "Learning CI triggers require a safe correlation id"
            )
        headers = dict(self._headers)
        headers["Idempotency-Key"] = request.correlation_id
        response = await self._client.request(
            "POST",
            f"/api/v1/definitions/{self._definition_path}/runs",
            headers=headers,
            json_body={
                "ref": request.ref,
                "variables": request.variables,
            },
        )
        payload = self._json_object(response, {200, 201, 202})
        return self._normalize(payload)

    async def get(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        run_id = self._validated_run_id(external_id)
        response = await self._client.request(
            "GET",
            f"/api/v1/runs/{quote(run_id, safe='')}",
            headers=self._headers,
        )
        return self._normalize(self._json_object(response))

    async def cancel(self, external_id: str) -> ProviderRun:
        self._require_enabled()
        run_id = self._validated_run_id(external_id)
        response = await self._client.request(
            "POST",
            f"/api/v1/runs/{quote(run_id, safe='')}/cancel",
            headers=self._headers,
        )
        return self._normalize(self._json_object(response, {200, 202}))

    def _normalize(self, payload: Mapping[str, Any]) -> ProviderRun:
        external_id = self._validated_run_id(
            self._string_field(payload, "id")
        )
        definition = self._string_field(payload, "definition")
        self._require_definition(definition, self._definition_id)
        raw_status = self._string_field(payload, "status")
        if len(raw_status) > 100:
            raise ProviderResponseError(
                "Learning CI returned an invalid run status"
            )

        message_value = payload.get("message")
        if message_value is not None and (
            not isinstance(message_value, str) or len(message_value) > 500
        ):
            raise ProviderResponseError(
                "Learning CI returned an invalid run message"
            )
        metadata_value = payload.get("metadata", {})
        if not isinstance(metadata_value, dict):
            raise ProviderResponseError(
                "Learning CI returned invalid run metadata"
            )
        metadata = dict(metadata_value)
        revision = payload.get("definition_revision")
        if isinstance(revision, int) and not isinstance(revision, bool):
            metadata["definition_revision"] = revision
        replayed = payload.get("replayed")
        if isinstance(replayed, bool):
            metadata["replayed"] = replayed

        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=_learning_ci_status(raw_status),
            raw_status=raw_status,
            web_url=self._safe_web_url(payload.get("web_url")),
            message=message_value,
            metadata=metadata,
        )

    @staticmethod
    def _validated_run_id(value: str) -> str:
        if _RUN_ID.fullmatch(value) is None:
            raise ProviderResponseError("Learning CI run id is invalid")
        return value


__all__ = ["LearningCiPipelineProvider"]
