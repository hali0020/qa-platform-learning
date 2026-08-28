from __future__ import annotations

import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import httpx
import pytest

from app.ci_lab.database import CiLabDatabase
from app.ci_lab.models import TriggerRunRequest, WebhookDeliveryStatus
from app.ci_lab.registry import DEFAULT_DEFINITION_REGISTRY
from app.ci_lab.service import CiLabService
from app.ci_lab.webhook_worker import (
    CiLabWebhookWorker,
    WebhookTargetMode,
    load_webhook_secret,
    load_worker_config,
    sign_delivery,
)
from app.runtime.webhook_security import sign_webhook


BASE = datetime(2026, 8, 28, 9, 0, tzinfo=timezone.utc)
WEBHOOK_SECRET = b"worker-test-webhook-secret-32-bytes-minimum"


@dataclass
class ManualClock:
    value: datetime = BASE

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, seconds: int) -> None:
        self.value += timedelta(seconds=seconds)


async def subscribed_service(
    path: Path,
    clock: ManualClock,
    *,
    correlation_id: str,
) -> tuple[CiLabService, str, str]:
    service = CiLabService(
        CiLabDatabase(path),
        DEFAULT_DEFINITION_REGISTRY,
        clock=clock,
    )
    await service.initialize()
    connection_id = str(uuid4())
    run = await service.trigger(
        "local-quality-gate",
        TriggerRunRequest(
            webhook_connection_id=connection_id,
            correlation_id=correlation_id,
        ),
        correlation_id,
    )
    return service, run.id, connection_id


def test_worker_configuration_is_fixed_target_and_signature_compatible(
    tmp_path: Path,
) -> None:
    secret_file = tmp_path / "webhook.secret"
    secret_file.write_text(WEBHOOK_SECRET.decode("ascii") + "\n", encoding="utf-8")
    database_path = tmp_path / "ci-lab.db"
    environ = {
        "CI_LAB_DATABASE_PATH": str(database_path.resolve()),
        "CI_LAB_WEBHOOK_SECRET_FILE": str(secret_file.resolve()),
        "CI_LAB_WEBHOOK_TARGET_MODE": "host_loopback",
        "CI_LAB_WEBHOOK_WORKER_ID": "worker-config-test",
    }

    config = load_worker_config(environ)
    assert config.database_path == database_path.resolve()
    assert config.secret_file == secret_file.resolve()
    assert config.target_mode == WebhookTargetMode.HOST_LOOPBACK
    assert load_webhook_secret(config.secret_file) == WEBHOOK_SECRET

    raw_body = b'{"sequence":1,"status":"queued"}'
    assert sign_delivery(
        WEBHOOK_SECRET,
        timestamp=1_800_000_000,
        event_id="ci-lab-event-001",
        raw_body=raw_body,
    ) == sign_webhook(
        WEBHOOK_SECRET,
        timestamp=1_800_000_000,
        event_id="ci-lab-event-001",
        raw_body=raw_body,
    )

    with pytest.raises(RuntimeError, match="arbitrary"):
        load_worker_config(
            {
                **environ,
                "CI_LAB_WEBHOOK_TARGET_URL": "https://forbidden.invalid/webhook",
            }
        )
    with pytest.raises(RuntimeError, match="TARGET_MODE"):
        load_worker_config(
            {
                key: value
                for key, value in environ.items()
                if key != "CI_LAB_WEBHOOK_TARGET_MODE"
            }
        )


