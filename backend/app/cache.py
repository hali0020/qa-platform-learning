from __future__ import annotations

from dataclasses import dataclass
from typing import Protocol


@dataclass(frozen=True, slots=True)
class CacheLookup:
    value: str | None
    failed: bool = False


class JsonCache(Protocol):
    async def get(self, key: str) -> CacheLookup: ...

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> bool: ...

    async def delete(self, *keys: str) -> bool: ...

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

    async def get(self, key: str) -> CacheLookup:
        try:
            value = await self._client.get(key)
            return CacheLookup(value if isinstance(value, str) else None)
        except Exception:
            return CacheLookup(None, failed=True)

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        try:
            await self._client.set(key, value, ex=ttl_seconds)
            return True
        except Exception:
            return False

    async def delete(self, *keys: str) -> bool:
        if not keys:
            return True
        try:
            await self._client.delete(*keys)
            return True
        except Exception:
            return False

    async def aclose(self) -> None:
        try:
            await self._client.aclose()
        except Exception:
            return


__all__ = ["CacheLookup", "JsonCache", "RedisJsonCache"]
