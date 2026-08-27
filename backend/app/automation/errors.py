class AutomationError(Exception):
    """Base automation-domain error."""


class AutomationNotFoundError(AutomationError):
    pass


class AutomationConflictError(AutomationError):
    pass


class AutomationLeaseError(AutomationError):
    pass


class AutomationValidationError(AutomationError):
    pass


__all__ = [
    "AutomationConflictError",
    "AutomationError",
    "AutomationLeaseError",
    "AutomationNotFoundError",
    "AutomationValidationError",
]
