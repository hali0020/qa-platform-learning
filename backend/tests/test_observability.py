from __future__ import annotations

import io
import json
import logging
from collections import Counter
from types import SimpleNamespace
from typing import cast

import pytest
from fastapi import FastAPI
from httpx import ASGITransport, AsyncClient
from prometheus_client import CollectorRegistry, generate_latest

from app.database import Database
from app.observability import (
    DeviceMetricState,
    JsonLogFormatter,
    ObservabilityMetrics,
    ProviderMetricKind,
    ReadinessResult,
    SqliteReadinessProbe,
    TaskMetricState,
    install_observability,
    observability_router,
)


def settings(*, metrics_enabled: bool = True) -> SimpleNamespace:
    return SimpleNamespace(
        app_name="observability-test",
        app_env="test",
        metrics_enabled=metrics_enabled,
        request_logging_enabled=True,
    )


def build_test_logger() -> tuple[logging.Logger, io.StringIO]:
    stream = io.StringIO()
    handler = logging.StreamHandler(stream)
    handler.setFormatter(JsonLogFormatter())
    logger = logging.Logger("qa.test.observability", level=logging.INFO)
    logger.addHandler(handler)
    return logger, stream


@pytest.mark.asyncio
async def test_http_metrics_use_route_templates_and_request_id_logs() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    registry = CollectorRegistry(auto_describe=True)
    logger, log_stream = build_test_logger()
    application = FastAPI()

    @application.get("/items/{item_id}")
    async def item(item_id: str) -> dict[str, str]:
        return {"item_id": item_id}

    install_observability(
        application,
        database=database,
        settings=settings(),
        registry=registry,
        logger=logger,
        configure_logger=False,
    )
    application.include_router(observability_router)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            first = await client.get(
                "/items/device-secret-001?access_token=must-not-be-logged",
                headers={"X-Request-ID": "lesson-123"},
            )
            second = await client.get(
                "/items/device-secret-002",
                headers={"X-Request-ID": "invalid request id"},
            )
            scrape = await client.get("/metrics")
    finally:
        await database.shutdown()

    assert first.headers["x-request-id"] == "lesson-123"
    generated_id = second.headers["x-request-id"]
    assert generated_id != "invalid request id"
    assert len(generated_id) == 32
    assert scrape.status_code == 200

    metrics_text = scrape.text
    assert (
        'qa_http_requests_total{method="GET",route="/items/{item_id}",'
        'status_code="200"} 2.0'
    ) in metrics_text
    assert "device-secret-001" not in metrics_text
    assert "device-secret-002" not in metrics_text
    assert "qa_http_requests_in_flight 0.0" in metrics_text

    log_lines = [json.loads(line) for line in log_stream.getvalue().splitlines()]
    item_logs = [
        line for line in log_lines if line.get("route") == "/items/{item_id}"
    ]
    assert len(item_logs) == 2
    assert item_logs[0]["request_id"] == "lesson-123"
    assert item_logs[1]["request_id"] == generated_id
    assert "device-secret" not in log_stream.getvalue()
    assert "must-not-be-logged" not in log_stream.getvalue()


@pytest.mark.asyncio
async def test_health_routes_and_metrics_switch_are_independent() -> None:
    database = Database("sqlite+aiosqlite:///:memory:")
    application = FastAPI()
    logger, _ = build_test_logger()
    install_observability(
        application,
        database=database,
        settings=settings(metrics_enabled=False),
        registry=CollectorRegistry(auto_describe=True),
        logger=logger,
        configure_logger=False,
    )
    application.include_router(observability_router)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            live = await client.get("/health/live")
            ready = await client.get("/health/ready")
            metrics = await client.get("/metrics")
    finally:
        await database.shutdown()

    assert live.status_code == 200
    assert live.json()["status"] == "alive"
    assert ready.status_code == 200
    assert ready.json() == {
        "status": "ready",
        "checks": {"database": {"status": "up", "reason": "ok"}},
    }
    assert metrics.status_code == 404


@pytest.mark.asyncio
async def test_readiness_failure_returns_503_without_details() -> None:
    class FailingProbe:
        async def check(self) -> ReadinessResult:
            return ReadinessResult(False, "database_query_failed")

    database = Database("sqlite+aiosqlite:///:memory:")
    application = FastAPI()
    logger, _ = build_test_logger()
    install_observability(
        application,
        database=database,
        settings=settings(),
        registry=CollectorRegistry(auto_describe=True),
        readiness_probe=FailingProbe(),
        logger=logger,
        configure_logger=False,
    )
    application.include_router(observability_router)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/health/ready")
    finally:
        await database.shutdown()

    assert response.status_code == 503
    assert response.json()["checks"]["database"] == {
        "status": "down",
        "reason": "database_query_failed",
    }