@pytest.mark.asyncio
async def test_worker_sends_exact_persisted_body_to_fixed_loopback_and_settles(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    service, run_id, connection_id = await subscribed_service(
        tmp_path / "worker-success.db",
        clock,
        correlation_id="worker-success-001",
    )
    requests: list[dict[str, object]] = []

    async def receiver(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        event_id = request.headers["x-qa-webhook-event-id"]
        timestamp = int(request.headers["x-qa-webhook-timestamp"])
        assert request.headers["x-qa-webhook-signature"] == sign_webhook(
            WEBHOOK_SECRET,
            timestamp=timestamp,
            event_id=event_id,
            raw_body=body,
        )
        requests.append(
            {
                "url": str(request.url),
                "body": body,
                "event_id": event_id,
                "authorization": request.headers.get("authorization"),
            }
        )
        return httpx.Response(
            200,
            json={
                "data": {
                    "event_id": event_id,
                    "result": "applied",
                    "run_id": "local-run",
                    "reconciliation_required": False,
                }
            },
        )

    worker = CiLabWebhookWorker(
        service,
        secret=WEBHOOK_SECRET,
        target_mode=WebhookTargetMode.HOST_LOOPBACK,
        worker_id="worker-success",
        transport=httpx.MockTransport(receiver),
    )
    try:
        assert await worker.run_once() is True
        assert await worker.run_once() is False

        assert len(requests) == 1
        request = requests[0]
        assert request["url"] == (
            "http://127.0.0.1:23100/api/v1/webhooks/learning-ci/"
            f"{connection_id}"
        )
        assert request["authorization"] is None
        payload = json.loads(request["body"])
        assert payload["connection_id"] == connection_id
        assert payload["correlation_id"] == "worker-success-001"
        assert payload["external_id"] == run_id
        assert payload["sequence"] == 1
        assert payload["status"] == "queued"

        [delivery] = await service.list_webhook_deliveries(run_id=run_id)
        assert delivery.status == WebhookDeliveryStatus.DELIVERED
        assert delivery.attempts == 1
        assert delivery.delivered_at == clock.value
        assert delivery.lease_owner is None
    finally:
        await worker.close()
        await service.close()


@pytest.mark.asyncio
async def test_worker_retries_network_failure_with_same_event_and_body(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    service, run_id, _connection_id = await subscribed_service(
        tmp_path / "worker-retry.db",
        clock,
        correlation_id="worker-retry-001",
    )
    attempts: list[tuple[str, bytes]] = []

    async def flaky_receiver(request: httpx.Request) -> httpx.Response:
        body = await request.aread()
        event_id = request.headers["x-qa-webhook-event-id"]
        attempts.append((event_id, body))
        if len(attempts) == 1:
            raise httpx.ConnectError("test connection failure", request=request)
        return httpx.Response(
            200,
            json={
                "data": {
                    "event_id": event_id,
                    "result": "duplicate",
                    "run_id": "local-run",
                    "reconciliation_required": False,
                }
            },
        )

    worker = CiLabWebhookWorker(
        service,
        secret=WEBHOOK_SECRET,
        target_mode=WebhookTargetMode.HOST_LOOPBACK,
        worker_id="worker-retry",
        transport=httpx.MockTransport(flaky_receiver),
    )
    try:
        assert await worker.run_once() is True
        [waiting] = await service.list_webhook_deliveries(run_id=run_id)
        assert waiting.status == WebhookDeliveryStatus.RETRY_WAIT
        assert waiting.attempts == 1
        assert waiting.last_error_code == "network_error"
        assert waiting.available_at == clock.value + timedelta(seconds=1)
        assert await worker.run_once() is False

        clock.advance(seconds=1)
        assert await worker.run_once() is True
        assert len(attempts) == 2
        assert attempts[0] == attempts[1]

        deliveries = await service.list_webhook_deliveries(run_id=run_id)
        delivered = next(item for item in deliveries if item.sequence == 1)
        assert delivered.status == WebhookDeliveryStatus.DELIVERED
        assert delivered.attempts == 2
        assert delivered.last_error_code is None
    finally:
        await worker.close()
        await service.close()


@pytest.mark.asyncio
async def test_worker_dead_letters_a_permanent_http_rejection(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    service, run_id, _connection_id = await subscribed_service(
        tmp_path / "worker-permanent.db",
        clock,
        correlation_id="worker-permanent-001",
    )

    async def rejecting_receiver(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(400, json={"detail": "rejected"})

    worker = CiLabWebhookWorker(
        service,
        secret=WEBHOOK_SECRET,
        target_mode=WebhookTargetMode.HOST_LOOPBACK,
        worker_id="worker-permanent",
        transport=httpx.MockTransport(rejecting_receiver),
    )
    try:
        assert await worker.run_once() is True
        [delivery] = await service.list_webhook_deliveries(run_id=run_id)
        assert delivery.status == WebhookDeliveryStatus.DEAD_LETTER
        assert delivery.attempts == 1
        assert delivery.last_error_code == "http_400"
        assert delivery.dead_lettered_at == clock.value
        assert await worker.run_once() is False
    finally:
        await worker.close()
        await service.close()
