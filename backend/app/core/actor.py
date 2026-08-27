from contextvars import ContextVar, Token
from dataclasses import dataclass
from uuid import UUID


@dataclass(frozen=True, slots=True)
class ActorIdentity:
    user_id: UUID
    username: str


_current_actor: ContextVar[ActorIdentity | None] = ContextVar(
    "current_actor",
    default=None,
)


def set_current_actor(actor: ActorIdentity) -> Token:
    return _current_actor.set(actor)


def reset_current_actor(token: Token) -> None:
    _current_actor.reset(token)


def get_current_actor() -> ActorIdentity | None:
    return _current_actor.get()
