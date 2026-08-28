"""Database transaction and worker-claim boundary for runtime services."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, TypeVar

from sqlalchemy import DateTime, Select, Update, and_, func, or_, select, update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.models import DeviceLeaseStatus, DeviceStatus, TaskStatus
from app.core.errors import ConflictError
from app.database.base import Base
from app.database.session import Database
from app.runtime.orm import (
    AutomationTaskRecord,
    AutomationTaskWakeupOutboxRecord,
    DeviceLeaseRecord,
    DeviceRecord,
    ProviderRunRecord,
    ProviderTriggerIntentRecord,
    ScheduleRecord,
)


RecordT = TypeVar("RecordT", bound=Base)


@dataclass(frozen=True, slots=True)
class LockedDeviceLeaseGraph:
    """Rows locked in the global task -> device -> lease order."""

    task: AutomationTaskRecord | None
    device: DeviceRecord | None
    lease: DeviceLeaseRecord | None
    expected_task_id: str
    expected_device_id: str


def build_task_claim_statement(
    *,
    queues: Collection[str],
    now: datetime,
    use_skip_locked: bool,
) -> Select[tuple[AutomationTaskRecord]]:
    """Build the deterministic claim query used inside one transaction.

    PostgreSQL workers lock only the highest-ranked available row and skip a
    row already being claimed by another transaction.  SQLite deliberately
    omits the clause and relies on the repository's teaching-mode process lock.
    """

    statement = (
        select(AutomationTaskRecord)
        .where(
            AutomationTaskRecord.queue.in_(queues),
            AutomationTaskRecord.status.in_(
                (TaskStatus.QUEUED.value, TaskStatus.RETRY_WAIT.value)
            ),
            AutomationTaskRecord.attempts < AutomationTaskRecord.max_attempts,
            AutomationTaskRecord.available_at <= now,
            AutomationTaskRecord.cancel_requested.is_(False),
        )
        .order_by(
            AutomationTaskRecord.priority.desc(),
            AutomationTaskRecord.available_at,
            AutomationTaskRecord.created_at,
            AutomationTaskRecord.id,
        )
        .limit(1)
    )
    if use_skip_locked:
        statement = statement.with_for_update(skip_locked=True)
    return statement


def build_expired_task_lease_statement(
    *,
    now: datetime,
    use_skip_locked: bool,
) -> Select[tuple[AutomationTaskRecord]]:
    """Select expired running leases for transactional recovery."""

    statement = (
        select(AutomationTaskRecord)
        .where(
            AutomationTaskRecord.status == TaskStatus.RUNNING.value,
            AutomationTaskRecord.lease_expires_at <= now,
        )
        .order_by(AutomationTaskRecord.lease_expires_at, AutomationTaskRecord.id)
    )
    if use_skip_locked:
        statement = statement.with_for_update(skip_locked=True)
    return statement


def build_device_candidate_statement(
    *,
    candidate_ids: Collection[str],
    heartbeat_after: datetime,
    use_skip_locked: bool,
) -> Select[tuple[DeviceRecord]]:
    """Build a deterministic, one-row device claim query.

    Capability matching is intentionally completed before this statement with
    a read-only column snapshot because generic JSON containment has different
    semantics in SQLite and PostgreSQL.  This statement then locks only IDs
    that matched that snapshot, and callers must revalidate every invariant on
    the returned, locked row.
    """

    statement = (
        select(DeviceRecord)
        .where(
            DeviceRecord.id.in_(candidate_ids),
            DeviceRecord.enabled.is_(True),
            DeviceRecord.status == DeviceStatus.IDLE.value,
            DeviceRecord.active_lease_id.is_(None),
            DeviceRecord.last_heartbeat_at.is_not(None),
            DeviceRecord.last_heartbeat_at > heartbeat_after,
        )
        .order_by(func.lower(DeviceRecord.name), DeviceRecord.id)
        .limit(1)
    )
    if use_skip_locked:
        statement = statement.with_for_update(skip_locked=True)
    return statement


def build_postgres_clock_statement() -> Select[tuple[datetime]]:
    """Use wall-clock time after lock waits, not transaction-start time."""

    return select(func.clock_timestamp(type_=DateTime(timezone=True)))


def build_provider_trigger_claim_statement(
    *,
    now: datetime,
    use_skip_locked: bool,
) -> Select[tuple[ProviderTriggerIntentRecord]]:
    """Claim one due intent, including a dispatcher lease that expired."""

    statement = (
        select(ProviderTriggerIntentRecord)
        .where(
            ProviderTriggerIntentRecord.attempts
            < ProviderTriggerIntentRecord.max_attempts,
            or_(
                and_(
                    ProviderTriggerIntentRecord.status.in_(
                        ("pending", "retry_wait")
                    ),
                    ProviderTriggerIntentRecord.available_at <= now,
                ),
                and_(
                    ProviderTriggerIntentRecord.status == "claimed",
                    ProviderTriggerIntentRecord.lease_expires_at.is_not(None),
                    ProviderTriggerIntentRecord.lease_expires_at <= now,
                ),
            ),
        )
        .order_by(
            ProviderTriggerIntentRecord.available_at,
            ProviderTriggerIntentRecord.created_at,
            ProviderTriggerIntentRecord.id,
        )
        .limit(1)
    )
    if use_skip_locked:
        statement = statement.with_for_update(skip_locked=True)
    return statement


def build_task_wakeup_outbox_claim_statement(
    *,
    now: datetime,
    use_skip_locked: bool,
) -> Select[tuple[AutomationTaskWakeupOutboxRecord]]:
    """Select one due or lease-expired content-free wake-up event."""

    statement = (
        select(AutomationTaskWakeupOutboxRecord)
        .where(
            or_(
                and_(
                    AutomationTaskWakeupOutboxRecord.status.in_(
                        ("pending", "retry_wait")
                    ),
                    AutomationTaskWakeupOutboxRecord.available_at <= now,
                ),
                and_(
                    AutomationTaskWakeupOutboxRecord.status == "claimed",
                    AutomationTaskWakeupOutboxRecord.lease_expires_at.is_not(None),
                    AutomationTaskWakeupOutboxRecord.lease_expires_at <= now,
                ),
            )
        )
        .order_by(
            AutomationTaskWakeupOutboxRecord.available_at,
            AutomationTaskWakeupOutboxRecord.created_at,
            AutomationTaskWakeupOutboxRecord.id,
        )
        .limit(1)
    )
    if use_skip_locked:
        statement = statement.with_for_update(skip_locked=True)
    return statement


def build_task_wakeup_outbox_settle_statement(
    *,
    outbox_id: str,
    dispatcher_id: str,
    lease_token_hash: str,
    expected_version: int,
    now: datetime,
    published: bool,
    retry_at: datetime | None = None,
    use_database_clock: bool = False,
) -> Update:
    """Build the token/version/expiry CAS used after broker I/O."""

    lease_cutoff = (
        func.clock_timestamp(type_=DateTime(timezone=True))
        if use_database_clock
        else now
    )
    if published:
        values = {
            "status": "published",
            "published_at": now,
            "last_error_code": None,
        }
    else:
        if retry_at is None:
            raise ValueError("retry_at is required for a failed publish")
        values = {
            "status": "retry_wait",
            "available_at": retry_at,
            "published_at": None,
            "last_error_code": "broker_publish_failed",
        }
    return (
        update(AutomationTaskWakeupOutboxRecord)
        .where(
            AutomationTaskWakeupOutboxRecord.id == outbox_id,
            AutomationTaskWakeupOutboxRecord.status == "claimed",
            AutomationTaskWakeupOutboxRecord.lease_owner == dispatcher_id,
            AutomationTaskWakeupOutboxRecord.lease_token_hash
            == lease_token_hash,
            AutomationTaskWakeupOutboxRecord.version == expected_version,
            AutomationTaskWakeupOutboxRecord.lease_expires_at.is_not(None),
            AutomationTaskWakeupOutboxRecord.lease_expires_at > lease_cutoff,
        )
        .values(
            **values,
            lease_owner=None,
            lease_token_hash=None,
            lease_expires_at=None,
            updated_at=now,
            version=AutomationTaskWakeupOutboxRecord.version + 1,
        )
        .returning(AutomationTaskWakeupOutboxRecord.id)
    )


def build_schedule_claim_statement(
    *,
    now: datetime,
    due_at: datetime | None = None,
    use_skip_locked: bool,
    excluded_ids: Collection[str] = (),
) -> Select[tuple[ScheduleRecord]]:
    """Select one due schedule whose short dispatcher lease is available."""

    due_cutoff = due_at or now
    statement = select(ScheduleRecord).where(
        ScheduleRecord.enabled.is_(True),
        ScheduleRecord.next_run_at.is_not(None),
        ScheduleRecord.next_run_at <= due_cutoff,
        or_(
            ScheduleRecord.claim_expires_at.is_(None),
            ScheduleRecord.claim_expires_at <= now,
        ),
    )
    if excluded_ids:
        statement = statement.where(ScheduleRecord.id.not_in(excluded_ids))
    statement = statement.order_by(
        ScheduleRecord.next_run_at,
        ScheduleRecord.id,
    ).limit(1)
    if use_skip_locked:
        statement = statement.with_for_update(skip_locked=True)
    return statement


def build_schedule_finalize_statement(
    *,
    schedule_id: str,
    expected_version: int,
    expected_next_run_at: datetime,
    scheduler_id: str,
    claim_token_hash: str,
    now: datetime,
    last_run_at: datetime,
    next_run_at: datetime,
    use_database_clock: bool = False,
) -> Update:
    """Build the token + version CAS that authorizes one schedule finalization."""

    lease_cutoff = (
        func.clock_timestamp(type_=DateTime(timezone=True))
        if use_database_clock
        else now
    )
    return (
        update(ScheduleRecord)
        .where(
            ScheduleRecord.id == schedule_id,
            ScheduleRecord.enabled.is_(True),
            ScheduleRecord.version == expected_version,
            ScheduleRecord.next_run_at == expected_next_run_at,
            ScheduleRecord.claim_owner == scheduler_id,
            ScheduleRecord.claim_token_hash == claim_token_hash,
            ScheduleRecord.claim_expires_at.is_not(None),
            ScheduleRecord.claim_expires_at > lease_cutoff,
        )
        .values(
            last_run_at=last_run_at,
            next_run_at=next_run_at,
            claim_owner=None,
            claim_token_hash=None,
            claim_expires_at=None,
            version=ScheduleRecord.version + 1,
            updated_at=now,
        )
        .returning(ScheduleRecord.id)
    )


class RuntimeRepository:
    """Keep SQLite deterministic and PostgreSQL claims multi-worker safe."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._is_postgresql = database.engine.dialect.name == "postgresql"
        self._single_process_lock = asyncio.Lock()

    @property
    def is_postgresql(self) -> bool:
        return self._is_postgresql

    async def initialize(self) -> None:
        await self.database.initialize()

    @asynccontextmanager
    async def transaction(self) -> AsyncIterator[AsyncSession]:
        await self.initialize()

        if self._is_postgresql:
            async with self._transaction_session() as session:
                yield session
            return

        # SQLite remains an explicitly single-process teaching runtime.  Its
        # dialect ignores FOR UPDATE, so retain the local serialization lock.
        async with self._single_process_lock:
            async with self._transaction_session() as session:
                yield session

    @asynccontextmanager
    async def _transaction_session(self) -> AsyncIterator[AsyncSession]:
        async with self.database.session_factory() as session:
            try:
                yield session
                await session.commit()
            except IntegrityError as error:
                await session.rollback()
                raise ConflictError("数据违反唯一性或关联约束") from error
            except BaseException:
                await session.rollback()
                raise

    async def claim_task_candidate(
        self,
        session: AsyncSession,
        *,
        queues: Collection[str],
        now: datetime,
    ) -> AutomationTaskRecord | None:
        statement = build_task_claim_statement(
            queues=queues,
            now=now,
            use_skip_locked=self._is_postgresql,
        )
        return await session.scalar(statement)

    async def claim_provider_trigger_intent(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> ProviderTriggerIntentRecord | None:
        statement = build_provider_trigger_claim_statement(
            now=now,
            use_skip_locked=self._is_postgresql,
        )
        return await session.scalar(statement)

    async def claim_task_wakeup_outbox(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> AutomationTaskWakeupOutboxRecord | None:
        statement = build_task_wakeup_outbox_claim_statement(
            now=now,
            use_skip_locked=self._is_postgresql,
        )
        return await session.scalar(statement)

    async def settle_task_wakeup_outbox(
        self,
        session: AsyncSession,
        *,
        outbox_id: str,
        dispatcher_id: str,
        lease_token_hash: str,
        expected_version: int,
        now: datetime,
        published: bool,
        retry_at: datetime | None = None,
    ) -> bool:
        statement = build_task_wakeup_outbox_settle_statement(
            outbox_id=outbox_id,
            dispatcher_id=dispatcher_id,
            lease_token_hash=lease_token_hash,
            expected_version=expected_version,
            now=now,
            published=published,
            retry_at=retry_at,
            use_database_clock=self._is_postgresql,
        )
        return await session.scalar(statement) is not None

    async def claim_schedule_candidate(
        self,
        session: AsyncSession,
        *,
        now: datetime,
        due_at: datetime | None = None,
        excluded_ids: Collection[str] = (),
    ) -> ScheduleRecord | None:
        statement = build_schedule_claim_statement(
            now=now,
            due_at=due_at,
            use_skip_locked=self._is_postgresql,
            excluded_ids=excluded_ids,
        )
        return await session.scalar(statement)

    async def finalize_schedule_claim(
        self,
        session: AsyncSession,
        *,
        schedule_id: str,
        expected_version: int,
        expected_next_run_at: datetime,
        scheduler_id: str,
        claim_token_hash: str,
        now: datetime,
        last_run_at: datetime,
        next_run_at: datetime,
    ) -> bool:
        statement = build_schedule_finalize_statement(
            schedule_id=schedule_id,
            expected_version=expected_version,
            expected_next_run_at=expected_next_run_at,
            scheduler_id=scheduler_id,
            claim_token_hash=claim_token_hash,
            now=now,
            last_run_at=last_run_at,
            next_run_at=next_run_at,
            use_database_clock=self._is_postgresql,
        )
        return await session.scalar(statement) is not None

    async def release_schedule_claim(
        self,
        session: AsyncSession,
        *,
        schedule_id: str,
        scheduler_id: str,
        claim_token_hash: str,
    ) -> None:
        await session.execute(
            update(ScheduleRecord)
            .where(
                ScheduleRecord.id == schedule_id,
                ScheduleRecord.claim_owner == scheduler_id,
                ScheduleRecord.claim_token_hash == claim_token_hash,
            )
            .values(
                claim_owner=None,
                claim_token_hash=None,
                claim_expires_at=None,
            )
        )

    async def get_provider_trigger_intent_for_update(
        self,
        session: AsyncSession,
        intent_id: str,
    ) -> ProviderTriggerIntentRecord | None:
        statement = select(ProviderTriggerIntentRecord).where(
            ProviderTriggerIntentRecord.id == intent_id
        )
        if self._is_postgresql:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_provider_run_for_update(
        self,
        session: AsyncSession,
        run_id: str,
    ) -> ProviderRunRecord | None:
        statement = select(ProviderRunRecord).where(
            ProviderRunRecord.id == run_id
        )
        if self._is_postgresql:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_schedule_for_update(
        self,
        session: AsyncSession,
        schedule_id: str,
    ) -> ScheduleRecord | None:
        statement = select(ScheduleRecord).where(ScheduleRecord.id == schedule_id)
        if self._is_postgresql:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def database_now(
        self,
        session: AsyncSession,
        *,
        fallback: datetime,
    ) -> datetime:
        """Return one authoritative lease clock for the active backend.

        SQLite remains a single-process teaching mode and keeps its injected
        Python clock, which is also important for deterministic unit tests.
        PostgreSQL workers use the database wall clock so separate processes
        agree on lease expiry even after waiting for row locks.
        """

        if not self._is_postgresql:
            return fallback
        value = await session.scalar(build_postgres_clock_statement())
        if value is None:
            raise RuntimeError("PostgreSQL 数据库时钟未返回时间")
        if value.tzinfo is None:
            return value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc)

    async def lock_expired_task_leases(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> list[AutomationTaskRecord]:
        statement = build_expired_task_lease_statement(
            now=now,
            use_skip_locked=self._is_postgresql,
        )
        return list((await session.scalars(statement)).all())

    async def get_task_for_update(
        self,
        session: AsyncSession,
        task_id: str,
    ) -> AutomationTaskRecord | None:
        statement = select(AutomationTaskRecord).where(
            AutomationTaskRecord.id == task_id
        )
        if self._is_postgresql:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def list_device_candidate_snapshots(
        self,
        session: AsyncSession,
        *,
        heartbeat_after: datetime,
    ) -> list[tuple[str, list[str]]]:
        """List eligible IDs/capabilities without putting entities in the identity map."""

        statement = (
            select(DeviceRecord.id, DeviceRecord.capabilities)
            .where(
                DeviceRecord.enabled.is_(True),
                DeviceRecord.status == DeviceStatus.IDLE.value,
                DeviceRecord.active_lease_id.is_(None),
                DeviceRecord.last_heartbeat_at.is_not(None),
                DeviceRecord.last_heartbeat_at > heartbeat_after,
            )
            .order_by(func.lower(DeviceRecord.name), DeviceRecord.id)
        )
        rows = (await session.execute(statement)).all()
        return [(str(row[0]), list(row[1])) for row in rows]

    async def claim_device_candidate(
        self,
        session: AsyncSession,
        *,
        candidate_ids: Collection[str],
        heartbeat_after: datetime,
    ) -> DeviceRecord | None:
        if not candidate_ids:
            return None
        statement = build_device_candidate_statement(
            candidate_ids=candidate_ids,
            heartbeat_after=heartbeat_after,
            use_skip_locked=self._is_postgresql,
        )
        return await session.scalar(statement)

    async def list_expired_device_lease_ids(
        self,
        session: AsyncSession,
        *,
        now: datetime,
    ) -> list[str]:
        """Return ordered locators; the service locks each graph before mutation."""

        statement = (
            select(DeviceLeaseRecord.id)
            .where(
                DeviceLeaseRecord.status == DeviceLeaseStatus.ACTIVE.value,
                DeviceLeaseRecord.expires_at <= now,
            )
            .order_by(DeviceLeaseRecord.expires_at, DeviceLeaseRecord.id)
        )
        return [str(value) for value in (await session.scalars(statement)).all()]

    async def get_device_for_update(
        self,
        session: AsyncSession,
        device_id: str,
    ) -> DeviceRecord | None:
        statement = select(DeviceRecord).where(DeviceRecord.id == device_id)
        if self._is_postgresql:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def get_device_lease_for_update(
        self,
        session: AsyncSession,
        lease_id: str,
    ) -> DeviceLeaseRecord | None:
        statement = select(DeviceLeaseRecord).where(DeviceLeaseRecord.id == lease_id)
        if self._is_postgresql:
            statement = statement.with_for_update()
        return await session.scalar(statement)

    async def lock_device_lease_graph(
        self,
        session: AsyncSession,
        lease_id: str,
    ) -> LockedDeviceLeaseGraph | None:
        """Lock related rows in one global order and leave validation to the service.

        The first query is a non-locking locator only. Every mutation path then
        acquires row locks in the same task -> device -> lease order. Foreign
        keys are immutable in the service, but the returned IDs are still
        revalidated after the final row is locked.
        """

        locator = (
            await session.execute(
                select(DeviceLeaseRecord.task_id, DeviceLeaseRecord.device_id).where(
                    DeviceLeaseRecord.id == lease_id
                )
            )
        ).one_or_none()
        if locator is None:
            return None
        expected_task_id = str(locator[0])
        expected_device_id = str(locator[1])
        task = await self.get_task_for_update(session, expected_task_id)
        device = await self.get_device_for_update(session, expected_device_id)
        lease = await self.get_device_lease_for_update(session, lease_id)
        return LockedDeviceLeaseGraph(
            task=task,
            device=device,
            lease=lease,
            expected_task_id=expected_task_id,
            expected_device_id=expected_device_id,
        )

    @staticmethod
    async def get(
        session: AsyncSession,
        record_type: type[RecordT],
        entity_id: str,
    ) -> RecordT | None:
        return await session.get(record_type, entity_id)

    @staticmethod
    async def list(
        session: AsyncSession,
        record_type: type[RecordT],
        *,
        order_by=None,
    ) -> list[RecordT]:
        statement = select(record_type)
        if order_by is not None:
            statement = statement.order_by(order_by)
        return list((await session.scalars(statement)).all())


__all__ = [
    "LockedDeviceLeaseGraph",
    "RuntimeRepository",
    "build_device_candidate_statement",
    "build_expired_task_lease_statement",
    "build_postgres_clock_statement",
    "build_provider_trigger_claim_statement",
    "build_schedule_claim_statement",
    "build_schedule_finalize_statement",
    "build_task_claim_statement",
    "build_task_wakeup_outbox_claim_statement",
    "build_task_wakeup_outbox_settle_statement",
]
