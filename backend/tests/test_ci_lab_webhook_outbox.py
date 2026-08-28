from __future__ import annotations

import asyncio
import hashlib
import json
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from app.ci_lab import create_ci_lab_app
from app.ci_lab.database import (
    CiLabDatabase,
    approvals,
    metadata,
    quality_gates,
    runs,
)
from app.ci_lab.models import TriggerRunRequest, WebhookDeliveryStatus
from app.ci_lab.registry import DEFAULT_DEFINITION_REGISTRY
from app.ci_lab.service import CiLabConflict, CiLabService


BASE = datetime(2026, 8, 28, 8, 0, tzinfo=timezone.utc)
MACHINE_TOKEN = "test-only-ci-lab-machine-token-0001"


@dataclass
class ManualClock:
    value: datetime = BASE

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, milliseconds: int = 0, seconds: int = 0) -> None:
        self.value += timedelta(milliseconds=milliseconds, seconds=seconds)


async def open_service(
    path: Path,
    clock: ManualClock,
) -> CiLabService:
    database = CiLabDatabase(path)
    service = CiLabService(
        database,
        DEFAULT_DEFINITION_REGISTRY,
        clock=clock,
    )
    await service.initialize()
    return service


async def subscribe_run(
    service: CiLabService,
    *,
    correlation_id: str,
) -> tuple[str, str]:
    connection_id = str(uuid4())
    run = await service.trigger(
        "local-quality-gate",
        TriggerRunRequest(
            ref="main",
            variables={"SUITE": "smoke"},
            webhook_connection_id=connection_id,
            correlation_id=correlation_id,
        ),
        correlation_id,
    )
    return run.id, connection_id


@pytest.mark.asyncio
async def test_database_initialization_is_cross_process_safe_and_additive(
    tmp_path: Path,
) -> None:
    legacy_path = tmp_path / "legacy-ci-lab.db"
    legacy = CiLabDatabase(legacy_path)
    try:
        async with legacy.engine.begin() as connection:
            await connection.run_sync(
                lambda sync_connection: metadata.create_all(
                    sync_connection,
                    tables=[runs, quality_gates, approvals],
                )
            )
    finally:
        await legacy.close()

    first = CiLabDatabase(legacy_path)
    second = CiLabDatabase(legacy_path)
    fresh_path = tmp_path / "fresh-ci-lab.db"
    fresh_first = CiLabDatabase(fresh_path)
    fresh_second = CiLabDatabase(fresh_path)
    try:
        # Both a pre-outbox database and a brand-new database can be opened by
        # the API and worker at the same time without duplicate-DDL races.
        await asyncio.gather(first.initialize(), second.initialize())
        await asyncio.gather(fresh_first.initialize(), fresh_second.initialize())

        async with first.read() as connection:
            table_names = set(
                (
                    await connection.exec_driver_sql(
                        "SELECT name FROM sqlite_master WHERE type = 'table'"
                    )
                ).scalars()
            )
        assert {
            "ci_lab_runs",
            "ci_lab_quality_gates",
            "ci_lab_run_approvals",
            "ci_lab_webhook_subscriptions",
            "ci_lab_webhook_deliveries",
        } <= table_names
    finally:
        await asyncio.gather(
            first.close(),
            second.close(),
            fresh_first.close(),
            fresh_second.close(),
        )


