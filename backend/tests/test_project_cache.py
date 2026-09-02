from __future__ import annotations

from uuid import uuid4
from asyncio import Lock

import pytest

from app.domain.models import Project
from app.cache import CacheLookup
from app.repositories.cached_projects import CachedProjectRepository
from app.repositories.memory import InMemoryRepository
from app.schemas.projects import ProjectUpdate
from app.services.projects import ProjectService


class FakeCache:
    def __init__(self) -> None:
        self.values: dict[str, str] = {}
        self.gets: list[str] = []
        self.sets: list[tuple[str, int]] = []
        self.deletes: list[tuple[str, ...]] = []
        self.fail = False

    async def get(self, key: str) -> CacheLookup:
        self.gets.append(key)
        if self.fail:
            return CacheLookup(None, failed=True)
        return CacheLookup(self.values.get(key))

    async def set(self, key: str, value: str, *, ttl_seconds: int) -> bool:
        if self.fail:
            return False
        self.values[key] = value
        self.sets.append((key, ttl_seconds))
        return True

    async def delete(self, *keys: str) -> bool:
        self.deletes.append(keys)
        for key in keys:
            self.values.pop(key, None)
        return not self.fail

    async def aclose(self) -> None:
        return


class FakeMetrics:
    def __init__(self) -> None:
        self.lookups: list[str] = []
        self.operations: list[tuple[str, bool]] = []
        self.fallbacks = 0

    def record_cache_lookup(self, *, cache: str, outcome: str) -> None:
        assert cache == "projects"
        self.lookups.append(outcome)

    def record_cache_operation(
        self, *, cache: str, operation: str, succeeded: bool
    ) -> None:
        assert cache == "projects"
        self.operations.append((operation, succeeded))

    def observe_database_fallback(
        self, *, cache: str, duration_seconds: float
    ) -> None:
        assert cache == "projects"
        assert duration_seconds >= 0
        self.fallbacks += 1


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
    assert repository._item_key(created.id) not in cache.values
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


@pytest.mark.asyncio
async def test_cache_metrics_record_miss_hit_fallback_fill_and_invalidation() -> None:
    source: InMemoryRepository[Project] = InMemoryRepository()
    project = await source.create(Project(key="METRIC", name="Metrics"))
    cache = FakeCache()
    metrics = FakeMetrics()
    repository = CachedProjectRepository(
        source, cache, ttl_seconds=60, metrics=metrics
    )

    assert await repository.get(project.id) == project
    assert await repository.get(project.id) == project
    await repository.invalidate(project.id)

    assert metrics.lookups == ["miss", "hit"]
    assert metrics.fallbacks == 1
    assert metrics.operations == [("fill", True), ("invalidate", True)]


@pytest.mark.asyncio
async def test_service_uses_cache_for_display_but_database_for_update_validation() -> None:
    projects: InMemoryRepository[Project] = InMemoryRepository()
    project = await projects.create(Project(key="SPLIT", name="Database value"))
    cache = FakeCache()
    queries = CachedProjectRepository(projects, cache, ttl_seconds=60)
    cache.values[queries._item_key(project.id)] = project.model_copy(
        update={"name": "Stale cache"}
    ).model_dump_json()
    empty = InMemoryRepository()
    service = ProjectService(
        projects,
        empty,
        empty,
        empty,
        empty,
        empty,
        empty,
        Lock(),
        project_queries=queries,
        cache_invalidator=queries,
    )

    assert (await service.get(project.id)).name == "Stale cache"
    updated = await service.update(
        project.id, ProjectUpdate(description="fresh write")
    )

    assert updated.name == "Database value"
    assert updated.description == "fresh write"
    assert queries._item_key(project.id) not in cache.values
