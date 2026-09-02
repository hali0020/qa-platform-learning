from __future__ import annotations

from typing import Protocol


class JsonCache(Protocol):
    async def get(self, key: str) -> str | None: ...

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None: ...

    async def delete(self, *keys: str) -> None: ...

    async def aclose(self) -> None: ...


class RedisJsonCache:
    """Small fail-open Redis boundary used only for derived cache data."""

    def __init__(self, url: str, *, operation_timeout_seconds: float) -> None:
        from redis.asyncio import Redis

        self._client = Redis.from_url(
            url,
            encoding="utf-8",
            decode_responses=True,
            socket_connect_timeout=operation_timeout_seconds,
            socket_timeout=operation_timeout_seconds,
            retry_on_timeout=False,
        )

    async def get(self, key: str) -> str | None:
        try:
            value = await self._client.get(key)
            return value if isinstance(value, str) else None
        except Exception:
            return None

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        try:
            await self._client.set(key, value, ex=ttl_seconds)
        except Exception:
            return

    async def delete(self, *keys: str) -> None:
        if not keys:
            return
        try:
            await self._client.delete(*keys)
        except Exception:
            return

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            return


__all__ = ["JsonCache", "RedisJsonCache"]
