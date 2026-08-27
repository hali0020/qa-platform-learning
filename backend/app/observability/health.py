from __future__ import annotations

import asyncio
from dataclasses import dataclass
from typing import Protocol

from sqlalchemy import text

from app.database import Database


@dataclass(frozen=True, slots=True)
class ReadinessResult:
    ready: bool
    reason: str


class ReadinessProbe(Protocol):
    async def check(self) -> ReadinessResult: ...


class DatabaseReadinessProbe:
    """A bounded readiness query for a validated local database target.

    ``Database`` independently rejects every topology except local SQLite and
    the exact ``postgres:5432`` internal-container target.  The backend check
    below remains a second fail-closed guard for future dialect additions.
    """

    def __init__(self, database: Database, *, timeout_seconds: float = 1.0) -> None:
        if timeout_seconds <= 0:
            raise ValueError("timeout_seconds must be positive")
        self.database = database
        self.timeout_seconds = timeout_seconds

    async def check(self) -> ReadinessResult:
        if self.database.engine.url.get_backend_name() not in {
            "sqlite",
            "postgresql",
        }:
            return ReadinessResult(False, "unsupported_database_backend")
        try:
            value = await asyncio.wait_for(
                self._select_one(),
                timeout=self.timeout_seconds,
            )
        except asyncio.TimeoutError:
            return ReadinessResult(False, "database_timeout")
        except Exception:
            # Never leak a path, SQL error, or connection detail through health.
            return ReadinessResult(False, "database_query_failed")
        return ReadinessResult(value == 1, "ok" if value == 1 else "bad_result")

    async def _select_one(self) -> int:
        async with self.database.engine.connect() as connection:
            result = await connection.execute(text("SELECT 1"))
            return int(result.scalar_one())


# Compatibility name for lessons written before the PostgreSQL phase.  New
# code should use DatabaseReadinessProbe so the supported scope is explicit.
SqliteReadinessProbe = DatabaseReadinessProbe
