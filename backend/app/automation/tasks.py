from __future__ import annotations

import asyncio
import hmac
import json
import random
import secrets
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from hashlib import sha256
from typing import Any

from app.automation.errors import (
    AutomationConflictError,
    AutomationLeaseError,
    AutomationNotFoundError,
    AutomationValidationError,
)
from app.automation.models import (
    AutomationTask,
    ClaimedTask,
    EnqueueResult,
    TaskStatus,
    utc_now,
)


def _as_utc(value: datetime) -> datetime:
    if value.tzinfo is None:
        raise AutomationValidationError("automation timestamps must be timezone-aware")
    return value.astimezone(timezone.utc)


def _fingerprint(value: object) -> str:
    try:
        encoded = json.dumps(
            value,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise AutomationValidationError("task payload must be JSON serializable") from error
    if len(encoded) > 262_144:
        raise AutomationValidationError("task payload exceeds 256 KiB")
    return sha256(encoded).hexdigest()


def _token_hash(value: str) -> str:
    return sha256(value.encode("utf-8")).hexdigest()


@dataclass(frozen=True, slots=True)
class RetryPolicy:
    base_seconds: float = 1.0
    maximum_seconds: float = 300.0
    jitter_ratio: float = 0.2

    def delay(self, attempt: int, entropy: float) -> float:
        if attempt < 1 or not 0 <= entropy <= 1:
            raise AutomationValidationError("retry policy input is invalid")
        base = min(self.maximum_seconds, self.base_seconds * (2 ** (attempt - 1)))
        jitter = base * self.jitter_ratio * ((2 * entropy) - 1)
        return max(0.0, base + jitter)


class InMemoryTaskQueue:
    """At-least-once task semantics with leases and idempotent enqueue.

    This is a local teaching adapter. A SQL adapter must implement claim,
    lease validation and state transition with a transaction/CAS predicate;
    copying these multi-step in-memory operations into CRUD calls is unsafe.
    """

    def __init__(
        self,
        *,
        retry_policy: RetryPolicy | None = None,
        entropy: Callable[[], float] = random.random,
    ) -> None:
        self._tasks: dict[str, AutomationTask] = {}
        self._idempotency: dict[tuple[str, str], tuple[str, str]] = {}
        self._lock = asyncio.Lock()
        self._retry_policy = retry_policy or RetryPolicy()
        self._entropy = entropy

    async def enqueue(
        self,
        task_type: str,
        payload: dict[str, Any],
        *,
        queue: str = "default",
        priority: int = 50,
        max_attempts: int = 3,
        idempotency_key: str | None = None,
        source_schedule_id: str | None = None,
        available_at: datetime | None = None,
    ) -> EnqueueResult:
        now = utc_now()
        due_at = _as_utc(available_at or now)
        request_value = {
            "task_type": task_type,
            "payload": payload,
            "queue": queue,
            "priority": priority,
            "max_attempts": max_attempts,
            "source_schedule_id": source_schedule_id,
            # Omitted means "available immediately" and remains stable across
            # idempotent retries even though the concrete enqueue time changes.
            "available_at": (
                _as_utc(available_at).isoformat()
                if available_at is not None
                else None
            ),
        }
        fingerprint = _fingerprint(request_value)
        async with self._lock:
            if idempotency_key is not None:
                previous = self._idempotency.get((task_type, idempotency_key))
                if previous is not None:
                    task_id, previous_fingerprint = previous
                    if not hmac.compare_digest(fingerprint, previous_fingerprint):
                        raise AutomationConflictError(
                            "task idempotency key was reused with different input"
                        )
                    return EnqueueResult(
                        task=self._copy(self._tasks[task_id]),
                        replayed=True,
                    )
            task = AutomationTask(
                task_type=task_type,
                payload=payload,
                queue=queue,
                priority=priority,
                idempotency_key=idempotency_key,
                request_fingerprint=fingerprint,
                source_schedule_id=source_schedule_id,
                max_attempts=max_attempts,
                available_at=due_at,
                created_at=now,
            )
            self._tasks[task.id] = task
            if idempotency_key is not None:
                self._idempotency[(task_type, idempotency_key)] = (
                    task.id,
                    fingerprint,
                )
            return EnqueueResult(task=self._copy(task))

    async def get(self, task_id: str) -> AutomationTask:
        async with self._lock:
            return self._copy(self._require(task_id))

    async def list_tasks(self) -> list[AutomationTask]:
        async with self._lock:
            return [
                self._copy(task)
                for task in sorted(
                    self._tasks.values(),
                    key=lambda item: (item.created_at, item.id),
                )
            ]

    async def claim(
        self,
        worker_id: str,
        *,
        queues: Iterable[str] = ("default",),
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> ClaimedTask | None:
        if not worker_id or lease_seconds < 1:
            raise AutomationValidationError("worker id and lease duration are required")
        current = _as_utc(now or utc_now())
        accepted_queues = set(queues)
        if not accepted_queues:
            raise AutomationValidationError("worker must subscribe to at least one queue")
        async with self._lock:
            self._recover_expired_locked(current)
            candidates = [
                task
                for task in self._tasks.values()
                if task.queue in accepted_queues
                and task.status in {TaskStatus.QUEUED, TaskStatus.RETRY_WAIT}
                and task.available_at <= current
                and not task.cancel_requested
            ]
            if not candidates:
                return None
            task = min(
                candidates,
                key=lambda item: (
                    -item.priority,
                    item.available_at,
                    item.created_at,
                    item.id,
                ),
            )
            token = secrets.token_urlsafe(32)
            task.status = TaskStatus.RUNNING
            task.attempts += 1
            task.lease_owner = worker_id
            task.lease_token_hash = _token_hash(token)
            task.lease_expires_at = current + timedelta(seconds=lease_seconds)
            task.heartbeat_at = current
            task.started_at = task.started_at or current
            return ClaimedTask(task=self._copy(task), lease_token=token)

    async def heartbeat(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        *,
        lease_seconds: int = 30,
        now: datetime | None = None,
    ) -> AutomationTask:
        if lease_seconds < 1:
            raise AutomationValidationError("task lease duration must be positive")
        current = _as_utc(now or utc_now())
        async with self._lock:
            task = self._require_valid_lease(task_id, worker_id, lease_token, current)
            task.heartbeat_at = current
            task.lease_expires_at = current + timedelta(seconds=lease_seconds)
            return self._copy(task)

    async def complete(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        result: dict[str, Any] | None = None,
        *,
        now: datetime | None = None,
    ) -> AutomationTask:
        current = _as_utc(now or utc_now())
        _fingerprint(result or {})
        async with self._lock:
            task = self._require_valid_lease(task_id, worker_id, lease_token, current)
            if task.cancel_requested:
                task.status = TaskStatus.CANCELLED
                task.result = None
            else:
                task.status = TaskStatus.SUCCEEDED
                task.result = result or {}
            task.finished_at = current
            self._clear_lease(task)
            return self._copy(task)

    async def fail(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        *,
        error_code: str,
        retryable: bool,
        now: datetime | None = None,
    ) -> AutomationTask:
        current = _as_utc(now or utc_now())
        if not error_code or len(error_code) > 100:
            raise AutomationValidationError("task error code is invalid")
        async with self._lock:
            task = self._require_valid_lease(task_id, worker_id, lease_token, current)
            task.error_code = error_code
            if task.cancel_requested:
                task.status = TaskStatus.CANCELLED
                task.finished_at = current
            elif retryable and task.attempts < task.max_attempts:
                task.status = TaskStatus.RETRY_WAIT
                delay = self._retry_policy.delay(task.attempts, self._entropy())
                task.available_at = current + timedelta(seconds=delay)
            elif retryable:
                task.status = TaskStatus.DEAD_LETTER
                task.finished_at = current
            else:
                task.status = TaskStatus.FAILED
                task.finished_at = current
            self._clear_lease(task)
            return self._copy(task)

    async def request_cancel(self, task_id: str, *, now: datetime | None = None) -> AutomationTask:
        current = _as_utc(now or utc_now())
        async with self._lock:
            task = self._require(task_id)
            if task.status in {TaskStatus.QUEUED, TaskStatus.RETRY_WAIT}:
                task.status = TaskStatus.CANCELLED
                task.cancel_requested = True
                task.finished_at = current
                self._clear_lease(task)
            elif task.status == TaskStatus.RUNNING:
                task.cancel_requested = True
            return self._copy(task)

    async def acknowledge_cancel(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        *,
        now: datetime | None = None,
    ) -> AutomationTask:
        current = _as_utc(now or utc_now())
        async with self._lock:
            task = self._require_valid_lease(task_id, worker_id, lease_token, current)
            if not task.cancel_requested:
                raise AutomationConflictError("task cancellation was not requested")
            task.status = TaskStatus.CANCELLED
            task.finished_at = current
            self._clear_lease(task)
            return self._copy(task)

    async def recover_expired(self, *, now: datetime | None = None) -> list[AutomationTask]:
        current = _as_utc(now or utc_now())
        async with self._lock:
            changed = self._recover_expired_locked(current)
            return [self._copy(task) for task in changed]

    async def active_for_schedule(self, schedule_id: str) -> list[AutomationTask]:
        async with self._lock:
            return [
                self._copy(task)
                for task in self._tasks.values()
                if task.source_schedule_id == schedule_id and task.status.is_active
            ]

    def _recover_expired_locked(self, now: datetime) -> list[AutomationTask]:
        changed: list[AutomationTask] = []
        for task in self._tasks.values():
            if (
                task.status == TaskStatus.RUNNING
                and task.lease_expires_at is not None
                and task.lease_expires_at <= now
            ):
                task.error_code = "lease_expired"
                if task.cancel_requested:
                    task.status = TaskStatus.CANCELLED
                    task.finished_at = now
                elif task.attempts < task.max_attempts:
                    task.status = TaskStatus.RETRY_WAIT
                    delay = self._retry_policy.delay(task.attempts, self._entropy())
                    task.available_at = now + timedelta(seconds=delay)
                else:
                    task.status = TaskStatus.DEAD_LETTER
                    task.finished_at = now
                self._clear_lease(task)
                changed.append(task)
        return changed

    def _require_valid_lease(
        self,
        task_id: str,
        worker_id: str,
        lease_token: str,
        now: datetime,
    ) -> AutomationTask:
        task = self._require(task_id)
        supplied_hash = _token_hash(lease_token)
        if (
            task.status != TaskStatus.RUNNING
            or task.lease_owner != worker_id
            or task.lease_token_hash is None
            or not hmac.compare_digest(task.lease_token_hash, supplied_hash)
            or task.lease_expires_at is None
            or task.lease_expires_at <= now
        ):
            raise AutomationLeaseError("task lease is invalid or expired")
        return task

    def _require(self, task_id: str) -> AutomationTask:
        try:
            return self._tasks[task_id]
        except KeyError as error:
            raise AutomationNotFoundError("automation task was not found") from error

    @staticmethod
    def _clear_lease(task: AutomationTask) -> None:
        task.lease_owner = None
        task.lease_token_hash = None
        task.lease_expires_at = None
        task.heartbeat_at = None

    @staticmethod
    def _copy(task: AutomationTask) -> AutomationTask:
        return task.model_copy(deep=True)


__all__ = ["InMemoryTaskQueue", "RetryPolicy"]
