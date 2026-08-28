from __future__ import annotations

import asyncio
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
from httpx import ASGITransport, AsyncClient
from sqlalchemy import insert

from app.ci_lab import create_ci_lab_app
from app.ci_lab.database import CiLabDatabase, runs
from app.ci_lab.registry import JobDefinition, PipelineDefinition, StageDefinition


TOKEN = "test-only-ci-lab-machine-token-0001"
BASE = datetime(2026, 8, 27, 0, 0, tzinfo=timezone.utc)


class ManualClock:
    def __init__(self) -> None:
        self.value = BASE

    def __call__(self) -> datetime:
        return self.value

    def advance(self, milliseconds: int) -> None:
        self.value += timedelta(milliseconds=milliseconds)


def auth() -> dict[str, str]:
    return {"Authorization": f"Bearer {TOKEN}"}


def decision(
    event_id: str,
    value: str = "approve",
    *,
    comment: str = "reviewed",
) -> dict[str, str]:
    return {
        "event_id": event_id,
        "decision": value,
        "actor_id": "qa-lead-1",
        "actor_name": "QA Lead",
        "comment": comment,
    }


async def open_lab(path: Path, clock: ManualClock, *, registry=None):
    options = {
        "database_path": path,
        "machine_token": TOKEN,
        "clock": clock,
    }
    if registry is not None:
        options["registry"] = registry
    application = create_ci_lab_app(**options)
    await application.state.ci_lab_service.initialize()
    client = AsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://ci-lab.test",
    )
    return application, client


