from __future__ import annotations

import asyncio
import math

from app.broker.errors import BrokerStateError


def _validate_timeout(timeout: float | None) -> None:
    if timeout is not None and (timeout < 0 or not math.isfinite(timeout)):
        raise ValueError("timeout must be finite and greater than or equal to zero")


class DisabledLocalWakeupBroker:
    """No-network default that lets Workers fall back to bounded DB polling."""

    def __init__(self) -> None:
        self._started = False
        self._closed = False
        self._closed_event = asyncio.Event()

    async def start(self) -> None:
        if self._closed:
            raise BrokerStateError("wake-up broker is closed")
        self._started = True

    async def publish_wakeup(self) -> None:
        if self._closed:
            raise BrokerStateError("wake-up broker is closed")

    async def wait(self, timeout: float | None = None) -> bool:
        _validate_timeout(timeout)
        if self._closed:
            return False
        if not self._started:
            raise BrokerStateError("wake-up source has not been started")
        try:
            await asyncio.wait_for(self._closed_event.wait(), timeout=timeout)
        except asyncio.TimeoutError:
            return False
        return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        self._closed_event.set()


__all__ = ["DisabledLocalWakeupBroker"]