@pytest.mark.asyncio
async def test_unbound_replay_accepts_the_pre_webhook_request_fingerprint(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    service = await open_service(tmp_path / "legacy-fingerprint.db", clock)
    request = TriggerRunRequest(ref="main", variables={"SUITE": "smoke"})
    idempotency_key = "legacy-unbound-run-001"
    run_id = str(uuid4())
    legacy_document = json.dumps(
        {
            "definition": "local-failure-demo",
            "definition_revision": 1,
            "ref": "main",
            "variables": {"SUITE": "smoke"},
        },
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ).encode("utf-8")
    try:
        async with service.database.write() as connection:
            await connection.execute(
                insert(runs).values(
                    id=run_id,
                    definition="local-failure-demo",
                    definition_revision=1,
                    ref="main",
                    variables={"SUITE": "smoke"},
                    idempotency_key=idempotency_key,
                    request_fingerprint=hashlib.sha256(legacy_document).hexdigest(),
                    status="queued",
                    message=None,
                    created_at=BASE,
                    updated_at=BASE,
                    started_at=None,
                    finished_at=None,
                    cancelled_at=None,
                )
            )

        replayed = await service.trigger(
            "local-failure-demo",
            request,
            idempotency_key,
        )
        assert replayed.id == run_id
        assert replayed.replayed is True
        assert await service.list_webhook_deliveries(run_id=run_id) == []
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_outbox_snapshots_are_atomic_idempotent_and_strictly_ordered(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    service = await open_service(tmp_path / "ordered-outbox.db", clock)
    try:
        run_id, connection_id = await subscribe_run(
            service,
            correlation_id="outbox-order-001",
        )

        initial = await service.list_webhook_deliveries(run_id=run_id)
        assert len(initial) == 1
        assert initial[0].sequence == 1
        assert initial[0].normalized_status.value == "queued"
        assert initial[0].status == WebhookDeliveryStatus.PENDING
        assert initial[0].connection_id.hex == connection_id.replace("-", "")

        replay = await service.trigger(
            "local-quality-gate",
            TriggerRunRequest(
                ref="main",
                variables={"SUITE": "smoke"},
                webhook_connection_id=connection_id,
                correlation_id="outbox-order-001",
            ),
            "outbox-order-001",
        )
        assert replay.id == run_id
        assert replay.replayed is True
        assert len(await service.list_webhook_deliveries(run_id=run_id)) == 1

        clock.advance(milliseconds=100)
        assert await service.advance_webhook_runs_once() == 1
        assert await service.advance_webhook_runs_once() == 0

        deliveries = await service.list_webhook_deliveries(run_id=run_id)
        assert [item.sequence for item in deliveries] == [2, 1]
        assert [item.normalized_status.value for item in deliveries] == [
            "running",
            "queued",
        ]

        first = await service.claim_webhook_delivery("outbox-worker-a")
        assert first is not None
        first_payload = json.loads(first.raw_body)
        assert first_payload == {
            "connection_id": connection_id,
            "correlation_id": "outbox-order-001",
            "external_id": run_id,
            "message": None,
            "occurred_at": BASE.isoformat(),
            "sequence": 1,
            "status": "queued",
        }
        assert await service.claim_webhook_delivery("outbox-worker-b") is None
        await service.complete_webhook_delivery(first)

        second = await service.claim_webhook_delivery("outbox-worker-b")
        assert second is not None
        second_payload = json.loads(second.raw_body)
        assert second_payload["sequence"] == 2
        assert second_payload["status"] == "running"
        assert second_payload["connection_id"] == connection_id
        assert second_payload["correlation_id"] == "outbox-order-001"
        await service.complete_webhook_delivery(second)

        assert await service.claim_webhook_delivery("outbox-worker-c") is None
        settled = await service.list_webhook_deliveries(run_id=run_id)
        assert all(item.status == WebhookDeliveryStatus.DELIVERED for item in settled)
        assert all(item.attempts == 1 for item in settled)
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_claim_lease_fences_stale_workers_and_retry_is_dead_letter_safe(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    service = await open_service(tmp_path / "lease-outbox.db", clock)
    try:
        run_id, _connection_id = await subscribe_run(
            service,
            correlation_id="outbox-lease-001",
        )

        claims = await asyncio.gather(
            service.claim_webhook_delivery("lease-worker-a", lease_seconds=5),
            service.claim_webhook_delivery("lease-worker-b", lease_seconds=5),
        )
        winners = [claim for claim in claims if claim is not None]
        assert len(winners) == 1
        stale_claim = winners[0]
        assert stale_claim.attempts == 1

        clock.advance(seconds=5)
        replacement = await service.claim_webhook_delivery(
            "lease-worker-replacement",
            lease_seconds=5,
        )
        assert replacement is not None
        assert replacement.id == stale_claim.id
        assert replacement.attempts == 2
        assert replacement.lease_token != stale_claim.lease_token

        with pytest.raises(CiLabConflict, match="lease"):
            await service.complete_webhook_delivery(stale_claim)

        retry_wait = await service.fail_webhook_delivery(
            replacement,
            error_code="network_error",
            retryable=True,
        )
        assert retry_wait.status == WebhookDeliveryStatus.RETRY_WAIT
        assert retry_wait.attempts == 2
        assert retry_wait.last_error_code == "network_error"
        assert retry_wait.available_at == clock.value + timedelta(seconds=2)
        assert await service.claim_webhook_delivery("too-early-worker") is None

        clock.advance(seconds=2)
        final_attempt = await service.claim_webhook_delivery("final-worker")
        assert final_attempt is not None
        assert final_attempt.attempts == 3
        dead_letter = await service.fail_webhook_delivery(
            final_attempt,
            error_code="http_400",
            retryable=False,
        )
        assert dead_letter.status == WebhookDeliveryStatus.DEAD_LETTER
        assert dead_letter.dead_lettered_at == clock.value
        assert dead_letter.lease_owner is None

        reset = await service.retry_webhook_delivery(dead_letter.id)
        replayed_reset = await service.retry_webhook_delivery(dead_letter.id)
        assert reset.status == WebhookDeliveryStatus.PENDING
        assert reset.attempts == 0
        assert reset.last_error_code is None
        assert reset.dead_lettered_at is None
        assert replayed_reset == reset

        retried = await service.claim_webhook_delivery("manual-retry-worker")
        assert retried is not None
        assert retried.attempts == 1
        completed = await service.complete_webhook_delivery(retried)
        assert completed.status == WebhookDeliveryStatus.DELIVERED
        assert completed.delivered_at == clock.value

        [stored] = await service.list_webhook_deliveries(run_id=run_id)
        assert stored.status == WebhookDeliveryStatus.DELIVERED
        assert stored.version > stale_claim.version
    finally:
        await service.close()


@pytest.mark.asyncio
async def test_delivery_api_is_machine_only_safe_and_retries_dead_letters(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    application = create_ci_lab_app(
        database_path=tmp_path / "delivery-api.db",
        machine_token=MACHINE_TOKEN,
        clock=clock,
    )
    await application.state.ci_lab_service.initialize()
    client = AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://ci-lab.test",
    )
    connection_id = str(uuid4())
    auth = {"Authorization": f"Bearer {MACHINE_TOKEN}"}
    trigger_url = "/api/v1/definitions/local-quality-gate/runs"
    try:
        for invalid_payload in (
            {
                "webhook_connection_id": connection_id,
                "correlation_id": "api-binding-001",
                "callback_url": "https://forbidden.invalid/webhook",
            },
            {"webhook_connection_id": connection_id},
            {"correlation_id": "api-binding-001"},
        ):
            rejected = await client.post(
                trigger_url,
                headers={**auth, "Idempotency-Key": "api-binding-001"},
                json=invalid_payload,
            )
            assert rejected.status_code == 422
            assert "forbidden.invalid" not in rejected.text

        created = await client.post(
            trigger_url,
            headers={**auth, "Idempotency-Key": "api-binding-001"},
            json={
                "webhook_connection_id": connection_id,
                "correlation_id": "api-binding-001",
            },
        )
        assert created.status_code == 202
        run_id = created.json()["id"]

        unauthorized = await client.get("/api/v1/webhook-deliveries")
        assert unauthorized.status_code == 401
        listed = await client.get(
            "/api/v1/webhook-deliveries",
            headers=auth,
            params={"run_id": run_id},
        )
        assert listed.status_code == 200
        [delivery] = listed.json()
        assert delivery["run_id"] == run_id
        assert delivery["connection_id"] == connection_id
        assert delivery["sequence"] == 1
        assert delivery["status"] == "pending"
        assert not {
            "payload_body",
            "body_sha256",
            "lease_token",
            "lease_token_hash",
            "target_url",
        }.intersection(delivery)

        claimed = await application.state.ci_lab_service.claim_webhook_delivery(
            "api-dead-letter-worker"
        )
        assert claimed is not None
        dead = await application.state.ci_lab_service.fail_webhook_delivery(
            claimed,
            error_code="http_400",
            retryable=False,
        )
        unauthorized_retry = await client.post(
            f"/api/v1/webhook-deliveries/{dead.id}/retry"
        )
        assert unauthorized_retry.status_code == 401

        retried = await client.post(
            f"/api/v1/webhook-deliveries/{dead.id}/retry",
            headers=auth,
        )
        assert retried.status_code == 200
        assert retried.json()["status"] == "pending"
        assert retried.json()["attempts"] == 0
        assert retried.json()["last_error_code"] is None
        assert retried.json()["dead_lettered_at"] is None
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()
