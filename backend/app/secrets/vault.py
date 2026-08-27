"""Read-only async client for the project-owned local Vault teaching service.

There is deliberately no arbitrary endpoint, mount, path, list, write, or
delete mode.  Public/company Vault installations cannot be selected through
this adapter.
"""

from __future__ import annotations

import asyncio
import json
import os
import stat
from collections.abc import Awaitable, Iterable
from dataclasses import dataclass
from typing import Any

import httpx

from app.secrets.base import (
    BUILTIN_SECRET_NAMES,
    provider_secret_allowlist,
    require_secret_name,
)
from app.secrets.errors import (
    SecretNotFoundError,
    SecretStoreConfigurationError,
    SecretStoreResponseError,
    SecretStoreUnavailableError,
)


SECRET_STORE_RUNTIME_MODES = frozenset(
    {"env_local", "vault_local_container"}
)
VAULT_LOCAL_ENDPOINT = "http://vault:8200"
VAULT_KV_V2_MOUNT = "qa-platform"
VAULT_RUNTIME_PATH = "runtime"
VAULT_PROVIDERS_PATH = "providers"
VAULT_APP_TOKEN_FILE = "/run/secrets/vault_app_token"
_MAX_RESPONSE_BYTES = 16 * 1024


def _valid_token(value: str) -> bool:
    return (
        bool(value)
        and value == value.strip()
        and len(value) <= 4096
        and not any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in value
        )
    )


def read_vault_app_token_file() -> str:
    """Read the bootstrap token from one fixed container secret file.

    This helper never consults a user-controlled path and rejects symlinks,
    non-regular files, oversized values, and control characters.  Root/unseal
    material must never be mounted at this location.
    """

    flags = os.O_RDONLY
    if hasattr(os, "O_NOFOLLOW"):
        flags |= os.O_NOFOLLOW
    descriptor: int | None = None
    failed = False
    token = ""
    try:
        descriptor = os.open(VAULT_APP_TOKEN_FILE, flags)
        metadata = os.fstat(descriptor)
        if not stat.S_ISREG(metadata.st_mode) or not 0 < metadata.st_size <= 4096:
            failed = True
        else:
            chunks: list[bytes] = []
            remaining = 4097
            while remaining > 0:
                chunk = os.read(descriptor, remaining)
                if not chunk:
                    break
                chunks.append(chunk)
                remaining -= len(chunk)
            raw = b"".join(chunks)
            if len(raw) > 4096:
                failed = True
            else:
                try:
                    token = raw.decode("utf-8").rstrip("\r\n")
                except UnicodeDecodeError:
                    failed = True
    except OSError:
        failed = True
    finally:
        if descriptor is not None:
            try:
                os.close(descriptor)
            except OSError:
                pass
    if failed or not _valid_token(token):
        raise SecretStoreConfigurationError(
            "Vault application token file is unavailable or invalid"
        )
    return token


def validate_secret_store_runtime_target(
    *,
    runtime_mode: str,
    app_env: str,
    endpoint_url: str,
    kv_mount: str,
    token: str,
    max_concurrency: int = 4,
    operation_timeout_seconds: float = 3.0,
    max_attempts: int = 3,
) -> None:
    """Fail closed unless the exact project-owned Vault topology is selected."""

    if runtime_mode not in SECRET_STORE_RUNTIME_MODES:
        raise SecretStoreConfigurationError("secret store mode is invalid")
    if not 1 <= max_concurrency <= 16:
        raise SecretStoreConfigurationError("secret store concurrency is invalid")
    if not 0.1 <= operation_timeout_seconds <= 30:
        raise SecretStoreConfigurationError("secret store timeout is invalid")
    if not 1 <= max_attempts <= 3:
        raise SecretStoreConfigurationError("secret store retry count is invalid")
    if runtime_mode == "env_local":
        if endpoint_url or kv_mount or token:
            raise SecretStoreConfigurationError(
                "env_local forbids dormant Vault configuration"
            )
        return
    if app_env != "local-container":
        raise SecretStoreConfigurationError(
            "Vault is restricted to the local-container lab"
        )
    if endpoint_url != VAULT_LOCAL_ENDPOINT:
        raise SecretStoreConfigurationError(
            "Vault endpoint must be the project-owned internal service"
        )
    if kv_mount != VAULT_KV_V2_MOUNT:
        raise SecretStoreConfigurationError("Vault KV mount is not allowlisted")
    if not _valid_token(token):
        raise SecretStoreConfigurationError("Vault application token is invalid")