@pytest.mark.asyncio
async def test_quality_gate_waits_until_one_idempotent_approval_and_survives_restart(
    tmp_path: Path,
) -> None:
    path = tmp_path / "approval.db"
    clock = ManualClock()
    application, client = await open_lab(path, clock)
    try:
        created = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "approval-run-001"},
            json={"ref": "main", "variables": {}},
        )
        run_id = created.json()["id"]

        too_early = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json=decision("early-001"),
        )
        assert too_early.status_code == 409

        clock.advance(600)
        waiting = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert waiting.json()["status"] == "waiting_approval"
        assert waiting.json()["quality_gate"] == {
            "required": True,
            "status": "waiting_approval",
            "policy_revision": 1,
            "reached_at": "2026-08-27T00:00:00.600000Z",
            "decided_at": None,
        }
        assert waiting.json()["approvals"] == []

        clock.advance(60_000)
        still_waiting = await client.get(
            f"/api/v1/runs/{run_id}", headers=auth()
        )
        assert still_waiting.json()["status"] == "waiting_approval"

        payload = decision("approval-001")
        approved = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json=payload,
        )
        replayed = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json=payload,
        )
        assert approved.status_code == replayed.status_code == 200
        assert approved.json()["status"] == "succeeded"
        assert approved.json()["quality_gate"]["status"] == "approved"
        assert len(approved.json()["approvals"]) == 1
        assert approved.json()["approvals"][0]["event_id"] == "approval-001"
        assert approved.json()["replayed"] is False
        assert replayed.json()["replayed"] is True

        changed_replay = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json=decision("approval-001", comment="changed"),
        )
        second_decision = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json=decision("approval-002", "reject"),
        )
        assert changed_replay.status_code == second_decision.status_code == 409
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()

    restored_app, restored_client = await open_lab(path, clock)
    try:
        restored = await restored_client.get(
            f"/api/v1/runs/{run_id}", headers=auth()
        )
        assert restored.json()["status"] == "succeeded"
        assert restored.json()["quality_gate"]["status"] == "approved"
        assert len(restored.json()["approvals"]) == 1
    finally:
        await restored_client.aclose()
        await restored_app.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_reject_is_terminal_and_concurrent_decisions_create_one_record(
    tmp_path: Path,
) -> None:
    clock = ManualClock()
    application, client = await open_lab(tmp_path / "reject.db", clock)
    try:
        created = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "reject-run-001"},
            json={"ref": "main", "variables": {}},
        )
        run_id = created.json()["id"]
        clock.advance(600)
        responses = await asyncio.gather(
            client.post(
                f"/api/v1/runs/{run_id}/gate-decisions",
                headers=auth(),
                json=decision("race-approve", "approve"),
            ),
            client.post(
                f"/api/v1/runs/{run_id}/gate-decisions",
                headers=auth(),
                json=decision("race-reject", "reject"),
            ),
        )
        assert sorted(response.status_code for response in responses) == [200, 409]
        final = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert final.json()["status"] in {"succeeded", "failed"}
        assert final.json()["quality_gate"]["status"] in {"approved", "rejected"}
        assert len(final.json()["approvals"]) == 1

        second = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "reject-run-002"},
            json={"ref": "main", "variables": {}},
        )
        second_id = second.json()["id"]
        clock.advance(600)
        rejection = decision("reject-002", "reject", comment="release blocked")
        rejected = await client.post(
            f"/api/v1/runs/{second_id}/gate-decisions",
            headers=auth(),
            json=rejection,
        )
        replayed = await client.post(
            f"/api/v1/runs/{second_id}/gate-decisions",
            headers=auth(),
            json=rejection,
        )
        assert rejected.status_code == replayed.status_code == 200
        assert rejected.json()["status"] == "failed"
        assert rejected.json()["quality_gate"]["status"] == "rejected"
        assert rejected.json()["approvals"][0]["decision"] == "reject"
        assert replayed.json()["replayed"] is True
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_non_gated_definition_keeps_legacy_success_semantics(
    tmp_path: Path,
) -> None:
    definition = PipelineDefinition(
        key="legacy-success-demo",
        name="Legacy success demo",
        revision=1,
        queue_delay_ms=10,
        stages=(
            StageDefinition(
                key="test",
                name="Test",
                jobs=(JobDefinition(key="pass", name="Pass", duration_ms=10),),
            ),
        ),
    )
    clock = ManualClock()
    application, client = await open_lab(
        tmp_path / "legacy.db",
        clock,
        registry={definition.key: definition},
    )
    try:
        created = await client.post(
            "/api/v1/definitions/legacy-success-demo/runs",
            headers={**auth(), "Idempotency-Key": "legacy-run-001"},
            json={"ref": "main", "variables": {}},
        )
        run_id = created.json()["id"]
        clock.advance(20)
        completed = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert completed.json()["status"] == "succeeded"
        assert completed.json()["quality_gate"] == {
            "required": False,
            "status": "not_required",
            "policy_revision": None,
            "reached_at": None,
            "decided_at": None,
        }
        rejected = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json=decision("legacy-approval"),
        )
        assert rejected.status_code == 409
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_failed_quality_checks_can_never_be_approved(tmp_path: Path) -> None:
    definition = PipelineDefinition(
        key="local-quality-gate",
        name="Failing quality gate",
        revision=1,
        queue_delay_ms=10,
        stages=(
            StageDefinition(
                key="test",
                name="Test",
                jobs=(
                    JobDefinition(
                        key="fixed-failure",
                        name="Fixed failure",
                        duration_ms=10,
                        should_fail=True,
                    ),
                ),
            ),
        ),
    )
    clock = ManualClock()
    application, client = await open_lab(
        tmp_path / "failed-gate.db",
        clock,
        registry={definition.key: definition},
    )
    try:
        created = await client.post(
            "/api/v1/definitions/local-quality-gate/runs",
            headers={**auth(), "Idempotency-Key": "failed-gate-001"},
            json={"ref": "main", "variables": {}},
        )
        run_id = created.json()["id"]
        clock.advance(20)
        failed = await client.get(f"/api/v1/runs/{run_id}", headers=auth())
        assert failed.json()["status"] == "failed"
        assert failed.json()["quality_gate"]["status"] == "failed"
        approval = await client.post(
            f"/api/v1/runs/{run_id}/gate-decisions",
            headers=auth(),
            json=decision("cannot-approve"),
        )
        assert approval.status_code == 409
        assert failed.json()["approvals"] == []
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()


@pytest.mark.asyncio
async def test_legacy_quality_definition_run_without_gate_row_remains_readable(
    tmp_path: Path,
) -> None:
    path = tmp_path / "legacy-quality-run.db"
    database = CiLabDatabase(path)
    try:
        async with database.write() as connection:
            await connection.execute(
                insert(runs).values(
                    id="00000000-0000-0000-0000-000000000101",
                    definition="local-quality-gate",
                    definition_revision=1,
                    ref="main",
                    variables={},
                    idempotency_key="legacy-quality-001",
                    request_fingerprint="f" * 64,
                    status="queued",
                    message=None,
                    created_at=BASE,
                    updated_at=BASE,
                    started_at=None,
                    finished_at=None,
                    cancelled_at=None,
                )
            )
    finally:
        await database.close()

    clock = ManualClock()
    clock.advance(600)
    application, client = await open_lab(path, clock)
    try:
        restored = await client.get(
            "/api/v1/runs/00000000-0000-0000-0000-000000000101",
            headers=auth(),
        )
        assert restored.status_code == 200
        assert restored.json()["status"] == "succeeded"
        assert restored.json()["quality_gate"]["status"] == "not_required"
    finally:
        await client.aclose()
        await application.state.ci_lab_service.close()
