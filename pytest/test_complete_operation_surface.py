from __future__ import annotations

import re
from uuid import UUID

import pytest

from app.core.config import Settings
from app.main import create_app


pytestmark = [pytest.mark.asyncio, pytest.mark.area("完整操作面触达")]


def _discover_operations():
    discovery_app = create_app(Settings(app_env="test", auth_enabled=False, database_url="sqlite+aiosqlite:///:memory:"))
    operations = []
    for route in discovery_app.router.routes:
        path = getattr(route, "path", "")
        if not (path.startswith("/api/v1") or path in {"/health/live", "/health/ready", "/metrics"}):
            continue
        for method in sorted(getattr(route, "methods", set())):
            if method in {"HEAD", "OPTIONS"}:
                continue
            operations.append((method, path))
    return operations


OPERATIONS = _discover_operations()


def _materialize(path: str) -> str:
    replacements = {
        "entity": "test-cases",
        "provider": "local",
        "operation": "trigger",
    }
    def replace(match):
        name = match.group(1)
        return replacements.get(name, "00000000-0000-0000-0000-000000000001")
    return re.sub(r"\{([^}:]+)(?::[^}]+)?\}", replace, path)


@pytest.mark.parametrize("method,path", OPERATIONS, ids=[f"{m} {p}" for m, p in OPERATIONS])
async def test_every_public_operation_is_addressable_without_server_error(client, method, path):
    concrete = _materialize(path)
    kwargs = {}
    if method in {"POST", "PUT", "PATCH"}:
        kwargs["json"] = {}
    response = await client.request(method, concrete, **kwargs)
    assert response.status_code < 500, f"{method} {path} returned {response.status_code}: {response.text}"
    assert response.headers.get("x-request-id"), f"{method} {path} missed Request ID middleware"

