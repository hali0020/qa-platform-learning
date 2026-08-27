from typing import Protocol, runtime_checkable


@runtime_checkable
class WakeupPublisher(Protocol):
    """Publish a content-free hint that durable work may be available."""

    async def start(self) -> None: ...

    async def publish_wakeup(self) -> None: ...

    async def close(self) -> None: ...


@runtime_checkable
class WakeupSource(Protocol):
    """Wake one Worker waiter so it can claim authoritative database work.

    A source instance is owned by one Worker runner and supports one active
    ``wait`` call. Multiple Worker processes must create separate instances.
    """

    async def start(self) -> None: ...

    async def wait(self, timeout: float | None = None) -> bool: ...

    async def close(self) -> None: ...


__all__ = ["WakeupPublisher", "WakeupSource"]
