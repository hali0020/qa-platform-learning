"""Minimal read-only port shared by local environment and Vault adapters."""

from __future__ import annotations

import re
from collections.abc import Iterable
from enum import Enum
from typing import Protocol, runtime_checkable

from app.secrets.errors import SecretStoreConfigurationError


class SecretName(str, Enum):
    """The complete application secret allowlist.

    This enum intentionally contains configuration keys rather than Vault
    paths.  Callers cannot turn user input into a path, and adapters cannot
    grow a generic list/read API by accident.
    """

    DATABASE_URL = "DATABASE_URL"
    BROKER_URL = "BROKER_URL"
    OBJECT_STORAGE_ACCESS_KEY = "OBJECT_STORAGE_ACCESS_KEY"
    OBJECT_STORAGE_SECRET_KEY = "OBJECT_STORAGE_SECRET_KEY"
    OIDC_CLIENT_SECRET = "OIDC_CLIENT_SECRET"


BUILTIN_SECRET_NAMES = frozenset(item.value for item in SecretName)
_PROVIDER_SECRET_NAME = re.compile(r"QA_PROVIDER_SECRET_[A-Z0-9_]{1,109}\Z")


def provider_secret_allowlist(names: Iterable[str]) -> frozenset[str]:
    """Freeze and validate the caller-controlled provider field allowlist."""

    values = frozenset(names)
    if any(
        not isinstance(name, str)
        or _PROVIDER_SECRET_NAME.fullmatch(name) is None
        for name in values
    ):
        raise SecretStoreConfigurationError(
            "provider secret allowlist contains an invalid name"
        )
    return values


def require_secret_name(name: str, allowed_names: frozenset[str]) -> str:
    """Reject arbitrary names before either adapter touches its backing store."""

    if (
        not isinstance(name, str)
        or not name
        or name not in allowed_names
    ):
        raise SecretStoreConfigurationError("secret name is not allowlisted")
    return name


@runtime_checkable
class SecretStore(Protocol):
    """Read one allowlisted secret; listing and mutation are not supported."""

    async def read(self, name: str) -> str: ...

    async def aclose(self) -> None: ...


__all__ = [
    "BUILTIN_SECRET_NAMES",
    "SecretName",
    "SecretStore",
    "provider_secret_allowlist",
    "require_secret_name",
]
