from __future__ import annotations

import pytest

from app.core.config import Settings
from app.worker.main import build_worker_from_settings, build_worker_options


def test_worker_options_use_container_hostname_and_bounded_values() -> None:
    options = build_worker_options(
        {
            "HOSTNAME": "worker-container-abc",
            "WORKER_QUEUES": "default,quality",
            "WORKER_LEASE_SECONDS": "60",
            "WORKER_HEARTBEAT_SECONDS": "15",
            "WORKER_POLL_SECONDS": "2.5",
            "WORKER_SHUTDOWN_GRACE_SECONDS": "20",
        }
    )

    assert options.worker_id == "worker-container-abc"
    assert options.queues == ("default", "quality")
    assert options.lease_seconds == 60
    assert options.poll_interval_seconds == 2.5


def test_worker_options_reject_missing_identity_and_invalid_numbers() -> None:
    with pytest.raises(ValueError, match="worker_id"):
        build_worker_options({})
    with pytest.raises(RuntimeError, match="整数"):
        build_worker_options({"HOSTNAME": "worker-1", "WORKER_LEASE_SECONDS": "3.5"})
    with pytest.raises(ValueError, match="30"):
        build_worker_options(
            {
                "HOSTNAME": "worker-1",
                "WORKER_SHUTDOWN_GRACE_SECONDS": "31",
            }
        )


@pytest.mark.asyncio
async def test_worker_refuses_sqlite_or_non_container_runtime_before_startup() -> None:
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        await build_worker_from_settings(
            Settings(),
            environ={"HOSTNAME": "worker-1"},
        )

    with pytest.raises(RuntimeError, match="postgres_local_container"):
        await build_worker_from_settings(
            Settings(app_env="local-container"),
            environ={"HOSTNAME": "worker-1"},
        )


@pytest.mark.asyncio
async def test_worker_requires_rabbitmq_mode_before_database_construction() -> None:
    settings = Settings(
        app_env="local-container",
        database_runtime_mode="postgres_local_container",
        database_url="postgresql+asyncpg://qa:local-test@postgres:5432/qa",
        broker_runtime_mode="disabled_local",
        broker_url="",
    )

    with pytest.raises(RuntimeError, match="rabbitmq_local_container"):
        await build_worker_from_settings(
            settings,
            environ={"HOSTNAME": "worker-1"},
        )
