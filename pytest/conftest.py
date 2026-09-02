from __future__ import annotations

import json
import os
import time
from collections import Counter
from pathlib import Path
from typing import Any

import pytest
import pytest_asyncio
from httpx import ASGITransport, AsyncClient, Response

from app.core.config import Settings
from app.main import create_app


REPORT_DIR = Path(__file__).resolve().parent / "reports"
RESULTS: list[dict[str, Any]] = []
HTTP_OBSERVATIONS: list[dict[str, Any]] = []

_APPLICATION_ENV_NAMES = {
    "AWS_EC2_METADATA_DISABLED", "CORS_ORIGINS", "DEBUG", "HOST",
    "LOCAL_ONLY", "PORT",
}
_APPLICATION_ENV_PREFIXES = (
    "APP_", "AUTH_", "BROKER_", "COMPOSE_", "CORS_", "CSRF_",
    "DATABASE_", "IMAGE_", "KEYCLOAK_", "METRICS_", "OBJECT_STORAGE_",
    "OIDC_", "PASSWORD_", "POSTGRES_", "PROVIDER_", "QA_PROVIDER_SECRET_",
    "RABBITMQ_", "REQUEST_LOGGING_", "SECRET_STORE_", "SESSION_", "UPLOAD_",
    "VAULT_", "WORKER_",
)

for _name in tuple(os.environ):
    if _name in _APPLICATION_ENV_NAMES or _name.startswith(_APPLICATION_ENV_PREFIXES):
        os.environ.pop(_name, None)
os.environ["QA_PLATFORM_SKIP_LOCAL_ENV"] = "1"


def _route_template(response: Response) -> str:
    route = response.extensions.get("route")
    if route is not None and getattr(route, "path", None):
        return route.path
    request = response.request
    return request.url.path


class ObservedAsyncClient(AsyncClient):
    def __init__(self, *args, application=None, **kwargs):
        super().__init__(*args, **kwargs)
        self._application = application

    async def request(self, method: str, url, **kwargs):
        started = time.perf_counter()
        response = await super().request(method, url, **kwargs)
        elapsed_ms = (time.perf_counter() - started) * 1000
        template = None
        if self._application is not None:
            for route in self._application.router.routes:
                methods = getattr(route, "methods", set())
                path_regex = getattr(route, "path_regex", None)
                if path_regex is not None and path_regex.fullmatch(response.request.url.path) and method.upper() in methods:
                    template = route.path
                    break
        HTTP_OBSERVATIONS.append({
            "method": method.upper(),
            "path": response.request.url.path,
            "route_template": template or response.request.url.path,
            "status_code": response.status_code,
            "duration_ms": round(elapsed_ms, 3),
        })
        return response


@pytest.fixture
def application(tmp_path: Path):
    database_url = f"sqlite+aiosqlite:///{(tmp_path / 'learning-site.db').as_posix()}"
    return create_app(Settings(
        app_env="test",
        auth_enabled=False,
        database_url=database_url,
        request_logging_enabled=False,
    ))


@pytest_asyncio.fixture
async def client(application):
    async with ObservedAsyncClient(
        transport=ASGITransport(app=application),
        base_url="http://testserver",
        application=application,
    ) as value:
        yield value


def pytest_configure(config):
    config.addinivalue_line("markers", "area(name): product area used by the HTML report")


@pytest.hookimpl(hookwrapper=True)
def pytest_runtest_makereport(item, call):
    outcome = yield
    report = outcome.get_result()
    if report.when != "call":
        return
    marker = item.get_closest_marker("area")
    area = marker.args[0] if marker and marker.args else "uncategorized"
    RESULTS.append({
        "nodeid": report.nodeid,
        "name": item.name,
        "area": area,
        "outcome": report.outcome,
        "duration_ms": round(report.duration * 1000, 3),
        "error": str(report.longrepr)[:3000] if report.failed else "",
    })


def pytest_sessionfinish(session, exitstatus):
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    payload = {
        "generated_at": time.strftime("%Y-%m-%d %H:%M:%S %z"),
        "exit_status": exitstatus,
        "tests": RESULTS,
        "http_observations": HTTP_OBSERVATIONS,
        "summary": dict(Counter(item["outcome"] for item in RESULTS)),
    }
    (REPORT_DIR / "raw_results.json").write_text(
        json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8"
    )
