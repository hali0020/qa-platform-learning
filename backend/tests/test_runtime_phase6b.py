from __future__ import annotations

import json
from datetime import datetime, timezone
from pathlib import Path
from uuid import UUID, uuid4

import pytest

from app.core.actor import ActorIdentity, reset_current_actor, set_current_actor
from app.core.config import (
    CI_LAB_PROVIDER_SECRET_NAME,
    CI_LAB_WEBHOOK_SECRET_NAME,
    Settings,
)
from app.core.errors import AuthorizationError, BusinessValidationError, ConflictError
from app.database.models import RoleRecord, UserRecord
from app.database.session import Database
from app.pipeline.models import PipelineStatus
from app.pipeline.providers import ProviderConfigurationError, ProviderResponseError
from app.pipeline.providers.models import (
    ProviderApproval,
    ProviderGateDecisionRequest,
    ProviderKind,
    ProviderQualityGate,
    ProviderQualityGateStatus,
    ProviderRun,
)
from app.runtime.schemas import (
    ProviderConnectionCreate,
    ProviderRunApprovalPayload,
    ProviderTriggerPayload,
)
from app.runtime.service import PersistentRuntimeService, create_runtime_service
from app.runtime.webhook_security import sign_webhook


PROVIDER_SECRET = "local-learning-token-value"
WEBHOOK_SECRET = "local-webhook-secret-value-32-bytes-minimum"


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


class _Phase6Provider:
    kind = ProviderKind.LEARNING_CI

    def __init__(self) -> None:
        self.service: PersistentRuntimeService | None = None
        self.trigger_requests = []
        self.trigger_error: Exception | None = None
        self.decisions: list[ProviderGateDecisionRequest] = []

    async def trigger(self, request):
        assert self.service is not None
        # SQLite's process lock spans the SQL transaction. If this assertion
        # fails, provider HTTP has slipped back inside the durable transaction.
        assert not self.service.repository._single_process_lock.locked()
        self.trigger_requests.append(request)
        if self.trigger_error is not None:
            raise self.trigger_error
        external_id = f"phase6-run-{len(self.trigger_requests)}"
        if request.definition_ref == "local-quality-gate":
            return ProviderRun(
                provider=self.kind,
                external_id=external_id,
                status=PipelineStatus.QUEUED,
                raw_status="waiting_approval",
                message="quality checks passed; waiting for approval",
                quality_gate=ProviderQualityGate(
                    required=True,
                    status=ProviderQualityGateStatus.WAITING_APPROVAL,
                    policy_revision=1,
                    reached_at=datetime.now(timezone.utc),
                ),
            )
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.QUEUED,
            raw_status="queued",
        )

    async def get(self, external_id: str):
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.RUNNING,
            raw_status="running",
        )

    async def cancel(self, external_id: str):
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.CANCELLED,
            raw_status="cancelled",
        )

    async def decide_gate(
        self,
        external_id: str,
        request: ProviderGateDecisionRequest,
    ) -> ProviderRun:
        self.decisions.append(request)
        now = datetime.now(timezone.utc)
        approved = request.decision.value == "approve"
        return ProviderRun(
            provider=self.kind,
            external_id=external_id,
            status=PipelineStatus.SUCCEEDED if approved else PipelineStatus.FAILED,
            raw_status="succeeded" if approved else "failed",
            quality_gate=ProviderQualityGate(
                required=True,
                status=(
                    ProviderQualityGateStatus.APPROVED
                    if approved
                    else ProviderQualityGateStatus.REJECTED
                ),
                policy_revision=1,
                reached_at=now,
                decided_at=now,
            ),
            approvals=[
                ProviderApproval(
                    id=f"provider-{request.event_id}",
                    event_id=request.event_id,
                    decision=request.decision,
                    actor_id=request.actor_id,
                    actor_name=request.actor_name,
                    comment=request.comment,
                    created_at=now,
                )
            ],
        )

    async def aclose(self) -> None:
        return None


