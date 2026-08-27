from typing import Protocol, TypeVar
from uuid import UUID

from pydantic import BaseModel


EntityT = TypeVar("EntityT", bound=BaseModel)


class AsyncRepository(Protocol[EntityT]):
    """领域服务使用的最小异步持久化契约。"""

    async def create(
        self,
        entity: EntityT,
        *,
        unique_fields: tuple[str, ...] = (),
    ) -> EntityT: ...

    async def get(self, entity_id: UUID) -> EntityT | None: ...

    async def list(self) -> list[EntityT]: ...

    async def update(
        self,
        entity: EntityT,
        *,
        unique_fields: tuple[str, ...] = (),
    ) -> EntityT: ...

    async def delete(self, entity_id: UUID) -> bool: ...

    async def clear(self) -> None: ...
