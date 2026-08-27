class BrokerError(RuntimeError):
    """Base error for wake-up broker adapters."""


class BrokerDependencyError(BrokerError):
    """Raised when an explicitly enabled adapter dependency is unavailable."""


class BrokerStateError(BrokerError):
    """Raised when a broker endpoint is used outside its lifecycle."""


class BrokerTransportError(BrokerError):
    """Raised without embedding a URL, credential, or remote error body."""


__all__ = [
    "BrokerDependencyError",
    "BrokerError",
    "BrokerStateError",
    "BrokerTransportError",
]