async def _new_learning_service(
    tmp_path: Path,
    *,
    definition_ref: str,
    webhook: bool = False,
) -> tuple[Database, PersistentRuntimeService, _Phase6Provider, object]:
    fake = _Phase6Provider()
    settings = Settings(
        provider_runtime_mode="ci_lab_local",
        provider_secret_env_names=(
            CI_LAB_PROVIDER_SECRET_NAME,
            CI_LAB_WEBHOOK_SECRET_NAME,
        ),
    )
    environ = {
        CI_LAB_PROVIDER_SECRET_NAME: PROVIDER_SECRET,
        CI_LAB_WEBHOOK_SECRET_NAME: WEBHOOK_SECRET,
    }
    database = Database(_sqlite_url(tmp_path / f"{uuid4().hex}.db"))
    service = create_runtime_service(
        database,
        settings,
        environ=environ,
        provider_builder=lambda _connection, _secret, _policy: fake,
    )
    fake.service = service
    connection = await service.create_connection(
        ProviderConnectionCreate(
            name=f"Phase 6B {uuid4().hex}",
            kind="learning_ci",
            definition_ref=definition_ref,
            secret_env_var=CI_LAB_PROVIDER_SECRET_NAME,
            webhook_secret_env_var=(
                CI_LAB_WEBHOOK_SECRET_NAME if webhook else None
            ),
            enabled=True,
        )
    )
    return database, service, fake, connection


async def _create_actor(database: Database, username: str) -> ActorIdentity:
    user_id = uuid4()
    now = datetime.now(timezone.utc)
    async with database.session() as session:
        if await session.get(RoleRecord, "system_admin") is None:
            session.add(
                RoleRecord(
                    key="system_admin",
                    name="System administrator",
                    description="Phase 6B test role",
                    is_builtin=True,
                )
            )
            await session.flush()
        session.add(
            UserRecord(
                id=str(user_id),
                username=username,
                username_normalized=username.casefold(),
                display_name=username,
                password_hash="test-only-not-a-real-password-hash",
                role_key="system_admin",
                status="active",
                failed_login_count=0,
                locked_until=None,
                last_login_at=None,
                password_changed_at=now,
                created_at=now,
                updated_at=now,
            )
        )
        await session.commit()
    return ActorIdentity(user_id=UUID(str(user_id)), username=username)


