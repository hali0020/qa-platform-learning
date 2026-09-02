from __future__ import annotations

import json
import time
from typing import Protocol
from uuid import UUID

from app.cache import JsonCache
from app.domain.models import Project
from app.repositories.base import AsyncRepository


class CacheMetrics(Protocol):
    def record_cache_lookup(self, *, cache: str, outcome: str) -> None: ...
    def record_cache_operation(
        self, *, cache: str, operation: str, succeeded: bool
    ) -> None: ...
    def observe_database_fallback(
        self, *, cache: str, duration_seconds: float
    ) -> None: ...


class CachedProjectRepository:
    """Cache-aside decorator; the wrapped repository remains authoritative."""

    _LIST_KEY = "qa:v1:projects:all"

    def __init__(
        self,
        repository: AsyncRepository[Project],
        cache: JsonCache,
        *,
        ttl_seconds: int,
        metrics: CacheMetrics | None = None,
    ) -> None:
        self._repository = repository
        self._cache = cache
        self._ttl_seconds = ttl_seconds
        self._metrics = metrics

    @staticmethod
    def _item_key(project_id: UUID) -> str:
        return f"qa:v1:projects:item:{project_id}"

    async def create(
        self, entity: Project, *, unique_fields: tuple[str, ...] = ()
    ) -> Project:
        created = await self._repository.create(entity, unique_fields=unique_fields)
        await self._invalidate(created.id)
        return created

    async def get(self, entity_id: UUID) -> Project | None:
        key = self._item_key(entity_id)
        lookup = await self._cache.get(key)
        self._record_lookup(
            "error"
            if lookup.failed
            else "hit"
            if lookup.value is not None
            else "miss"
        )
        if lookup.value is not None:
            try:
                return Project.model_validate_json(lookup.value)
            except (ValueError, TypeError):
                succeeded = await self._cache.delete(key)
                self._record_operation("invalidate", succeeded)
        started = time.perf_counter()
        project = await self._repository.get(entity_id)
        self._record_fallback(time.perf_counter() - started)
        if project is not None:
            await self._store_item(project)
        return project

    async def list(self) -> list[Project]:
        lookup = await self._cache.get(self._LIST_KEY)
        self._record_lookup(
            "error"
            if lookup.failed
            else "hit"
            if lookup.value is not None
            else "miss"
        )
        if lookup.value is not None:
            try:
                values = json.loads(lookup.value)
                if not isinstance(values, list):
                    raise ValueError("cached project list is not a list")
                return [Project.model_validate(value) for value in values]
            except (ValueError, TypeError):
                succeeded = await self._cache.delete(self._LIST_KEY)
                self._record_operation("invalidate", succeeded)
        started = time.perf_counter()
        projects = await self._repository.list()
        self._record_fallback(time.perf_counter() - started)
        succeeded = await self._cache.set(
            self._LIST_KEY,
            json.dumps(
                [project.model_dump(mode="json") for project in projects],
                separators=(",", ":"),
            ),
            ttl_seconds=self._ttl_seconds,
        )
        self._record_operation("fill", succeeded)
        return projects

    async def update(
        self, entity: Project, *, unique_fields: tuple[str, ...] = ()
    ) -> Project:
        updated = await self._repository.update(entity, unique_fields=unique_fields)
        await self._invalidate(updated.id)
        return updated

    async def delete(self, entity_id: UUID) -> bool:
        deleted = await self._repository.delete(entity_id)
        if deleted:
            await self._invalidate(entity_id)
        return deleted

    async def clear(self) -> None:
        projects = await self._repository.list()
        await self._repository.clear()
        succeeded = await self._cache.delete(
            self._LIST_KEY,
            *(self._item_key(project.id) for project in projects),
        )
        self._record_operation("invalidate", succeeded)

    async def _store_item(self, project: Project) -> None:
        succeeded = await self._cache.set(
            self._item_key(project.id),
            project.model_dump_json(),
            ttl_seconds=self._ttl_seconds,
        )
        self._record_operation("fill", succeeded)

    async def _invalidate(self, project_id: UUID) -> None:
        succeeded = await self._cache.delete(
            self._LIST_KEY,
            self._item_key(project_id),
        )
        self._record_operation("invalidate", succeeded)

    async def invalidate(self, project_id: UUID) -> None:
        await self._invalidate(project_id)

    def _record_lookup(self, outcome: str) -> None:
        if self._metrics is not None:
            self._metrics.record_cache_lookup(cache="projects", outcome=outcome)

    def _record_operation(self, operation: str, succeeded: bool) -> None:
        if self._metrics is not None:
            self._metrics.record_cache_operation(
                cache="projects", operation=operation, succeeded=succeeded
            )

    def _record_fallback(self, duration_seconds: float) -> None:
        if self._metrics is not None:
            self._metrics.observe_database_fallback(
                cache="projects", duration_seconds=duration_seconds
            )


__all__ = ["CachedProjectRepository"]
