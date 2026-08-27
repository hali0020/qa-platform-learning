from __future__ import annotations

from dataclasses import dataclass
from typing import Any


@dataclass(frozen=True, slots=True)
class ObservabilityOptions:
    """Runtime switches kept separate from the application's core settings.

    ``from_settings`` deliberately uses ``getattr`` so the observability module
    can be wired before the shared ``Settings`` class grows the corresponding
    fields. Metrics stay enabled by default for the loopback-only learning app.
    """

    service_name: str = "qa-platform-learning"
    environment: str = "local"
    metrics_enabled: bool = True
    request_logging_enabled: bool = True

    @classmethod
    def from_settings(cls, settings: Any) -> "ObservabilityOptions":
        return cls(
            service_name=str(
                getattr(settings, "app_name", cls.service_name)
            ),
            environment=str(getattr(settings, "app_env", cls.environment)),
            metrics_enabled=bool(
                getattr(settings, "metrics_enabled", cls.metrics_enabled)
            ),
            request_logging_enabled=bool(
                getattr(
                    settings,
                    "request_logging_enabled",
                    cls.request_logging_enabled,
                )
            ),
        )
