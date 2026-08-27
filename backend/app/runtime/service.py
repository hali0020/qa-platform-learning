"""Durable, local-first integration and automation learning service.

Every state transition is persisted in SQL. SQLite keeps its process-local
teaching lock; PostgreSQL task workers claim and recover leases with row locks
and ``SKIP LOCKED``. This remains an at-least-once task database, not a broker:
scheduler leader election and an outbox are separate production concerns.
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
from app.database.session import Database
from app.pipeline.providers.base import PipelineProvider
from app.pipeline.providers.bkci import BkCiPipelineProvider
from app.pipeline.providers.errors import (
    ProviderConflictError,
    ProviderDisabledError,
    ProviderError,
    ProviderSecurityError,
)
from app.pipeline.providers.gitlab import GitLabPipelineProvider
from app.pipeline.providers.jenkins import JenkinsPipelineProvider
from app.pipeline.providers.learning_ci import LearningCiPipelineProvider
from app.pipeline.providers.models import ProviderKind, ProviderRun, ProviderTriggerRequest
from app.pipeline.providers.security import OutboundPolicy
from app.runtime.orm import (
    AutomationTaskRecord,
    DeviceLeaseRecord,
    DeviceRecord,
    ProviderConnectionRecord,
    ProviderRunRecord,
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
    ProviderRunView,
    ProviderTestResult,
    ProviderTriggerPayload,
    ScheduleCreate,
    ScheduleFireView,
    SchedulePatch,
    ScheduleView,
    TaskEnqueue,
    TaskView,
)
from app.secrets import (
    EnvironmentSecretStore,
    SecretStore,
    SecretStoreError,
)


DEFAULT_TASK_TYPES = frozenset(
    {
        "qa.import.validate",
        "qa.pipeline.poll",
        "qa.quality.generate",
        "qa.device.execute",
    }
)

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
        request = ProviderTriggerRequest(
            definition_ref="placeholder",  # replaced from stored binding below
            ref=payload.ref,
            variables=payload.variables,
            correlation_id=payload.correlation_id,
        )
        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            request.definition_ref = connection.definition_ref
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
                    return self._provider_run_view(previous)

            kind = ProviderKind(connection.kind)
            with self._observe_provider(kind, "trigger"):
                if kind == ProviderKind.LOCAL and not connection.enabled:
                    raise AuthorizationError("该 provider 连接未开启")
                if kind == ProviderKind.LOCAL:
                    run = ProviderRun(
                        provider=kind,
                        external_id=str(uuid4()),
                        status="queued",
                        raw_status="queued",
                        metadata={"definition_ref": connection.definition_ref, "ref": payload.ref},
                    )
                else:
                    provider = await self._build_enabled_provider(connection)
                    try:
                        try:
                            run = await provider.trigger(request)
                        except ProviderError as error:
                            raise _safe_provider_error(error) from error
                    finally:
                        await provider.aclose()
            now = _utc_now()
            record = ProviderRunRecord(
                id=str(uuid4()),
                connection_id=connection_id,
                external_id=run.external_id,
                status=run.status.value,
                raw_status=run.raw_status,
                web_url=run.web_url,
                message=run.message,
                run_metadata=dict(run.metadata),
                correlation_id=payload.correlation_id,
                request_fingerprint=fingerprint,
                created_at=now,
                updated_at=now,
            )
            session.add(record)
            await session.flush()
            return self._provider_run_view(record)

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
            return [self._provider_run_view(record) for record in records]

    async def get_provider_run(self, connection_id: str, run_id: str) -> ProviderRunView:
        return await self._refresh_provider_run(connection_id, run_id, cancel=False)

    async def cancel_provider_run(self, connection_id: str, run_id: str) -> ProviderRunView:
        return await self._refresh_provider_run(connection_id, run_id, cancel=True)

    async def _refresh_provider_run(
        self, connection_id: str, run_id: str, *, cancel: bool
    ) -> ProviderRunView:
        async with self.repository.transaction() as session:
            connection = await self._require_connection(session, connection_id)
            record = await session.get(ProviderRunRecord, run_id)
            if record is None or record.connection_id != connection_id:
                raise NotFoundError("集成运行", run_id)
            kind = ProviderKind(connection.kind)
            with self._observe_provider(kind, "cancel" if cancel else "query"):
                if kind == ProviderKind.LOCAL and not connection.enabled:
                    raise AuthorizationError("该 provider 连接未开启")
                if kind == ProviderKind.LOCAL:
                    if cancel and record.status not in {"succeeded", "failed", "cancelled"}:
                        record.status = "cancelled"
                        record.raw_status = "cancelled"
                        record.message = "cancelled by local simulator"
                        record.updated_at = _utc_now()
                    return self._provider_run_view(record)
                provider = await self._build_enabled_provider(connection)
                try:
                    try:
                        run = (
                            await provider.cancel(record.external_id)
                            if cancel
                            else await provider.get(record.external_id)
                        )
                    except ProviderError as error:
                        raise _safe_provider_error(error) from error
                finally:
                    await provider.aclose()
                record.status = run.status.value
                record.raw_status = run.raw_status
                record.web_url = run.web_url
                record.message = run.message
                record.run_metadata = dict(run.metadata)
                record.updated_at = _utc_now()
                return self._provider_run_view(record)

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
        await self._publish_task_wakeup()
        return result

    async def _publish_task_wakeup(self) -> None:
        """Best-effort hint after the authoritative database commit.

        RabbitMQ never owns task state. A failed hint is therefore logged with
        metadata only and recovered by the Worker's bounded database polling.
        """

        if self._wakeup_publisher is None:
            return
        try:
            await self._wakeup_publisher.publish_wakeup()
        except Exception as error:
            # Do not log exception text: transport errors can contain a URL or
            # credential. Cancellation still propagates on supported Python.
            logger.warning(
                "task wake-up publish failed error_type=%s",
                type(error).__name__,
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
            record = await self._require_schedule(session, schedule_id)
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
            return self._schedule_view(record)

    async def delete_schedule(self, schedule_id: str) -> bool:
        async with self.repository.transaction() as session:
            record = await self._require_schedule(session, schedule_id)
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
            schedule = await self._require_schedule(session, schedule_id)
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

    async def tick_schedules(self, now: datetime | None = None) -> list[ScheduleFireView]:
        current = _aware(now or _utc_now())
        created: list[ScheduleFireView] = []
        async with self.repository.transaction() as session:
            schedules = list(
                (
                    await session.scalars(
                        select(ScheduleRecord).where(
                            ScheduleRecord.enabled.is_(True),
                            ScheduleRecord.next_run_at.is_not(None),
                            ScheduleRecord.next_run_at <= current,
                        )
                    )
                ).all()
            )
            schedules.sort(key=lambda item: (_aware(item.next_run_at), item.id))
            for schedule in schedules:
                created.extend(await self._tick_one_schedule(session, schedule, current))
        return created

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
            if base_url is not None or secret_env_var is not None:
                raise BusinessValidationError("本地模拟器不能配置 URL 或凭据环境变量")
        elif kind == ProviderKind.LEARNING_CI:
            if base_url is not None:
                raise BusinessValidationError(
                    "Learning CI 地址由运行模式固定，不能存入连接表"
                )
            if secret_env_var != CI_LAB_PROVIDER_SECRET_NAME:
                raise BusinessValidationError(
                    "Learning CI 只能使用 QA_PROVIDER_SECRET_CI_LAB"
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
        return record, False

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

    async def _tick_one_schedule(
        self, session: AsyncSession, schedule: ScheduleRecord, now: datetime
    ) -> list[ScheduleFireView]:
        expression = _parse_cron(schedule.cron)
        first_due = _utc(schedule.next_run_at)
        if first_due is None:
            return []
        policy = MisfirePolicy(schedule.misfire_policy)
        try:
            plan = await asyncio.to_thread(
                _cron_due_plan,
                expression,
                first_due,
                now,
                schedule.timezone,
                policy,
                schedule.misfire_grace_seconds,
                schedule.catch_up_limit,
            )
        except AutomationValidationError as error:
            raise BusinessValidationError(str(error)) from error

        created: list[ScheduleFireView] = []
        for instant in plan.skipped:
            fire = await self._create_fire_if_absent(
                session, schedule, instant, ScheduleFireStatus.SKIPPED_MISFIRE, now
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
                            AutomationTaskRecord.source_schedule_id == schedule.id,
                            AutomationTaskRecord.status.in_(["queued", "running", "retry_wait"]),
                        )
                    )
                ).all()
            )
            overlap = OverlapPolicy(schedule.overlap_policy)
            if overlap == OverlapPolicy.FORBID and active:
                fire = await self._create_fire_if_absent(
                    session, schedule, instant, ScheduleFireStatus.SKIPPED_OVERLAP, now
                )
            else:
                if overlap == OverlapPolicy.REPLACE:
                    for task in active:
                        if task.status == TaskStatus.RUNNING.value:
                            task.cancel_requested = True
                        else:
                            task.cancel_requested = True
                            task.status = TaskStatus.CANCELLED.value
                            task.finished_at = now
                task, _ = await self._enqueue_record(
                    session,
                    task_type=schedule.task_type,
                    task_payload=schedule.payload,
                    queue=schedule.queue,
                    priority=schedule.priority,
                    max_attempts=schedule.max_attempts,
                    idempotency_key=f"schedule:{schedule.id}:{instant.isoformat()}",
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
        schedule.last_run_at = plan.latest_due
        schedule.next_run_at = plan.next_run_at
        schedule.version += 1
        schedule.updated_at = now
        return created

    async def _create_fire_if_absent(
        self,
        session: AsyncSession,
        schedule: ScheduleRecord,
        instant: datetime,
        status: ScheduleFireStatus,
        now: datetime,
    ) -> ScheduleFireRecord | None:
        key = f"cron:{instant.isoformat()}"
        exists = await session.scalar(
            select(ScheduleFireRecord.id).where(
                ScheduleFireRecord.schedule_id == schedule.id,
                ScheduleFireRecord.fire_key == key,
            )
        )
        if exists is not None:
            return None
        fire = ScheduleFireRecord(
            id=str(uuid4()),
            schedule_id=schedule.id,
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
        self, session: AsyncSession, schedule_id: str
    ) -> ScheduleRecord:
        record = await session.get(ScheduleRecord, schedule_id)
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
            enabled=record.enabled,
            version=record.version,
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
        )

    @staticmethod
    def _provider_run_view(record: ProviderRunRecord) -> ProviderRunView:
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
            created_at=_aware(record.created_at),
            updated_at=_aware(record.updated_at),
        )

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
