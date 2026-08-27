"""Safe error types for the local-only secret store boundary."""


class SecretStoreError(Exception):
    """Base error whose message is safe to log without secret material."""


class SecretStoreConfigurationError(SecretStoreError):
    """The selected secret store topology or requested name is invalid."""


class SecretStoreUnavailableError(SecretStoreError):
    """The selected secret store could not complete a bounded operation."""


class SecretNotFoundError(SecretStoreError):
    """An allowlisted secret is absent from the selected store."""


class SecretStoreResponseError(SecretStoreError):
    """The selected store returned an invalid or unsafe response."""


__all__ = [
    "SecretNotFoundError",
    "SecretStoreConfigurationError",
    "SecretStoreError",
    "SecretStoreResponseError",
    "SecretStoreUnavailableError",
]
