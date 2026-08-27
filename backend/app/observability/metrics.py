from __future__ import annotations

from collections.abc import Mapping
from enum import Enum

from prometheus_client import CollectorRegistry, Counter, Gauge, Histogram


class TaskMetricState(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    RETRY_WAIT = "retry_wait"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"
    DEAD_LETTER = "dead_letter"


class DeviceMetricState(str, Enum):
    OFFLINE = "offline"
    IDLE = "idle"
    LEASED = "leased"
    BUSY = "busy"
    UNHEALTHY = "unhealthy"


class ProviderMetricKind(str, Enum):
    LOCAL = "local"
    JENKINS = "jenkins"
    GITLAB = "gitlab"
    BK_CI = "bk_ci"


class ProviderMetricOperation(str, Enum):
    TEST_CONNECTION = "test_connection"
    TRIGGER = "trigger"
    QUERY = "query"
    CANCEL = "cancel"


class ProviderMetricOutcome(str, Enum):
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    REJECTED = "rejected"
    TIMEOUT = "timeout"


_HTTP_METHODS = frozenset(
    {"GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS"}
)


def bounded_http_method(value: str) -> str:
    normalized = value.upper()
    return normalized if normalized in _HTTP_METHODS else "OTHER"


def route_template(scope: Mapping[str, object]) -> str:
    """Return a route template, never a concrete user-controlled URL path."""

    route = scope.get("route")
    path = getattr(route, "path", None)
    if isinstance(path, str) and path:
        return path
    return "_unmatched"


class HttpMetrics:
    def __init__(self, registry: CollectorRegistry) -> None:
        self.requests_total = Counter(
            "qa_http_requests_total",
            "Completed HTTP requests.",
            ("method", "route", "status_code"),
            registry=registry,
        )
        self.request_duration_seconds = Histogram(
            "qa_http_request_duration_seconds",
            "HTTP request duration in seconds.",
            ("method", "route"),
            buckets=(0.005, 0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5),
            registry=registry,
        )
        # No labels: in-flight is a process-wide saturation signal and must not
        # create one series per route.
        self.requests_in_flight = Gauge(
            "qa_http_requests_in_flight",
            "HTTP requests currently being processed.",
            registry=registry,
        )

    def started(self) -> None:
        self.requests_in_flight.inc()

    def completed(
        self,
        *,
        method: str,
        route: str,
        status_code: int,
        duration_seconds: float,
    ) -> None:
        method_label = bounded_http_method(method)
        status_label = str(status_code if 100 <= status_code <= 599 else 500)
        self.requests_total.labels(
            method=method_label,
            route=route,
            status_code=status_label,
        ).inc()
        self.request_duration_seconds.labels(
            method=method_label,
            route=route,
        ).observe(max(0.0, duration_seconds))
        self.requests_in_flight.dec()


class BusinessMetrics:
    """Bounded-label metrics API for automation, devices and CI providers.

    IDs, hostnames, project names, URLs and exception messages are purposely
    absent from every label. Enum conversion rejects accidental high-cardinality
    labels at the call site.
    """

    def __init__(self, registry: CollectorRegistry) -> None:
        self.task_count = Gauge(
            "qa_automation_tasks",
            "Automation tasks by bounded lifecycle state.",
            ("state",),
            registry=registry,
        )
        self.device_count = Gauge(
            "qa_devices",
            "Managed devices by bounded availability state.",
            ("state",),
            registry=registry,
        )
        self.provider_requests_total = Counter(
            "qa_provider_requests_total",
            "CI provider operations by provider, operation and outcome.",
            ("provider", "operation", "outcome"),
            registry=registry,
        )
        self.provider_request_duration_seconds = Histogram(
            "qa_provider_request_duration_seconds",
            "CI provider operation duration in seconds.",
            ("provider", "operation"),
            buckets=(0.01, 0.025, 0.05, 0.1, 0.25, 0.5, 1, 2.5, 5, 10, 30),
            registry=registry,
        )

    def set_task_snapshot(
        self,
        counts: Mapping[TaskMetricState | str, int],
    ) -> None:
        normalized = {
            TaskMetricState(state): self._safe_count(count)
            for state, count in counts.items()
        }
        for state in TaskMetricState:
            self.task_count.labels(state=state.value).set(normalized.get(state, 0))

    def set_device_snapshot(
        self,
        counts: Mapping[DeviceMetricState | str, int],
    ) -> None:
        normalized = {
            DeviceMetricState(state): self._safe_count(count)
            for state, count in counts.items()
        }
        for state in DeviceMetricState:
            self.device_count.labels(state=state.value).set(
                normalized.get(state, 0)
            )

    def observe_provider_request(
        self,
        *,
        provider: ProviderMetricKind | str,
        operation: ProviderMetricOperation | str,
        outcome: ProviderMetricOutcome | str,
        duration_seconds: float,
    ) -> None:
        provider_value = ProviderMetricKind(provider).value
        operation_value = ProviderMetricOperation(operation).value
        outcome_value = ProviderMetricOutcome(outcome).value
        self.provider_requests_total.labels(
            provider=provider_value,
            operation=operation_value,
            outcome=outcome_value,
        ).inc()
        self.provider_request_duration_seconds.labels(
            provider=provider_value,
            operation=operation_value,
        ).observe(max(0.0, duration_seconds))

    @staticmethod
    def _safe_count(value: int) -> int:
        if isinstance(value, bool) or not isinstance(value, int) or value < 0:
            raise ValueError("metric counts must be non-negative integers")
        return value


class ObservabilityMetrics:
    """Metrics bound to one explicit registry, safe for app-factory tests."""

    def __init__(self, registry: CollectorRegistry | None = None) -> None:
        self.registry = registry or CollectorRegistry(auto_describe=True)
        self.http = HttpMetrics(self.registry)
        self.business = BusinessMetrics(self.registry)
