class ProviderError(Exception):
    """Base error whose message is safe to expose without credentials."""


class ProviderConfigurationError(ProviderError):
    """Static provider configuration is invalid."""


class ProviderDisabledError(ProviderError):
    """A real provider was invoked without an explicit enable gate."""


class ProviderSecurityError(ProviderError):
    """An outbound URL or resolved address violated the egress policy."""


class ProviderTransportError(ProviderError):
    """The remote service could not be reached safely."""


class ProviderResponseError(ProviderError):
    """The remote service returned an unusable response."""


class ProviderConflictError(ProviderError):
    """An idempotency identifier was reused with different input."""


__all__ = [
    "ProviderConfigurationError",
    "ProviderConflictError",
    "ProviderDisabledError",
    "ProviderError",
    "ProviderResponseError",
    "ProviderSecurityError",
    "ProviderTransportError",
]
