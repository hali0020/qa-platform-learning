from __future__ import annotations

import pytest

from app.core.config import Settings
from app.scheduler.main import (
    build_scheduler_from_settings,
    build_scheduler_options,
)


def test_scheduler_options_use_explicit_or_container_identity() -> None:
    explicit = build_scheduler_options(
        {
            "SCHEDULER_ID": "scheduler-explicit",
            "HOSTNAME": "ignored-hostname",
            "SCHEDULER_LEASE_SECONDS": "60",
            "SCHEDULER_POLL_SECONDS": "2.5",
        }
    )
    fallback = build_scheduler_options({"HOSTNAME": "scheduler-container"})

    assert explicit.scheduler_id == "scheduler-explicit"
    assert explicit.lease_seconds == 60
    assert explicit.poll_interval_seconds == 2.5
    assert fallback.scheduler_id == "scheduler-container"


def test_scheduler_options_reject_missing_identity_and_invalid_numbers() -> None:
    with pytest.raises(ValueError, match="scheduler_id"):
        build_scheduler_options({})
    with pytest.raises(RuntimeError, match="整数"):
        build_scheduler_options(
            {"HOSTNAME": "scheduler-a", "SCHEDULER_LEASE_SECONDS": "3.5"}
        )


@pytest.mark.asyncio
async def test_independent_scheduler_fails_closed_for_non_postgres_runtime() -> None:
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        await build_scheduler_from_settings(
            Settings(),
            environ={"HOSTNAME": "scheduler-a"},
        )

    with pytest.raises(RuntimeError, match="SQLite 仅保留教学单进程"):
        await build_scheduler_from_settings(
            Settings(app_env="local-container"),
            environ={"HOSTNAME": "scheduler-a"},
        )
