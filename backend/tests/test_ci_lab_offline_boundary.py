from __future__ import annotations

import asyncio
import socket
import subprocess
from datetime import datetime, timezone
from pathlib import Path
from typing import NoReturn

import pytest

from app.ci_lab.database import CiLabDatabase
from app.ci_lab.models import TriggerRunRequest
from app.ci_lab.registry import DEFAULT_DEFINITION_REGISTRY
from app.ci_lab.service import CiLabService


def forbidden(*_args: object, **_kwargs: object) -> NoReturn:
    raise AssertionError("CI Lab crossed its offline deterministic boundary")


@pytest.mark.asyncio
async def test_core_never_opens_network_or_subprocess(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setattr(socket, "create_connection", forbidden)
    monkeypatch.setattr(subprocess, "Popen", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_exec", forbidden)
    monkeypatch.setattr(asyncio, "create_subprocess_shell", forbidden)

    now = datetime(2026, 8, 27, tzinfo=timezone.utc)
    service = CiLabService(
        CiLabDatabase(tmp_path / "offline.db"),
        DEFAULT_DEFINITION_REGISTRY,
        clock=lambda: now,
    )
    try:
        created = await service.trigger(
            "local-quality-gate",
            TriggerRunRequest(ref="main", variables={"BUILD_MODE": "learning"}),
            "offline-boundary-001",
        )
        queried = await service.get(created.id)
        cancelled = await service.cancel(created.id)
        assert queried.id == created.id
        assert cancelled.status.value == "cancelled"
    finally:
        await service.close()
