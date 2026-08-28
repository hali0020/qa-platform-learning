"""Deterministic CI Lab application service."""

from __future__ import annotations

import hashlib
import hmac
import json
import re
import secrets
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from typing import Any
from uuid import UUID, uuid4

from sqlalchemy import and_, exists, func, insert, or_, select, update
from sqlalchemy.engine import RowMapping

from app.ci_lab.database import (
    CiLabDatabase,
    approvals,
    quality_gates,
    runs,
    webhook_deliveries,
    webhook_subscriptions,
)
from app.ci_lab.models import (
    ApprovalView,
    DefinitionView,
    GateDecision,
    GateDecisionRequest,
    JobView,
    QualityGateStatus,
    QualityGateView,
    RunStatus,
    RunView,
    StageView,
    TriggerRunRequest,
    WebhookDeliveryStatus,
    WebhookDeliveryView,
)
from app.ci_lab.registry import DefinitionRegistry, JobDefinition, PipelineDefinition


_IDEMPOTENCY_KEY = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_WORKER_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,199}\Z")
_ERROR_CODE = re.compile(r"[a-z][a-z0-9_]{0,99}\Z")
_QUALITY_GATE_POLICY_REVISIONS = {"local-quality-gate": 1}
_WEBHOOK_MAX_BODY_BYTES = 16 * 1024
_WEBHOOK_DEFAULT_MAX_ATTEMPTS = 8


class CiLabError(Exception):
    def __init__(self, message: str, *, status_code: int, code: str) -> None:
        super().__init__(message)
        self.message = message
        self.status_code = status_code
        self.code = code


class CiLabNotFound(CiLabError):
    def __init__(self, resource: str) -> None:
        super().__init__(
            f"{resource} was not found",
            status_code=404,
            code="ci_lab_not_found",
        )


class CiLabConflict(CiLabError):
    def __init__(self, message: str) -> None:
        super().__init__(message, status_code=409, code="ci_lab_conflict")


class CiLabValidationError(CiLabError):
    def __init__(self, message: str) -> None:
        super().__init__(
            message,
            status_code=422,
            code="ci_lab_validation_error",
        )


