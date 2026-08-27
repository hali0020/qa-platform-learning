from typing import Protocol, runtime_checkable

from app.pipeline.providers.models import ProviderKind, ProviderRun, ProviderTriggerRequest


@runtime_checkable
class PipelineProvider(Protocol):
    """Port consumed by a future provider-neutral pipeline orchestrator."""

    kind: ProviderKind

    async def trigger(self, request: ProviderTriggerRequest) -> ProviderRun: ...

    async def get(self, external_id: str) -> ProviderRun: ...

    async def cancel(self, external_id: str) -> ProviderRun: ...

    async def aclose(self) -> None: ...


__all__ = ["PipelineProvider"]
