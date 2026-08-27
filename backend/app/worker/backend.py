"""Adapter from the worker ports to the persistent runtime service."""

from __future__ import annotations

from typing import Any

from app.runtime.service import PersistentRuntimeService
from app.worker.contracts import ClaimedWork


class RuntimeTaskLeaseBackend:
    def __init__(self, service: PersistentRuntimeService) -> None:
        self._service = service

    async def claim(
        self,
        worker_id: str,
        queues: tuple[str, ...],
        lease_seconds: int,
    ) -> ClaimedWork | None:
        claimed = await self._service.claim_task(
            worker_id,
            list(queues),
            lease_seconds,
        )
        if claimed is None:
            return None
        return ClaimedWork(
            task_id=claimed.task.id,
            task_type=claimed.task.task_type,
            payload=dict(claimed.task.payload),
            lease_token=claimed.lease_token,
        )

    async def heartbeat(
        self,
        work: ClaimedWork,
        worker_id: str,
        lease_seconds: int,
    ) -> bool:
        task = await self._service.heartbeat_task(
            work.task_id,
            worker_id,
            work.lease_token,
            lease_seconds,
        )
        return task.cancel_requested

    async def complete(
        self,
        work: ClaimedWork,
        worker_id: str,
        result: dict[str, Any],
    ) -> None:
        await self._service.complete_task(
            work.task_id,
            worker_id,
            work.lease_token,
            result,
        )

    async def fail(
        self,
        work: ClaimedWork,
        worker_id: str,
        error_code: str,
        retryable: bool,
    ) -> None:
        await self._service.fail_task(
            work.task_id,
            worker_id,
            work.lease_token,
            error_code,
            retryable,
        )


__all__ = ["RuntimeTaskLeaseBackend"]
