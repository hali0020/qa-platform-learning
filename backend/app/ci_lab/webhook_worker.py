"""Single-worker, fixed-target webhook delivery for the local CI Lab.

This module deliberately has no arbitrary URL setting.  The only destinations
are the QA API on host loopback or its fixed address on the isolated Compose
network.  The exact event body is persisted before this worker sees it.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from pathlib import Path
from typing import Final
from uuid import UUID

import httpx

from app.ci_lab.database import CiLabDatabase, require_local_filesystem_path
from app.ci_lab.registry import DEFAULT_DEFINITION_REGISTRY
from app.ci_lab.service import (
    CiLabConflict,
    CiLabService,
    ClaimedWebhookDelivery,
)


_MAX_SECRET_FILE_BYTES: Final = 4096
_MAX_RESPONSE_BYTES: Final = 8 * 1024
_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}\Z")
_SUCCESS_RESULTS: Final = frozenset(
    {"applied", "duplicate", "stale", "reconcile_required"}
)
logger = logging.getLogger("qa.ci_lab.webhook_worker")


class WebhookTargetMode(str, Enum):
    HOST_LOOPBACK = "host_loopback"
    COMPOSE_INTERNAL = "compose_internal"


_FIXED_BASE_URLS: Final = {
    WebhookTargetMode.HOST_LOOPBACK: "http://127.0.0.1:23100",
    WebhookTargetMode.COMPOSE_INTERNAL: "http://172.30.60.3:23100",
}


@dataclass(frozen=True, slots=True)
class WebhookWorkerConfig:
    database_path: Path
    secret_file: Path
    target_mode: WebhookTargetMode
    worker_id: str
    poll_seconds: float = 0.5
    lease_seconds: int = 30
    refresh_limit: int = 100


@dataclass(frozen=True, slots=True)
class DeliveryOutcome:
    delivered: bool
    retryable: bool
    error_code: str | None


def _required_absolute_local_path(environ: Mapping[str, str], name: str) -> Path:
    raw = environ.get(name, "")
    if not raw:
        raise RuntimeError(f"{name} is required")
    try:
        selected = require_local_filesystem_path(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a local filesystem path") from error
    if not selected.is_absolute():
        raise RuntimeError(f"{name} must be an absolute local path")
    return selected


def load_webhook_secret(path: Path) -> bytes:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("CI Lab webhook secret must be a regular file")
    size = path.stat().st_size
    if not 1 <= size <= _MAX_SECRET_FILE_BYTES:
        raise RuntimeError("CI Lab webhook secret file has an invalid size")
    try:
        value = path.read_text(encoding="utf-8").rstrip("\r\n")
    except UnicodeError as error:
        raise RuntimeError("CI Lab webhook secret file must be UTF-8") from error
    encoded = value.encode("utf-8")
    if not 32 <= len(encoded) <= 512:
        raise RuntimeError("CI Lab webhook secret must contain 32-512 UTF-8 bytes")
    return encoded


def load_worker_config(
    environ: Mapping[str, str] | None = None,
) -> WebhookWorkerConfig:
    values = os.environ if environ is None else environ
    if values.get("CI_LAB_WEBHOOK_TARGET_URL"):
        raise RuntimeError("arbitrary CI Lab webhook target URLs are forbidden")
    try:
        target_mode = WebhookTargetMode(values.get("CI_LAB_WEBHOOK_TARGET_MODE", ""))
    except ValueError as error:
        raise RuntimeError("CI_LAB_WEBHOOK_TARGET_MODE is invalid") from error
    worker_id = values.get("CI_LAB_WEBHOOK_WORKER_ID", "ci-lab-webhook-worker").strip()
    if _WORKER_ID.fullmatch(worker_id) is None:
        raise RuntimeError("CI_LAB_WEBHOOK_WORKER_ID is invalid")
    try:
        poll_seconds = float(values.get("CI_LAB_WEBHOOK_POLL_SECONDS", "0.5"))
        lease_seconds = int(values.get("CI_LAB_WEBHOOK_LEASE_SECONDS", "30"))
        refresh_limit = int(values.get("CI_LAB_WEBHOOK_REFRESH_LIMIT", "100"))
    except ValueError as error:
        raise RuntimeError("CI Lab webhook worker numeric setting is invalid") from error
    if not 0.1 <= poll_seconds <= 60:
        raise RuntimeError("CI Lab webhook poll interval is invalid")
    if not 5 <= lease_seconds <= 300:
        raise RuntimeError("CI Lab webhook lease duration is invalid")
    if not 1 <= refresh_limit <= 500:
        raise RuntimeError("CI Lab webhook refresh limit is invalid")
    return WebhookWorkerConfig(
        database_path=_required_absolute_local_path(values, "CI_LAB_DATABASE_PATH"),
        secret_file=_required_absolute_local_path(
            values,
            "CI_LAB_WEBHOOK_SECRET_FILE",
        ),
        target_mode=target_mode,
        worker_id=worker_id,
        poll_seconds=poll_seconds,
        lease_seconds=lease_seconds,
        refresh_limit=refresh_limit,
    )


def sign_delivery(
    secret: bytes,
    *,
    timestamp: int,
    event_id: str,
    raw_body: bytes,
) -> str:
    if not 32 <= len(secret) <= 512:
        raise ValueError("webhook secret length is invalid")
    if _EVENT_ID.fullmatch(event_id) is None:
        raise ValueError("webhook event id is invalid")
    if not 1_000_000_000 <= timestamp <= 9_999_999_999:
        raise ValueError("webhook timestamp is invalid")
    body_digest = hashlib.sha256(raw_body).hexdigest()
    canonical = f"v1\n{timestamp}\n{event_id}\n{body_digest}".encode("ascii")
    return f"v1={hmac.digest(secret, canonical, 'sha256').hex()}"


class CiLabWebhookWorker:
    def __init__(
        self,
        service: CiLabService,
        *,
        secret: bytes,
        target_mode: WebhookTargetMode,
        worker_id: str,
        lease_seconds: int = 30,
        refresh_limit: int = 100,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if not 32 <= len(secret) <= 512:
            raise ValueError("webhook secret length is invalid")
        if _WORKER_ID.fullmatch(worker_id) is None:
            raise ValueError("webhook worker id is invalid")
        if not 5 <= lease_seconds <= 300:
            raise ValueError("webhook lease duration is invalid")
        if not 1 <= refresh_limit <= 500:
            raise ValueError("webhook refresh limit is invalid")
        self._service = service
        self._secret = bytes(secret)
        self._target_mode = target_mode
        self._worker_id = worker_id
        self._lease_seconds = lease_seconds
        self._refresh_limit = refresh_limit
        self._client = httpx.AsyncClient(
            base_url=_FIXED_BASE_URLS[target_mode],
            transport=transport,
            timeout=httpx.Timeout(5.0, connect=2.0, read=3.0, write=3.0),
            follow_redirects=False,
            trust_env=False,
            headers={
                "Accept": "application/json",
                "Accept-Encoding": "identity",
                "User-Agent": "qa-platform-learning-ci-lab-webhook/1",
            },
        )

    async def run_once(self) -> bool:
        await self._service.advance_webhook_runs_once(limit=self._refresh_limit)
        claimed = await self._service.claim_webhook_delivery(
            self._worker_id,
            lease_seconds=self._lease_seconds,
        )
        if claimed is None:
            return False
        outcome = await self._deliver(claimed)
        try:
            if outcome.delivered:
                await self._service.complete_webhook_delivery(claimed)
                logger.info(
                    "ci_lab_webhook_delivery_delivered",
                    extra={"delivery_id": claimed.id, "attempt": claimed.attempts},
                )
            else:
                await self._service.fail_webhook_delivery(
                    claimed,
                    error_code=outcome.error_code or "delivery_failed",
                    retryable=outcome.retryable,
                )
                logger.warning(
                    "ci_lab_webhook_delivery_failed",
                    extra={
                        "delivery_id": claimed.id,
                        "attempt": claimed.attempts,
                        "error_code": outcome.error_code or "delivery_failed",
                    },
                )
        except CiLabConflict:
            # A lease expiry allows another worker generation to take over.
            # Never log the token, request, response, target or exception text.
            logger.warning(
                "ci_lab_webhook_delivery_lease_lost",
                extra={"delivery_id": claimed.id, "attempt": claimed.attempts},
            )
        return True

    async def _deliver(self, claimed: ClaimedWebhookDelivery) -> DeliveryOutcome:
        try:
            connection_id = str(UUID(claimed.connection_id))
        except ValueError:
            return DeliveryOutcome(False, False, "connection_id_invalid")
        timestamp = int(datetime.now(timezone.utc).timestamp())
        signature = sign_delivery(
            self._secret,
            timestamp=timestamp,
            event_id=claimed.event_id,
            raw_body=claimed.raw_body,
        )
        headers = {
            "Content-Type": "application/json",
            "X-QA-Webhook-Event-ID": claimed.event_id,
            "X-QA-Webhook-Timestamp": str(timestamp),
            "X-QA-Webhook-Signature": signature,
        }
        try:
            async with self._client.stream(
                "POST",
                f"/api/v1/webhooks/learning-ci/{connection_id}",
                headers=headers,
                content=claimed.raw_body,
            ) as response:
                body = bytearray()
                async for chunk in response.aiter_bytes():
                    if len(chunk) > _MAX_RESPONSE_BYTES - len(body):
                        return DeliveryOutcome(False, False, "response_too_large")
                    body.extend(chunk)
                status_code = response.status_code
        except (httpx.TimeoutException, httpx.NetworkError):
            return DeliveryOutcome(False, True, "network_error")
        except httpx.HTTPError:
            return DeliveryOutcome(False, True, "http_transport_error")

        if status_code in {408, 425, 429} or status_code >= 500:
            return DeliveryOutcome(False, True, f"http_{status_code}")
        if not 200 <= status_code < 300:
            return DeliveryOutcome(False, False, f"http_{status_code}")
        try:
            document = json.loads(bytes(body))
            data = document["data"]
            result = data["result"]
            response_event_id = data["event_id"]
        except (KeyError, TypeError, ValueError, UnicodeError):
            return DeliveryOutcome(False, False, "response_invalid")
        if not isinstance(result, str) or not isinstance(response_event_id, str):
            return DeliveryOutcome(False, False, "response_invalid")
        if response_event_id != claimed.event_id:
            return DeliveryOutcome(False, False, "response_event_mismatch")
        if result in _SUCCESS_RESULTS:
            return DeliveryOutcome(True, False, None)
        return DeliveryOutcome(False, False, "response_result_invalid")

    async def run_forever(
        self,
        stop_event: asyncio.Event,
        *,
        poll_seconds: float,
    ) -> None:
        if not 0.1 <= poll_seconds <= 60:
            raise ValueError("webhook poll interval is invalid")
        logger.info(
            "ci_lab_webhook_worker_started",
            extra={"target_mode": self._target_mode.value},
        )
        try:
            while not stop_event.is_set():
                try:
                    handled = await self.run_once()
                except Exception as error:
                    # A transient local database/HTTP adapter failure must not
                    # silently stop source mode. Never include exception text,
                    # paths, headers, payloads or credentials in this log.
                    logger.error(
                        "ci_lab_webhook_worker_cycle_failed",
                        extra={"error_type": type(error).__name__},
                    )
                    handled = False
                if handled:
                    continue
                try:
                    await asyncio.wait_for(stop_event.wait(), timeout=poll_seconds)
                except TimeoutError:
                    pass
        finally:
            logger.info("ci_lab_webhook_worker_stopped")

    async def close(self) -> None:
        await self._client.aclose()
        # Best-effort overwrite of our private copy before releasing it.
        self._secret = b""


def build_worker(
    config: WebhookWorkerConfig,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> tuple[CiLabWebhookWorker, CiLabService]:
    secret = load_webhook_secret(config.secret_file)
    database = CiLabDatabase(config.database_path)
    service = CiLabService(database, DEFAULT_DEFINITION_REGISTRY)
    worker = CiLabWebhookWorker(
        service,
        secret=secret,
        target_mode=config.target_mode,
        worker_id=config.worker_id,
        lease_seconds=config.lease_seconds,
        refresh_limit=config.refresh_limit,
        transport=transport,
    )
    return worker, service


__all__ = [
    "CiLabWebhookWorker",
    "DeliveryOutcome",
    "WebhookTargetMode",
    "WebhookWorkerConfig",
    "build_worker",
    "load_webhook_secret",
    "load_worker_config",
    "sign_delivery",
]
