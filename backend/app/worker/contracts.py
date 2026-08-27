"""Narrow ports used by the process-level worker.

The broker is deliberately only a wake-up hint.  A worker receives authority
to execute work exclusively from a database-backed lease returned by
``TaskLeaseBackend.claim``.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from typing import Any, Protocol


@dataclass(frozen=True, slots=True)
class ClaimedWork:
    task_id: str
    task_type: str
    payload: Mapping[str, Any] = field(repr=False)
    lease_token: str = field(repr=False)


class TaskLeaseBackend(Protocol):
    async def claim(
        self,
        worker_id: str,
        queues: tuple[str, ...],
        lease_seconds: int,
    ) -> ClaimedWork | None: ...

    async def heartbeat(
        self,
        work: ClaimedWork,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        """Renew a lease and return whether cooperative cancellation was requested."""

    async def complete(
        self,
        work: ClaimedWork,
        worker_id: str,
        result: dict[str, Any],
    ) -> None: ...

    async def fail(
        self,
        work: ClaimedWork,
        worker_id: str,
        error_code: str,
        retryable: bool,
    ) -> None: ...


class WakeupSource(Protocol):
    """Advisory notification source; messages never contain lease authority."""

    async def start(self) -> None: ...

    async def wait(self, timeout_seconds: float) -> bool:
        """Wait for a hint and return True, or False when the timeout expires."""

    async def close(self) -> None: ...


__all__ = ["ClaimedWork", "TaskLeaseBackend", "WakeupSource"]