@dataclass(frozen=True, slots=True)
class _VaultResponse:
    status_code: int
    body: bytes


class VaultSecretStore:
    """Minimal KV v2 reader with bounded retries and redacted failures."""

    runtime_mode = "vault_local_container"

    def __init__(
        self,
        *,
        app_env: str,
        endpoint_url: str,
        kv_mount: str,
        token: str,
        max_concurrency: int = 4,
        operation_timeout_seconds: float = 3.0,
        max_attempts: int = 3,
        allowed_names: Iterable[str] = (),
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        validate_secret_store_runtime_target(
            runtime_mode=self.runtime_mode,
            app_env=app_env,
            endpoint_url=endpoint_url,
            kv_mount=kv_mount,
            token=token,
            max_concurrency=max_concurrency,
            operation_timeout_seconds=operation_timeout_seconds,
            max_attempts=max_attempts,
        )
        self._token = token
        self._mount = kv_mount
        provider_names = provider_secret_allowlist(allowed_names)
        self._provider_names = provider_names
        self._allowed_names = BUILTIN_SECRET_NAMES | provider_names
        self._timeout = operation_timeout_seconds
        self._max_attempts = max_attempts
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client = httpx.AsyncClient(
            base_url=f"{endpoint_url}/",
            timeout=httpx.Timeout(operation_timeout_seconds),
            limits=httpx.Limits(
                max_connections=max_concurrency,
                max_keepalive_connections=max_concurrency,
            ),
            follow_redirects=False,
            trust_env=False,
            transport=transport,
        )
        self._closed = False
        self._close_lock = asyncio.Lock()
        self._close_task: asyncio.Task[None] | None = None

    async def read(self, name: str) -> str:
        allowed_name = require_secret_name(name, self._allowed_names)
        if self._closed:
            raise SecretStoreUnavailableError("secret store is closed")
        async with self._semaphore:
            if self._closed:
                raise SecretStoreUnavailableError("secret store is closed")
            path = (
                VAULT_PROVIDERS_PATH
                if allowed_name in self._provider_names
                else VAULT_RUNTIME_PATH
            )
            response = await self._read_with_retries(path)
        return self._extract_value(response, allowed_name)

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._close_task is None:
                self._closed = True
                self._close_task = asyncio.create_task(self._close_client())
            task = self._close_task
        await self._await_cleanup_task(task)

    def __repr__(self) -> str:
        state = "closed" if self._closed else "open"
        return (
            "VaultSecretStore(runtime_mode='vault_local_container', "
            f"state={state!r})"
        )

    async def _read_with_retries(self, path: str) -> _VaultResponse:
        last_response: _VaultResponse | None = None
        for attempt in range(self._max_attempts):
            response = await self._bounded_request(path)
            if response is not None:
                last_response = response
                if response.status_code not in {429, 500, 502, 503, 504}:
                    break
            if attempt + 1 < self._max_attempts:
                await asyncio.sleep(min(0.05 * (2**attempt), 0.2))

        if last_response is None:
            raise SecretStoreUnavailableError("Vault request failed")
        if last_response.status_code == 404:
            raise SecretNotFoundError("required secret is missing")
        if last_response.status_code != 200:
            if last_response.status_code in {401, 403, 429, 500, 502, 503, 504}:
                raise SecretStoreUnavailableError("Vault request failed")
            raise SecretStoreResponseError("Vault returned an invalid response")
        return last_response

    async def _bounded_request(self, path: str) -> _VaultResponse | None:
        request = asyncio.create_task(self._request_once(path))
        try:
            return await asyncio.wait_for(
                request,
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            request.cancel()
            await self._settle_cancelled_task(request)
            raise
        except asyncio.TimeoutError:
            request.cancel()
            await self._settle_cancelled_task(request)
            return None

    async def _request_once(self, path: str) -> _VaultResponse | None:
        response: httpx.Response | None = None
        result: _VaultResponse | None = None
        failed = False
        try:
            request = self._client.build_request(
                "GET",
                f"v1/{self._mount}/data/{path}",
                headers={
                    "Accept": "application/json",
                    "Accept-Encoding": "identity",
                    "X-Vault-Token": self._token,
                },
            )
            response = await self._client.send(request, stream=True)
            content_encoding = response.headers.get("Content-Encoding", "")
            if content_encoding.strip().lower() not in {"", "identity"}:
                return _VaultResponse(response.status_code, b"")
            declared = response.headers.get("Content-Length")
            if declared is not None:
                try:
                    declared_size = int(declared)
                except ValueError:
                    declared_size = -1
                if declared_size < 0 or declared_size > _MAX_RESPONSE_BYTES:
                    return _VaultResponse(response.status_code, b"")

            chunks: list[bytes] = []
            size = 0
            # Read wire bytes, not HTTPX's decoded stream. Otherwise a tiny
            # compressed body could allocate far beyond the response budget
            # before this code gets a chance to apply its size limit.
            if response.is_stream_consumed:
                # MockTransport materializes ordinary byte responses before
                # handing them to the client. Compressed responses were
                # already rejected above, so the cached fake body is safe.
                wire_chunks = (response.content,)
            else:
                wire_chunks = response.aiter_raw()
            async for chunk in _as_async_bytes(wire_chunks):
                size += len(chunk)
                if size > _MAX_RESPONSE_BYTES:
                    return _VaultResponse(response.status_code, b"")
                chunks.append(chunk)
            result = _VaultResponse(response.status_code, b"".join(chunks))
        except asyncio.CancelledError:
            raise
        except Exception:
            # HTTPX errors can include request headers and URLs.  Discard the
            # entire exception chain and return only a retry signal.
            failed = True
        finally:
            if response is not None:
                await self._bounded_cleanup(response.aclose())
        return None if failed else result

    def _extract_value(self, response: _VaultResponse, name: str) -> str:
        invalid = False
        payload: Any = None
        try:
            payload = json.loads(response.body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            invalid = True
        if not invalid and isinstance(payload, dict):
            outer_data = payload.get("data")
            if isinstance(outer_data, dict):
                inner_data = outer_data.get("data")
                if isinstance(inner_data, dict):
                    value = inner_data.get(name)
                    if isinstance(value, str):
                        if (
                            value
                            and value.strip()
                            and len(value) <= 8192
                            and "\x00" not in value
                            and "\r" not in value
                            and "\n" not in value
                        ):
                            return value
        raise SecretStoreResponseError("Vault returned an invalid secret")

    async def _close_client(self) -> None:
        acquired = 0
        try:
            for _ in range(self._max_concurrency):
                await self._semaphore.acquire()
                acquired += 1
            await self._bounded_cleanup(self._client.aclose())
        finally:
            for _ in range(acquired):
                self._semaphore.release()

    async def _bounded_cleanup(self, awaitable: Awaitable[Any]) -> None:
        task = asyncio.ensure_future(awaitable)
        try:
            await asyncio.wait_for(asyncio.shield(task), timeout=self._timeout)
        except asyncio.CancelledError:
            await self._finish_cleanup_after_cancel(task)
            raise
        except asyncio.TimeoutError:
            task.cancel()
            await self._settle_cancelled_task(task)
        except Exception:
            # Cleanup failures are intentionally safe and best effort.
            pass

    async def _await_cleanup_task(self, task: asyncio.Task[None]) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._close_budget,
            )
        except asyncio.CancelledError:
            await self._finish_cleanup_after_cancel(task)
            raise
        except asyncio.TimeoutError:
            task.cancel()
            await self._settle_cancelled_task(task)

    async def _finish_cleanup_after_cancel(
        self,
        task: asyncio.Future[Any],
    ) -> None:
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._close_budget,
            )
        except BaseException:
            task.cancel()
            await self._settle_cancelled_task(task)

    async def _settle_cancelled_task(
        self,
        task: asyncio.Future[Any],
    ) -> None:
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._timeout,
            )
        except BaseException:
            task.cancel()

    @property
    def _close_budget(self) -> float:
        retry_delay = sum(
            min(0.05 * (2**attempt), 0.2)
            for attempt in range(max(0, self._max_attempts - 1))
        )
        return (self._timeout * (self._max_attempts + 2)) + retry_delay


async def _as_async_bytes(
    chunks: Iterable[bytes] | Any,
):
    if hasattr(chunks, "__aiter__"):
        async for chunk in chunks:
            yield chunk
        return
    for chunk in chunks:
        yield chunk


__all__ = [
    "SECRET_STORE_RUNTIME_MODES",
    "VAULT_APP_TOKEN_FILE",
    "VAULT_KV_V2_MOUNT",
    "VAULT_LOCAL_ENDPOINT",
    "VAULT_PROVIDERS_PATH",
    "VAULT_RUNTIME_PATH",
    "VaultSecretStore",
    "read_vault_app_token_file",
    "validate_secret_store_runtime_target",
]
