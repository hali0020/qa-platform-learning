from __future__ import annotations

import asyncio
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import httpx
from httpx import ASGITransport, AsyncClient

from app.ci_lab import create_ci_lab_app
from app.pipeline.models import PipelineStatus
from app.pipeline.providers.learning_ci import LearningCiPipelineProvider
from app.pipeline.providers.models import ProviderTriggerRequest
from app.pipeline.providers.security import OutboundPolicy


TOKEN = "test-only-ci-lab-machine-token-0001"
BASE = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


@dataclass
class ManualClock:
    value: datetime = BASE

    def __call__(self) -> datetime:
        return self.value

    def advance(self, *, milliseconds: int) -> None:
        self.value += timedelta(milliseconds=milliseconds)


def auth(token: str = TOKEN) -> dict[str, str]:
    return {"Authorization": f"Bearer {token}"}


async def client_for(path: Path, clock: ManualClock):
    application = create_ci_lab_app(
        database_path=path,
        machine_token=TOKEN,
        clock=clock,
    )
    await application.state.ci_lab_service.initialize()
    client = AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://ci-lab.test",
    )
    return application, client


@pytest.mark.asyncio
async def test_machine_authentication_and_fixed_definition_registry(
    tmp_path: Path,
) -> None:
    application, client = await client_for(tmp_path / "auth.db", ManualClock())
    try:
        live = await client.get("/health/live")
        assert live.status_code == 200
        assert live.json() == {"service": "ci-lab", "status": "ok"}
        assert (await client.get("/openapi.json")).status_code == 404
        assert (await client.get("/docs")).status_code == 404
        assert (await client.get("/redoc")).status_code == 404
        oversized_get = await client.request(
            "GET",
            "/health/live",
            content=b"x" * (16 * 1024 + 1),
        )
        assert oversized_get.status_code == 413

        missing = await client.get("/api/v1/definitions")
        wrong = await client.get(
            "/api/v1/definitions",
            headers=auth("wrong-machine-token-that-is-long-enough"),
        )
        oversized_auth = await client.get(
            "/api/v1/definitions",
            headers={"Authorization": f"Bearer {'x' * 2048}"},
        )
        assert missing.status_code == wrong.status_code == oversized_auth.status_code == 401
        assert missing.headers["www-authenticate"] == "Bearer"

        accepted = await client.get("/api/v1/definitions", headers=auth())
        assert accepted.status_code == 200
        assert [item["key"] for item in accepted.json()] == [
            "local-failure-demo",
            "local-quality-gate",
        ]
        assert all(
            set(job) == {"key", "name", "duration_ms", "should_fail"}
            for definition in accepted.json()
            for stage in definition["stages"]
            for job in stage["jobs"]
        )
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_trigger_advances_deterministically_without_background_tasks(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    application, client = await client_for(tmp_path / "timeline.db", clock)
    headers = {**auth(), "Idempotency-Key": "run-success-001"}
    try:
        triggered = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers=headers,
            json={"ref": "main", "variables": {"BUILD_MODE": "learning"}},
        )
        assert triggered.status_code == 202
        run = triggered.json()
        run_id = run["id"]
        assert run["status"] == "queued"
        assert run["replayed"] is False
        assert run["metadata"] == {
            "definition": "local-quality-gate",
            "definition_revision": 1,
            "ref": "main",
            "stage_count": 3,
            "deterministic": True,
        }
        assert "variables" not in run
        assert all(stage["status"] == "queued" for stage in run["stages"])

        clock.advance(milliseconds=100)
        running = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert running.status_code == 200
        assert running.json()["status"] == "running"
        assert running.json()["stages"][0]["jobs"][0]["status"] == "running"

        clock.advance(milliseconds=500)
        waiting = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert waiting.status_code == 200
        body = waiting.json()
        assert body["status"] == "waiting_approval"
        assert body["finished_at"] is None
        assert all(
            job["status"] == "succeeded"
            for stage in body["stages"]
            for job in stage["jobs"]
        )

        completed = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json={
                "event_id": "approve-timeline-001",
                "decision": "approve",
                "actor_id": "qa-lead-1",
                "actor_name": "QA Lead",
                "comment": "checks reviewed",
            },
        )
        assert completed.status_code == 200
        assert completed.json()["status"] == "succeeded"
        assert completed.json()["finished_at"] == "2026-08-27T00:00:00.600000Z"
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_trigger_idempotency_replays_only_identical_input(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    application, client = await client_for(tmp_path / "idempotency.db", clock)
    headers = {**auth(), "Idempotency-Key": "same-request"}
    payload = {"ref": "main", "variables": {"BUILD_MODE": "learning"}}
    try:
        first, replay = await asyncio.gather(
            client.post(
                "/api/v1/definitions/local-quality-gate/runs",
                headers=headers,
                json=payload,
            ),
            client.post(
                "/api/v1/definitions/local-quality-gate/runs",
                headers=headers,
                json=payload,
            ),
        )
        assert first.status_code == replay.status_code == 202
        assert first.json()["id"] == replay.json()["id"]
        assert {first.json()["replayed"], replay.json()["replayed"]} == {
            False,
            True,
        }

        conflict = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers=headers,
            json={"ref": "release", "variables": {"BUILD_MODE": "learning"}},
        )
        assert conflict.status_code == 409
        assert conflict.json()["code"] == "ci_lab_conflict"
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_failure_and_cancellation_have_terminal_invariants(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    application, client = await client_for(tmp_path / "terminal.db", clock)
    try:
        failed_trigger = await client.post(
            "/api/v1/definitions/local-failure-demo/runs",
            headers={**auth(), "Idempotency-Key": "failure-001"},
            json={"ref": "main", "variables": {}},
        )
        failed_id = failed_trigger.json()["id"]
        clock.advance(milliseconds=200)
        failed = await client.get(f"/api/v1/runs/{failed_id}", headers=auth())
        assert failed.json()["status"] == "failed"
        assert failed.json()["stages"][1]["jobs"][0]["status"] == "failed"
        assert failed.json()["stages"][2]["jobs"][0]["status"] == "cancelled"
        late_cancel = await client.post(
            f"/api/v1/runs/{failed_id}/cancel",
            headers=auth(),
        )
        assert late_cancel.status_code == 409

        active_trigger = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "cancel-001"},
            json={"ref": "main", "variables": {}},
        )
        active_id = active_trigger.json()["id"]
        clock.advance(milliseconds=50)
        cancelled = await client.post(
            f"/api/v1/runs/{active_id}/cancel",
            headers=auth(),
        )
        repeated = await client.post(
            f"/api/v1/runs/{active_id}/cancel",
            headers=auth(),
        )
        assert cancelled.status_code == repeated.status_code == 200
        assert cancelled.json()["status"] == "cancelled"
        assert cancelled.json()["replayed"] is False
        assert repeated.json()["replayed"] is True
        assert all(
            job["status"] == "cancelled"
            for stage in cancelled.json()["stages"]
            for job in stage["jobs"]
        )
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_run_survives_service_restart_and_uses_injected_clock(
    tmp_path: Path,
) -> None:
    path = tmp_path / "restart.db"
    clock = ManualClock()
    first_app, first_client = await client_for(path, clock)
    created = await first_client.post(
        "/api/v1/definitions/local-quality-gate/runs",
        headers={**auth(), "Idempotency-Key": "restart-001"},
        json={"ref": "main", "variables": {}},
    )
    run_id = created.json()["id"]
    await first_client.aclose()
    await first_app.state.ci_lab_service.close()

    clock.advance(milliseconds=600)
    second_app, second_client = await client_for(path, clock)
    try:
        restored = await second_client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert restored.status_code == 200
        assert restored.json()["status"] == "waiting_approval"
        assert restored.json()["id"] == run_id
    finally:
        await second_client.aclose()
        await second_app.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_observed_states_never_regress_when_wall_clock_moves_back(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    application, client = await client_for(tmp_path / "clock-rollback.db", clock)
    try:
        created = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "clock-rollback-001"},
            json={"ref": "main", "variables": {}},
        )
        run_id = created.json()["id"]

        clock.advance(milliseconds=100)
        running = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert running.json()["status"] == "running"

        clock.value = BASE - timedelta(seconds=5)
        still_running = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert still_running.json()["status"] == "running"
        assert still_running.json()["stages"][0]["jobs"][0]["status"] == "running"

        clock.value = BASE + timedelta(milliseconds=600)
        waiting = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert waiting.json()["status"] == "waiting_approval"

        clock.value = BASE
        still_waiting = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert still_waiting.json()["status"] == "waiting_approval"
        approved = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json={
                "event_id": "clock-approval-001",
                "decision": "approve",
                "actor_id": "qa-lead-1",
                "actor_name": "QA Lead",
                "comment": "",
            },
        )
        assert approved.json()["status"] == "succeeded"
        assert approved.json()["finished_at"] == "2026-08-27T00:00:00.600000Z"
        assert all(
            job["status"] == "succeeded"
            for stage in approved.json()["stages"]
            for job in stage["jobs"]
        )
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload",
    [
        {"ref": "main", "variables": {"API_TOKEN": "must-not-be-accepted"}},
        {"ref": "main", "variables": {"TARGET": "https://example.invalid"}},
        {"ref": "../main", "variables": {}},
        {"ref": "main", "variables": {"lowercase": "value"}},
        {"ref": "main", "variables": {"MODE": "../outside"}},
        {"ref": "main", "variables": {}, "command": "whoami"},
    ],
)
async def test_trigger_rejects_secret_url_path_and_extra_inputs_without_echoing_them(
    tmp_path: Path,
    payload: dict[str, object],
) -> None:
    application, client = await client_for(tmp_path / "validation.db", ManualClock())
    try:
        response = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "invalid-input"},
            json=payload,
        )
        assert response.status_code == 422
        assert response.json() == {
            "code": "ci_lab_request_validation_error",
            "detail": "request validation failed",
        }
        assert "must-not-be-accepted" not in response.text
        assert "whoami" not in response.text
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_unknown_resources_bad_ids_and_oversized_requests_are_bounded(
    tmp_path: Path,
) -> None:
    application, client = await client_for(tmp_path / "bounds.db", ManualClock())
    try:
        unknown = await client.post(
            "/api/v1/definitions/not-registered/runs",
            headers={**auth(), "Idempotency-Key": "unknown-definition"},
            json={"ref": "main", "variables": {}},
        )
        bad_id = await client.get("/api/v1/runs/not-a-uuid", headers=auth())
        bad_key = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "unsafe key"},
            json={"ref": "main", "variables": {}},
        )
        oversized = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "oversized"},
            content=b"x" * (16 * 1024 + 1),
        )
        assert unknown.status_code == 404
        assert bad_id.status_code == 422
        assert bad_id.json()["detail"] == "request validation failed"
        assert bad_key.status_code == 422
        assert bad_key.json()["code"] == "ci_lab_validation_error"
        assert oversized.status_code == 413
        assert oversized.json()["code"] == "request_body_too_large"
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


