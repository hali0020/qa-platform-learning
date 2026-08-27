"""Database transaction and worker-claim boundary for runtime services."""

from __future__ import annotations

import asyncio
from collections.abc import Collection
from contextlib import asynccontextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import AsyncIterator, TypeVar

from sqlalchemy import DateTime, Select, func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from app.automation.models import DeviceLeaseStatus, DeviceStatus, TaskStatus
from app.core.errors import ConflictError
from app.database.base import Base
from app.database.session import Database
from app.runtime.orm import AutomationTaskRecord, DeviceLeaseRecord, DeviceRecord


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


class RuntimeRepository:
    """Keep SQLite deterministic and PostgreSQL claims multi-worker safe."""

    def __init__(self, database: Database) -> None:
        self.database = database
        self._is_postgresql = database.engine.dialect.name == "postgresql"
        self._single_process_lock = asyncio.Lock()

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
    "build_task_claim_statement",
]