def _signed_webhook(
    *,
    event_id: str,
    external_id: str,
    sequence: int,
    status: str,
    occurred_at: datetime,
    connection_id: str,
    correlation_id: str,
    message: str | None = None,
) -> tuple[bytes, list[tuple[bytes, bytes]]]:
    payload = {
        "external_id": external_id,
        "sequence": sequence,
        "occurred_at": occurred_at.isoformat(),
        "status": status,
        "message": message,
        "connection_id": connection_id,
        "correlation_id": correlation_id,
    }
    raw_body = json.dumps(
        payload,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    timestamp = int(datetime.now(timezone.utc).timestamp())
    signature = sign_webhook(
        WEBHOOK_SECRET.encode("utf-8"),
        timestamp=timestamp,
        event_id=event_id,
        raw_body=raw_body,
    )
    return raw_body, [
        (b"x-qa-webhook-event-id", event_id.encode("ascii")),
        (b"x-qa-webhook-timestamp", str(timestamp).encode("ascii")),
        (b"x-qa-webhook-signature", signature.encode("ascii")),
    ]


@pytest.mark.asyncio
async def test_trigger_outbox_is_committed_before_provider_io_and_is_idempotent(
    tmp_path: Path,
) -> None:
    database, service, fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-pipeline",
    )
    try:
        payload = ProviderTriggerPayload(
            ref="main",
            variables={"SUITE": "smoke"},
            correlation_id="phase6-outbox-1",
        )
        pending = await service.trigger_provider(connection.id, payload)
        replay = await service.trigger_provider(connection.id, payload)

        assert pending.id == replay.id
        assert pending.external_id is None
        assert pending.dispatch_status == "pending"
        assert fake.trigger_requests == []
        intents = await service.list_provider_trigger_intents()
        assert len(intents) == 1
        assert intents[0].status == "pending"
        assert intents[0].attempts == 0

        dispatched = await service.dispatch_provider_trigger_once(
            "phase6-worker",
            lease_seconds=30,
        )
        assert dispatched is not None
        assert dispatched.id == pending.id
        assert dispatched.external_id == "phase6-run-1"
        assert dispatched.dispatch_status == "dispatched"
        assert len(fake.trigger_requests) == 1

        [settled] = await service.list_provider_trigger_intents()
        assert settled.status == "succeeded"
        assert settled.attempts == 1
        assert settled.lease_owner is None
        assert settled.lease_expires_at is None
        assert await service.dispatch_provider_trigger_once("phase6-worker") is None
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_first_webhook_can_bind_run_before_dispatch_finalize_by_correlation(
    tmp_path: Path,
) -> None:
    database, service, fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-pipeline",
        webhook=True,
    )
    correlation_id = "phase6-early-webhook-1"
    external_id = "phase6-early-external-1"
    observed_results = []
    replay_request: tuple[bytes, list[tuple[bytes, bytes]]] | None = None

    async def trigger_with_early_webhook(request):
        nonlocal replay_request
        assert request.correlation_id == correlation_id
        body, headers = _signed_webhook(
            event_id="event-before-dispatch-finalize-1",
            external_id=external_id,
            sequence=1,
            status="queued",
            occurred_at=datetime.now(timezone.utc),
            connection_id=connection.id,
            correlation_id=correlation_id,
        )
        replay_request = (body, headers)
        observed_results.append(
            await service.process_learning_ci_webhook(
                connection.id,
                raw_body=body,
                raw_headers=headers,
            )
        )
        return ProviderRun(
            provider=ProviderKind.LEARNING_CI,
            external_id=external_id,
            status=PipelineStatus.QUEUED,
            raw_status="queued",
            metadata={"webhook_sequence": 1},
        )

    fake.trigger = trigger_with_early_webhook
    try:
        pending = await service.trigger_provider(
            connection.id,
            ProviderTriggerPayload(correlation_id=correlation_id),
        )
        assert pending.external_id is None
        assert pending.dispatch_status == "pending"

        dispatched = await service.dispatch_provider_trigger_once(
            "early-webhook-worker"
        )
        assert dispatched is not None
        assert len(observed_results) == 1
        assert observed_results[0].result == "applied"
        assert observed_results[0].run_id == pending.id
        assert dispatched.id == pending.id
        assert dispatched.external_id == external_id
        assert dispatched.correlation_id == correlation_id
        assert dispatched.dispatch_status == "dispatched"
        assert dispatched.last_provider_sequence == 1
        assert dispatched.reconciliation_required is False

        assert replay_request is not None
        duplicate = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=replay_request[0],
            raw_headers=replay_request[1],
        )
        assert duplicate.result == "duplicate"
        assert duplicate.run_id == pending.id
        final = await service.get_provider_run(connection.id, pending.id)
        assert final.external_id == external_id
        assert final.last_provider_sequence == 1
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_newer_webhook_snapshot_is_not_overwritten_by_trigger_response(
    tmp_path: Path,
) -> None:
    database, service, fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-pipeline",
        webhook=True,
    )
    correlation_id = "phase6-newer-webhook-wins"
    external_id = "phase6-newer-webhook-external"

    async def trigger_with_two_early_webhooks(request):
        for sequence, status in ((1, "queued"), (2, "running")):
            body, headers = _signed_webhook(
                event_id=f"event-newer-webhook-wins-{sequence}",
                external_id=external_id,
                sequence=sequence,
                status=status,
                occurred_at=datetime.now(timezone.utc),
                connection_id=connection.id,
                correlation_id=correlation_id,
            )
            result = await service.process_learning_ci_webhook(
                connection.id,
                raw_body=body,
                raw_headers=headers,
            )
            assert result.result == "applied"
        # This is the older trigger HTTP snapshot that was prepared after the
        # first event but before the second event reached QA.
        return ProviderRun(
            provider=ProviderKind.LEARNING_CI,
            external_id=external_id,
            status=PipelineStatus.QUEUED,
            raw_status="queued",
            metadata={"webhook_sequence": 1},
        )

    fake.trigger = trigger_with_two_early_webhooks
    try:
        pending = await service.trigger_provider(
            connection.id,
            ProviderTriggerPayload(correlation_id=correlation_id),
        )
        dispatched = await service.dispatch_provider_trigger_once(
            "newer-webhook-worker"
        )

        assert dispatched is not None
        assert dispatched.id == pending.id
        assert dispatched.external_id == external_id
        assert dispatched.dispatch_status == "dispatched"
        assert dispatched.status == "running"
        assert dispatched.raw_status == "running"
        assert dispatched.last_provider_sequence == 2
        assert dispatched.reconciliation_required is False
        [intent] = await service.list_provider_trigger_intents()
        assert intent.status == "succeeded"
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_verified_webhook_prevents_trigger_error_from_regressing_run(
    tmp_path: Path,
) -> None:
    database, service, fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-pipeline",
        webhook=True,
    )
    correlation_id = "phase6-webhook-before-timeout"
    external_id = "phase6-webhook-before-timeout-external"

    async def trigger_with_webhook_then_error(request):
        body, headers = _signed_webhook(
            event_id="event-webhook-before-timeout",
            external_id=external_id,
            sequence=1,
            status="running",
            occurred_at=datetime.now(timezone.utc),
            connection_id=connection.id,
            correlation_id=correlation_id,
        )
        applied = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=body,
            raw_headers=headers,
        )
        assert applied.result == "applied"
        raise ProviderResponseError("simulated response timeout")

    fake.trigger = trigger_with_webhook_then_error
    try:
        pending = await service.trigger_provider(
            connection.id,
            ProviderTriggerPayload(correlation_id=correlation_id),
        )
        settled = await service.dispatch_provider_trigger_once(
            "webhook-before-timeout-worker"
        )

        assert settled is not None
        assert settled.id == pending.id
        assert settled.external_id == external_id
        assert settled.dispatch_status == "dispatched"
        assert settled.status == "running"
        assert settled.raw_status == "running"
        assert settled.last_provider_sequence == 1
        assert settled.reconciliation_required is False
        [intent] = await service.list_provider_trigger_intents()
        assert intent.status == "succeeded"
        assert intent.last_error_code is None
        assert intent.completed_at is not None
        assert await service.dispatch_provider_trigger_once(
            "webhook-before-timeout-worker"
        ) is None
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_definitive_provider_rejection_converges_run_to_terminal_failure(
    tmp_path: Path,
) -> None:
    database, service, fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-quality-gate",
    )
    fake.trigger_error = ProviderConfigurationError(
        "Learning CI rejected the provider request"
    )
    try:
        pending = await service.trigger_provider(
            connection.id,
            ProviderTriggerPayload(correlation_id="phase6-definitive-failure"),
        )
        failed = await service.dispatch_provider_trigger_once("phase6-worker")

        assert failed is not None
        assert failed.id == pending.id
        assert failed.external_id is None
        assert failed.status == "failed"
        assert failed.raw_status == "trigger_failed"
        assert failed.dispatch_status == "failed"
        assert failed.quality_gate_status == "failed"
        assert failed.reconciliation_required is False
        assert len(fake.trigger_requests) == 1

        [intent] = await service.list_provider_trigger_intents()
        assert intent.status == "failed"
        assert intent.attempts == 1
        assert intent.last_error_code == "provider_rejected"
        assert intent.completed_at is not None
        assert await service.dispatch_provider_trigger_once("phase6-worker") is None
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_learning_ci_rejects_input_outside_the_lab_contract_before_enqueue(
    tmp_path: Path,
) -> None:
    database, service, fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-pipeline",
    )
    invalid_payloads = (
        ProviderTriggerPayload(ref="feature/with-slash"),
        ProviderTriggerPayload(correlation_id="correlation with spaces"),
        ProviderTriggerPayload(variables={"suite": "smoke"}),
        ProviderTriggerPayload(variables={"BUILD_KEY": "not-a-secret"}),
        ProviderTriggerPayload(variables={"SUITE": ""}),
        ProviderTriggerPayload(variables={"SUITE": "游" * 171}),
        ProviderTriggerPayload(variables={"SUITE": "line\nbreak"}),
        ProviderTriggerPayload(variables={"SOURCE": "http://localhost/input"}),
        ProviderTriggerPayload(
            variables={f"V{index}": "x" for index in range(33)}
        ),
        ProviderTriggerPayload(
            variables={f"V{index}": "x" * 500 for index in range(17)}
        ),
    )
    try:
        for payload in invalid_payloads:
            with pytest.raises(BusinessValidationError, match="Learning CI"):
                await service.trigger_provider(connection.id, payload)

        assert await service.list_provider_trigger_intents() == []
        assert fake.trigger_requests == []
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_quality_gate_replay_is_idempotent_and_triggerer_cannot_self_approve(
    tmp_path: Path,
) -> None:
    database, service, fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-quality-gate",
    )
    triggerer = await _create_actor(database, "triggerer")
    approver = await _create_actor(database, "approver")
    try:
        actor_token = set_current_actor(triggerer)
        try:
            pending = await service.trigger_provider(
                connection.id,
                ProviderTriggerPayload(correlation_id="phase6-gate-1"),
            )
        finally:
            reset_current_actor(actor_token)

        waiting = await service.dispatch_provider_trigger_once("gate-worker")
        assert waiting is not None
        assert waiting.id == pending.id
        assert waiting.raw_status == "waiting_approval"
        assert waiting.quality_gate_status == "waiting_approval"

        decision = ProviderRunApprovalPayload(
            event_id="approval-1",
            decision="approve",
            comment="quality evidence reviewed",
        )
        actor_token = set_current_actor(triggerer)
        try:
            with pytest.raises(AuthorizationError, match="不能审批自己"):
                await service.decide_provider_quality_gate(
                    connection.id,
                    pending.id,
                    decision,
                )
        finally:
            reset_current_actor(actor_token)
        assert fake.decisions == []

        actor_token = set_current_actor(approver)
        try:
            approved = await service.decide_provider_quality_gate(
                connection.id,
                pending.id,
                decision,
            )
            replay = await service.decide_provider_quality_gate(
                connection.id,
                pending.id,
                decision,
            )
            with pytest.raises(ConflictError, match="event_id"):
                await service.decide_provider_quality_gate(
                    connection.id,
                    pending.id,
                    decision.model_copy(update={"decision": "reject"}),
                )
        finally:
            reset_current_actor(actor_token)

        assert approved.quality_gate_status == "approved"
        assert approved.status == "succeeded"
        assert replay.approvals == approved.approvals
        assert len(replay.approvals) == 1
        assert len(fake.decisions) == 1
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_quality_gate_cannot_be_bypassed_by_a_success_webhook(
    tmp_path: Path,
) -> None:
    database, service, _fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-quality-gate",
        webhook=True,
    )
    try:
        pending = await service.trigger_provider(
            connection.id,
            ProviderTriggerPayload(correlation_id="phase6-gate-webhook"),
        )
        waiting = await service.dispatch_provider_trigger_once("gate-worker")
        assert waiting is not None
        assert waiting.external_id is not None
        assert waiting.quality_gate_status == "waiting_approval"

        body, headers = _signed_webhook(
            event_id="gate-bypass-attempt-1",
            external_id=waiting.external_id,
            sequence=1,
            status="succeeded",
            occurred_at=datetime.now(timezone.utc),
            connection_id=connection.id,
            correlation_id="phase6-gate-webhook",
        )
        result = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=body,
            raw_headers=headers,
        )
        unchanged = await service.get_provider_run(connection.id, pending.id)

        assert result.result == "reconcile_required"
        assert result.reconciliation_required is True
        assert unchanged.status == "queued"
        assert unchanged.raw_status == "waiting_approval"
        assert unchanged.quality_gate_status == "waiting_approval"
        assert unchanged.last_provider_sequence == 0
    finally:
        await service.shutdown()
        await database.shutdown()


