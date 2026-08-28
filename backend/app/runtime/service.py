"""Durable, local-first integration and automation learning service.

Every state transition is persisted in SQL. SQLite keeps its process-local
teaching lock; PostgreSQL workers, schedulers and outbox dispatchers claim
leases with row locks and ``SKIP LOCKED``. Schedule planning and broker I/O
run outside database transactions; token/version CAS authorizes finalization.
The task database remains authoritative and RabbitMQ carries no task data.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import secrets
import time
from collections import Counter
from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any, Protocol
from uuid import uuid4

from pydantic import ValidationError
from sqlalchemy import and_, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from app.broker.base import WakeupPublisher
from app.automation.cron import CronExpression
from app.automation.errors import AutomationValidationError
from app.automation.models import (
    DeviceLeaseStatus,
    DeviceStatus,
    MisfirePolicy,
    OverlapPolicy,
    ScheduleFireStatus,
    TaskStatus,
)
from app.core.config import (
    CI_LAB_CONTAINER_ADDRESS,
    CI_LAB_CONTAINER_BASE_URL,
    CI_LAB_HOST_ADDRESS,
    CI_LAB_HOST_BASE_URL,
    CI_LAB_PROVIDER_SECRET_NAME,
    CI_LAB_WEBHOOK_SECRET_NAME,
    PROVIDER_RUNTIME_MODES,
    _is_loopback_host,
    _is_loopback_network,
)
from app.core.errors import (
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    InvalidStateError,
    NotFoundError,
)
from app.core.actor import get_current_actor
from app.database.session import Database
from app.pipeline.providers.base import PipelineProvider
from app.pipeline.providers.bkci import BkCiPipelineProvider
from app.pipeline.providers.errors import (
    ProviderConfigurationError,
    ProviderConflictError,
    ProviderDisabledError,
    ProviderError,
    ProviderSecurityError,
)
from app.pipeline.providers.gitlab import GitLabPipelineProvider
from app.pipeline.providers.jenkins import JenkinsPipelineProvider
from app.pipeline.providers.learning_ci import LearningCiPipelineProvider
from app.pipeline.providers.models import (
    ProviderGateDecision,
    ProviderGateDecisionRequest,
    ProviderKind,
    ProviderQualityGateStatus,
    ProviderRun,
    ProviderTriggerRequest,
)
from app.pipeline.providers.security import OutboundPolicy
from app.runtime.orm import (
    AutomationTaskRecord,
    AutomationTaskWakeupOutboxRecord,
    DeviceLeaseRecord,
    DeviceRecord,
    ProviderConnectionRecord,
    ProviderRunApprovalRecord,
    ProviderRunRecord,
    ProviderTriggerIntentRecord,
    ProviderWebhookEventRecord,
    ScheduleFireRecord,
    ScheduleRecord,
)
from app.runtime.repository import RuntimeRepository
from app.runtime.schemas import (
    ClaimedDeviceView,
    ClaimedTaskView,
    DeviceAcquire,
    DeviceCreate,
    DeviceLeaseView,
    DevicePatch,
    DeviceView,
    ProviderConnectionCreate,
    ProviderConnectionPatch,
    ProviderConnectionView,
    ProviderRunApprovalPayload,
    ProviderRunApprovalView,
    ProviderRunView,
    ProviderTestResult,
    ProviderTriggerPayload,
    ProviderTriggerIntentView,
    ProviderWebhookPayload,
    ProviderWebhookResult,
    ScheduleCreate,
    ScheduleFireView,
    SchedulePatch,
    ScheduleView,
    TaskEnqueue,
    TaskView,
    TaskWakeupOutboxView,
)
from app.secrets import (
    EnvironmentSecretStore,
    SecretStore,
    SecretStoreError,
)
from app.runtime.webhook_security import (
    WebhookSecurityError,
    WebhookVerifier,
)


DEFAULT_TASK_TYPES = frozenset(
    {
        "qa.import.validate",
        "qa.pipeline.poll",
        "qa.quality.generate",
        "qa.device.execute",
    }
)
SCHEDULE_TICK_BATCH_LIMIT = 100

logger = logging.getLogger("qa.runtime")


def _utc_now() -> datetime:
    return datetime.now(timezone.utc)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _aware(value: datetime) -> datetime:
    normalized = _utc(value)
    assert normalized is not None
    return normalized


def _json_fingerprint(value: object) -> str:
    try:
        encoded = json.dumps(
            value, ensure_ascii=False, separators=(",", ":"), sort_keys=True
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise BusinessValidationError("任务数据必须是可序列化的 JSON") from error
    if len(encoded) > 262_144:
        raise BusinessValidationError("任务数据不能超过 256 KiB")
    return hashlib.sha256(encoded).hexdigest()


def _token_hash(value: str) -> str:
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def _safe_provider_error(error: ProviderError) -> Exception:
    if isinstance(error, ProviderConflictError):
        return ConflictError(str(error))
    if isinstance(error, (ProviderSecurityError, ProviderDisabledError)):
        return AuthorizationError(str(error))
    return BusinessValidationError(str(error))


def _parse_cron(value: str) -> CronExpression:
    try:
        return CronExpression.parse(value)
    except AutomationValidationError as error:
        raise BusinessValidationError(str(error)) from error


async def _next_cron_after(
    expression: CronExpression, instant: datetime, timezone_name: str
) -> datetime:
    try:
        return await asyncio.to_thread(
            expression.next_after, instant, timezone_name
        )
    except AutomationValidationError as error:
        raise BusinessValidationError(str(error)) from error


@dataclass(frozen=True, slots=True)
class _CronDuePlan:
    """A bounded scheduler decision calculated outside the event loop."""

    selected: tuple[datetime, ...]
    skipped: tuple[datetime, ...]
    latest_due: datetime
    next_run_at: datetime


@dataclass(frozen=True, slots=True)
class _ClaimedSchedule:
    """Immutable input captured by the short schedule-claim transaction."""

    id: str
    task_type: str
    payload: dict[str, Any]
    queue: str
    priority: int
    max_attempts: int
    cron: str
    timezone: str
    misfire_policy: str
    overlap_policy: str
    misfire_grace_seconds: int
    catch_up_limit: int
    first_due: datetime
    claimed_at: datetime
    expected_version: int
    scheduler_id: str
    claim_token: str


@dataclass(frozen=True, slots=True)
class _ClaimedTaskWakeup:
    """Lease proof retained only while one content-free hint is published."""

    outbox_id: str
    dispatcher_id: str
    lease_token: str
    expected_version: int
    publish_attempts: int


def _cron_due_plan(
    expression: CronExpression,
    first_due: datetime,
    now: datetime,
    timezone_name: str,
    policy: MisfirePolicy,
    misfire_grace_seconds: int,
    catch_up_limit: int,
) -> _CronDuePlan:
    """Plan missed occurrences without enumerating an unbounded backlog.

    ``fire_once`` searches only for the latest occurrence. ``catch_up`` walks
    backwards at most its configured limit (100). ``skip`` examines at most
    the one-day grace window (1,441 possible minute slots) and records one
    representative skipped occurrence for older backlog.
    """

    latest = expression.previous_at_or_before(now, timezone_name)
    next_run = expression.next_after(now, timezone_name)
    if latest < first_due:
        raise BusinessValidationError("定时任务 next_run_at 与 cron 规则不一致")

    if policy == MisfirePolicy.FIRE_ONCE:
        return _CronDuePlan((latest,), (), latest, next_run)

    if policy == MisfirePolicy.CATCH_UP_LIMITED:
        descending: list[datetime] = []
        cursor = latest
        while cursor >= first_due and len(descending) < catch_up_limit:
            descending.append(cursor)
            if len(descending) >= catch_up_limit or cursor == first_due:
                break
            cursor = expression.previous_at_or_before(
                cursor - timedelta(microseconds=1), timezone_name
            )
        return _CronDuePlan(
            tuple(reversed(descending)),
            (),
            latest,
            next_run,
        )

    cutoff = now - timedelta(seconds=misfire_grace_seconds)
    lower_bound = max(first_due, cutoff)
    selected: list[datetime] = []
    cursor = expression.next_after(
        lower_bound - timedelta(microseconds=1), timezone_name
    )
    # A five-field cron can fire at most once per minute. The schema caps the
    # grace window at one day, so this bound is independent of total downtime.
    while cursor <= now:
        selected.append(cursor)
        if len(selected) > 1_441:
            raise BusinessValidationError("定时任务宽限窗口超过安全上限")
        cursor = expression.next_after(cursor, timezone_name)

    skipped: tuple[datetime, ...] = ()
    if first_due < lower_bound:
        representative = expression.previous_at_or_before(
            lower_bound - timedelta(microseconds=1), timezone_name
        )
        if representative >= first_due:
            skipped = (representative,)
    return _CronDuePlan(tuple(selected), skipped, latest, next_run)


@dataclass(frozen=True, slots=True)
class RuntimeSafetyConfig:
    app_env: str = "local"
    provider_runtime_mode: str = "local_lab"
    provider_self_hosted_ownership_acknowledged: bool = False
    provider_allowed_hosts: tuple[str, ...] = ()
    provider_allowed_ports: tuple[int, ...] = (443,)
    provider_allowed_networks: tuple[str, ...] = ()
    provider_allow_loopback_http: bool = False
    provider_secret_env_names: tuple[str, ...] = ()

    @classmethod
    def from_settings(cls, settings: object | None) -> "RuntimeSafetyConfig":
        if settings is None:
            return cls()
        mode = (
            str(getattr(settings, "provider_runtime_mode", "local_lab"))
            .strip()
            .lower()
        )
        if mode not in PROVIDER_RUNTIME_MODES:
            raise RuntimeError(
                "PROVIDER_RUNTIME_MODE 只能是 local_lab、ci_lab_local "
                "或 self_hosted_lab"
            )
        return cls(
            app_env=str(getattr(settings, "app_env", "local")).strip().lower(),
            provider_runtime_mode=mode,
            provider_self_hosted_ownership_acknowledged=bool(
                getattr(
                    settings,
                    "provider_self_hosted_ownership_acknowledged",
                    False,
                )
            ),
            provider_allowed_hosts=tuple(
                getattr(settings, "provider_allowed_hosts", ())
            ),
            provider_allowed_ports=tuple(
                getattr(settings, "provider_allowed_ports", (443,))
            ),
            provider_allowed_networks=tuple(
                getattr(settings, "provider_allowed_networks", ())
            ),
            provider_allow_loopback_http=bool(
                getattr(settings, "provider_allow_loopback_http", False)
            ),
            provider_secret_env_names=tuple(
                getattr(settings, "provider_secret_env_names", ())
            ),
        )


class ProviderBuilder(Protocol):
    def __call__(
        self,
        connection: ProviderConnectionRecord,
        secret: str,
        policy: OutboundPolicy,
    ) -> PipelineProvider: ...


class ProviderMetricsObserver(Protocol):
    def observe_provider_request(
        self,
        *,
        provider: str,
        operation: str,
        outcome: str,
        duration_seconds: float,
    ) -> None: ...


@dataclass(frozen=True, slots=True)
class _ClaimedProviderTrigger:
    intent_id: str
    run_id: str
    lease_token: str
    connection: ProviderConnectionRecord
    request: ProviderTriggerRequest
    attempts: int
    max_attempts: int


class PersistentRuntimeService:
    """Persistent API application service for one local Python process."""

    def __init__(
        self,
        database: Database,
        settings: object | None = None,
        *,
        environ: Mapping[str, str] | None = None,
        provider_builder: ProviderBuilder | None = None,
        provider_metrics: ProviderMetricsObserver | None = None,
        wakeup_publisher: WakeupPublisher | None = None,
        secret_store: SecretStore | None = None,
        task_types: frozenset[str] = DEFAULT_TASK_TYPES,
        offline_after_seconds: int = 90,
    ) -> None:
        self.repository = RuntimeRepository(database)
        self.safety = RuntimeSafetyConfig.from_settings(settings)
        self._environ = environ if environ is not None else os.environ
        self._secret_store = secret_store or EnvironmentSecretStore(
            self._environ,
            allowed_names=self.safety.provider_secret_env_names,
        )
        self._secret_store_runtime_mode = str(
            getattr(self._secret_store, "runtime_mode", "unknown")
        )
        self._provider_builder = provider_builder or self._default_provider_builder
        self._provider_metrics = provider_metrics
        self._wakeup_publisher = wakeup_publisher
        self.task_types = task_types
        self._offline_after = timedelta(seconds=offline_after_seconds)

    async def initialize(self) -> None:
        await self.repository.initialize()

    async def shutdown(self) -> None:
        await self._secret_store.aclose()

    # ------------------------------------------------------------------
    # Provider connections and provider runs
    # ------------------------------------------------------------------
    def provider_runtime_status(self) -> dict[str, str | bool]:
        """Expose the active lab boundary without leaking hosts or secrets."""

        self_hosted = self.safety.provider_runtime_mode == "self_hosted_lab"
        learning_ci = self.safety.provider_runtime_mode == "ci_lab_local"
        return {
            "mode": self.safety.provider_runtime_mode,
            "network_providers_allowed": (
                learning_ci
                or (
                    self_hosted
                    and self.safety.provider_self_hosted_ownership_acknowledged
                )
            ),
            "target_scope": (
                "internal_container"
                if self.safety.app_env == "local-container"
                else "loopback_only"
            ),
            "external_public_mode_supported": False,
        }

    async def create_connection(
        self, payload: ProviderConnectionCreate
    ) -> ProviderConnectionView:
        name = payload.name.strip()
        definition_ref = payload.definition_ref.strip()
        config = {key: value.strip() for key, value in payload.config.items()}
        self._validate_connection_input(
            payload.kind,
            payload.base_url,
            definition_ref,
            config,
            payload.secret_env_var,
            payload.webhook_secret_env_var,
        )
        if not name or not definition_ref:
            raise BusinessValidationError("连接名称和 definition_ref 不能为空")
        now = _utc_now()
        record = ProviderConnectionRecord(
            id=str(uuid4()),
            name=name,
            kind=payload.kind.value,
            base_url=payload.base_url.rstrip("/") if payload.base_url else None,
            definition_ref=definition_ref,
            config=config,
            secret_env_var=payload.secret_env_var,
            webhook_secret_env_var=payload.webhook_secret_env_var,
            enabled=payload.enabled,
            version=0,
            created_at=now,
            updated_at=now,
        )
        async with self.repository.transaction() as session:
            session.add(record)
            await session.flush()
            return self._connection_view(record)

    async def list_connections(self) -> list[ProviderConnectionView]:
        async with self.repository.transaction() as session:
            records = list(
                (
                    await session.scalars(
                        select(ProviderConnectionRecord).order_by(
                            ProviderConnectionRecord.name, ProviderConnectionRecord.id
                        )
                    )
                ).all()
            )
            return [self._connection_view(record) for record in records]

    async def get_connection(self, connection_id: str) -> ProviderConnectionView:
        async with self.repository.transaction() as session:
            return self._connection_view(await self._require_connection(session, connection_id))

    async def update_connection(
        self, connection_id: str, payload: ProviderConnectionPatch
    ) -> ProviderConnectionView:
        async with self.repository.transaction() as session:
            record = await self._require_connection(session, connection_id)
            if record.version != payload.version:
                raise ConflictError("连接配置已被其他请求修改，请刷新后重试")
            values = payload.model_dump(exclude_unset=True, exclude={"version"})
            for field, value in values.items():
                if field in {"name", "definition_ref", "config", "enabled"} and value is None:
                    raise BusinessValidationError(f"连接字段 {field} 不能设为空")
                if field in {"name", "definition_ref"} and value is not None:
                    value = value.strip()
                if field == "config" and value is not None:
                    value = {key: item.strip() for key, item in value.items()}
                if field == "base_url" and value:
                    value = value.rstrip("/")
                setattr(record, field, value)
            if not record.name or not record.definition_ref:
                raise BusinessValidationError("连接名称和 definition_ref 不能为空")
            self._validate_connection_input(
                ProviderKind(record.kind),
                record.base_url,
                record.definition_ref,
                dict(record.config),
                record.secret_env_var,
                record.webhook_secret_env_var,
            )
            record.version += 1
            record.updated_at = _utc_now()
            await session.flush()
            return self._connection_view(record)

    async def delete_connection(self, connection_id: str) -> bool:
        async with self.repository.transaction() as session:
            record = await self._require_connection(session, connection_id)
            has_runs = await session.scalar(
                select(ProviderRunRecord.id).where(
                    ProviderRunRecord.connection_id == connection_id
                ).limit(1)
            )
            if has_runs is not None:
                raise ConflictError("连接已有运行历史，只能停用，不能删除")
            await session.delete(record)
            return True

    async def test_connection(self, connection_id: str) -> ProviderTestResult:
        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            kind = ProviderKind(connection.kind)
            with self._observe_provider(kind, "test_connection"):
                if kind == ProviderKind.LOCAL and not connection.enabled:
                    raise AuthorizationError("该 provider 连接未开启")
                if kind == ProviderKind.LOCAL:
                    return ProviderTestResult(
                        ready=True,
                        message="本地模拟器可用；没有打开套接字或启动进程",
                    )
                provider = await self._build_enabled_provider(connection)
                try:
                    # Construction validates URL, allowlists and provider metadata.
                    # A generic provider-neutral health endpoint does not exist, so
                    # this check deliberately performs no undocumented network call.
                    return ProviderTestResult(
                        ready=True,
                        message=(
                            "自建实验室门禁、凭据环境变量和静态配置均已通过；"
                            "未执行网络探测"
                        ),
                    )
                finally:
                    await provider.aclose()

    async def trigger_provider(
        self, connection_id: str, payload: ProviderTriggerPayload
    ) -> ProviderRunView:
        """Persist a trigger intent without contacting the provider.

        The dispatcher performs provider I/O after this transaction commits.
        This removes the former long transaction and leaves an observable row
        when the remote result is uncertain.
        """

        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            if not connection.enabled:
                raise AuthorizationError("该 provider 连接未开启")
            self._validate_provider_dispatch_boundary(connection)
            kind = ProviderKind(connection.kind)
            if kind == ProviderKind.LEARNING_CI:
                try:
                    payload.validate_learning_ci_contract()
                except ValueError as error:
                    raise BusinessValidationError(
                        f"Learning CI 触发参数无效：{error}"
                    ) from None
            correlation_id = payload.correlation_id or f"qa-{uuid4().hex}"
            request = ProviderTriggerRequest(
                definition_ref=connection.definition_ref,
                ref=payload.ref,
                variables=payload.variables,
                correlation_id=correlation_id,
            )
            fingerprint = _json_fingerprint(request.model_dump(mode="json"))
            if payload.correlation_id is not None:
                previous = await session.scalar(
                    select(ProviderRunRecord).where(
                        ProviderRunRecord.connection_id == connection_id,
                        ProviderRunRecord.correlation_id == payload.correlation_id,
                    )
                )
                if previous is not None:
                    if not hmac.compare_digest(previous.request_fingerprint, fingerprint):
                        raise ConflictError("correlation_id 已用于不同的触发参数")
                    approvals = await self._approval_views(session, previous.id)
                    return self._provider_run_view(previous, approvals)

            gate_status = (
                ProviderQualityGateStatus.EVALUATING.value
                if kind == ProviderKind.LEARNING_CI
                and connection.definition_ref == "local-quality-gate"
                else ProviderQualityGateStatus.NOT_REQUIRED.value
            )
            actor = get_current_actor()
            now = _utc_now()
            run_id = str(uuid4())
            record = ProviderRunRecord(
                id=run_id,
                connection_id=connection_id,
                external_id=None,
                status="queued",
                raw_status="trigger_pending",
                web_url=None,
                message="trigger intent is waiting for a dispatcher",
                run_metadata={
                    "definition_ref": connection.definition_ref,
                    "ref": payload.ref,
                },
                correlation_id=correlation_id,
                request_fingerprint=fingerprint,
                dispatch_status="pending",
                quality_gate_status=gate_status,
                last_provider_sequence=0,
                last_provider_occurred_at=None,
                reconciliation_required=False,
                triggered_by_user_id=(str(actor.user_id) if actor is not None else None),
                triggered_by_name=(actor.username if actor is not None else "local-user"),
                version=0,
                created_at=now,
                updated_at=now,
            )
            intent = ProviderTriggerIntentRecord(
                id=str(uuid4()),
                run_id=run_id,
                connection_id=connection_id,
                connection_version=connection.version,
                request_payload=request.model_dump(mode="json"),
                idempotency_key=correlation_id,
                request_fingerprint=fingerprint,
                status="pending",
                attempts=0,
                max_attempts=5,
                available_at=now,
                lease_owner=None,
                lease_token_hash=None,
                lease_expires_at=None,
                last_error_code=None,
                created_at=now,
                updated_at=now,
                completed_at=None,
            )
            session.add(record)
            session.add(intent)
            await session.flush()
            return self._provider_run_view(record, [])

    async def list_provider_trigger_intents(self) -> list[ProviderTriggerIntentView]:
        async with self.repository.transaction() as session:
            records = list(
                (
                    await session.scalars(
                        select(ProviderTriggerIntentRecord).order_by(
                            ProviderTriggerIntentRecord.created_at.desc(),
                            ProviderTriggerIntentRecord.id,
                        )
                    )
                ).all()
            )
            return [self._provider_intent_view(record) for record in records]

    async def dispatch_provider_trigger_once(
        self,
        worker_id: str,
        lease_seconds: int = 30,
    ) -> ProviderRunView | None:
        """Claim one outbox row, perform HTTP outside SQL, then finalize."""

        normalized_worker = worker_id.strip()
        if not normalized_worker or len(normalized_worker) > 200:
            raise BusinessValidationError("dispatcher worker_id 无效")
        if not 5 <= lease_seconds <= 3600:
            raise BusinessValidationError("dispatcher lease_seconds 必须在 5 到 3600 之间")
        claimed = await self._claim_provider_trigger(
            normalized_worker,
            lease_seconds,
        )
        if claimed is None:
            return None

        kind = ProviderKind(claimed.connection.kind)
        try:
            if claimed.connection.version != await self._connection_version(
                claimed.connection.id
            ):
                await self._settle_provider_trigger_failure(
                    claimed,
                    error_code="connection_version_changed",
                    retryable=False,
                )
                return await self._get_local_provider_run(claimed.run_id)

            with self._observe_provider(kind, "trigger"):
                if kind == ProviderKind.LOCAL:
                    run = ProviderRun(
                        provider=kind,
                        external_id=str(uuid4()),
                        status="queued",
                        raw_status="queued",
                        metadata={
                            "definition_ref": claimed.connection.definition_ref,
                            "ref": claimed.request.ref,
                        },
                    )
                else:
                    provider = await self._build_enabled_provider(claimed.connection)
                    try:
                        run = await provider.trigger(claimed.request)
                    finally:
                        await provider.aclose()
        except ProviderError as error:
            retryable = kind == ProviderKind.LEARNING_CI and not isinstance(
                error,
                (
                    ProviderConfigurationError,
                    ProviderConflictError,
                    ProviderSecurityError,
                    ProviderDisabledError,
                ),
            )
            await self._settle_provider_trigger_failure(
                claimed,
                error_code="provider_result_unknown" if retryable else "provider_rejected",
                retryable=retryable,
            )
            return await self._get_local_provider_run(claimed.run_id)
        except (AuthorizationError, BusinessValidationError):
            await self._settle_provider_trigger_failure(
                claimed,
                error_code="provider_configuration_rejected",
                retryable=False,
            )
            return await self._get_local_provider_run(claimed.run_id)

        return await self._finalize_provider_trigger(claimed, run)

    async def list_provider_runs(self, connection_id: str) -> list[ProviderRunView]:
        async with self.repository.transaction() as session:
            await self._require_connection(session, connection_id)
            records = list(
                (
                    await session.scalars(
                        select(ProviderRunRecord)
                        .where(ProviderRunRecord.connection_id == connection_id)
                        .order_by(ProviderRunRecord.created_at.desc())
                    )
                ).all()
            )
            approval_rows = list(
                (
                    await session.scalars(
                        select(ProviderRunApprovalRecord)
                        .join(
                            ProviderRunRecord,
                            ProviderRunRecord.id == ProviderRunApprovalRecord.run_id,
                        )
                        .where(ProviderRunRecord.connection_id == connection_id)
                        .order_by(ProviderRunApprovalRecord.created_at)
                    )
                ).all()
            )
            approvals_by_run: dict[str, list[ProviderRunApprovalView]] = {}
            for approval in approval_rows:
                approvals_by_run.setdefault(approval.run_id, []).append(
                    self._approval_view(approval)
                )
            return [
                self._provider_run_view(record, approvals_by_run.get(record.id, []))
                for record in records
            ]

    async def get_provider_run(self, connection_id: str, run_id: str) -> ProviderRunView:
        return await self._refresh_provider_run(connection_id, run_id, cancel=False)

    async def cancel_provider_run(self, connection_id: str, run_id: str) -> ProviderRunView:
        return await self._refresh_provider_run(connection_id, run_id, cancel=True)

    async def _refresh_provider_run(
        self, connection_id: str, run_id: str, *, cancel: bool
    ) -> ProviderRunView:
        # Prepare a detached snapshot first. Provider/Vault I/O happens only
        # after this transaction has committed.
        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            record = await session.get(ProviderRunRecord, run_id)
            if record is None or record.connection_id != connection_id:
                raise NotFoundError("集成运行", run_id)
            approvals = await self._approval_views(session, run_id)
            if record.external_id is None:
                if cancel and record.dispatch_status in {"pending", "retry_wait"}:
                    intent = await session.scalar(
                        select(ProviderTriggerIntentRecord).where(
                            ProviderTriggerIntentRecord.run_id == run_id
                        )
                    )
                    if intent is not None and intent.status in {"pending", "retry_wait"}:
                        now = _utc_now()
                        intent.status = "cancelled"
                        intent.completed_at = now
                        intent.updated_at = now
                        record.status = "cancelled"
                        record.raw_status = "cancelled_before_dispatch"
                        record.dispatch_status = "cancelled"
                        record.quality_gate_status = "cancelled"
                        record.message = "cancelled before provider dispatch"
                        record.updated_at = now
                        record.version += 1
                return self._provider_run_view(record, approvals)
            kind = ProviderKind(connection.kind)
            external_id = record.external_id
            expected_version = record.version

        with self._observe_provider(kind, "cancel" if cancel else "query"):
            if kind == ProviderKind.LOCAL:
                run = ProviderRun(
                    provider=kind,
                    external_id=external_id,
                    status="cancelled" if cancel else record.status,
                    raw_status="cancelled" if cancel else record.raw_status,
                    web_url=record.web_url,
                    message=(
                        "cancelled by local simulator" if cancel else record.message
                    ),
                    metadata=dict(record.run_metadata),
                )
            else:
                provider = await self._build_enabled_provider(connection)
                try:
                    try:
                        run = (
                            await provider.cancel(external_id)
                            if cancel
                            else await provider.get(external_id)
                        )
                    except ProviderError as error:
                        raise _safe_provider_error(error) from error
                finally:
                    await provider.aclose()

        async with self.repository.transaction() as session:
            current = await self.repository.get_provider_run_for_update(session, run_id)
            if current is None or current.connection_id != connection_id:
                raise NotFoundError("集成运行", run_id)
            if current.version != expected_version:
                # A webhook or another poll won the race. Return its newer
                # local truth and ask the caller to refresh if still needed.
                approvals = await self._approval_views(session, run_id)
                return self._provider_run_view(current, approvals)
            self._apply_provider_run_snapshot(current, run, source="poll")
            approvals = await self._approval_views(session, run_id)
            return self._provider_run_view(current, approvals)

    async def decide_provider_quality_gate(
        self,
        connection_id: str,
        run_id: str,
        payload: ProviderRunApprovalPayload,
    ) -> ProviderRunView:
        """Apply one idempotent human decision to the CI Lab truth source."""

        actor = get_current_actor()
        actor_user_id = str(actor.user_id) if actor is not None else None
        actor_name = actor.username if actor is not None else "local-user"
        request_fingerprint = _json_fingerprint(
            {
                "event_id": payload.event_id,
                "decision": payload.decision,
                "comment": payload.comment.strip(),
                "actor_user_id": actor_user_id,
                "actor_name": actor_name,
            }
        )

        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            record = await session.get(ProviderRunRecord, run_id)
            if record is None or record.connection_id != connection_id:
                raise NotFoundError("集成运行", run_id)
            previous = await session.scalar(
                select(ProviderRunApprovalRecord).where(
                    ProviderRunApprovalRecord.run_id == run_id,
                    ProviderRunApprovalRecord.event_id == payload.event_id,
                )
            )
            if previous is not None:
                if not hmac.compare_digest(
                    previous.request_fingerprint, request_fingerprint
                ):
                    raise ConflictError("审批 event_id 已用于不同的决定")
                approvals = await self._approval_views(session, run_id)
                return self._provider_run_view(record, approvals)
            if await session.scalar(
                select(ProviderRunApprovalRecord.id).where(
                    ProviderRunApprovalRecord.run_id == run_id
                )
            ) is not None:
                raise ConflictError("质量门禁已经形成不可变决定")
            if ProviderKind(connection.kind) != ProviderKind.LEARNING_CI:
                raise InvalidStateError("只有 Learning CI 支持教学质量门禁")
            if (
                record.external_id is None
                or record.dispatch_status != "dispatched"
                or record.quality_gate_status != "waiting_approval"
                or record.raw_status != "waiting_approval"
            ):
                raise InvalidStateError("运行尚未到达可审批的质量门禁")
            if (
                actor_user_id is not None
                and record.triggered_by_user_id == actor_user_id
            ):
                raise AuthorizationError("触发人不能审批自己的质量门禁")
            external_id = record.external_id
            expected_version = record.version

        decision_request = ProviderGateDecisionRequest(
            event_id=payload.event_id,
            decision=ProviderGateDecision(payload.decision),
            actor_id=actor_user_id or "local-user",
            actor_name=actor_name,
            comment=payload.comment,
        )
        provider = await self._build_enabled_provider(connection)
        try:
            try:
                run = await provider.decide_gate(external_id, decision_request)
            except ProviderError as error:
                raise _safe_provider_error(error) from error
        finally:
            await provider.aclose()

        async with self.repository.transaction() as session:
            current = await self.repository.get_provider_run_for_update(session, run_id)
            if current is None or current.connection_id != connection_id:
                raise NotFoundError("集成运行", run_id)
            previous = await session.scalar(
                select(ProviderRunApprovalRecord).where(
                    ProviderRunApprovalRecord.run_id == run_id,
                    ProviderRunApprovalRecord.event_id == payload.event_id,
                )
            )
            if previous is not None:
                if not hmac.compare_digest(
                    previous.request_fingerprint, request_fingerprint
                ):
                    raise ConflictError("审批 event_id 已用于不同的决定")
                approvals = await self._approval_views(session, run_id)
                return self._provider_run_view(current, approvals)
            if current.version != expected_version:
                raise ConflictError("运行状态已变化，请刷新后使用同一 event_id 重试")
            approval = ProviderRunApprovalRecord(
                id=str(uuid4()),
                run_id=run_id,
                event_id=payload.event_id,
                decision=payload.decision,
                request_fingerprint=request_fingerprint,
                actor_user_id=actor_user_id,
                actor_name=actor_name,
                comment=payload.comment.strip(),
                created_at=_utc_now(),
            )
            session.add(approval)
            self._apply_provider_run_snapshot(current, run, source="gate_decision")
            await session.flush()
            return self._provider_run_view(current, [self._approval_view(approval)])

    async def _claim_provider_trigger(
        self,
        worker_id: str,
        lease_seconds: int,
    ) -> _ClaimedProviderTrigger | None:
        now_fallback = _utc_now()
        lease_token = secrets.token_urlsafe(32)
        async with self.repository.transaction() as session:
            now = await self.repository.database_now(
                session,
                fallback=now_fallback,
            )
            intent = await self.repository.claim_provider_trigger_intent(
                session,
                now=now,
            )
            if intent is None:
                return None
            run = await self.repository.get_provider_run_for_update(
                session,
                intent.run_id,
            )
            connection = await session.get(
                ProviderConnectionRecord,
                intent.connection_id,
            )
            if run is None or connection is None:
                raise InvalidStateError("触发 Intent 的关联数据不存在")
            intent.status = "claimed"
            intent.attempts += 1
            intent.lease_owner = worker_id
            intent.lease_token_hash = _token_hash(lease_token)
            intent.lease_expires_at = now + timedelta(seconds=lease_seconds)
            intent.updated_at = now
            run.dispatch_status = "dispatching"
            run.updated_at = now
            run.version += 1
            request = ProviderTriggerRequest.model_validate(intent.request_payload)
            return _ClaimedProviderTrigger(
                intent_id=intent.id,
                run_id=run.id,
                lease_token=lease_token,
                connection=connection,
                request=request,
                attempts=intent.attempts,
                max_attempts=intent.max_attempts,
            )

    async def _connection_version(self, connection_id: str) -> int:
        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            return connection.version

    async def _finalize_provider_trigger(
        self,
        claimed: _ClaimedProviderTrigger,
        run: ProviderRun,
    ) -> ProviderRunView:
        async with self.repository.transaction() as session:
            intent = await self.repository.get_provider_trigger_intent_for_update(
                session,
                claimed.intent_id,
            )
            record = await self.repository.get_provider_run_for_update(
                session,
                claimed.run_id,
            )
            if intent is None or record is None:
                raise InvalidStateError("触发 Intent 在分发期间被删除")
            self._require_provider_intent_lease(intent, claimed)
            now = _utc_now()
            intent.status = "succeeded"
            intent.lease_owner = None
            intent.lease_token_hash = None
            intent.lease_expires_at = None
            intent.last_error_code = None
            intent.updated_at = now
            intent.completed_at = now
            record.dispatch_status = "dispatched"
            record.reconciliation_required = False
            self._apply_provider_run_snapshot(record, run, source="dispatch")
            approvals = await self._approval_views(session, record.id)
            return self._provider_run_view(record, approvals)

    async def _settle_provider_trigger_failure(
        self,
        claimed: _ClaimedProviderTrigger,
        *,
        error_code: str,
        retryable: bool,
    ) -> None:
        async with self.repository.transaction() as session:
            intent = await self.repository.get_provider_trigger_intent_for_update(
                session,
                claimed.intent_id,
            )
            run = await self.repository.get_provider_run_for_update(
                session,
                claimed.run_id,
            )
            if intent is None or run is None:
                raise InvalidStateError("触发 Intent 在失败结算期间被删除")
            self._require_provider_intent_lease(intent, claimed)
            now = _utc_now()
            should_retry = retryable and intent.attempts < intent.max_attempts
            intent.status = "retry_wait" if should_retry else (
                "unknown" if retryable else "failed"
            )
            intent.available_at = now + timedelta(
                seconds=min(60, 2 ** min(intent.attempts, 6))
            )
            intent.lease_owner = None
            intent.lease_token_hash = None
            intent.lease_expires_at = None
            intent.last_error_code = error_code
            intent.updated_at = now
            intent.completed_at = None if should_retry else now
            run.dispatch_status = (
                "retry_wait" if should_retry else "unknown" if retryable else "failed"
            )
            run.reconciliation_required = retryable
            if not retryable:
                run.status = "failed"
                if (
                    run.quality_gate_status
                    != ProviderQualityGateStatus.NOT_REQUIRED.value
                ):
                    run.quality_gate_status = ProviderQualityGateStatus.FAILED.value
            run.raw_status = "trigger_result_unknown" if retryable else "trigger_failed"
            run.message = (
                "provider result is uncertain; idempotent reconciliation is pending"
                if retryable
                else "provider trigger was rejected before a run was created"
            )
            run.updated_at = now
            run.version += 1

    @staticmethod
    def _require_provider_intent_lease(
        intent: ProviderTriggerIntentRecord,
        claimed: _ClaimedProviderTrigger,
    ) -> None:
        if (
            intent.status != "claimed"
            or intent.lease_token_hash is None
            or not hmac.compare_digest(
                intent.lease_token_hash,
                _token_hash(claimed.lease_token),
            )
        ):
            raise ConflictError("dispatcher 已失去触发 Intent 租约")

    async def _get_local_provider_run(self, run_id: str) -> ProviderRunView:
        async with self.repository.transaction() as session:
            record = await session.get(ProviderRunRecord, run_id)
            if record is None:
                raise NotFoundError("集成运行", run_id)
            approvals = await self._approval_views(session, run_id)
            return self._provider_run_view(record, approvals)

    async def process_learning_ci_webhook(
        self,
        connection_id: str,
        *,
        raw_body: bytes,
        raw_headers: list[tuple[bytes, bytes]],
    ) -> ProviderWebhookResult:
        """Authenticate and reduce one machine event without browser auth."""

        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            if (
                ProviderKind(connection.kind) != ProviderKind.LEARNING_CI
                or not connection.enabled
            ):
                raise AuthorizationError("Webhook 连接未启用")
            secret_name = connection.webhook_secret_env_var
            if (
                secret_name != CI_LAB_WEBHOOK_SECRET_NAME
                or secret_name not in self.safety.provider_secret_env_names
            ):
                raise AuthorizationError("Webhook Secret 未绑定到独立白名单")

        try:
            secret = await self._secret_store.read(secret_name)
        except SecretStoreError:
            raise AuthorizationError("Webhook Secret 不可用") from None
        try:
            verified = WebhookVerifier(secret.encode("utf-8")).verify(
                raw_body=raw_body,
                raw_headers=raw_headers,
            )
        except (WebhookSecurityError, ValueError):
            raise AuthorizationError("Webhook 验证失败") from None
        try:
            payload = ProviderWebhookPayload.model_validate_json(raw_body)
        except ValidationError:
            raise BusinessValidationError("Webhook 事件格式无效") from None

        received_at = _utc_now()
        async with self.repository.transaction() as session:
            previous = await session.scalar(
                select(ProviderWebhookEventRecord).where(
                    ProviderWebhookEventRecord.connection_id == connection_id,
                    ProviderWebhookEventRecord.event_id == verified.event_id,
                )
            )
            if previous is not None:
                if not hmac.compare_digest(
                    previous.body_sha256,
                    verified.body_sha256,
                ):
                    raise ConflictError("Webhook event_id 已绑定到不同内容")
                run = (
                    await session.get(ProviderRunRecord, previous.run_id)
                    if previous.run_id is not None
                    else None
                )
                return ProviderWebhookResult(
                    event_id=verified.event_id,
                    result="duplicate",
                    run_id=previous.run_id,
                    reconciliation_required=(
                        bool(run.reconciliation_required) if run is not None else False
                    ),
                )

            run = await session.scalar(
                select(ProviderRunRecord).where(
                    ProviderRunRecord.connection_id == connection_id,
                    ProviderRunRecord.external_id == payload.external_id,
                )
            )
            result = "ignored"
            reconciliation_required = False
            if run is not None:
                occurred_at = _aware(payload.occurred_at)
                if payload.sequence <= run.last_provider_sequence:
                    result = "stale"
                elif payload.sequence != run.last_provider_sequence + 1:
                    result = "reconcile_required"
                    run.reconciliation_required = True
                    run.updated_at = received_at
                    run.version += 1
                elif (
                    run.last_provider_occurred_at is not None
                    and occurred_at < _aware(run.last_provider_occurred_at)
                ):
                    result = "reconcile_required"
                    run.reconciliation_required = True
                    run.updated_at = received_at
                    run.version += 1
                elif self._apply_webhook_snapshot(run, payload):
                    result = "applied"
                    run.last_provider_sequence = payload.sequence
                    run.last_provider_occurred_at = occurred_at
                    run.reconciliation_required = False
                else:
                    result = "reconcile_required"
                    run.reconciliation_required = True
                    run.updated_at = received_at
                    run.version += 1
                reconciliation_required = run.reconciliation_required

            receipt = ProviderWebhookEventRecord(
                id=str(uuid4()),
                connection_id=connection_id,
                run_id=run.id if run is not None else None,
                event_id=verified.event_id,
                external_id=payload.external_id,
                body_sha256=verified.body_sha256,
                sequence=payload.sequence,
                occurred_at=_aware(payload.occurred_at),
                normalized_status=payload.status,
                result=result,
                received_at=received_at,
                processed_at=_utc_now(),
            )
            session.add(receipt)
            await session.flush()
            return ProviderWebhookResult(
                event_id=verified.event_id,
                result=result,
                run_id=run.id if run is not None else None,
                reconciliation_required=reconciliation_required,
            )

    @staticmethod
    def _apply_webhook_snapshot(
        record: ProviderRunRecord,
        payload: ProviderWebhookPayload,
    ) -> bool:
        # A quality-gated run cannot be advanced to ``running`` by a webhook.
        # Only the dedicated approval endpoint may release the gate; accepting
        # a provider event here would make the supposedly mandatory gate
        # bypassable.
        if (
            record.quality_gate_status == "waiting_approval"
            and payload.status not in {
                "waiting_approval",
                "failed",
                "cancelled",
            }
        ):
            return False
        next_local = (
            "queued" if payload.status in {"queued", "waiting_approval"} else payload.status
        )
        allowed: dict[str, set[str]] = {
            "queued": {"queued", "running", "succeeded", "failed", "cancelled"},
            "running": {"running", "queued", "succeeded", "failed", "cancelled"},
            "succeeded": {"succeeded"},
            "failed": {"failed"},
            "cancelled": {"cancelled"},
        }
        # ``running -> queued`` is allowed only for the explicit approval wait,
        # which the normalized pipeline status represents as queued.
        if next_local not in allowed.get(record.status, set()):
            return False
        if (
            record.status == "running"
            and next_local == "queued"
            and payload.status != "waiting_approval"
        ):
            return False
        record.status = next_local
        record.raw_status = payload.status
        record.message = payload.message
        if payload.status == "waiting_approval":
            record.quality_gate_status = "waiting_approval"
        elif payload.status == "succeeded" and record.quality_gate_status == "waiting_approval":
            record.quality_gate_status = "approved"
        elif payload.status == "failed":
            record.quality_gate_status = (
                "rejected"
                if record.quality_gate_status == "waiting_approval"
                else "failed"
            )
        elif payload.status == "cancelled":
            record.quality_gate_status = "cancelled"
        record.updated_at = _utc_now()
        record.version += 1
        return True

    # ------------------------------------------------------------------
    # Persistent at-least-once task queue
    # ------------------------------------------------------------------
    async def enqueue_task(self, payload: TaskEnqueue) -> tuple[TaskView, bool]:
        self._require_registered_task(payload.task_type)
        if not payload.queue.strip():
            raise BusinessValidationError("队列名不能为空")
        async with self.repository.transaction() as session:
            record, replayed = await self._enqueue_record(
                session,
                task_type=payload.task_type,
                task_payload=payload.payload,
                queue=payload.queue,
                priority=payload.priority,
                max_attempts=payload.max_attempts,
                idempotency_key=payload.idempotency_key,
                source_schedule_id=None,
                available_at=payload.available_at,
            )
            result = self._task_view(record), replayed
        # Production Web processes do not receive a publisher.  This optional
        # seam keeps deterministic adapter tests useful while still going
        # through the durable outbox state machine.
        if self._wakeup_publisher is not None:
            await self.dispatch_task_wakeup_once(
                dispatcher_id="inline-test-outbox",
                publisher=self._wakeup_publisher,
                lease_seconds=30,
            )
        return result

    async def list_task_wakeup_outbox(self) -> list[TaskWakeupOutboxView]:
        async with self.repository.transaction() as session:
            records = list(
                (
                    await session.scalars(
                        select(AutomationTaskWakeupOutboxRecord).order_by(
                            AutomationTaskWakeupOutboxRecord.created_at,
                            AutomationTaskWakeupOutboxRecord.id,
                        )
                    )
                ).all()
            )
            return [self._task_wakeup_outbox_view(record) for record in records]

    async def dispatch_task_wakeup_once(
        self,
        *,
        dispatcher_id: str,
        publisher: WakeupPublisher,
        lease_seconds: int = 30,
    ) -> bool:
        """Publish one claimed hint outside SQL and CAS the durable result."""

        self._validate_task_wakeup_dispatcher(dispatcher_id, lease_seconds)
        claimed = await self._claim_task_wakeup(dispatcher_id, lease_seconds)
        if claimed is None:
            return False
        published = True
        try:
            await publisher.publish_wakeup()
        except Exception as error:
            published = False
            # Never log exception text: AMQP errors may contain credentials.
            logger.warning(
                "task outbox publish failed error_type=%s",
                type(error).__name__,
            )
        settled = await self._settle_task_wakeup(
            claimed,
            published=published,
        )
        if not settled:
            logger.warning("task outbox settlement lost its lease")
        return True

    @staticmethod
    def _validate_task_wakeup_dispatcher(
        dispatcher_id: str,
        lease_seconds: int,
    ) -> None:
        if not dispatcher_id.strip() or len(dispatcher_id) > 200:
            raise BusinessValidationError(
                "outbox dispatcher_id 必须是 1 到 200 个字符的稳定标识"
            )
        if not 5 <= lease_seconds <= 3_600:
            raise BusinessValidationError("Outbox 租约必须在 5 到 3600 秒之间")

    async def _claim_task_wakeup(
        self,
        dispatcher_id: str,
        lease_seconds: int,
    ) -> _ClaimedTaskWakeup | None:
        async with self.repository.transaction() as session:
            now = await self.repository.database_now(session, fallback=_utc_now())
            record = await self.repository.claim_task_wakeup_outbox(
                session,
                now=now,
            )
            if record is None:
                return None
            lease_token = secrets.token_urlsafe(32)
            record.status = "claimed"
            record.publish_attempts += 1
            record.lease_owner = dispatcher_id
            record.lease_token_hash = _token_hash(lease_token)
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.updated_at = now
            record.version += 1
            await session.flush()
            return _ClaimedTaskWakeup(
                outbox_id=record.id,
                dispatcher_id=dispatcher_id,
                lease_token=lease_token,
                expected_version=record.version,
                publish_attempts=record.publish_attempts,
            )

    async def _settle_task_wakeup(
        self,
        claimed: _ClaimedTaskWakeup,
        *,
        published: bool,
    ) -> bool:
        async with self.repository.transaction() as session:
            now = await self.repository.database_now(session, fallback=_utc_now())
            retry_at = None
            if not published:
                retry_at = now + timedelta(
                    seconds=min(
                        60,
                        2 ** max(0, claimed.publish_attempts - 1),
                    )
                )
            return await self.repository.settle_task_wakeup_outbox(
                session,
                outbox_id=claimed.outbox_id,
                dispatcher_id=claimed.dispatcher_id,
                lease_token_hash=_token_hash(claimed.lease_token),
                expected_version=claimed.expected_version,
                now=now,
                published=published,
                retry_at=retry_at,
            )

    async def list_tasks(self) -> list[TaskView]:
        async with self.repository.transaction() as session:
            records = list(
                (await session.scalars(select(AutomationTaskRecord).order_by(
                    AutomationTaskRecord.created_at.desc(), AutomationTaskRecord.id
                ))).all()
            )
            return [self._task_view(record) for record in records]

    async def observability_snapshot(self) -> tuple[Counter[str], Counter[str]]:
        """Return bounded task/device counts without advancing lifecycle state.

        Metrics scrapes must not recover leases, claim work, contact providers,
        or otherwise become an implicit scheduler.  Expired/offline devices are
        represented through their effective read-time status only; normal API
        and worker operations remain responsible for persisting lease expiry.
        """

        now = _utc_now()
        async with self.repository.transaction() as session:
            tasks = list((await session.scalars(select(AutomationTaskRecord))).all())
            devices = list((await session.scalars(select(DeviceRecord))).all())
            return (
                Counter(record.status for record in tasks),
                Counter(self._effective_device_status(record, now) for record in devices),
            )

    async def get_task(self, task_id: str) -> TaskView:
        async with self.repository.transaction() as session:
            return self._task_view(await self._require_task(session, task_id))

    async def claim_task(
        self, worker_id: str, queues: list[str], lease_seconds: int
    ) -> ClaimedTaskView | None:
        accepted = {queue for queue in queues if queue}
        if len(accepted) != len(queues) or any(not queue.strip() for queue in queues):
            raise BusinessValidationError("队列名不能为空")
        async with self.repository.transaction() as session:
            now = await self.repository.database_now(session, fallback=_utc_now())
            await self._recover_expired_tasks(session, now)
            record = await self.repository.claim_task_candidate(
                session,
                queues=accepted,
                now=now,
            )
            if record is None:
                return None
            token = secrets.token_urlsafe(32)
            record.status = TaskStatus.RUNNING.value
            record.attempts += 1
            record.error_code = None
            record.result = None
            record.finished_at = None
            record.lease_owner = worker_id
            record.lease_token_hash = _token_hash(token)
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            record.heartbeat_at = now
            record.started_at = record.started_at or now
            return ClaimedTaskView(task=self._task_view(record), lease_token=token)

    async def heartbeat_task(
        self, task_id: str, worker_id: str, lease_token: str, lease_seconds: int
    ) -> TaskView:
        async with self.repository.transaction() as session:
            record, now = await self._require_task_lease(
                session, task_id, worker_id, lease_token
            )
            record.heartbeat_at = now
            record.lease_expires_at = now + timedelta(seconds=lease_seconds)
            return self._task_view(record)

    async def complete_task(
        self, task_id: str, worker_id: str, lease_token: str, result: dict[str, Any]
    ) -> TaskView:
        _json_fingerprint(result)
        async with self.repository.transaction() as session:
            record, now = await self._require_task_lease(
                session, task_id, worker_id, lease_token
            )
            record.status = (
                TaskStatus.CANCELLED.value
                if record.cancel_requested
                else TaskStatus.SUCCEEDED.value
            )
            record.result = None if record.cancel_requested else result
            record.finished_at = now
            self._clear_task_lease(record)
            return self._task_view(record)

    async def fail_task(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        error_code: str,
        retryable: bool,
    ) -> TaskView:
        async with self.repository.transaction() as session:
            record, now = await self._require_task_lease(
                session, task_id, worker_id, lease_token
            )
            record.error_code = error_code
            if record.cancel_requested:
                record.status = TaskStatus.CANCELLED.value
                record.finished_at = now
            elif retryable and record.attempts < record.max_attempts:
                record.status = TaskStatus.RETRY_WAIT.value
                record.available_at = now + timedelta(
                    seconds=min(300, 2 ** max(0, record.attempts - 1))
                )
                await self._add_task_wakeup_outbox(session, record, now=now)
            elif retryable:
                record.status = TaskStatus.DEAD_LETTER.value
                record.finished_at = now
            else:
                record.status = TaskStatus.FAILED.value
                record.finished_at = now
            self._clear_task_lease(record)
            return self._task_view(record)

    async def cancel_task(self, task_id: str) -> TaskView:
        async with self.repository.transaction() as session:
            record = await self._require_task(session, task_id, for_update=True)
            now = await self.repository.database_now(session, fallback=_utc_now())
            if record.status in {TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value}:
                record.status = TaskStatus.CANCELLED.value
                record.cancel_requested = True
                record.finished_at = now
                self._clear_task_lease(record)
            elif record.status == TaskStatus.RUNNING.value:
                record.cancel_requested = True
            return self._task_view(record)

    async def retry_task(self, task_id: str) -> TaskView:
        async with self.repository.transaction() as session:
            record = await self._require_task(session, task_id, for_update=True)
            if record.status != TaskStatus.FAILED.value:
                raise InvalidStateError("只有非重试失败的任务可由管理员手动重试")
            if record.attempts >= record.max_attempts:
                raise InvalidStateError("任务已达到最大尝试次数")
            record.status = TaskStatus.RETRY_WAIT.value
            record.available_at = await self.repository.database_now(
                session, fallback=_utc_now()
            )
            record.finished_at = None
            record.error_code = None
            await self._add_task_wakeup_outbox(
                session,
                record,
                now=_aware(record.available_at),
            )
            return self._task_view(record)

    async def dead_letter_task(self, task_id: str, error_code: str) -> TaskView:
        async with self.repository.transaction() as session:
            record = await self._require_task(session, task_id, for_update=True)
            if record.status == TaskStatus.RUNNING.value:
                raise InvalidStateError("运行中的任务必须由持有租约的 worker 结束")
            if record.status in {TaskStatus.SUCCEEDED.value, TaskStatus.CANCELLED.value}:
                raise InvalidStateError("已成功或已取消的任务不能进入死信")
            record.status = TaskStatus.DEAD_LETTER.value
            record.error_code = error_code
            record.finished_at = await self.repository.database_now(
                session, fallback=_utc_now()
            )
            self._clear_task_lease(record)
            return self._task_view(record)

    # ------------------------------------------------------------------
    # Devices and exclusive device leases
    # ------------------------------------------------------------------
    async def create_device(self, payload: DeviceCreate) -> DeviceView:
        self._validate_capabilities(payload.capabilities)
        if not payload.name.strip() or not payload.agent_id.strip():
            raise BusinessValidationError("设备名称和 agent_id 不能为空")
        now = _utc_now()
        record = DeviceRecord(
            id=str(uuid4()),
            name=payload.name.strip(),
            kind=payload.kind,
            platform=payload.platform,
            capabilities=sorted(payload.capabilities),
            agent_id=payload.agent_id,
            enabled=True,
            status=DeviceStatus.OFFLINE.value,
            last_heartbeat_at=None,
            active_lease_id=None,
            version=0,
            created_at=now,
            updated_at=now,
        )
        async with self.repository.transaction() as session:
            session.add(record)
            await session.flush()
            return self._device_view(record, now)

    async def list_devices(self) -> list[DeviceView]:
        await self._expire_device_leases()
        async with self.repository.transaction() as session:
            now = await self.repository.database_now(session, fallback=_utc_now())
            records = list(
                (
                    await session.scalars(
                        select(DeviceRecord).order_by(DeviceRecord.name)
                    )
                ).all()
            )
            return [self._device_view(record, now) for record in records]

    async def get_device(self, device_id: str) -> DeviceView:
        await self._expire_device_leases()
        async with self.repository.transaction() as session:
            now = await self.repository.database_now(session, fallback=_utc_now())
            return self._device_view(await self._require_device(session, device_id), now)

    async def update_device(self, device_id: str, payload: DevicePatch) -> DeviceView:
        await self._expire_device_leases()
        async with self.repository.transaction() as session:
            record = await self._require_device(session, device_id, for_update=True)
            now = await self.repository.database_now(session, fallback=_utc_now())
            if record.version != payload.version:
                raise ConflictError("设备已被其他请求修改，请刷新后重试")
            values = payload.model_dump(exclude_unset=True, exclude={"version", "maintenance"})
            if any(value is None for value in values.values()):
                raise BusinessValidationError("设备更新字段不能设为空")
            if "capabilities" in values:
                self._validate_capabilities(values["capabilities"])
                values["capabilities"] = sorted(values["capabilities"])
            for field, value in values.items():
                setattr(record, field, value.strip() if field == "name" else value)
            if payload.maintenance is not None:
                if payload.maintenance and record.active_lease_id is not None:
                    raise ConflictError("有活动租约的设备不能进入维护状态")
                record.status = (
                    DeviceStatus.MAINTENANCE.value
                    if payload.maintenance
                    else self._available_device_status(record, now)
                )
            elif not record.enabled and record.active_lease_id is None:
                record.status = DeviceStatus.OFFLINE.value
            record.version += 1
            record.updated_at = now
            return self._device_view(record, now)

    async def delete_device(self, device_id: str) -> bool:
        async with self.repository.transaction() as session:
            record = await self._require_device(session, device_id, for_update=True)
            lease = await session.scalar(
                select(DeviceLeaseRecord.id).where(DeviceLeaseRecord.device_id == device_id).limit(1)
            )
            if lease is not None:
                raise ConflictError("设备已有租约历史，只能停用，不能删除")
            await session.delete(record)
            return True

    async def heartbeat_device(self, device_id: str, agent_id: str) -> DeviceView:
        async with self.repository.transaction() as session:
            record = await self._require_device(session, device_id, for_update=True)
            now = await self.repository.database_now(session, fallback=_utc_now())
            if not hmac.compare_digest(record.agent_id, agent_id):
                raise AuthorizationError("设备 agent 身份不匹配")
            record.last_heartbeat_at = now
            if record.status != DeviceStatus.MAINTENANCE.value and record.active_lease_id is None:
                record.status = (
                    DeviceStatus.IDLE.value if record.enabled else DeviceStatus.OFFLINE.value
                )
            record.version += 1
            record.updated_at = now
            return self._device_view(record, now)

    async def acquire_device(self, payload: DeviceAcquire) -> ClaimedDeviceView | None:
        self._validate_capabilities(payload.required_capabilities)
        # Expiry is recovered in independent graph-lock transactions before
        # this request locks its task. Sweeping unrelated tasks while holding
        # the requester task could otherwise introduce a task-to-task cycle.
        await self._expire_device_leases()
        async with self.repository.transaction() as session:
            task, now = await self._require_active_device_task_lease(
                session,
                payload.task_id,
                payload.owner,
                payload.task_lease_token,
            )
            required = set(payload.required_capabilities)
            heartbeat_after = now - self._offline_after
            snapshots = await self.repository.list_device_candidate_snapshots(
                session,
                heartbeat_after=heartbeat_after,
            )
            candidate_ids = [
                device_id
                for device_id, capabilities in snapshots
                if required.issubset(set(capabilities))
            ]
            device: DeviceRecord | None = None
            while candidate_ids:
                candidate = await self.repository.claim_device_candidate(
                    session,
                    candidate_ids=candidate_ids,
                    heartbeat_after=heartbeat_after,
                )
                if candidate is None:
                    return None
                # PostgreSQL READ COMMITTED can observe a newer row after the
                # capability snapshot. Never trust pre-lock eligibility.
                if (
                    candidate.enabled
                    and self._effective_device_status(candidate, now)
                    == DeviceStatus.IDLE.value
                    and required.issubset(set(candidate.capabilities))
                ):
                    device = candidate
                    break
                candidate_ids.remove(candidate.id)
            if device is None:
                return None
            token = secrets.token_urlsafe(32)
            lease = DeviceLeaseRecord(
                id=str(uuid4()),
                device_id=device.id,
                task_id=payload.task_id,
                owner=payload.owner,
                token_hash=_token_hash(token),
                status=DeviceLeaseStatus.ACTIVE.value,
                acquired_at=now,
                expires_at=min(
                    now + timedelta(seconds=payload.lease_seconds),
                    _aware(task.lease_expires_at),
                ),
                released_at=None,
                version=0,
            )
            session.add(lease)
            device.active_lease_id = lease.id
            device.status = DeviceStatus.RESERVED.value
            device.version += 1
            device.updated_at = now
            await session.flush()
            return ClaimedDeviceView(
                device=self._device_view(device, now),
                lease=self._device_lease_view(lease),
                lease_token=token,
            )

    async def start_device_work(
        self, lease_id: str, owner: str, lease_token: str
    ) -> ClaimedDeviceView:
        async with self.repository.transaction() as session:
            lease, device, task, now = await self._require_device_lease(
                session, lease_id, owner, lease_token
            )
            self._validate_device_task_state(task, owner, now)
            self._validate_leased_device_state(device, now)
            device.status = DeviceStatus.BUSY.value
            device.version += 1
            device.updated_at = now
            return ClaimedDeviceView(
                device=self._device_view(device, now),
                lease=self._device_lease_view(lease),
                lease_token=lease_token,
            )

    async def renew_device_lease(
        self,
        lease_id: str,
        owner: str,
        lease_token: str,
        task_lease_token: str,
        lease_seconds: int,
    ) -> DeviceLeaseView:
        async with self.repository.transaction() as session:
            lease, device, task, now = await self._require_device_lease(
                session, lease_id, owner, lease_token
            )
            self._validate_task_lease_record(
                task,
                owner,
                task_lease_token,
                now,
            )
            if task.cancel_requested:
                raise InvalidStateError("任务已请求取消，不能申请或续租设备")
            self._validate_leased_device_state(device, now)
            lease.expires_at = min(
                now + timedelta(seconds=lease_seconds),
                _aware(task.lease_expires_at),
            )
            lease.version += 1
            return self._device_lease_view(lease)

    async def release_device_lease(
        self, lease_id: str, owner: str, lease_token: str
    ) -> DeviceLeaseView:
        async with self.repository.transaction() as session:
            lease, device, _, now = await self._require_device_lease(
                session, lease_id, owner, lease_token
            )
            lease.status = DeviceLeaseStatus.RELEASED.value
            lease.released_at = now
            lease.version += 1
            device.active_lease_id = None
            device.status = self._available_device_status(device, now)
            device.version += 1
            device.updated_at = now
            return self._device_lease_view(lease)

    # ------------------------------------------------------------------
    # Persistent manual scheduler
    # ------------------------------------------------------------------
    async def create_schedule(self, payload: ScheduleCreate) -> ScheduleView:
        self._require_registered_task(payload.task_type)
        _json_fingerprint(payload.payload)
        if not payload.name.strip() or not payload.queue.strip():
            raise BusinessValidationError("定时任务名称和队列名不能为空")
        now = _utc_now()
        expression = _parse_cron(payload.cron)
        next_run = await _next_cron_after(expression, now, payload.timezone)
        record = ScheduleRecord(
            id=str(uuid4()),
            name=payload.name.strip(),
            task_type=payload.task_type,
            payload=payload.payload,
            queue=payload.queue,
            priority=payload.priority,
            max_attempts=payload.max_attempts,
            cron=payload.cron,
            timezone=payload.timezone,
            misfire_policy=payload.misfire_policy.value,
            overlap_policy=payload.overlap_policy.value,
            misfire_grace_seconds=payload.misfire_grace_seconds,
            catch_up_limit=payload.catch_up_limit,
            enabled=payload.enabled,
            next_run_at=next_run,
            last_run_at=None,
            claim_owner=None,
            claim_token_hash=None,
            claim_expires_at=None,
            version=0,
            created_at=now,
            updated_at=now,
        )
        async with self.repository.transaction() as session:
            session.add(record)
            await session.flush()
            return self._schedule_view(record)

    async def list_schedules(self) -> list[ScheduleView]:
        async with self.repository.transaction() as session:
            records = list((await session.scalars(select(ScheduleRecord).order_by(ScheduleRecord.name))).all())
            return [self._schedule_view(record) for record in records]

    async def get_schedule(self, schedule_id: str) -> ScheduleView:
        async with self.repository.transaction() as session:
            return self._schedule_view(await self._require_schedule(session, schedule_id))

    async def update_schedule(
        self, schedule_id: str, payload: SchedulePatch
    ) -> ScheduleView:
        now = _utc_now()
        async with self.repository.transaction() as session:
            record = await self._require_schedule(
                session,
                schedule_id,
                for_update=True,
            )
            if record.version != payload.version:
                raise ConflictError("定时任务已被其他请求修改，请刷新后重试")
            was_enabled = record.enabled
            values = payload.model_dump(exclude_unset=True, exclude={"version"})
            if any(value is None for value in values.values()):
                raise BusinessValidationError("定时任务更新字段不能设为空")
            for field, value in values.items():
                if field in {"misfire_policy", "overlap_policy"} and value is not None:
                    value = value.value
                setattr(record, field, value.strip() if field == "name" else value)
            _json_fingerprint(record.payload)
            expression = _parse_cron(record.cron)
            rule_changed = "cron" in values or "timezone" in values
            reenabled = values.get("enabled") is True and not was_enabled
            if (
                rule_changed
                or reenabled
                or (
                    record.enabled
                    and (
                        record.next_run_at is None
                        or _aware(record.next_run_at) <= now
                    )
                )
            ):
                record.next_run_at = await _next_cron_after(
                    expression, now, record.timezone
                )
            record.version += 1
            record.updated_at = now
            # Any business edit invalidates an in-flight plan. The scheduler's
            # token/version CAS will observe the new version and must re-plan.
            record.claim_owner = None
            record.claim_token_hash = None
            record.claim_expires_at = None
            return self._schedule_view(record)

    async def delete_schedule(self, schedule_id: str) -> bool:
        async with self.repository.transaction() as session:
            record = await self._require_schedule(
                session,
                schedule_id,
                for_update=True,
            )
            has_history = await session.scalar(
                select(ScheduleFireRecord.id).where(
                    ScheduleFireRecord.schedule_id == schedule_id
                ).limit(1)
            )
            if has_history is not None:
                raise ConflictError("定时任务已有触发历史，只能停用，不能删除")
            await session.delete(record)
            return True

    async def list_schedule_fires(self, schedule_id: str) -> list[ScheduleFireView]:
        async with self.repository.transaction() as session:
            await self._require_schedule(session, schedule_id)
            records = list(
                (
                    await session.scalars(
                        select(ScheduleFireRecord)
                        .where(ScheduleFireRecord.schedule_id == schedule_id)
                        .order_by(ScheduleFireRecord.scheduled_for.desc())
                    )
                ).all()
            )
            return [self._schedule_fire_view(record) for record in records]

    async def run_schedule_now(self, schedule_id: str) -> ScheduleFireView:
        now = _utc_now()
        async with self.repository.transaction() as session:
            schedule = await self._require_schedule(
                session,
                schedule_id,
                for_update=True,
            )
            fire_id = str(uuid4())
            task, _ = await self._enqueue_record(
                session,
                task_type=schedule.task_type,
                task_payload=schedule.payload,
                queue=schedule.queue,
                priority=schedule.priority,
                max_attempts=schedule.max_attempts,
                idempotency_key=f"schedule-manual:{schedule.id}:{fire_id}",
                source_schedule_id=schedule.id,
                available_at=now,
            )
            fire = ScheduleFireRecord(
                id=fire_id,
                schedule_id=schedule.id,
                fire_key=f"manual:{fire_id}",
                scheduled_for=now,
                status=ScheduleFireStatus.ENQUEUED.value,
                task_id=task.id,
                created_at=now,
            )
            session.add(fire)
            await session.flush()
            return self._schedule_fire_view(fire)

    async def tick_schedules(
        self,
        now: datetime | None = None,
        *,
        scheduler_id: str | None = None,
        lease_seconds: int = 30,
    ) -> list[ScheduleFireView]:
        """Claim and finalize each currently due schedule at most once.

        PostgreSQL instances coordinate only through row locks and persisted
        leases. ``excluded_ids`` prevents one batch from spinning if a claim
        loses its final CAS to an administrative update.
        """

        owner = scheduler_id or f"inline-scheduler:{uuid4()}"
        self._validate_scheduler_claim(owner, lease_seconds)
        created: list[ScheduleFireView] = []
        attempted: set[str] = set()
        while len(attempted) < SCHEDULE_TICK_BATCH_LIMIT:
            schedule = await self._claim_due_schedule(
                owner,
                lease_seconds,
                fallback_now=now,
                excluded_ids=attempted,
            )
            if schedule is None:
                break
            attempted.add(schedule.id)
            created.extend(await self._execute_schedule_claim(schedule))
        return created

    async def tick_schedule_once(
        self,
        scheduler_id: str,
        lease_seconds: int,
        now: datetime | None = None,
    ) -> list[ScheduleFireView]:
        """Process at most one due schedule for an independent Scheduler."""

        self._validate_scheduler_claim(scheduler_id, lease_seconds)
        schedule = await self._claim_due_schedule(
            scheduler_id,
            lease_seconds,
            fallback_now=now,
        )
        if schedule is None:
            return []
        return await self._execute_schedule_claim(schedule)

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    @contextmanager
    def _observe_provider(self, kind: ProviderKind, operation: str):
        started = time.perf_counter()
        outcome = "succeeded"
        try:
            yield
        except AuthorizationError:
            outcome = "rejected"
            raise
        except Exception:
            outcome = "failed"
            raise
        finally:
            if self._provider_metrics is not None:
                try:
                    self._provider_metrics.observe_provider_request(
                        provider=kind.value,
                        operation=operation,
                        outcome=outcome,
                        duration_seconds=time.perf_counter() - started,
                    )
                except Exception:
                    # Metrics are diagnostic and must never change provider
                    # operation semantics.
                    pass

    def _validate_connection_input(
        self,
        kind: ProviderKind,
        base_url: str | None,
        definition_ref: str,
        config: dict[str, str],
        secret_env_var: str | None,
        webhook_secret_env_var: str | None,
    ) -> None:
        rules: dict[ProviderKind, tuple[set[str], set[str]]] = {
            ProviderKind.LOCAL: (set(), set()),
            ProviderKind.LEARNING_CI: (set(), set()),
            ProviderKind.JENKINS: ({"job_name", "username"}, {"job_name", "username"}),
            ProviderKind.GITLAB: ({"project_id"}, {"project_id"}),
            ProviderKind.BK_CI: (
                {"project_id", "pipeline_id", "user_id"},
                {"project_id", "pipeline_id", "user_id", "api_prefix"},
            ),
        }
        required, allowed = rules[kind]
        if set(config) - allowed or not required.issubset(config):
            raise BusinessValidationError("连接 config 字段与 provider 类型不匹配")
        if any(
            not isinstance(value, str) or not value.strip()
            for value in config.values()
        ):
            raise BusinessValidationError("连接 config 的值不能为空")
        if kind == ProviderKind.LOCAL:
            if (
                base_url is not None
                or secret_env_var is not None
                or webhook_secret_env_var is not None
            ):
                raise BusinessValidationError(
                    "本地模拟器不能配置 URL、凭据或 Webhook Secret"
                )
        elif kind == ProviderKind.LEARNING_CI:
            if base_url is not None:
                raise BusinessValidationError(
                    "Learning CI 地址由运行模式固定，不能存入连接表"
                )
            if secret_env_var != CI_LAB_PROVIDER_SECRET_NAME:
                raise BusinessValidationError(
                    "Learning CI 只能使用 QA_PROVIDER_SECRET_CI_LAB"
                )
            if webhook_secret_env_var not in {None, CI_LAB_WEBHOOK_SECRET_NAME}:
                raise BusinessValidationError(
                    "Learning CI Webhook 只能使用独立的 "
                    "QA_PROVIDER_SECRET_CI_LAB_WEBHOOK"
                )
        elif not base_url or not secret_env_var:
            raise BusinessValidationError(
                "自建实验室 provider 必须配置 URL 和凭据环境变量名"
            )
        else:
            definition_keys = {
                ProviderKind.JENKINS: "job_name",
                ProviderKind.GITLAB: "project_id",
                ProviderKind.BK_CI: "pipeline_id",
            }
            expected = config[definition_keys[kind]].strip()
            if definition_ref.strip() != expected:
                raise BusinessValidationError(
                    "definition_ref 必须与 provider 的 Job/Project/Pipeline 标识一致"
                )

    async def _build_enabled_provider(
        self,
        connection: ProviderConnectionRecord,
    ) -> PipelineProvider:
        kind = ProviderKind(connection.kind)
        if kind == ProviderKind.LEARNING_CI:
            if self.safety.provider_runtime_mode != "ci_lab_local":
                raise AuthorizationError(
                    "Learning CI 只允许在显式 ci_lab_local 模式中访问"
                )
            address = (
                CI_LAB_CONTAINER_ADDRESS
                if self.safety.app_env == "local-container"
                else CI_LAB_HOST_ADDRESS
            )
            port = 8080 if self.safety.app_env == "local-container" else 23020
            policy = OutboundPolicy(
                allowed_hosts=(address,),
                allowed_ports=(port,),
                allowed_networks=(f"{address}/32",),
                allowed_http_hosts=(address,),
            )
        else:
            if self.safety.provider_runtime_mode != "self_hosted_lab":
                raise AuthorizationError(
                    "PROVIDER_RUNTIME_MODE="
                    f"{self.safety.provider_runtime_mode} 拒绝 "
                    "Jenkins/GitLab/BK-CI 网络操作"
                )
            if not self.safety.provider_self_hosted_ownership_acknowledged:
                raise AuthorizationError("尚未确认自建 Provider 实验室的环境所有权")
            if self.safety.app_env != "local-container":
                try:
                    loopback_only = all(
                        _is_loopback_host(host)
                        for host in self.safety.provider_allowed_hosts
                    ) and all(
                        _is_loopback_network(network)
                        for network in self.safety.provider_allowed_networks
                    )
                except ValueError:
                    loopback_only = False
                if not loopback_only:
                    raise AuthorizationError(
                        "宿主机 self_hosted_lab 只允许环回目标；"
                        "自建私网只能用于隔离的 local-container"
                    )
            policy = OutboundPolicy(
                allowed_hosts=self.safety.provider_allowed_hosts,
                allowed_ports=self.safety.provider_allowed_ports,
                allowed_networks=self.safety.provider_allowed_networks,
                allow_loopback_http=self.safety.provider_allow_loopback_http,
            )
        if not connection.enabled:
            raise AuthorizationError("该 provider 连接未开启")
        if not connection.secret_env_var:
            raise AuthorizationError("该 provider 未绑定凭据环境变量")
        if connection.secret_env_var not in self.safety.provider_secret_env_names:
            raise AuthorizationError("provider 凭据环境变量不在服务端白名单中")
        try:
            secret = await self._secret_store.read(connection.secret_env_var)
        except SecretStoreError:
            # Secret adapters deliberately expose only generic errors. Do not
            # preserve their exception chain at the HTTP/domain boundary.
            raise AuthorizationError(
                "provider 凭据环境变量不存在或为空，"
                "或 Vault 引用无效/当前不可用"
            ) from None
        try:
            return self._provider_builder(connection, secret, policy)
        except ProviderError as error:
            raise _safe_provider_error(error) from error

    def _validate_provider_dispatch_boundary(
        self,
        connection: ProviderConnectionRecord,
    ) -> None:
        """Fail before persisting an intent that this process may not dispatch."""

        kind = ProviderKind(connection.kind)
        if kind == ProviderKind.LOCAL:
            return
        if kind == ProviderKind.LEARNING_CI:
            if self.safety.provider_runtime_mode != "ci_lab_local":
                raise AuthorizationError(
                    "Learning CI 只允许在显式 ci_lab_local 模式中访问"
                )
            return
        if self.safety.provider_runtime_mode != "self_hosted_lab":
            raise AuthorizationError(
                "当前运行模式拒绝 Jenkins/GitLab/BK-CI 网络操作"
            )
        if not self.safety.provider_self_hosted_ownership_acknowledged:
            raise AuthorizationError("尚未确认自建 Provider 实验室的环境所有权")
        if self.safety.app_env != "local-container":
            try:
                loopback_only = all(
                    _is_loopback_host(host)
                    for host in self.safety.provider_allowed_hosts
                ) and all(
                    _is_loopback_network(network)
                    for network in self.safety.provider_allowed_networks
                )
            except ValueError:
                loopback_only = False
            if not loopback_only:
                raise AuthorizationError(
                    "宿主机 self_hosted_lab 只允许环回目标；"
                    "自建私网只能用于隔离的 local-container"
                )

    @staticmethod
    def _default_provider_builder(
        connection: ProviderConnectionRecord,
        secret: str,
        policy: OutboundPolicy,
    ) -> PipelineProvider:
        kind = ProviderKind(connection.kind)
        config = connection.config
        if kind == ProviderKind.LEARNING_CI:
            if policy.allowed_hosts == (CI_LAB_CONTAINER_ADDRESS,):
                base_url = CI_LAB_CONTAINER_BASE_URL
            elif policy.allowed_hosts == (CI_LAB_HOST_ADDRESS,):
                base_url = CI_LAB_HOST_BASE_URL
            else:
                raise BusinessValidationError(
                    "Learning CI 出站策略与固定实验拓扑不匹配"
                )
            return LearningCiPipelineProvider(
                base_url=base_url,
                definition_id=connection.definition_ref,
                bearer_token=secret,
                policy=policy,
                enabled=True,
            )
        if connection.base_url is None:
            raise BusinessValidationError("真实 provider 缺少 base_url")
        if kind == ProviderKind.JENKINS:
            return JenkinsPipelineProvider(
                base_url=connection.base_url,
                job_name=config["job_name"],
                username=config["username"],
                api_token=secret,
                policy=policy,
                enabled=True,
            )
        if kind == ProviderKind.GITLAB:
            return GitLabPipelineProvider(
                base_url=connection.base_url,
                project_id=config["project_id"],
                private_token=secret,
                policy=policy,
                enabled=True,
            )
        if kind == ProviderKind.BK_CI:
            return BkCiPipelineProvider(
                base_url=connection.base_url,
                project_id=config["project_id"],
                pipeline_id=config["pipeline_id"],
                user_id=config["user_id"],
                bearer_token=secret,
                api_prefix=config.get("api_prefix", "/ms/process/api/user/builds"),
                policy=policy,
                enabled=True,
            )
        raise BusinessValidationError("本地 provider 不需要 HTTP client")

    async def _enqueue_record(
        self,
        session: AsyncSession,
        *,
        task_type: str,
        task_payload: dict[str, Any],
        queue: str,
        priority: int,
        max_attempts: int,
        idempotency_key: str | None,
        source_schedule_id: str | None,
        available_at: datetime | None,
    ) -> tuple[AutomationTaskRecord, bool]:
        self._require_registered_task(task_type)
        now = _utc_now()
        due = _aware(available_at or now)
        request = {
            "task_type": task_type,
            "payload": task_payload,
            "queue": queue,
            "priority": priority,
            "max_attempts": max_attempts,
            "source_schedule_id": source_schedule_id,
            "available_at": _aware(available_at).isoformat() if available_at else None,
        }
        fingerprint = _json_fingerprint(request)
        if idempotency_key is not None:
            previous = await session.scalar(
                select(AutomationTaskRecord).where(
                    AutomationTaskRecord.task_type == task_type,
                    AutomationTaskRecord.idempotency_key == idempotency_key,
                )
            )
            if previous is not None:
                if not hmac.compare_digest(previous.request_fingerprint, fingerprint):
                    raise ConflictError("幂等键已用于不同的任务参数")
                return previous, True
        record = AutomationTaskRecord(
            id=str(uuid4()),
            task_type=task_type,
            payload=task_payload,
            queue=queue,
            priority=priority,
            status=TaskStatus.QUEUED.value,
            idempotency_key=idempotency_key,
            request_fingerprint=fingerprint,
            source_schedule_id=source_schedule_id,
            attempts=0,
            max_attempts=max_attempts,
            available_at=due,
            lease_owner=None,
            lease_token_hash=None,
            lease_expires_at=None,
            heartbeat_at=None,
            cancel_requested=False,
            result=None,
            error_code=None,
            created_at=now,
            started_at=None,
            finished_at=None,
        )
        session.add(record)
        await session.flush()
        await self._add_task_wakeup_outbox(session, record, now=now)
        return record, False

    @staticmethod
    async def _add_task_wakeup_outbox(
        session: AsyncSession,
        task: AutomationTaskRecord,
        *,
        now: datetime,
    ) -> None:
        """Persist a data-free publish fact in the task's own transaction."""

        record = AutomationTaskWakeupOutboxRecord(
            id=str(uuid4()),
            task_id=task.id,
            generation=task.attempts,
            status="pending",
            publish_attempts=0,
            available_at=_aware(task.available_at),
            lease_owner=None,
            lease_token_hash=None,
            lease_expires_at=None,
            last_error_code=None,
            version=0,
            created_at=_aware(now),
            updated_at=_aware(now),
            published_at=None,
        )
        session.add(record)
        await session.flush()

    async def _recover_expired_tasks(self, session: AsyncSession, now: datetime) -> None:
        records = await self.repository.lock_expired_task_leases(
            session,
            now=now,
        )
        for record in records:
            record.error_code = "lease_expired"
            if record.cancel_requested:
                record.status = TaskStatus.CANCELLED.value
                record.finished_at = now
            elif record.attempts < record.max_attempts:
                record.status = TaskStatus.RETRY_WAIT.value
                record.available_at = now + timedelta(
                    seconds=min(300, 2 ** max(0, record.attempts - 1))
                )
                await self._add_task_wakeup_outbox(session, record, now=now)
            else:
                record.status = TaskStatus.DEAD_LETTER.value
                record.finished_at = now
            self._clear_task_lease(record)

    async def _require_task_lease(
        self,
        session: AsyncSession,
        task_id: str,
        worker_id: str,
        lease_token: str,
    ) -> tuple[AutomationTaskRecord, datetime]:
        record = await self._require_task(session, task_id, for_update=True)
        now = await self.repository.database_now(session, fallback=_utc_now())
        self._validate_task_lease_record(record, worker_id, lease_token, now)
        return record, now

    @staticmethod
    def _validate_task_lease_record(
        record: AutomationTaskRecord,
        worker_id: str,
        lease_token: str,
        now: datetime,
    ) -> None:
        if (
            record.status != TaskStatus.RUNNING.value
            or record.lease_owner != worker_id
            or record.lease_token_hash is None
            or not hmac.compare_digest(record.lease_token_hash, _token_hash(lease_token))
            or record.lease_expires_at is None
            or _aware(record.lease_expires_at) <= now
        ):
            raise AuthorizationError("任务租约无效或已过期")

    @staticmethod
    def _validate_device_task_state(
        record: AutomationTaskRecord,
        owner: str,
        now: datetime,
    ) -> None:
        """Validate the locked parent task when no task token is supplied."""

        if (
            record.status != TaskStatus.RUNNING.value
            or record.lease_owner != owner
            or record.lease_token_hash is None
            or record.lease_expires_at is None
            or _aware(record.lease_expires_at) <= now
        ):
            raise AuthorizationError("设备关联的任务租约无效或已过期")
        if record.cancel_requested:
            raise InvalidStateError("任务已请求取消，不能开始设备工作")

    def _validate_leased_device_state(
        self,
        record: DeviceRecord,
        now: datetime,
    ) -> None:
        if self._effective_device_status(record, now) not in {
            DeviceStatus.RESERVED.value,
            DeviceStatus.BUSY.value,
        }:
            raise InvalidStateError("设备已离线、停用或不可继续执行")

    async def _require_active_device_task_lease(
        self,
        session: AsyncSession,
        task_id: str,
        owner: str,
        task_lease_token: str,
    ) -> tuple[AutomationTaskRecord, datetime]:
        """Authenticate the worker before assigning or extending a device."""

        record, now = await self._require_task_lease(
            session, task_id, owner, task_lease_token
        )
        if record.cancel_requested:
            raise InvalidStateError("任务已请求取消，不能申请或续租设备")
        return record, now

    @staticmethod
    def _clear_task_lease(record: AutomationTaskRecord) -> None:
        record.lease_owner = None
        record.lease_token_hash = None
        record.lease_expires_at = None
        record.heartbeat_at = None

    async def _expire_device_leases(self) -> None:
        """Expire each lease under task -> device -> lease locks.

        Locator reads are intentionally non-locking. Each candidate is then
        handled in its own short transaction, so two sweepers can serialize on
        one graph without taking unrelated task locks in opposite orders.
        """

        async with self.repository.transaction() as session:
            observed_now = await self.repository.database_now(
                session, fallback=_utc_now()
            )
            lease_ids = await self.repository.list_expired_device_lease_ids(
                session,
                now=observed_now,
            )
        for lease_id in lease_ids:
            async with self.repository.transaction() as session:
                graph = await self.repository.lock_device_lease_graph(
                    session, lease_id
                )
                if graph is None or graph.lease is None:
                    continue
                if graph.task is None or graph.device is None:
                    raise ConflictError("设备租约关联数据不完整")
                lease = graph.lease
                device = graph.device
                if (
                    lease.task_id != graph.expected_task_id
                    or lease.device_id != graph.expected_device_id
                ):
                    raise ConflictError("设备租约关联在加锁期间发生变化")
                now = await self.repository.database_now(
                    session, fallback=_utc_now()
                )
                if (
                    lease.status != DeviceLeaseStatus.ACTIVE.value
                    or _aware(lease.expires_at) > now
                ):
                    continue
                lease.status = DeviceLeaseStatus.EXPIRED.value
                lease.released_at = now
                lease.version += 1
                if device.active_lease_id == lease.id:
                    device.active_lease_id = None
                    device.status = self._available_device_status(device, now)
                    device.version += 1
                    device.updated_at = now

    async def _require_device_lease(
        self,
        session: AsyncSession,
        lease_id: str,
        owner: str,
        lease_token: str,
    ) -> tuple[DeviceLeaseRecord, DeviceRecord, AutomationTaskRecord, datetime]:
        graph = await self.repository.lock_device_lease_graph(session, lease_id)
        if graph is None or graph.lease is None:
            raise NotFoundError("设备租约", lease_id)
        if graph.task is None or graph.device is None:
            raise ConflictError("设备租约关联数据不完整")
        lease = graph.lease
        device = graph.device
        task = graph.task
        if (
            lease.task_id != graph.expected_task_id
            or lease.device_id != graph.expected_device_id
            or task.id != lease.task_id
            or device.id != lease.device_id
        ):
            raise ConflictError("设备租约关联在加锁期间发生变化")
        now = await self.repository.database_now(session, fallback=_utc_now())
        if (
            lease.status != DeviceLeaseStatus.ACTIVE.value
            or lease.owner != owner
            or _aware(lease.expires_at) <= now
            or not hmac.compare_digest(lease.token_hash, _token_hash(lease_token))
            or device.active_lease_id != lease.id
        ):
            raise AuthorizationError("设备租约无效或已过期")
        return lease, device, task, now

    @staticmethod
    def _validate_scheduler_claim(scheduler_id: str, lease_seconds: int) -> None:
        if not scheduler_id.strip() or len(scheduler_id) > 200:
            raise BusinessValidationError(
                "scheduler_id 必须是 1 到 200 个字符的稳定实例标识"
            )
        if not 5 <= lease_seconds <= 3_600:
            raise BusinessValidationError("Scheduler 租约必须在 5 到 3600 秒之间")

    async def _claim_due_schedule(
        self,
        scheduler_id: str,
        lease_seconds: int,
        *,
        fallback_now: datetime | None,
        excluded_ids: set[str] | None = None,
    ) -> _ClaimedSchedule | None:
        """T1: lock one candidate and commit only a bounded lease."""

        async with self.repository.transaction() as session:
            lease_now = await self.repository.database_now(
                session,
                fallback=_utc_now(),
            )
            # Injected time exists only for deterministic SQLite lessons.
            # PostgreSQL always uses its own clock for both due selection and
            # lease expiry, so one process cannot time-travel other instances.
            due_at = (
                lease_now
                if self.repository.is_postgresql
                else _aware(fallback_now or lease_now)
            )
            record = await self.repository.claim_schedule_candidate(
                session,
                now=lease_now,
                due_at=due_at,
                excluded_ids=excluded_ids or (),
            )
            if record is None or record.next_run_at is None:
                return None
            claim_token = secrets.token_urlsafe(32)
            record.claim_owner = scheduler_id
            record.claim_token_hash = _token_hash(claim_token)
            record.claim_expires_at = lease_now + timedelta(seconds=lease_seconds)
            await session.flush()
            return _ClaimedSchedule(
                id=record.id,
                task_type=record.task_type,
                payload=dict(record.payload),
                queue=record.queue,
                priority=record.priority,
                max_attempts=record.max_attempts,
                cron=record.cron,
                timezone=record.timezone,
                misfire_policy=record.misfire_policy,
                overlap_policy=record.overlap_policy,
                misfire_grace_seconds=record.misfire_grace_seconds,
                catch_up_limit=record.catch_up_limit,
                first_due=_aware(record.next_run_at),
                claimed_at=due_at,
                expected_version=record.version,
                scheduler_id=scheduler_id,
                claim_token=claim_token,
            )

    async def _execute_schedule_claim(
        self,
        schedule: _ClaimedSchedule,
    ) -> list[ScheduleFireView]:
        """Plan outside SQL, then let T2 atomically CAS and enqueue."""

        try:
            expression = _parse_cron(schedule.cron)
            policy = MisfirePolicy(schedule.misfire_policy)
            plan = await asyncio.to_thread(
                _cron_due_plan,
                expression,
                schedule.first_due,
                schedule.claimed_at,
                schedule.timezone,
                policy,
                schedule.misfire_grace_seconds,
                schedule.catch_up_limit,
            )
        except AutomationValidationError as error:
            await self._release_schedule_claim(schedule)
            raise BusinessValidationError(str(error)) from error
        except BaseException:
            await self._release_schedule_claim(schedule)
            raise
        return await self._finalize_schedule_claim(schedule, plan)

    async def _release_schedule_claim(self, schedule: _ClaimedSchedule) -> None:
        async with self.repository.transaction() as session:
            await self.repository.release_schedule_claim(
                session,
                schedule_id=schedule.id,
                scheduler_id=schedule.scheduler_id,
                claim_token_hash=_token_hash(schedule.claim_token),
            )

    async def _finalize_schedule_claim(
        self,
        schedule: _ClaimedSchedule,
        plan: _CronDuePlan,
    ) -> list[ScheduleFireView]:
        """T2: token + version CAS, fires and tasks share one transaction."""

        async with self.repository.transaction() as session:
            now = await self.repository.database_now(
                session,
                fallback=_utc_now(),
            )
            finalized = await self.repository.finalize_schedule_claim(
                session,
                schedule_id=schedule.id,
                expected_version=schedule.expected_version,
                expected_next_run_at=schedule.first_due,
                scheduler_id=schedule.scheduler_id,
                claim_token_hash=_token_hash(schedule.claim_token),
                now=now,
                last_run_at=plan.latest_due,
                next_run_at=plan.next_run_at,
            )
            if not finalized:
                # This is guarded by the token and cannot clear a newer claim.
                await self.repository.release_schedule_claim(
                    session,
                    schedule_id=schedule.id,
                    scheduler_id=schedule.scheduler_id,
                    claim_token_hash=_token_hash(schedule.claim_token),
                )
                return []

            created: list[ScheduleFireView] = []
            for instant in plan.skipped:
                fire = await self._create_fire_if_absent(
                    session,
                    schedule.id,
                    instant,
                    ScheduleFireStatus.SKIPPED_MISFIRE,
                    now,
                )
                if fire is not None:
                    created.append(self._schedule_fire_view(fire))

            for instant in plan.selected:
                fire_key = f"cron:{instant.isoformat()}"
                exists = await session.scalar(
                    select(ScheduleFireRecord.id).where(
                        ScheduleFireRecord.schedule_id == schedule.id,
                        ScheduleFireRecord.fire_key == fire_key,
                    )
                )
                if exists is not None:
                    continue
                active = list(
                    (
                        await session.scalars(
                            select(AutomationTaskRecord).where(
                                AutomationTaskRecord.source_schedule_id
                                == schedule.id,
                                AutomationTaskRecord.status.in_(
                                    ["queued", "running", "retry_wait"]
                                ),
                            )
                        )
                    ).all()
                )
                overlap = OverlapPolicy(schedule.overlap_policy)
                if overlap == OverlapPolicy.FORBID and active:
                    fire = await self._create_fire_if_absent(
                        session,
                        schedule.id,
                        instant,
                        ScheduleFireStatus.SKIPPED_OVERLAP,
                        now,
                    )
                else:
                    if overlap == OverlapPolicy.REPLACE:
                        for task in active:
                            task.cancel_requested = True
                            if task.status != TaskStatus.RUNNING.value:
                                task.status = TaskStatus.CANCELLED.value
                                task.finished_at = now
                    task, _ = await self._enqueue_record(
                        session,
                        task_type=schedule.task_type,
                        task_payload=schedule.payload,
                        queue=schedule.queue,
                        priority=schedule.priority,
                        max_attempts=schedule.max_attempts,
                        idempotency_key=(
                            f"schedule:{schedule.id}:{instant.isoformat()}"
                        ),
                        source_schedule_id=schedule.id,
                        available_at=instant,
                    )
                    fire = ScheduleFireRecord(
                        id=str(uuid4()),
                        schedule_id=schedule.id,
                        fire_key=fire_key,
                        scheduled_for=instant,
                        status=ScheduleFireStatus.ENQUEUED.value,
                        task_id=task.id,
                        created_at=now,
                    )
                    session.add(fire)
                    await session.flush()
                if fire is not None:
                    created.append(self._schedule_fire_view(fire))
            return created

    async def _create_fire_if_absent(
        self,
        session: AsyncSession,
        schedule_id: str,
        instant: datetime,
        status: ScheduleFireStatus,
        now: datetime,
    ) -> ScheduleFireRecord | None:
        key = f"cron:{instant.isoformat()}"
        exists = await session.scalar(
            select(ScheduleFireRecord.id).where(
                ScheduleFireRecord.schedule_id == schedule_id,
                ScheduleFireRecord.fire_key == key,
            )
        )
        if exists is not None:
            return None
        fire = ScheduleFireRecord(
            id=str(uuid4()),
            schedule_id=schedule_id,
            fire_key=key,
            scheduled_for=instant,
            status=status.value,
            task_id=None,
            created_at=now,
        )
        session.add(fire)
        await session.flush()
        return fire

    def _require_registered_task(self, task_type: str) -> None:
        if task_type not in self.task_types:
            raise BusinessValidationError(
                "未知 task_type；仅允许服务端固定注册表中的教学任务，不能执行 shell 或模块路径"
            )

    @staticmethod
    def _validate_capabilities(values: set[str]) -> None:
        if len(values) > 100 or any(not value or len(value) > 100 for value in values):
            raise BusinessValidationError("设备能力标签无效")

    async def _require_connection(
        self, session: AsyncSession, connection_id: str
    ) -> ProviderConnectionRecord:
        record = await session.get(ProviderConnectionRecord, connection_id)
        if record is None:
            raise NotFoundError("集成连接", connection_id)
        return record

    async def _require_task(
        self,
        session: AsyncSession,
        task_id: str,
        *,
        for_update: bool = False,
    ) -> AutomationTaskRecord:
        record = (
            await self.repository.get_task_for_update(session, task_id)
            if for_update
            else await session.get(AutomationTaskRecord, task_id)
        )
        if record is None:
            raise NotFoundError("自动化任务", task_id)
        return record

    async def _require_device(
        self,
        session: AsyncSession,
        device_id: str,
        *,
        for_update: bool = False,
    ) -> DeviceRecord:
        record = (
            await self.repository.get_device_for_update(session, device_id)
            if for_update
            else await session.get(DeviceRecord, device_id)
        )
        if record is None:
            raise NotFoundError("设备", device_id)
        return record

    async def _require_schedule(
        self,
        session: AsyncSession,
        schedule_id: str,
        *,
        for_update: bool = False,
    ) -> ScheduleRecord:
        record = (
            await self.repository.get_schedule_for_update(session, schedule_id)
            if for_update
            else await session.get(ScheduleRecord, schedule_id)
        )
        if record is None:
            raise NotFoundError("定时任务", schedule_id)
        return record

    def _connection_view(self, record: ProviderConnectionRecord) -> ProviderConnectionView:
        return ProviderConnectionView(
            id=record.id,
            name=record.name,
            kind=ProviderKind(record.kind),
            base_url=record.base_url,
            definition_ref=record.definition_ref,
            config=dict(record.config),
            secret_env_var=record.secret_env_var,
            secret_configured=bool(
                record.secret_env_var
                and record.secret_env_var in self.safety.provider_secret_env_names
                and (
                    self._secret_store_runtime_mode == "vault_local_container"
                    or self._environ.get(record.secret_env_var)
                )
            ),
            webhook_secret_env_var=record.webhook_secret_env_var,
            webhook_secret_configured=bool(
                record.webhook_secret_env_var
                and record.webhook_secret_env_var
                in self.safety.provider_secret_env_names
                and (
                    self._secret_store_runtime_mode == "vault_local_container"
                    or self._environ.get(record.webhook_secret_env_var)
                )
            ),
            enabled=record.enabled,
            version=record.version,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
        )

    @staticmethod
    def _provider_run_view(
        record: ProviderRunRecord,
        approvals: list[ProviderRunApprovalView] | None = None,
    ) -> ProviderRunView:
        return ProviderRunView(
            id=record.id,
            connection_id=record.connection_id,
            external_id=record.external_id,
            status=record.status,
            raw_status=record.raw_status,
            web_url=record.web_url,
            message=record.message,
            metadata=dict(record.run_metadata),
            correlation_id=record.correlation_id,
            dispatch_status=record.dispatch_status,
            quality_gate_status=record.quality_gate_status,
            reconciliation_required=record.reconciliation_required,
            last_provider_sequence=record.last_provider_sequence,
            triggered_by_name=record.triggered_by_name,
            approvals=approvals or [],
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
        )

    @staticmethod
    def _approval_view(record: ProviderRunApprovalRecord) -> ProviderRunApprovalView:
        return ProviderRunApprovalView(
            id=record.id,
            run_id=record.run_id,
            event_id=record.event_id,
            decision=record.decision,
            actor_name=record.actor_name,
            comment=record.comment,
            created_at=_aware(record.created_at),
        )

    async def _approval_views(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> list[ProviderRunApprovalView]:
        records = list(
            (
                await session.scalars(
                    select(ProviderRunApprovalRecord)
                    .where(ProviderRunApprovalRecord.run_id == run_id)
                    .order_by(ProviderRunApprovalRecord.created_at)
                )
            ).all()
        )
        return [self._approval_view(record) for record in records]

    @staticmethod
    def _provider_intent_view(
        record: ProviderTriggerIntentRecord,
    ) -> ProviderTriggerIntentView:
        return ProviderTriggerIntentView(
            id=record.id,
            run_id=record.run_id,
            connection_id=record.connection_id,
            status=record.status,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            available_at=_aware(record.available_at),
            lease_owner=record.lease_owner,
            lease_expires_at=_utc(record.lease_expires_at),
            last_error_code=record.last_error_code,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
            completed_at=_utc(record.completed_at),
        )

    @staticmethod
    def _apply_provider_run_snapshot(
        record: ProviderRunRecord,
        run: ProviderRun,
        *,
        source: str,
    ) -> bool:
        """Apply a provider snapshot without allowing terminal regression."""

        terminal = {"succeeded", "failed", "cancelled"}
        next_status = run.status.value
        if (
            source != "gate_decision"
            and record.quality_gate_status == "waiting_approval"
            and run.raw_status != "waiting_approval"
            and next_status not in {"failed", "cancelled"}
        ):
            # Polling is an observation channel, not an authorization channel.
            # A provider snapshot must never release a mandatory human gate.
            if not record.reconciliation_required:
                record.reconciliation_required = True
                record.updated_at = _utc_now()
                record.version += 1
            return False
        if record.status in terminal and next_status != record.status:
            record.reconciliation_required = True
            record.updated_at = _utc_now()
            record.version += 1
            return False
        record.external_id = run.external_id
        record.status = next_status
        record.raw_status = run.raw_status
        record.web_url = run.web_url
        record.message = run.message
        record.run_metadata = dict(run.metadata)
        gate_status = run.quality_gate.status.value
        if gate_status == ProviderQualityGateStatus.NOT_REQUIRED.value:
            if run.raw_status == "waiting_approval":
                gate_status = ProviderQualityGateStatus.WAITING_APPROVAL.value
            elif next_status == "failed" and record.quality_gate_status != "rejected":
                gate_status = ProviderQualityGateStatus.FAILED.value
            elif next_status == "cancelled":
                gate_status = ProviderQualityGateStatus.CANCELLED.value
        record.quality_gate_status = gate_status
        if source in {"poll", "gate_decision"}:
            record.reconciliation_required = False
        record.updated_at = _utc_now()
        record.version += 1
        return True

    @staticmethod
    def _task_view(record: AutomationTaskRecord) -> TaskView:
        return TaskView(
            id=record.id,
            task_type=record.task_type,
            payload=dict(record.payload),
            queue=record.queue,
            priority=record.priority,
            status=record.status,
            idempotency_key=record.idempotency_key,
            source_schedule_id=record.source_schedule_id,
            attempts=record.attempts,
            max_attempts=record.max_attempts,
            available_at=_aware(record.available_at),
            lease_owner=record.lease_owner,
            lease_expires_at=_utc(record.lease_expires_at),
            heartbeat_at=_utc(record.heartbeat_at),
            cancel_requested=record.cancel_requested,
            result=dict(record.result) if record.result is not None else None,
            error_code=record.error_code,
            created_at=_aware(record.created_at),
            started_at=_utc(record.started_at),
            finished_at=_utc(record.finished_at),
        )

    @staticmethod
    def _task_wakeup_outbox_view(
        record: AutomationTaskWakeupOutboxRecord,
    ) -> TaskWakeupOutboxView:
        return TaskWakeupOutboxView(
            id=record.id,
            task_id=record.task_id,
            generation=record.generation,
            status=record.status,
            publish_attempts=record.publish_attempts,
            available_at=_aware(record.available_at),
            lease_owner=record.lease_owner,
            lease_expires_at=_utc(record.lease_expires_at),
            last_error_code=record.last_error_code,
            version=record.version,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
            published_at=_utc(record.published_at),
        )

    def _effective_device_status(self, record: DeviceRecord, now: datetime) -> str:
        if record.status == DeviceStatus.MAINTENANCE.value:
            return record.status
        if not record.enabled or record.last_heartbeat_at is None:
            return DeviceStatus.OFFLINE.value
        if _aware(record.last_heartbeat_at) + self._offline_after <= now:
            return DeviceStatus.OFFLINE.value
        if record.active_lease_id is not None:
            return record.status
        return DeviceStatus.IDLE.value

    def _available_device_status(self, record: DeviceRecord, now: datetime) -> str:
        # This helper is used while *leaving* maintenance and while releasing
        # a lease.  It must therefore derive availability without preserving
        # the previous maintenance/reserved/busy state.
        if not record.enabled or record.last_heartbeat_at is None:
            return DeviceStatus.OFFLINE.value
        if _aware(record.last_heartbeat_at) + self._offline_after <= now:
            return DeviceStatus.OFFLINE.value
        return DeviceStatus.IDLE.value

    def _device_view(self, record: DeviceRecord, now: datetime) -> DeviceView:
        return DeviceView(
            id=record.id,
            name=record.name,
            kind=record.kind,
            platform=record.platform,
            capabilities=list(record.capabilities),
            enabled=record.enabled,
            status=self._effective_device_status(record, now),
            last_heartbeat_at=_utc(record.last_heartbeat_at),
            active_lease_id=record.active_lease_id,
            version=record.version,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
        )

    @staticmethod
    def _device_lease_view(record: DeviceLeaseRecord) -> DeviceLeaseView:
        return DeviceLeaseView(
            id=record.id,
            device_id=record.device_id,
            task_id=record.task_id,
            owner=record.owner,
            status=record.status,
            acquired_at=_aware(record.acquired_at),
            expires_at=_aware(record.expires_at),
            released_at=_utc(record.released_at),
            version=record.version,
        )

    @staticmethod
    def _schedule_view(record: ScheduleRecord) -> ScheduleView:
        return ScheduleView(
            id=record.id,
            name=record.name,
            task_type=record.task_type,
            payload=dict(record.payload),
            queue=record.queue,
            priority=record.priority,
            max_attempts=record.max_attempts,
            cron=record.cron,
            timezone=record.timezone,
            misfire_policy=record.misfire_policy,
            overlap_policy=record.overlap_policy,
            misfire_grace_seconds=record.misfire_grace_seconds,
            catch_up_limit=record.catch_up_limit,
            enabled=record.enabled,
            next_run_at=_utc(record.next_run_at),
            last_run_at=_utc(record.last_run_at),
            version=record.version,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
        )

    @staticmethod
    def _schedule_fire_view(record: ScheduleFireRecord) -> ScheduleFireView:
        return ScheduleFireView(
            id=record.id,
            schedule_id=record.schedule_id,
            scheduled_for=_aware(record.scheduled_for),
            status=record.status,
            task_id=record.task_id,
            created_at=_aware(record.created_at),
        )


def create_runtime_service(
    database: Database,
    settings: object | None = None,
    **kwargs: Any,
) -> PersistentRuntimeService:
    """Factory intended for ``application.state.runtime_service`` wiring."""

    return PersistentRuntimeService(database, settings, **kwargs)


__all__ = [
    "DEFAULT_TASK_TYPES",
    "PersistentRuntimeService",
    "RuntimeSafetyConfig",
    "create_runtime_service",
]
