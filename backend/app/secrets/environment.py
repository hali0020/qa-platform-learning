"""Default secret adapter backed only by the current process environment."""

from __future__ import annotations

import os
from collections.abc import Iterable, Mapping

from app.secrets.base import (
    BUILTIN_SECRET_NAMES,
    provider_secret_allowlist,
    require_secret_name,
)
from app.secrets.errors import SecretNotFoundError, SecretStoreResponseError


class EnvironmentSecretStore:
    """Read the fixed allowlist without opening sockets or mutating the source.

    The mapping is intentionally read on demand.  This keeps startup code in
    charge of which secrets it consumes and avoids copying unrelated process
    environment entries into a long-lived application object.
    """

    runtime_mode = "env_local"

    def __init__(
        self,
        environ: Mapping[str, str] | None = None,
        *,
        allowed_names: Iterable[str] = (),
    ) -> None:
        self._environ = os.environ if environ is None else environ
        self._allowed_names = (
            BUILTIN_SECRET_NAMES | provider_secret_allowlist(allowed_names)
        )
        self._closed = False

    async def read(self, name: str) -> str:
        allowed_name = require_secret_name(name, self._allowed_names)
        if self._closed:
            raise SecretNotFoundError("secret store is closed")
        value = self._environ.get(allowed_name)
        if value is None:
            raise SecretNotFoundError("required secret is missing")
        if (
            not value
            or not value.strip()
            or len(value) > 8192
            or "\x00" in value
            or "\r" in value
            or "\n" in value
        ):
            raise SecretStoreResponseError("secret value is invalid")
        return value

    async def aclose(self) -> None:
        self._closed = True

    def __repr__(self) -> str:
        return "EnvironmentSecretStore(runtime_mode='env_local')"


__all__ = ["EnvironmentSecretStore"]
