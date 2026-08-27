from __future__ import annotations

import json
from collections.abc import AsyncIterable, Iterable
from dataclasses import dataclass
from typing import Any, Mapping
from urllib.parse import urlparse

import httpx

from app.pipeline.providers.errors import ProviderResponseError, ProviderTransportError
from app.pipeline.providers.security import (
    AddressResolver,
    OutboundPolicy,
    default_resolver,
    validate_base_url,
    validate_relative_path,
    validate_resolved_addresses,
)


@dataclass(frozen=True, slots=True)
class SafeHttpResponse:
    status_code: int
    headers: Mapping[str, str]
    body: bytes

    def json(self) -> Any:
        try:
            return json.loads(self.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            # A JSONDecodeError retains the complete source document.  Do not
            # keep it in an exception chain that a debug logger could render.
            raise ProviderResponseError("provider returned invalid JSON") from None


class SafeHttpClient:
    """Small async HTTP boundary with default-deny egress behavior.

    Redirects and environment proxies are disabled. DNS is checked before
    every request. Application-level checks cannot completely eliminate the
    DNS-check/connect race, so production must additionally enforce an egress
    firewall or trusted outbound proxy.
    """

    def __init__(
        self,
        base_url: str,
        policy: OutboundPolicy,
        *,
        resolver: AddressResolver = default_resolver,
        transport: httpx.AsyncBaseTransport | None = None,
        connect_timeout: float = 3.0,
        read_timeout: float = 10.0,
        write_timeout: float = 10.0,
        pool_timeout: float = 2.0,
    ) -> None:
        self.base_url = validate_base_url(base_url, policy)
        self.policy = policy
        self._resolver = resolver
        parsed = urlparse(self.base_url)
        self._host = parsed.hostname or ""
        self._port = parsed.port or (443 if parsed.scheme == "https" else 80)
        self._client = httpx.AsyncClient(
            base_url=f"{self.base_url}/",
            timeout=httpx.Timeout(
                connect=connect_timeout,
                read=read_timeout,
                write=write_timeout,
                pool=pool_timeout,
            ),
            limits=httpx.Limits(max_connections=10, max_keepalive_connections=5),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )

    async def request(
        self,
        method: str,
        path: str,
        *,
        headers: Mapping[str, str] | None = None,
        params: Mapping[str, object] | None = None,
        json_body: object | None = None,
        form_data: Mapping[str, str] | None = None,
    ) -> SafeHttpResponse:
        validate_relative_path(path)
        await validate_resolved_addresses(
            self._host,
            self._port,
            self.policy,
            self._resolver,
        )
        try:
            request_headers = httpx.Headers(headers)
            # Always request wire-identical responses.  A caller cannot opt
            # back into transparent compression because decompression happens
            # before an application-level byte counter can enforce its budget.
            request_headers["Accept-Encoding"] = "identity"
            async with self._client.stream(
                method,
                path.lstrip("/"),
                headers=request_headers,
                params=params,
                json=json_body,
                data=form_data,
            ) as response:
                content_encoding = response.headers.get("Content-Encoding", "")
                if content_encoding.strip().lower() not in {"", "identity"}:
                    raise ProviderResponseError(
                        "provider response content encoding is not allowed"
                    )
                content_length = response.headers.get("Content-Length")
                if content_length is not None:
                    try:
                        declared_size = int(content_length)
                    except ValueError:
                        raise ProviderResponseError(
                            "provider response Content-Length is invalid"
                        ) from None
                    if (
                        declared_size < 0
                        or declared_size > self.policy.max_response_bytes
                    ):
                        raise ProviderResponseError(
                            "provider response exceeded the size limit"
                        )

                chunks: list[bytes] = []
                size = 0
                # MockTransport may hand back an already-consumed response.
                # Real transports remain streaming and must be read as raw
                # wire bytes rather than through HTTPX's decoding iterator.
                wire_chunks: AsyncIterable[bytes] | Iterable[bytes]
                if response.is_stream_consumed:
                    wire_chunks = (response.content,)
                else:
                    wire_chunks = response.aiter_raw()
                async for chunk in _iterate_bytes(wire_chunks):
                    size += len(chunk)
                    if size > self.policy.max_response_bytes:
                        raise ProviderResponseError(
                            "provider response exceeded the size limit"
                        )
                    chunks.append(chunk)
                return SafeHttpResponse(
                    status_code=response.status_code,
                    headers=dict(response.headers),
                    body=b"".join(chunks),
                )
        except ProviderResponseError:
            raise
        except (httpx.HTTPError, UnicodeError, ValueError):
            # Deliberately discard any exception that may retain a URL, header
            # value, request object, or remote response body.
            raise ProviderTransportError("provider request failed") from None

    async def aclose(self) -> None:
        await self._client.aclose()


async def _iterate_bytes(
    chunks: AsyncIterable[bytes] | Iterable[bytes],
) -> AsyncIterable[bytes]:
    if isinstance(chunks, AsyncIterable):
        async for chunk in chunks:
            yield chunk
        return
    for chunk in chunks:
        yield chunk


__all__ = ["SafeHttpClient", "SafeHttpResponse"]