Clock = Callable[[], datetime]


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _aware(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _fingerprint(
    definition: PipelineDefinition,
    request: TriggerRunRequest,
) -> str:
    payload: dict[str, Any] = {
        "definition": definition.key,
        "definition_revision": definition.revision,
        "ref": request.ref,
        "variables": request.variables,
    }
    # Keep the pre-webhook fingerprint byte-for-byte compatible for ordinary
    # runs already persisted in a user's local database. The new routing
    # binding affects idempotency only when it is actually present.
    if request.webhook_connection_id is not None:
        payload["webhook_connection_id"] = str(request.webhook_connection_id)
        payload["correlation_id"] = request.correlation_id
    canonical = json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _approval_fingerprint(request: GateDecisionRequest) -> str:
    canonical = json.dumps(
        request.model_dump(mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class _JobTimeline:
    definition: JobDefinition
    start_ms: int
    finish_ms: int


@dataclass(frozen=True, slots=True)
class ClaimedWebhookDelivery:
    id: str
    run_id: str
    connection_id: str
    event_id: str
    raw_body: bytes = field(repr=False)
    worker_id: str
    lease_token: str = field(repr=False)
    version: int
    attempts: int
    max_attempts: int


class CiLabService:
    def __init__(
        self,
        database: CiLabDatabase,
        registry: DefinitionRegistry,
        *,
        clock: Clock = utc_now,
    ) -> None:
        if not registry:
            raise ValueError("CI Lab definition registry cannot be empty")
        self.database = database
        self.registry = registry
        self._clock = clock

    async def initialize(self) -> None:
        await self.database.initialize()

    async def close(self) -> None:
        await self.database.close()

    def list_definitions(self) -> list[DefinitionView]:
        return [self.registry[key].to_view() for key in sorted(self.registry)]

    async def advance_webhook_runs_once(self, *, limit: int = 100) -> int:
        """Advance subscribed runs and atomically persist changed snapshots."""

        if not 1 <= limit <= 500:
            raise CiLabValidationError("webhook refresh limit must be between 1 and 500")
        now = self._now()
        created = 0
        async with self.database.write() as connection:
            records = (
                await connection.execute(
                    select(runs)
                    .join(
                        webhook_subscriptions,
                        webhook_subscriptions.c.run_id == runs.c.id,
                    )
                    .outerjoin(
                        quality_gates,
                        quality_gates.c.run_id == runs.c.id,
                    )
                    .where(
                        runs.c.status.in_(
                            (RunStatus.QUEUED.value, RunStatus.RUNNING.value)
                        ),
                        or_(
                            quality_gates.c.run_id.is_(None),
                            quality_gates.c.status
                            != QualityGateStatus.WAITING_APPROVAL.value,
                        ),
                    )
                    .order_by(runs.c.created_at, runs.c.id)
                    .limit(limit)
                )
            ).mappings().all()
            for record in records:
                current, gate = await self._refresh_record(connection, record, now)
                if await self._enqueue_webhook_if_changed(
                    connection,
                    current,
                    gate,
                    now=now,
                ):
                    created += 1
        return created

    async def claim_webhook_delivery(
        self,
        worker_id: str,
        *,
        lease_seconds: int = 30,
    ) -> ClaimedWebhookDelivery | None:
        """Claim one due delivery without retaining a SQL lock during HTTP."""

        selected_worker = worker_id.strip()
        if _WORKER_ID.fullmatch(selected_worker) is None:
            raise CiLabValidationError("webhook worker_id is invalid")
        if not 5 <= lease_seconds <= 300:
            raise CiLabValidationError(
                "webhook lease_seconds must be between 5 and 300"
            )
        now = self._now()
        async with self.database.write() as connection:
            # A worker may die during its final permitted attempt. Convert that
            # expired lease into a visible dead letter before looking for work.
            await connection.execute(
                update(webhook_deliveries)
                .where(
                    webhook_deliveries.c.status
                    == WebhookDeliveryStatus.CLAIMED.value,
                    webhook_deliveries.c.lease_expires_at <= now,
                    webhook_deliveries.c.attempts
                    >= webhook_deliveries.c.max_attempts,
                )
                .values(
                    status=WebhookDeliveryStatus.DEAD_LETTER.value,
                    lease_owner=None,
                    lease_token_hash=None,
                    lease_expires_at=None,
                    last_error_code="lease_expired",
                    updated_at=now,
                    dead_lettered_at=now,
                    version=webhook_deliveries.c.version + 1,
                )
            )

            lower = webhook_deliveries.alias("lower_webhook_delivery")
            candidate = (
                await connection.execute(
                    select(webhook_deliveries)
                    .where(
                        or_(
                            and_(
                                webhook_deliveries.c.status.in_(
                                    (
                                        WebhookDeliveryStatus.PENDING.value,
                                        WebhookDeliveryStatus.RETRY_WAIT.value,
                                    )
                                ),
                                webhook_deliveries.c.available_at <= now,
                            ),
                            and_(
                                webhook_deliveries.c.status
                                == WebhookDeliveryStatus.CLAIMED.value,
                                webhook_deliveries.c.lease_expires_at <= now,
                            ),
                        ),
                        webhook_deliveries.c.attempts
                        < webhook_deliveries.c.max_attempts,
                        ~exists(
                            select(1).where(
                                lower.c.run_id == webhook_deliveries.c.run_id,
                                lower.c.sequence < webhook_deliveries.c.sequence,
                                lower.c.status
                                != WebhookDeliveryStatus.DELIVERED.value,
                            )
                        ),
                    )
                    .order_by(
                        webhook_deliveries.c.available_at,
                        webhook_deliveries.c.created_at,
                        webhook_deliveries.c.id,
                    )
                    .limit(1)
                )
            ).mappings().one_or_none()
            if candidate is None:
                return None

            raw_body = str(candidate["payload_body"]).encode("utf-8")
            digest = hashlib.sha256(raw_body).hexdigest()
            if (
                len(raw_body) > _WEBHOOK_MAX_BODY_BYTES
                or not hmac.compare_digest(str(candidate["body_sha256"]), digest)
            ):
                await connection.execute(
                    update(webhook_deliveries)
                    .where(webhook_deliveries.c.id == candidate["id"])
                    .values(
                        status=WebhookDeliveryStatus.DEAD_LETTER.value,
                        lease_owner=None,
                        lease_token_hash=None,
                        lease_expires_at=None,
                        last_error_code="payload_integrity_error",
                        updated_at=now,
                        dead_lettered_at=now,
                        version=int(candidate["version"]) + 1,
                    )
                )
                return None

            lease_token = secrets.token_urlsafe(32)
            next_version = int(candidate["version"]) + 1
            await connection.execute(
                update(webhook_deliveries)
                .where(
                    webhook_deliveries.c.id == candidate["id"],
                    webhook_deliveries.c.version == candidate["version"],
                )
                .values(
                    status=WebhookDeliveryStatus.CLAIMED.value,
                    attempts=int(candidate["attempts"]) + 1,
                    lease_owner=selected_worker,
                    lease_token_hash=hashlib.sha256(
                        lease_token.encode("ascii")
                    ).hexdigest(),
                    lease_expires_at=now + timedelta(seconds=lease_seconds),
                    last_error_code=None,
                    updated_at=now,
                    delivered_at=None,
                    dead_lettered_at=None,
                    version=next_version,
                )
            )
            return ClaimedWebhookDelivery(
                id=str(candidate["id"]),
                run_id=str(candidate["run_id"]),
                connection_id=str(candidate["connection_id"]),
                event_id=str(candidate["event_id"]),
                raw_body=raw_body,
                worker_id=selected_worker,
                lease_token=lease_token,
                version=next_version,
                attempts=int(candidate["attempts"]) + 1,
                max_attempts=int(candidate["max_attempts"]),
            )

    async def complete_webhook_delivery(
        self,
        claimed: ClaimedWebhookDelivery,
    ) -> WebhookDeliveryView:
        now = self._now()
        async with self.database.write() as connection:
            record = await self._require_webhook_delivery_lease(
                connection,
                claimed,
                now,
            )
            values = {
                "status": WebhookDeliveryStatus.DELIVERED.value,
                "lease_owner": None,
                "lease_token_hash": None,
                "lease_expires_at": None,
                "last_error_code": None,
                "updated_at": now,
                "delivered_at": now,
                "dead_lettered_at": None,
                "version": int(record["version"]) + 1,
            }
            await connection.execute(
                update(webhook_deliveries)
                .where(
                    webhook_deliveries.c.id == claimed.id,
                    webhook_deliveries.c.version == claimed.version,
                )
                .values(**values)
            )
            return self._webhook_delivery_view({**dict(record), **values})

    async def fail_webhook_delivery(
        self,
        claimed: ClaimedWebhookDelivery,
        *,
        error_code: str,
        retryable: bool,
    ) -> WebhookDeliveryView:
        if _ERROR_CODE.fullmatch(error_code) is None:
            raise CiLabValidationError("webhook delivery error code is invalid")
        now = self._now()
        async with self.database.write() as connection:
            record = await self._require_webhook_delivery_lease(
                connection,
                claimed,
                now,
            )
            should_retry = retryable and int(record["attempts"]) < int(
                record["max_attempts"]
            )
            values = {
                "status": (
                    WebhookDeliveryStatus.RETRY_WAIT.value
                    if should_retry
                    else WebhookDeliveryStatus.DEAD_LETTER.value
                ),
                "available_at": (
                    now
                    + timedelta(
                        seconds=min(300, 2 ** max(0, int(record["attempts"]) - 1))
                    )
                    if should_retry
                    else record["available_at"]
                ),
                "lease_owner": None,
                "lease_token_hash": None,
                "lease_expires_at": None,
                "last_error_code": error_code,
                "updated_at": now,
                "delivered_at": None,
                "dead_lettered_at": None if should_retry else now,
                "version": int(record["version"]) + 1,
            }
            await connection.execute(
                update(webhook_deliveries)
                .where(
                    webhook_deliveries.c.id == claimed.id,
                    webhook_deliveries.c.version == claimed.version,
                )
                .values(**values)
            )
            return self._webhook_delivery_view({**dict(record), **values})

    async def list_webhook_deliveries(
        self,
        *,
        status: WebhookDeliveryStatus | None = None,
        run_id: str | UUID | None = None,
        limit: int = 100,
    ) -> list[WebhookDeliveryView]:
        if not 1 <= limit <= 500:
            raise CiLabValidationError("webhook delivery limit must be between 1 and 500")
        statement = select(webhook_deliveries)
        if status is not None:
            statement = statement.where(webhook_deliveries.c.status == status.value)
        if run_id is not None:
            statement = statement.where(webhook_deliveries.c.run_id == str(run_id))
        statement = statement.order_by(
            webhook_deliveries.c.created_at.desc(),
            webhook_deliveries.c.id,
        ).limit(limit)
        async with self.database.read() as connection:
            records = (await connection.execute(statement)).mappings().all()
        return [self._webhook_delivery_view(record) for record in records]

    async def retry_webhook_delivery(
        self,
        delivery_id: str | UUID,
    ) -> WebhookDeliveryView:
        selected_id = str(delivery_id)
        now = self._now()
        async with self.database.write() as connection:
            record = (
                await connection.execute(
                    select(webhook_deliveries).where(
                        webhook_deliveries.c.id == selected_id
                    )
                )
            ).mappings().one_or_none()
            if record is None:
                raise CiLabNotFound("webhook delivery")
            current = WebhookDeliveryStatus(str(record["status"]))
            if current in {
                WebhookDeliveryStatus.PENDING,
                WebhookDeliveryStatus.RETRY_WAIT,
            }:
                return self._webhook_delivery_view(record)
            if current != WebhookDeliveryStatus.DEAD_LETTER:
                raise CiLabConflict(
                    "only a dead-letter webhook delivery can be retried"
                )
            values = {
                "status": WebhookDeliveryStatus.PENDING.value,
                "attempts": 0,
                "max_attempts": _WEBHOOK_DEFAULT_MAX_ATTEMPTS,
                "available_at": now,
                "lease_owner": None,
                "lease_token_hash": None,
                "lease_expires_at": None,
                "last_error_code": None,
                "updated_at": now,
                "delivered_at": None,
                "dead_lettered_at": None,
                "version": int(record["version"]) + 1,
            }
            await connection.execute(
                update(webhook_deliveries)
                .where(webhook_deliveries.c.id == selected_id)
                .values(**values)
            )
            return self._webhook_delivery_view({**dict(record), **values})

    async def trigger(
        self,
        definition_key: str,
        request: TriggerRunRequest,
        idempotency_key: str,
    ) -> RunView:
        definition = self._require_definition(definition_key)
        selected_key = self._validate_idempotency_key(idempotency_key)
        if (
            request.correlation_id is not None
            and not hmac.compare_digest(request.correlation_id, selected_key)
        ):
            raise CiLabValidationError(
                "correlation_id must match the Idempotency-Key header"
            )
        request_fingerprint = _fingerprint(definition, request)
        now = self._now()

        async with self.database.write() as connection:
            previous = (
                await connection.execute(
                    select(runs).where(runs.c.idempotency_key == selected_key)
                )
            ).mappings().one_or_none()
            if previous is not None:
                if not hmac.compare_digest(
                    str(previous["request_fingerprint"]),
                    request_fingerprint,
                ):
                    raise CiLabConflict(
                        "Idempotency-Key was reused with different trigger input"
                    )
                current, gate = await self._refresh_record(
                    connection,
                    previous,
                    now,
                )
                await self._enqueue_webhook_if_changed(
                    connection,
                    current,
                    gate,
                    now=now,
                )
                return await self._run_view(
                    connection,
                    current,
                    definition,
                    now,
                    gate=gate,
                    replayed=True,
                )

            run_id = str(uuid4())
            await connection.execute(
                insert(runs).values(
                    id=run_id,
                    definition=definition.key,
                    definition_revision=definition.revision,
                    ref=request.ref,
                    variables=dict(request.variables),
                    idempotency_key=selected_key,
                    request_fingerprint=request_fingerprint,
                    status=RunStatus.QUEUED.value,
                    message=None,
                    created_at=now,
                    updated_at=now,
                    started_at=None,
                    finished_at=None,
                    cancelled_at=None,
                )
            )
            policy_revision = _QUALITY_GATE_POLICY_REVISIONS.get(definition.key)
            if policy_revision is not None:
                await connection.execute(
                    insert(quality_gates).values(
                        run_id=run_id,
                        policy_revision=policy_revision,
                        status=QualityGateStatus.EVALUATING.value,
                        reached_at=None,
                        decided_at=None,
                        updated_at=now,
                    )
                )
            if request.webhook_connection_id is not None:
                await connection.execute(
                    insert(webhook_subscriptions).values(
                        run_id=run_id,
                        connection_id=str(request.webhook_connection_id),
                        correlation_id=request.correlation_id,
                        created_at=now,
                    )
                )
            created = (
                await connection.execute(select(runs).where(runs.c.id == run_id))
            ).mappings().one()
            gate = await self._quality_gate_record(connection, run_id)
            await self._enqueue_webhook_if_changed(
                connection,
                created,
                gate,
                now=now,
            )
            return await self._run_view(
                connection,
                created,
                definition,
                now,
                gate=gate,
                replayed=False,
            )

    async def get(self, run_id: str | UUID) -> RunView:
        selected_id = str(run_id)
        now = self._now()
        async with self.database.write() as connection:
            record = (
                await connection.execute(select(runs).where(runs.c.id == selected_id))
            ).mappings().one_or_none()
            if record is None:
                raise CiLabNotFound("run")
            definition = self._definition_for_record(record)
            current, gate = await self._refresh_record(connection, record, now)
            await self._enqueue_webhook_if_changed(
                connection,
                current,
                gate,
                now=now,
            )
            return await self._run_view(
                connection,
                current,
                definition,
                now,
                gate=gate,
                replayed=False,
            )

    async def cancel(self, run_id: str | UUID) -> RunView:
        selected_id = str(run_id)
        now = self._now()
        async with self.database.write() as connection:
            record = (
                await connection.execute(select(runs).where(runs.c.id == selected_id))
            ).mappings().one_or_none()
            if record is None:
                raise CiLabNotFound("run")
            definition = self._definition_for_record(record)
            current, gate = await self._refresh_record(connection, record, now)
            status = RunStatus(str(current["status"]))
            if status == RunStatus.CANCELLED:
                await self._enqueue_webhook_if_changed(
                    connection,
                    current,
                    gate,
                    now=now,
                )
                return await self._run_view(
                    connection,
                    current,
                    definition,
                    now,
                    gate=gate,
                    replayed=True,
                )
            if status.is_terminal:
                raise CiLabConflict(f"a {status.value} run cannot be cancelled")

            cancelled_at = max(
                now,
                _aware(gate["reached_at"])
                if gate is not None and gate["reached_at"] is not None
                else now,
            )
            created_at = _aware(current["created_at"])
            started_at = (
                _aware(current["started_at"])
                if current["started_at"] is not None
                else (
                    created_at + timedelta(milliseconds=definition.queue_delay_ms)
                    if cancelled_at
                    >= created_at + timedelta(milliseconds=definition.queue_delay_ms)
                    else None
                )
            )
            values = {
                "status": RunStatus.CANCELLED.value,
                "message": "cancelled by an authenticated machine request",
                "updated_at": cancelled_at,
                "started_at": started_at,
                "finished_at": cancelled_at,
                "cancelled_at": cancelled_at,
            }
            await connection.execute(
                update(runs).where(runs.c.id == selected_id).values(**values)
            )
            cancelled = {**dict(current), **values}
            if gate is not None:
                gate_values = {
                    "status": QualityGateStatus.CANCELLED.value,
                    "decided_at": cancelled_at,
                    "updated_at": cancelled_at,
                }
                await connection.execute(
                    update(quality_gates)
                    .where(quality_gates.c.run_id == selected_id)
                    .values(**gate_values)
                )
                gate = {**dict(gate), **gate_values}
            await self._enqueue_webhook_if_changed(
                connection,
                cancelled,
                gate,
                now=cancelled_at,
            )
            return await self._run_view(
                connection,
                cancelled,
                definition,
                now,
                gate=gate,
                replayed=False,
            )

    async def decide_gate(
        self,
        run_id: str | UUID,
        request: GateDecisionRequest,
    ) -> RunView:
        """Append one idempotent decision and atomically release or reject a run."""

        selected_id = str(run_id)
        fingerprint = _approval_fingerprint(request)
        now = self._now()
        async with self.database.write() as connection:
            record = (
                await connection.execute(select(runs).where(runs.c.id == selected_id))
            ).mappings().one_or_none()
            if record is None:
                raise CiLabNotFound("run")
            definition = self._definition_for_record(record)
            current, gate = await self._refresh_record(connection, record, now)
            if gate is None:
                raise CiLabConflict("this run has no quality approval gate")

            previous = (
                await connection.execute(
                    select(approvals).where(
                        approvals.c.run_id == selected_id,
                        approvals.c.event_id == request.event_id,
                    )
                )
            ).mappings().one_or_none()
            if previous is not None:
                if not hmac.compare_digest(
                    str(previous["request_fingerprint"]),
                    fingerprint,
                ):
                    raise CiLabConflict(
                        "approval event_id was reused with different input"
                    )
                await self._enqueue_webhook_if_changed(
                    connection,
                    current,
                    gate,
                    now=now,
                )
                return await self._run_view(
                    connection,
                    current,
                    definition,
                    now,
                    gate=gate,
                    replayed=True,
                )

            existing_decision = (
                await connection.execute(
                    select(approvals.c.id)
                    .where(approvals.c.run_id == selected_id)
                    .limit(1)
                )
            ).scalar_one_or_none()
            if existing_decision is not None:
                raise CiLabConflict("the quality gate already has a decision")
            if QualityGateStatus(str(gate["status"])) != (
                QualityGateStatus.WAITING_APPROVAL
            ):
                raise CiLabConflict(
                    "quality checks must pass before the gate can be decided"
                )

            decision_at = max(
                now,
                _aware(gate["reached_at"])
                if gate["reached_at"] is not None
                else now,
            )

            approval_id = str(uuid4())
            await connection.execute(
                insert(approvals).values(
                    id=approval_id,
                    run_id=selected_id,
                    event_id=request.event_id,
                    request_fingerprint=fingerprint,
                    decision=request.decision.value,
                    actor_id=request.actor_id,
                    actor_name=request.actor_name,
                    comment=request.comment,
                    created_at=decision_at,
                )
            )

            approved = request.decision == GateDecision.APPROVE
            gate_values = {
                "status": (
                    QualityGateStatus.APPROVED.value
                    if approved
                    else QualityGateStatus.REJECTED.value
                ),
                "decided_at": decision_at,
                "updated_at": decision_at,
            }
            run_values = {
                "status": (
                    RunStatus.SUCCEEDED.value if approved else RunStatus.FAILED.value
                ),
                "message": (
                    "quality gate approved"
                    if approved
                    else "quality gate rejected"
                ),
                "updated_at": decision_at,
                "finished_at": decision_at,
            }
            await connection.execute(
                update(quality_gates)
                .where(quality_gates.c.run_id == selected_id)
                .values(**gate_values)
            )
            await connection.execute(
                update(runs)
                .where(runs.c.id == selected_id)
                    .values(**run_values)
            )
            final_run = {**dict(current), **run_values}
            final_gate = {**dict(gate), **gate_values}
            await self._enqueue_webhook_if_changed(
                connection,
                final_run,
                final_gate,
                now=decision_at,
            )
            return await self._run_view(
                connection,
                final_run,
                definition,
                now,
                gate=final_gate,
                replayed=False,
            )

    async def _refresh_record(
        self,
        connection,
        record: RowMapping,
        now: datetime,
    ) -> tuple[dict[str, Any], RowMapping | dict[str, Any] | None]:
        definition = self._definition_for_record(record)
        current = dict(record)
        gate = await self._quality_gate_record(connection, str(current["id"]))
        if current["cancelled_at"] is not None:
            return current, gate
        current_status = RunStatus(str(current["status"]))
        if current_status.is_terminal:
            return current, gate

        if gate is not None and QualityGateStatus(str(gate["status"])) == (
            QualityGateStatus.WAITING_APPROVAL
        ):
            # This is the non-bypassable pause: neither elapsed wall time nor
            # a read/restart can release the run. Only decide_gate may do so.
            if (
                current_status != RunStatus.RUNNING
                or current["message"]
                != "quality checks passed; waiting for approval"
            ):
                values = {
                    # Keep the private legacy run column within its original
                    # CHECK constraint. The durable gate row is the source of
                    # truth and _run_view exposes waiting_approval publicly.
                    "status": RunStatus.RUNNING.value,
                    "message": "quality checks passed; waiting for approval",
                    "updated_at": now,
                    "finished_at": None,
                }
                await connection.execute(
                    update(runs).where(runs.c.id == current["id"]).values(**values)
                )
                current.update(values)
            return current, gate

        created_at = _aware(current["created_at"])
        elapsed_ms = max(0, int((now - created_at).total_seconds() * 1000))
        terminal_ms, terminal_status, terminal_message = self._terminal_point(definition)
        started_at: datetime | None = None
        finished_at: datetime | None = None
        status = RunStatus.QUEUED
        message: str | None = None
        gate_reached = False

        if elapsed_ms >= definition.queue_delay_ms:
            started_at = created_at + timedelta(milliseconds=definition.queue_delay_ms)
            status = RunStatus.RUNNING
        terminal_at = created_at + timedelta(milliseconds=terminal_ms)
        if elapsed_ms >= terminal_ms:
            if gate is not None and terminal_status == RunStatus.SUCCEEDED:
                status = RunStatus.RUNNING
                message = "quality checks passed; waiting for approval"
                finished_at = None
                gate_reached = True
            else:
                status = terminal_status
                message = terminal_message
                finished_at = terminal_at

        # Wall clocks may be adjusted backwards. Once observed, a running
        # state cannot become queued again; terminal states returned above are
        # immutable. This also keeps injected teaching clocks deterministic.
        if current_status == RunStatus.RUNNING and status == RunStatus.QUEUED:
            status = RunStatus.RUNNING
            message = current["message"]
            started_at = _aware(current["started_at"])
            finished_at = None

        if (
            str(current["status"]) != status.value
            or (
                _aware(current["started_at"])
                if current["started_at"] is not None
                else None
            )
            != started_at
            or (
                _aware(current["finished_at"])
                if current["finished_at"] is not None
                else None
            )
            != finished_at
            or current["message"] != message
        ):
            values = {
                "status": status.value,
                "message": message,
                "updated_at": now,
                "started_at": started_at,
                "finished_at": finished_at,
            }
            await connection.execute(
                update(runs).where(runs.c.id == current["id"]).values(**values)
            )
            current.update(values)

        if gate is not None:
            current_gate_status = QualityGateStatus(str(gate["status"]))
            gate_values: dict[str, Any] = {}
            if (
                current_gate_status == QualityGateStatus.EVALUATING
                and gate_reached
            ):
                gate_values = {
                    "status": QualityGateStatus.WAITING_APPROVAL.value,
                    "reached_at": terminal_at,
                    "updated_at": now,
                }
            elif (
                current_gate_status == QualityGateStatus.EVALUATING
                and status == RunStatus.FAILED
            ):
                gate_values = {
                    "status": QualityGateStatus.FAILED.value,
                    "updated_at": now,
                }
            if gate_values:
                await connection.execute(
                    update(quality_gates)
                    .where(quality_gates.c.run_id == current["id"])
                    .values(**gate_values)
                )
                gate = {**dict(gate), **gate_values}
        return current, gate

    async def _enqueue_webhook_if_changed(
        self,
        connection,
        record: RowMapping | dict[str, Any],
        gate: RowMapping | dict[str, Any] | None,
        *,
        now: datetime,
    ) -> bool:
        values = dict(record)
        run_id = str(values["id"])
        subscription = (
            await connection.execute(
                select(webhook_subscriptions).where(
                    webhook_subscriptions.c.run_id == run_id
                )
            )
        ).mappings().one_or_none()
        if subscription is None:
            return False

        latest = (
            await connection.execute(
                select(webhook_deliveries)
                .where(webhook_deliveries.c.run_id == run_id)
                .order_by(webhook_deliveries.c.sequence.desc())
                .limit(1)
            )
        ).mappings().one_or_none()
        public_status = self._public_run_status(values, gate)
        message = values["message"]
        if latest is not None and (
            str(latest["normalized_status"]) == public_status.value
            and latest["message"] == message
        ):
            return False

        sequence = 1 if latest is None else int(latest["sequence"]) + 1
        occurred_at = _aware(values["updated_at"])
        if gate is not None and gate["updated_at"] is not None:
            occurred_at = max(occurred_at, _aware(gate["updated_at"]))
        if latest is not None:
            occurred_at = max(occurred_at, _aware(latest["occurred_at"]))
        occurred_at = max(occurred_at, _aware(now))
        payload = {
            "connection_id": str(subscription["connection_id"]),
            "correlation_id": str(subscription["correlation_id"]),
            "external_id": run_id,
            "message": message,
            "occurred_at": occurred_at.isoformat(),
            "sequence": sequence,
            "status": public_status.value,
        }
        raw_body = json.dumps(
            payload,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(raw_body) > _WEBHOOK_MAX_BODY_BYTES:
            raise RuntimeError("CI Lab generated an oversized webhook body")
        event_id = f"ci-lab-{uuid4().hex}"
        await connection.execute(
            insert(webhook_deliveries).values(
                id=str(uuid4()),
                run_id=run_id,
                connection_id=str(subscription["connection_id"]),
                event_id=event_id,
                sequence=sequence,
                occurred_at=occurred_at,
                normalized_status=public_status.value,
                message=message,
                payload_body=raw_body.decode("utf-8"),
                body_sha256=hashlib.sha256(raw_body).hexdigest(),
                status=WebhookDeliveryStatus.PENDING.value,
                attempts=0,
                max_attempts=_WEBHOOK_DEFAULT_MAX_ATTEMPTS,
                available_at=_aware(now),
                lease_owner=None,
                lease_token_hash=None,
                lease_expires_at=None,
                last_error_code=None,
                version=0,
                created_at=_aware(now),
                updated_at=_aware(now),
                delivered_at=None,
                dead_lettered_at=None,
            )
        )
        return True

    @staticmethod
    def _public_run_status(
        record: RowMapping | dict[str, Any],
        gate: RowMapping | dict[str, Any] | None,
    ) -> RunStatus:
        if gate is not None and QualityGateStatus(str(gate["status"])) == (
            QualityGateStatus.WAITING_APPROVAL
        ):
            return RunStatus.WAITING_APPROVAL
        return RunStatus(str(record["status"]))

    async def _require_webhook_delivery_lease(
        self,
        connection,
        claimed: ClaimedWebhookDelivery,
        now: datetime,
    ) -> RowMapping:
        record = (
            await connection.execute(
                select(webhook_deliveries).where(
                    webhook_deliveries.c.id == claimed.id
                )
            )
        ).mappings().one_or_none()
        expected_hash = hashlib.sha256(
            claimed.lease_token.encode("ascii")
        ).hexdigest()
        if record is None:
            raise CiLabNotFound("webhook delivery")
        if (
            str(record["status"]) != WebhookDeliveryStatus.CLAIMED.value
            or str(record["lease_owner"]) != claimed.worker_id
            or record["lease_token_hash"] is None
            or not hmac.compare_digest(str(record["lease_token_hash"]), expected_hash)
            or record["lease_expires_at"] is None
            or _aware(record["lease_expires_at"]) <= _aware(now)
            or int(record["version"]) != claimed.version
        ):
            raise CiLabConflict("webhook delivery lease is invalid or expired")
        return record

    @staticmethod
    def _webhook_delivery_view(
        record: RowMapping | dict[str, Any],
    ) -> WebhookDeliveryView:
        values = dict(record)
        return WebhookDeliveryView(
            id=str(values["id"]),
            run_id=str(values["run_id"]),
            connection_id=UUID(str(values["connection_id"])),
            event_id=str(values["event_id"]),
            sequence=int(values["sequence"]),
            occurred_at=_aware(values["occurred_at"]),
            normalized_status=RunStatus(str(values["normalized_status"])),
            status=WebhookDeliveryStatus(str(values["status"])),
            attempts=int(values["attempts"]),
            max_attempts=int(values["max_attempts"]),
            available_at=_aware(values["available_at"]),
            lease_owner=(
                str(values["lease_owner"])
                if values["lease_owner"] is not None
                else None
            ),
            lease_expires_at=(
                _aware(values["lease_expires_at"])
                if values["lease_expires_at"] is not None
                else None
            ),
            last_error_code=(
                str(values["last_error_code"])
                if values["last_error_code"] is not None
                else None
            ),
            version=int(values["version"]),
            created_at=_aware(values["created_at"]),
            updated_at=_aware(values["updated_at"]),
            delivered_at=(
                _aware(values["delivered_at"])
                if values["delivered_at"] is not None
                else None
            ),
            dead_lettered_at=(
                _aware(values["dead_lettered_at"])
                if values["dead_lettered_at"] is not None
                else None
            ),
        )

    @staticmethod
    async def _quality_gate_record(connection, run_id: str) -> RowMapping | None:
        return (
            await connection.execute(
                select(quality_gates).where(quality_gates.c.run_id == run_id)
            )
        ).mappings().one_or_none()

    async def _run_view(
        self,
        connection,
        record: RowMapping | dict[str, Any],
        definition: PipelineDefinition,
        now: datetime,
        *,
        gate: RowMapping | dict[str, Any] | None,
        replayed: bool,
    ) -> RunView:
        values = dict(record)
        created_at = _aware(values["created_at"])
        cancelled_at = (
            _aware(values["cancelled_at"])
            if values["cancelled_at"] is not None
            else None
        )
        status = RunStatus(str(values["status"]))
        public_status = self._public_run_status(values, gate)
        started_at = (
            _aware(values["started_at"])
            if values["started_at"] is not None
            else None
        )
        finished_at = (
            _aware(values["finished_at"])
            if values["finished_at"] is not None
            else None
        )
        observation_time = cancelled_at or now
        if status.is_terminal and finished_at is not None:
            observation_time = max(observation_time, finished_at)
        elif status == RunStatus.RUNNING and started_at is not None:
            observation_time = max(observation_time, started_at)
        elapsed_ms = max(
            0,
            int((observation_time - created_at).total_seconds() * 1000),
        )
        stages = self._stage_views(
            definition,
            created_at=created_at,
            elapsed_ms=elapsed_ms,
            cancelled_at=cancelled_at,
        )
        approval_rows = (
            await connection.execute(
                select(approvals)
                .where(approvals.c.run_id == str(values["id"]))
                .order_by(approvals.c.created_at, approvals.c.id)
            )
        ).mappings().all()
        approval_views = [
            ApprovalView(
                id=str(item["id"]),
                event_id=str(item["event_id"]),
                decision=GateDecision(str(item["decision"])),
                actor_id=str(item["actor_id"]),
                actor_name=str(item["actor_name"]),
                comment=str(item["comment"]),
                created_at=_aware(item["created_at"]),
            )
            for item in approval_rows
        ]
        webhook_sequence = int(
            (
                await connection.execute(
                    select(func.coalesce(func.max(webhook_deliveries.c.sequence), 0))
                    .where(webhook_deliveries.c.run_id == str(values["id"]))
                )
            ).scalar_one()
        )
        if gate is None:
            quality_gate = QualityGateView(
                required=False,
                status=QualityGateStatus.NOT_REQUIRED,
                policy_revision=None,
                reached_at=None,
                decided_at=None,
            )
        else:
            gate_values = dict(gate)
            quality_gate = QualityGateView(
                required=True,
                status=QualityGateStatus(str(gate_values["status"])),
                policy_revision=int(gate_values["policy_revision"]),
                reached_at=(
                    _aware(gate_values["reached_at"])
                    if gate_values["reached_at"] is not None
                    else None
                ),
                decided_at=(
                    _aware(gate_values["decided_at"])
                    if gate_values["decided_at"] is not None
                    else None
                ),
            )
        metadata = {
            "definition": definition.key,
            "definition_revision": definition.revision,
            "ref": values["ref"],
            "stage_count": len(definition.stages),
            "deterministic": True,
        }
        if webhook_sequence > 0:
            metadata["webhook_sequence"] = webhook_sequence
        return RunView(
            id=str(values["id"]),
            definition=definition.key,
            definition_revision=definition.revision,
            status=public_status,
            web_url=None,
            message=values["message"],
            metadata=metadata,
            created_at=created_at,
            updated_at=_aware(values["updated_at"]),
            started_at=started_at,
            finished_at=finished_at,
            stages=stages,
            quality_gate=quality_gate,
            approvals=approval_views,
            replayed=replayed,
        )

    def _stage_views(
        self,
        definition: PipelineDefinition,
        *,
        created_at: datetime,
        elapsed_ms: int,
        cancelled_at: datetime | None,
    ) -> list[StageView]:
        timeline = self._timeline(definition)
        first_failure = next(
            (item for item in timeline if item.definition.should_fail),
            None,
        )
        failed = first_failure is not None and elapsed_ms >= first_failure.finish_ms
        by_key: dict[str, JobView] = {}

        for item in timeline:
            start_at = created_at + timedelta(milliseconds=item.start_ms)
            finish_at = created_at + timedelta(milliseconds=item.finish_ms)
            status = RunStatus.QUEUED
            actual_started: datetime | None = None
            actual_finished: datetime | None = None
            message: str | None = None

            blocked_by_failure = (
                failed
                and first_failure is not None
                and item.start_ms >= first_failure.finish_ms
            )
            if blocked_by_failure:
                status = RunStatus.CANCELLED
                actual_finished = created_at + timedelta(
                    milliseconds=first_failure.finish_ms
                )
                message = "cancelled after an earlier fixed job failed"
            elif cancelled_at is not None and elapsed_ms < item.finish_ms:
                status = RunStatus.CANCELLED
                if elapsed_ms >= item.start_ms:
                    actual_started = start_at
                actual_finished = cancelled_at
                message = "cancelled by machine request"
            elif elapsed_ms >= item.finish_ms:
                actual_started = start_at
                actual_finished = finish_at
                if item.definition.should_fail:
                    status = RunStatus.FAILED
                    message = "failed by the immutable local teaching definition"
                else:
                    status = RunStatus.SUCCEEDED
            elif elapsed_ms >= item.start_ms:
                status = RunStatus.RUNNING
                actual_started = start_at

            by_key[item.definition.key] = JobView(
                key=item.definition.key,
                name=item.definition.name,
                status=status,
                duration_ms=item.definition.duration_ms,
                started_at=actual_started,
                finished_at=actual_finished,
                message=message,
            )

        stages: list[StageView] = []
        for stage in definition.stages:
            jobs = [by_key[job.key] for job in stage.jobs]
            statuses = {job.status for job in jobs}
            if RunStatus.FAILED in statuses:
                stage_status = RunStatus.FAILED
            elif statuses == {RunStatus.SUCCEEDED}:
                stage_status = RunStatus.SUCCEEDED
            elif RunStatus.CANCELLED in statuses:
                stage_status = RunStatus.CANCELLED
            elif RunStatus.RUNNING in statuses or RunStatus.SUCCEEDED in statuses:
                stage_status = RunStatus.RUNNING
            else:
                stage_status = RunStatus.QUEUED

            started_values = [job.started_at for job in jobs if job.started_at is not None]
            finished_values = [
                job.finished_at for job in jobs if job.finished_at is not None
            ]
            stages.append(
                StageView(
                    key=stage.key,
                    name=stage.name,
                    status=stage_status,
                    jobs=jobs,
                    started_at=min(started_values) if started_values else None,
                    finished_at=(
                        max(finished_values)
                        if stage_status.is_terminal and finished_values
                        else None
                    ),
                    message=(
                        "stage failed"
                        if stage_status == RunStatus.FAILED
                        else "stage was cancelled"
                        if stage_status == RunStatus.CANCELLED
                        else None
                    ),
                )
            )
        return stages

    @staticmethod
    def _timeline(definition: PipelineDefinition) -> list[_JobTimeline]:
        offset = definition.queue_delay_ms
        values: list[_JobTimeline] = []
        for stage in definition.stages:
            for job in stage.jobs:
                values.append(
                    _JobTimeline(
                        definition=job,
                        start_ms=offset,
                        finish_ms=offset + job.duration_ms,
                    )
                )
                offset += job.duration_ms
        return values

    def _terminal_point(
        self,
        definition: PipelineDefinition,
    ) -> tuple[int, RunStatus, str]:
        timeline = self._timeline(definition)
        failure = next((item for item in timeline if item.definition.should_fail), None)
        if failure is not None:
            return (
                failure.finish_ms,
                RunStatus.FAILED,
                f"fixed job {failure.definition.key} failed",
            )
        return (
            timeline[-1].finish_ms,
            RunStatus.SUCCEEDED,
            "completed by the deterministic local executor",
        )

    def _require_definition(self, definition_key: str) -> PipelineDefinition:
        try:
            return self.registry[definition_key]
        except KeyError as error:
            raise CiLabNotFound("definition") from error

    def _definition_for_record(self, record: RowMapping) -> PipelineDefinition:
        definition = self._require_definition(str(record["definition"]))
        if int(record["definition_revision"]) != definition.revision:
            raise CiLabConflict(
                "the immutable definition revision for this run is unavailable"
            )
        return definition

    @staticmethod
    def _validate_idempotency_key(value: str) -> str:
        if _IDEMPOTENCY_KEY.fullmatch(value) is None:
            raise CiLabValidationError(
                "Idempotency-Key must contain 1-200 safe ASCII characters"
            )
        return value

    def _now(self) -> datetime:
        value = self._clock()
        if value.tzinfo is None:
            raise RuntimeError("CI Lab clock must return a timezone-aware datetime")
        return value.astimezone(timezone.utc)


__all__ = [
    "ClaimedWebhookDelivery",
    "CiLabConflict",
    "CiLabError",
    "CiLabNotFound",
    "CiLabService",
    "CiLabValidationError",
    "Clock",
    "utc_now",
]
