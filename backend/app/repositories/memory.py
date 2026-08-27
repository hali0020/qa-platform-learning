import asyncio
from copy import deepcopy
from typing import Generic, TypeVar
from uuid import UUID

from pydantic import BaseModel

from app.core.errors import ConflictError, NotFoundError

EntityT = TypeVar("EntityT", bound=BaseModel)


class InMemoryRepository(Generic[EntityT]):
    """带锁的异步内存仓储，后续可由数据库实现替换。"""

    def __init__(self) -> None:
        self._items: dict[UUID, EntityT] = {}
        self._lock = asyncio.Lock()

    async def create(
        self,
        entity: EntityT,
        *,
        unique_fields: tuple[str, ...] = (),
    ) -> EntityT:
        async with self._lock:
            entity_id = getattr(entity, "id")
            if entity_id in self._items:
                raise ConflictError(f"实体 ID 已存在: {entity_id}")
            self._ensure_unique(entity, unique_fields)
            self._items[entity_id] = deepcopy(entity)
            return deepcopy(entity)

    async def get(self, entity_id: UUID) -> EntityT | None:
        async with self._lock:
            item = self._items.get(entity_id)
            return deepcopy(item) if item is not None else None

    async def list(self) -> list[EntityT]:
        async with self._lock:
            return [deepcopy(item) for item in self._items.values()]

    async def update(
        self,
        entity: EntityT,
        *,
        unique_fields: tuple[str, ...] = (),
    ) -> EntityT:
        async with self._lock:
            entity_id = getattr(entity, "id")
            if entity_id not in self._items:
                raise NotFoundError("实体", entity_id)
            self._ensure_unique(entity, unique_fields, exclude_id=entity_id)
            self._items[entity_id] = deepcopy(entity)
            return deepcopy(entity)

    async def delete(self, entity_id: UUID) -> bool:
        async with self._lock:
            return self._items.pop(entity_id, None) is not None

    async def clear(self) -> None:
        async with self._lock:
            self._items.clear()

    def _ensure_unique(
        self,
        entity: EntityT,
        fields: tuple[str, ...],
        *,
        exclude_id: UUID | None = None,
    ) -> None:
        for existing_id, existing in self._items.items():
            if existing_id == exclude_id:
                continue
            for field in fields:
                if getattr(existing, field) == getattr(entity, field):
                    raise ConflictError(
                        f"字段 {field} 的值已存在: {getattr(entity, field)}"
                    )
