from __future__ import annotations

import json
from uuid import UUID

from app.cache import JsonCache
from app.domain.models import Project
from app.repositories.base import AsyncRepository


class CachedProjectRepository:
    """Cache-aside decorator; the wrapped repository remains authoritative."""

    _LIST_KEY = "qa:v1:projects:all"

    def __init__(
        self,
        repository: AsyncRepository[Project],
        cache: JsonCache,
        *,
        ttl_seconds: int,
    ) -> None:
        self._repository = repository
        self._cache = cache
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _item_key(project_id: UUID) -> str:
        return f"qa:v1:projects:item:{project_id}"

    async def create(
        self, entity: Project, *, unique_fields: tuple[str, ...] = ()
    ) -> Project:
        created = await self._repository.create(entity, unique_fields=unique_fields)
        await self._cache.delete(self._LIST_KEY)
        await self._store_item(created)
        return created

    async def get(self, entity_id: UUID) -> Project | None:
        key = self._item_key(entity_id)
        cached = await self._cache.get(key)
        if cached is not None:
            try:
                return Project.model_validate_json(cached)
            except (ValueError, TypeError):
                await self._cache.delete(key)
        project = await self._repository.get(entity_id)
        if project is not None:
            await self._store_item(project)
        return project

    async def list(self) -> list[Project]:
        cached = await self._cache.get(self._LIST_KEY)
        if cached is not None:
            try:
                values = json.loads(cached)
                if not isinstance(values, list):
                    raise ValueError("cached project list is not a list")
                return [Project.model_validate(value) for value in values]
            except (ValueError, TypeError):
                await self._cache.delete(self._LIST_KEY)
        projects = await self._repository.list()
        await self._cache.set(
            self._LIST_KEY,
            json.dumps(
                [project.model_dump(mode="json") for project in projects],
                separators=(",", ":"),
            ),
            ttl_seconds=self._ttl_seconds,
        )
        return projects

    async def update(
        self, entity: Project, *, unique_fields: tuple[str, ...] = ()
    ) -> Project:
        updated = await self._repository.update(entity, unique_fields=unique_fields)
        await self._cache.delete(self._LIST_KEY)
        await self._store_item(updated)
        return updated

    async def delete(self, entity_id: UUID) -> bool:
        deleted = await self._repository.delete(entity_id)
        if deleted:
            await self._cache.delete(self._LIST_KEY, self._item_key(entity_id))
        return deleted

    async def clear(self) -> None:
        projects = await self._repository.list()
        await self._repository.clear()
        await self._cache.delete(
            self._LIST_KEY,
            *(self._item_key(project.id) for project in projects),
        )

    async def _store_item(self, project: Project) -> None:
        await self._cache.set(
            self._item_key(project.id),
            project.model_dump_json(),
            ttl_seconds=self._ttl_seconds,
        )


__all__ = ["CachedProjectRepository"]
