from __future__ import annotations

from collections.abc import Mapping
from typing import Any
from urllib.parse import urlparse

from app.pipeline.providers.errors import (
    ProviderConfigurationError,
    ProviderDisabledError,
    ProviderResponseError,
)
from app.pipeline.providers.http import SafeHttpClient, SafeHttpResponse
from app.pipeline.providers.models import ProviderKind


class ExternalHttpProvider:
    """Shared lifecycle and explicit enable gate for real providers."""

    kind: ProviderKind

    def __init__(self, client: SafeHttpClient, *, enabled: bool = False) -> None:
        self._client = client
        self._enabled = enabled

    def _require_enabled(self) -> None:
        if not self._enabled:
            raise ProviderDisabledError(
                f"{self.kind.value} provider is disabled; enable it at the application boundary"
            )

    @staticmethod
    def _expect_status(
        response: SafeHttpResponse,
        accepted: set[int],
    ) -> None:
        if response.status_code not in accepted:
            raise ProviderResponseError(
                f"provider returned unexpected HTTP status {response.status_code}"
            )

    @classmethod
    def _json_object(
        cls,
        response: SafeHttpResponse,
        accepted: set[int] = {200},
    ) -> dict[str, Any]:
        cls._expect_status(response, accepted)
        payload = response.json()
        if not isinstance(payload, dict):
            raise ProviderResponseError("provider returned an unexpected JSON shape")
        return payload

    def _safe_web_url(self, value: object) -> str | None:
        if not isinstance(value, str) or len(value) > 2048:
            return None
        if any(ord(character) < 32 for character in value):
            return None
        try:
            parsed = urlparse(value)
            base = urlparse(self._client.base_url)
            parsed_host = parsed.hostname
            base_host = base.hostname
            parsed_port = parsed.port or (443 if parsed.scheme == "https" else 80)
            base_port = base.port or (443 if base.scheme == "https" else 80)
        except ValueError:
            return None
        if (
            parsed.scheme not in {"http", "https"}
            or parsed.username is not None
            or parsed.password is not None
            or parsed_host is None
            or base_host is None
            or parsed.query
            or parsed.fragment
        ):
            return None
        if (
            parsed.scheme != base.scheme
            or parsed_host.rstrip(".").lower() != base_host.rstrip(".").lower()
            or parsed_port != base_port
        ):
            return None
        return value

    @staticmethod
    def _string_field(
        payload: Mapping[str, Any],
        key: str,
        *,
        required: bool = True,
    ) -> str:
        value = payload.get(key)
        if value is None and not required:
            return ""
        if not isinstance(value, (str, int)):
            raise ProviderResponseError(f"provider response is missing {key}")
        return str(value)

    @staticmethod
    def _require_definition(actual: str, configured: str) -> None:
        if actual != configured:
            raise ProviderConfigurationError(
                "trigger definition does not match the configured provider resource"
            )

    async def aclose(self) -> None:
        await self._client.aclose()


__all__ = ["ExternalHttpProvider"]