@pytest.mark.asyncio
async def test_signed_webhook_handles_replay_conflict_gaps_and_terminal_regression(
    tmp_path: Path,
) -> None:
    database, service, _fake, connection = await _new_learning_service(
        tmp_path,
        definition_ref="local-pipeline",
        webhook=True,
    )
    try:
        pending = await service.trigger_provider(
            connection.id,
            ProviderTriggerPayload(correlation_id="phase6-webhook-1"),
        )
        dispatched = await service.dispatch_provider_trigger_once("webhook-worker")
        assert dispatched is not None
        assert dispatched.id == pending.id
        assert dispatched.external_id is not None
        external_id = dispatched.external_id
        occurred_at = datetime.now(timezone.utc)

        legacy_body = json.dumps(
            {
                "external_id": external_id,
                "sequence": 1,
                "occurred_at": occurred_at.isoformat(),
                "status": "running",
                "message": None,
            },
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
        legacy_timestamp = int(datetime.now(timezone.utc).timestamp())
        legacy_event_id = "legacy-unbound-event-1"
        legacy_signature = sign_webhook(
            WEBHOOK_SECRET.encode("utf-8"),
            timestamp=legacy_timestamp,
            event_id=legacy_event_id,
            raw_body=legacy_body,
        )
        with pytest.raises(BusinessValidationError, match="格式"):
            await service.process_learning_ci_webhook(
                connection.id,
                raw_body=legacy_body,
                raw_headers=[
                    (b"x-qa-webhook-event-id", legacy_event_id.encode("ascii")),
                    (
                        b"x-qa-webhook-timestamp",
                        str(legacy_timestamp).encode("ascii"),
                    ),
                    (
                        b"x-qa-webhook-signature",
                        legacy_signature.encode("ascii"),
                    ),
                ],
            )

        body, headers = _signed_webhook(
            event_id="event-running-1",
            external_id=external_id,
            sequence=1,
            status="running",
            occurred_at=occurred_at,
            connection_id=connection.id,
            correlation_id="phase6-webhook-1",
        )
        applied = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=body,
            raw_headers=headers,
        )
        duplicate = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=body,
            raw_headers=headers,
        )
        assert applied.result == "applied"
        assert duplicate.result == "duplicate"

        conflicting_body, conflicting_headers = _signed_webhook(
            event_id="event-running-1",
            external_id=external_id,
            sequence=1,
            status="running",
            occurred_at=occurred_at,
            connection_id=connection.id,
            correlation_id="phase6-webhook-1",
            message="same event id, different body",
        )
        with pytest.raises(ConflictError, match="event_id"):
            await service.process_learning_ci_webhook(
                connection.id,
                raw_body=conflicting_body,
                raw_headers=conflicting_headers,
            )

        stale_body, stale_headers = _signed_webhook(
            event_id="event-stale-1",
            external_id=external_id,
            sequence=1,
            status="queued",
            occurred_at=occurred_at,
            connection_id=connection.id,
            correlation_id="phase6-webhook-1",
        )
        stale = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=stale_body,
            raw_headers=stale_headers,
        )
        assert stale.result == "stale"

        gap_body, gap_headers = _signed_webhook(
            event_id="event-gap-3",
            external_id=external_id,
            sequence=3,
            status="succeeded",
            occurred_at=occurred_at,
            connection_id=connection.id,
            correlation_id="phase6-webhook-1",
        )
        gap = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=gap_body,
            raw_headers=gap_headers,
        )
        assert gap.result == "reconcile_required"
        assert gap.reconciliation_required is True

        success_body, success_headers = _signed_webhook(
            event_id="event-success-2",
            external_id=external_id,
            sequence=2,
            status="succeeded",
            occurred_at=occurred_at,
            connection_id=connection.id,
            correlation_id="phase6-webhook-1",
        )
        success = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=success_body,
            raw_headers=success_headers,
        )
        assert success.result == "applied"
        assert success.reconciliation_required is False

        regression_body, regression_headers = _signed_webhook(
            event_id="event-regression-3",
            external_id=external_id,
            sequence=3,
            status="running",
            occurred_at=occurred_at,
            connection_id=connection.id,
            correlation_id="phase6-webhook-1",
        )
        regression = await service.process_learning_ci_webhook(
            connection.id,
            raw_body=regression_body,
            raw_headers=regression_headers,
        )
        final_run = await service.get_provider_run(connection.id, pending.id)
        assert regression.result == "reconcile_required"
        assert regression.reconciliation_required is True
        assert final_run.status == "succeeded"
        assert final_run.last_provider_sequence == 2
    finally:
        await service.shutdown()
        await database.shutdown()
