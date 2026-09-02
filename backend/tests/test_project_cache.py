from __future__ import annotations

from uuid import uuid4

import pytest

from app.domain.models import Project
from app.repositories.cached_projects import CachedProjectRepository
from app.repositories.memory import InMemoryRepository


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.gets: list[str] = []
        self.sets: list[tuple[str, int]] = []
        self.deletes: list[tuple[str, ...]] = []
        self.fail = False

    async def get(self, key: str) -> str | None:
        self.gets.append(key)
        if self.fail:
            return None
        return self.values.get(key)

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> None:
        if self.fail:
            return
        self.values[key] = value
        self.sets.append((key, ttl_seconds))

    async def delete(self, *keys: str) -> None:
        self.deletes.append(keys)
        for key in keys:
            self.values.pop(key, None)

    async def aclose(self) -> None:
        return


@pytest.mark.asyncio
async def test_detail_miss_reads_database_then_next_read_hits_cache() -> None:
    source: InMemoryRepository[Project] = InMemoryRepository()
    project = await source.create(Project(key="CACHE", name="Cache lesson"))
    cache = FakeCache()
    repository = CachedProjectRepository(source, cache, ttl_seconds=60)

    assert await repository.get(project.id) == project
    await source.delete(project.id)
    assert await repository.get(project.id) == project
    assert cache.gets == [repository._item_key(project.id)] * 2
    assert cache.sets == [(repository._item_key(project.id), 60)]


@pytest.mark.asyncio
async def test_list_cache_is_invalidated_after_create_update_and_delete() -> None:
    source: InMemoryRepository[Project] = InMemoryRepository()
    cache = FakeCache()
    repository = CachedProjectRepository(source, cache, ttl_seconds=45)

    assert await repository.list() == []
    created = await repository.create(Project(key="ONE", name="One"))
    assert CachedProjectRepository._LIST_KEY not in cache.values
    assert await repository.list() == [created]

    updated = created.model_copy(update={"name": "Updated"})
    assert await repository.update(updated) == updated
    assert CachedProjectRepository._LIST_KEY not in cache.values
    assert await repository.delete(created.id) is True
    assert CachedProjectRepository._LIST_KEY not in cache.values
    assert repository._item_key(created.id) not in cache.values


@pytest.mark.asyncio
async def test_corrupt_cache_is_removed_and_rebuilt_from_database() -> None:
    source: InMemoryRepository[Project] = InMemoryRepository()
    project = await source.create(Project(key="GOOD", name="Good"))
    cache = FakeCache()
    repository = CachedProjectRepository(source, cache, ttl_seconds=60)
    key = repository._item_key(project.id)
    cache.values[key] = "not-json"

    assert await repository.get(project.id) == project
    assert cache.values[key] == project.model_dump_json()
    assert (key,) in cache.deletes


@pytest.mark.asyncio
async def test_cache_outage_fails_open_to_authoritative_repository() -> None:
    source: InMemoryRepository[Project] = InMemoryRepository()
    project = await source.create(Project(key="DB", name="Database"))
    cache = FakeCache()
    cache.fail = True
    repository = CachedProjectRepository(source, cache, ttl_seconds=60)

    assert await repository.get(project.id) == project
    assert await repository.list() == [project]
    assert await repository.get(uuid4()) is None