def test_factory_rejects_weak_or_whitespace_machine_tokens(tmp_path: Path) -> None:
    with pytest.raises(ValueError):
        create_ci_lab_app(database_path=tmp_path / "weak.db", machine_token="short")
    with pytest.raises(ValueError):
        create_ci_lab_app(
            database_path=tmp_path / "space.db",
            machine_token=" test-only-ci-lab-machine-token-0001",
        )
    with pytest.raises(ValueError):
        create_ci_lab_app(
            database_path=tmp_path / "unicode.db",
            machine_token="本" * 32,
        )


@pytest.mark.parametrize(
    "database_path",
    [
        r"\\untrusted-share\ci-lab.db",
        "//server/share/ci-lab.db",
        "file:ci-lab.db",
        "sqlite:///ci-lab.db",
        "https://example.invalid/ci-lab.db",
    ],
)
def test_factory_rejects_database_urls_and_network_share_paths(
    database_path: str,
) -> None:
    with pytest.raises(ValueError, match="local filesystem"):
        create_ci_lab_app(database_path=database_path, machine_token=TOKEN)


@pytest.mark.asyncio
async def test_learning_ci_provider_calls_real_ci_lab_asgi_contract(
    tmp_path: Path,
) -> None:
    """Exercise the real provider client and CI Lab app without a socket."""

    clock = ManualClock()
    application = create_ci_lab_app(
        database_path=tmp_path / "provider-contract.db",
        machine_token=TOKEN,
        clock=clock,
    )
    await application.state.ci_lab_service.initialize()

    resolver_calls: list[tuple[str, int]] = []

    async def fixed_resolver(host: str, port: int) -> tuple[str, ...]:
        resolver_calls.append((host, port))
        return ("127.0.0.1",)

    provider = LearningCiPipelineProvider(
        base_url="http://127.0.0.1:23020",
        definition_id="local-quality-gate",
        bearer_token=TOKEN,
        policy=OutboundPolicy(
            allowed_hosts=("127.0.0.1",),
            allowed_ports=(23020,),
            allowed_networks=("127.0.0.1/32",),
            allowed_http_hosts=("127.0.0.1",),
        ),
        enabled=True,
        resolver=fixed_resolver,
        transport=httpx.ASGITransport(app=application),
    )
    try:
        triggered = await provider.trigger(
            ProviderTriggerRequest(
                definition_ref="local-quality-gate",
                ref="main",
                variables={"BUILD_MODE": "learning"},
                correlation_id="provider-contract-001",
            )
        )
        assert triggered.status == PipelineStatus.QUEUED
        assert triggered.metadata["definition_revision"] == 1
        assert triggered.metadata["replayed"] is False

        clock.advance(milliseconds=100)
        running = await provider.get(triggered.external_id)
        assert running.external_id == triggered.external_id
        assert running.status == PipelineStatus.RUNNING

        cancelled = await provider.cancel(triggered.external_id)
        assert cancelled.external_id == triggered.external_id
        assert cancelled.status == PipelineStatus.CANCELLED
        assert cancelled.message == "cancelled by an authenticated machine request"
        assert resolver_calls == [
            ("127.0.0.1", 23020),
            ("127.0.0.1", 23020),
            ("127.0.0.1", 23020),
        ]
    finally:
        await provider.aclose()
        await application.state.ci_lab_service.close()
