"""Construct exactly one local secret-store adapter from validated settings."""

from __future__ import annotations

from collections.abc import Callable, Mapping

from app.secrets.base import SecretStore
from app.secrets.environment import EnvironmentSecretStore
from app.secrets.errors import SecretStoreConfigurationError
from app.secrets.vault import VaultSecretStore, read_vault_app_token_file


def build_secret_store(
    settings: object,
    *,
    environ: Mapping[str, str] | None = None,
    token_reader: Callable[[], str] = read_vault_app_token_file,
) -> SecretStore:
    """Keep the default adapter socket-free and Vault explicitly opt-in."""

    runtime_mode = str(
        getattr(settings, "secret_store_runtime_mode", "env_local")
    )
    allowed_names = tuple(
        getattr(settings, "provider_secret_env_names", ())
    )
    if runtime_mode == "env_local":
        return EnvironmentSecretStore(
            environ,
            allowed_names=allowed_names,
        )
    if runtime_mode != "vault_local_container":
        raise SecretStoreConfigurationError("secret store mode is invalid")

    # The helper reads one fixed, non-symlink container secret path. It is
    # called only after the operator explicitly selects the local Vault lab.
    token = token_reader()
    return VaultSecretStore(
        app_env=str(getattr(settings, "app_env", "")),
        endpoint_url=str(getattr(settings, "vault_endpoint_url", "")),
        kv_mount=str(getattr(settings, "vault_kv_mount", "")),
        token=token,
        max_concurrency=int(
            getattr(settings, "vault_max_concurrency", 4)
        ),
        operation_timeout_seconds=float(
            getattr(settings, "vault_operation_timeout_seconds", 3.0)
        ),
        max_attempts=int(getattr(settings, "vault_max_attempts", 3)),
        allowed_names=allowed_names,
    )


__all__ = ["build_secret_store"]