@pytest.mark.asyncio
async def test_database_probe_rejects_unsupported_backend_before_connecting() -> None:
    class FakeUrl:
        def get_backend_name(self) -> str:
            return "mysql"

    class FakeEngine:
        url = FakeUrl()
        connect_calls = 0

        def connect(self):
            self.connect_calls += 1
            raise AssertionError("must not attempt an external connection")

    fake_database = SimpleNamespace(engine=FakeEngine())
    probe = SqliteReadinessProbe(cast(Database, fake_database))

    result = await probe.check()

    assert result == ReadinessResult(False, "unsupported_database_backend")
    assert fake_database.engine.connect_calls == 0


def test_business_metrics_have_only_bounded_labels() -> None:
    metrics = ObservabilityMetrics(CollectorRegistry(auto_describe=True))

    metrics.business.set_task_snapshot(
        {TaskMetricState.QUEUED: 3, TaskMetricState.RUNNING: 1}
    )
    metrics.business.set_device_snapshot(
        {DeviceMetricState.IDLE: 2, DeviceMetricState.LEASED: 1}
    )
    metrics.business.observe_provider_request(
        provider=ProviderMetricKind.JENKINS,
        operation="trigger",
        outcome="succeeded",
        duration_seconds=0.12,
    )

    text = generate_latest(metrics.registry).decode()
    assert 'qa_automation_tasks{state="queued"} 3.0' in text
    assert 'qa_automation_tasks{state="dead_letter"} 0.0' in text
    assert 'qa_devices{state="idle"} 2.0' in text
    assert (
        'qa_provider_requests_total{operation="trigger",outcome="succeeded",'
        'provider="jenkins"} 1.0'
    ) in text

    with pytest.raises(ValueError):
        metrics.business.set_task_snapshot({"task-id-123": 1})
    with pytest.raises(ValueError):
        metrics.business.observe_provider_request(
            provider="https://ci.example.test",
            operation="trigger",
            outcome="succeeded",
            duration_seconds=1,
        )


def test_each_runtime_uses_an_independent_registry() -> None:
    first = ObservabilityMetrics()
    second = ObservabilityMetrics()

    first.business.set_task_snapshot({"queued": 5})
    second.business.set_task_snapshot({"queued": 2})

    first_text = generate_latest(first.registry).decode()
    second_text = generate_latest(second.registry).decode()
    assert 'qa_automation_tasks{state="queued"} 5.0' in first_text
    assert 'qa_automation_tasks{state="queued"} 2.0' in second_text


def test_cache_metrics_expose_bounded_lookup_and_fallback_signals() -> None:
    metrics = ObservabilityMetrics(CollectorRegistry(auto_describe=True))

    metrics.cache.record_cache_lookup(cache="projects", outcome="hit")
    metrics.cache.record_cache_lookup(cache="projects", outcome="miss")
    metrics.cache.record_cache_operation(
        cache="projects", operation="fill", succeeded=True
    )
    metrics.cache.observe_database_fallback(
        cache="projects", duration_seconds=0.012
    )

    text = generate_latest(metrics.registry).decode()
    assert 'qa_cache_lookups_total{cache="projects",outcome="hit"} 1.0' in text
    assert 'qa_cache_lookups_total{cache="projects",outcome="miss"} 1.0' in text
    assert (
        'qa_cache_operations_total{cache="projects",operation="fill",'
        'outcome="succeeded"} 1.0'
    ) in text
    assert 'qa_cache_database_fallback_total{cache="projects"} 1.0' in text

    with pytest.raises(ValueError):
        metrics.cache.record_cache_lookup(cache="user-id", outcome="hit")


@pytest.mark.asyncio
async def test_metrics_uses_the_runtime_read_only_snapshot_contract() -> None:
    class SnapshotOnlyRuntime:
        calls = 0

        async def observability_snapshot(self):
            self.calls += 1
            return Counter({"queued": 2}), Counter({"reserved": 1, "maintenance": 1})

    database = Database("sqlite+aiosqlite:///:memory:")
    application = FastAPI()
    logger, _ = build_test_logger()
    install_observability(
        application,
        database=database,
        settings=settings(),
        registry=CollectorRegistry(auto_describe=True),
        logger=logger,
        configure_logger=False,
    )
    snapshot_runtime = SnapshotOnlyRuntime()
    application.state.runtime_service = snapshot_runtime
    application.include_router(observability_router)

    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://testserver",
        ) as client:
            response = await client.get("/metrics")
    finally:
        await database.shutdown()

    assert response.status_code == 200
    assert snapshot_runtime.calls == 1
    assert 'qa_automation_tasks{state="queued"} 2.0' in response.text
    assert 'qa_devices{state="leased"} 1.0' in response.text
    assert 'qa_devices{state="unhealthy"} 1.0' in response.text
