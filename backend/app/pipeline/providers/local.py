from __future__ import annotations

import asyncio
import json
from hashlib import sha256
from uuid import uuid4

from app.pipeline.models import PipelineStatus
from app.pipeline.providers.errors import (
    ProviderConflictError,
    ProviderResponseError,
)
from app.pipeline.providers.models import ProviderKind, ProviderRun, ProviderTriggerRequest


def _fingerprint(request: ProviderTriggerRequest) -> str:
    canonical = json.dumps(
        request.model_dump(exclude={"correlation_id"}, mode="json"),
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    )
    return sha256(canonical.encode("utf-8")).hexdigest()


class LocalPipelineProvider:
    """Deterministic local fake; it never opens a socket or starts a process."""

    kind = ProviderKind.LOCAL

    def __init__(self) -> None:
        self._runs: dict[str, ProviderRun] = {}
        self._correlations: dict[str, tuple[str, str]] = {}
        self._lock = asyncio.Lock()
        self._closed = False

    async def trigger(self, request: ProviderTriggerRequest) -> ProviderRun:
        async with self._lock:
            if self._closed:
                raise ProviderResponseError("local provider is closed")
            fingerprint = _fingerprint(request)
            if request.correlation_id is not None:
                previous = self._correlations.get(request.correlation_id)
                if previous is not None:
                    run_id, previous_fingerprint = previous
                    if fingerprint != previous_fingerprint:
                        raise ProviderConflictError(
                            "correlation id was reused with different input"
                        )
                    return self._runs[run_id].model_copy(deep=True)

            run_id = str(uuid4())
            run = ProviderRun(
                provider=self.kind,
                external_id=run_id,
                status=PipelineStatus.QUEUED,
                raw_status="queued",
                metadata={
                    "definition_ref": request.definition_ref,
                    "ref": request.ref,
                },
            )
            self._runs[run_id] = run
            if request.correlation_id is not None:
                self._correlations[request.correlation_id] = (run_id, fingerprint)
            return run.model_copy(deep=True)

    async def get(self, external_id: str) -> ProviderRun:
        async with self._lock:
            try:
                return self._runs[external_id].model_copy(deep=True)
            except KeyError as error:
                raise ProviderResponseError("local provider run was not found") from error

    async def cancel(self, external_id: str) -> ProviderRun:
        async with self._lock:
            try:
                run = self._runs[external_id]
            except KeyError as error:
                raise ProviderResponseError("local provider run was not found") from error
            if not run.status.is_terminal:
                run.status = PipelineStatus.CANCELLED
                run.raw_status = "cancelled"
                run.message = "cancelled by local simulator"
            return run.model_copy(deep=True)

    async def set_status(
        self,
        external_id: str,
        status: PipelineStatus,
        *,
        message: str | None = None,
    ) -> ProviderRun:
        """Test/lesson hook that replaces a provider webhook or poll result."""

        async with self._lock:
            try:
                run = self._runs[external_id]
            except KeyError as error:
                raise ProviderResponseError("local provider run was not found") from error
            if run.status.is_terminal and run.status != status:
                raise ProviderConflictError("terminal local provider runs cannot transition")
            run.status = status
            run.raw_status = status.value
            run.message = message
            return run.model_copy(deep=True)

    async def aclose(self) -> None:
        async with self._lock:
            self._closed = True


__all__ = ["LocalPipelineProvider"]
