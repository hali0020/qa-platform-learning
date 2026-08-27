"""Read-only secret store ports and local teaching adapters."""

from app.secrets.base import BUILTIN_SECRET_NAMES, SecretName, SecretStore
from app.secrets.environment import EnvironmentSecretStore
from app.secrets.factory import build_secret_store
from app.secrets.errors import (
    SecretNotFoundError,
    SecretStoreConfigurationError,
    SecretStoreError,
    SecretStoreResponseError,
    SecretStoreUnavailableError,
)
from app.secrets.vault import (
    SECRET_STORE_RUNTIME_MODES,
    VAULT_APP_TOKEN_FILE,
    VAULT_KV_V2_MOUNT,
    VAULT_LOCAL_ENDPOINT,
    VAULT_PROVIDERS_PATH,
    VAULT_RUNTIME_PATH,
    VaultSecretStore,
    read_vault_app_token_file,
    validate_secret_store_runtime_target,
)

__all__ = [
    "BUILTIN_SECRET_NAMES",
    "EnvironmentSecretStore",
    "SECRET_STORE_RUNTIME_MODES",
    "SecretName",
    "SecretNotFoundError",
    "SecretStore",
    "SecretStoreConfigurationError",
    "SecretStoreError",
    "SecretStoreResponseError",
    "SecretStoreUnavailableError",
    "VAULT_APP_TOKEN_FILE",
    "VAULT_KV_V2_MOUNT",
    "VAULT_LOCAL_ENDPOINT",
    "VAULT_PROVIDERS_PATH",
    "VAULT_RUNTIME_PATH",
    "VaultSecretStore",
    "build_secret_store",
    "read_vault_app_token_file",
    "validate_secret_store_runtime_target",
]
