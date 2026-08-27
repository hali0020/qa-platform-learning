from __future__ import annotations

import asyncio
from contextlib import suppress

from app.broker.errors import BrokerStateError
from app.broker.local import _validate_timeout


class FakeWakeupBroker:
    """Injectable, deterministic fake with duplicate-safe hint coalescing."""

    def __init__(self, *, publish_error: Exception | None = None) -> None:
        self._pending: asyncio.Queue[bool] = asyncio.Queue(maxsize=1)
        self._started = False
        self._closed = False
        self._publish_error = publish_error
        self.publish_count = 0

    async def start(self) -> None:
        if self._closed:
            raise BrokerStateError("wake-up broker is closed")
        self._started = True

    async def publish_wakeup(self) -> None:
        if self._closed:
            raise BrokerStateError("wake-up broker is closed")
        if self._publish_error is not None:
            raise self._publish_error
        self.publish_count += 1
        if self._pending.empty():
            self._pending.put_nowait(True)

    async def wait(self, timeout: float | None = None) -> bool:
        _validate_timeout(timeout)
        if self._closed:
            return False
        if not self._started:
            raise BrokerStateError("wake-up source has not been started")
        try:
            if timeout is None:
                return await self._pending.get()
            if timeout == 0:
                return self._pending.get_nowait()
            return await asyncio.wait_for(self._pending.get(), timeout=timeout)
        except (asyncio.QueueEmpty, asyncio.TimeoutError):
            return False

    async def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        with suppress(asyncio.QueueEmpty):
            self._pending.get_nowait()
        self._pending.put_nowait(False)


__all__ = ["FakeWakeupBroker"]
