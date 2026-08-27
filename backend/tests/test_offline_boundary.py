"""Regression tests for the provider lab's default-deny network boundary.

These tests deliberately use fake resolvers and ``httpx.MockTransport``.  A
passing test suite must never need DNS, a socket, or somebody else's CI system.
"""

from __future__ import annotations

import socket
from datetime import datetime, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import NoReturn

import httpx
import pytest

from app.core.config import Settings
from app.core.errors import AuthorizationError
from app.database.session import Database
from app.pipeline.providers import (
    OutboundPolicy,
    ProviderSecurityError,
    SafeHttpClient,
)
from app.pipeline.providers.security import (
    validate_base_url,
    validate_resolved_addresses,
)
from app.runtime.orm import ProviderRunRecord
from app.runtime.schemas import ProviderConnectionCreate, ProviderTriggerPayload
from app.runtime.service import create_runtime_service


def _sqlite_url(path: Path) -> str:
    return f"sqlite+aiosqlite:///{path.as_posix()}"


def _self_hosted_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "provider_runtime_mode": "self_hosted_lab",
        "provider_self_hosted_ownership_acknowledged": True,
        "provider_allowed_hosts": ("127.0.0.1",),
        "provider_allowed_ports": (443,),
        "provider_allowed_networks": ("127.0.0.1/32",),
        "provider_secret_env_names": ("QA_PROVIDER_SECRET_LAB_TOKEN",),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.parametrize("mode", ["external", "public", "production"])
def test_only_local_and_self_hosted_lab_modes_exist(mode: str) -> None:
    """There is intentionally no escape hatch for arbitrary external CI."""

    with pytest.raises(RuntimeError):
        Settings(provider_runtime_mode=mode).validate_local_safety()


def test_retired_external_switch_cannot_reenable_network(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("EXTERNAL_PROVIDERS_ENABLED", "true")

    with pytest.raises(ValueError, match="停用|local_lab|self_hosted_lab"):
        Settings.from_environment()


def test_complete_self_hosted_lab_configuration_passes_static_validation() -> None:
    _self_hosted_settings().validate_local_safety()


@pytest.mark.parametrize(
    ("app_env", "host", "network"),
    [
        ("local-container", "ci.lab.test", "10.20.30.0/24"),
        ("local-container", "10.20.30.40", "10.20.30.40/32"),
        ("local", "127.0.0.1", "127.0.0.1/32"),
        ("local-container", "fd00:1234:5678::1", "fd00:1234:5678::/64"),
        ("local", "::1", "::1/128"),
    ],
)
def test_narrow_private_and_loopback_networks_pass_static_validation(
    app_env: str,
    host: str,
    network: str,
) -> None:
    _self_hosted_settings(
        app_env=app_env,
        provider_allowed_hosts=(host,),
        provider_allowed_networks=(network,),
    ).validate_local_safety()


@pytest.mark.parametrize(
    ("host", "network"),
    [
        ("ci.lab.test", "10.20.30.40/32"),
        ("10.20.30.40", "10.20.30.40/32"),
        ("fd00:1234:5678::1", "fd00:1234:5678::/64"),
    ],
)
@pytest.mark.parametrize("app_env", ["local", "test"])
def test_non_container_lab_rejects_private_network_topology(
    app_env: str,
    host: str,
    network: str,
) -> None:
    with pytest.raises(RuntimeError, match="local-container|环回|loopback"):
        _self_hosted_settings(
            app_env=app_env,
            provider_allowed_hosts=(host,),
            provider_allowed_networks=(network,),
        ).validate_local_safety()


def test_self_hosted_lab_requires_explicit_ownership_acknowledgement() -> None:
    with pytest.raises(RuntimeError, match="ownership|归属|自建"):
        _self_hosted_settings(
            provider_self_hosted_ownership_acknowledged=False
        ).validate_local_safety()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_allowed_hosts", ()),
        ("provider_allowed_ports", ()),
        ("provider_allowed_networks", ()),
        ("provider_secret_env_names", ()),
        ("provider_allowed_hosts", ("*.lab.test",)),
        ("provider_allowed_hosts", ("93.184.216.34",)),
    ],
)
def test_self_hosted_lab_requires_narrow_complete_allowlists(
    field: str,
    value: tuple[object, ...],
) -> None:
    settings = _self_hosted_settings(**{field: value})

    with pytest.raises(RuntimeError):
        settings.validate_local_safety()


@pytest.mark.parametrize(
    "network",
    [
        "0.0.0.0/0",
        "10.0.0.0/8",
        "172.16.0.0/12",
        "192.168.0.0/16",
        "93.184.216.0/24",
        "fc00::/7",
        "2001:4860:4860::/48",
    ],
)
def test_self_hosted_lab_rejects_public_or_broad_network_allowlists(
    network: str,
) -> None:
    with pytest.raises(RuntimeError):
        _self_hosted_settings(
            provider_allowed_networks=(network,)
        ).validate_local_safety()


@pytest.mark.asyncio
async def test_default_local_lab_rejects_non_local_provider_before_dns_or_http(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    dns_calls = 0
    http_calls = 0

    def forbidden_dns(*_args: object, **_kwargs: object) -> object:
        nonlocal dns_calls
        dns_calls += 1
        raise AssertionError("local_lab must not perform DNS")

    def forbidden_http(*_args: object, **_kwargs: object) -> object:
        nonlocal http_calls
        http_calls += 1
        raise AssertionError("local_lab must not perform HTTP")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_dns)
    monkeypatch.setattr(httpx.AsyncClient, "stream", forbidden_http)

    database = Database(_sqlite_url(tmp_path / "local-lab.db"))
    service = create_runtime_service(
        database,
        Settings(),
        environ={"QA_PROVIDER_SECRET_LAB_TOKEN": "test-only-value"},
    )
    local_connection = await service.create_connection(
        ProviderConnectionCreate(
            name="Offline local simulator",
            kind="local",
            definition_ref="offline-pipeline",
            enabled=True,
        )
    )
    local_run = await service.trigger_provider(
        local_connection.id,
        ProviderTriggerPayload(ref="main"),
    )
    assert local_run.status == "queued"
    connection = await service.create_connection(
        ProviderConnectionCreate(
            name="Blocked GitLab lesson",
            kind="gitlab",
            base_url="https://ci.lab.test",
            definition_ref="learning/qa",
            config={"project_id": "learning/qa"},
            secret_env_var="QA_PROVIDER_SECRET_LAB_TOKEN",
            enabled=True,
        )
    )
    now = datetime.now(timezone.utc)
    run_id = "blocked-provider-run"
    async with service.repository.transaction() as session:
        session.add(
            ProviderRunRecord(
                id=run_id,
                connection_id=connection.id,
                external_id="external-run-that-must-not-be-queried",
                status="running",
                raw_status="running",
                web_url=None,
                message=None,
                run_metadata={},
                correlation_id=None,
                request_fingerprint="0" * 64,
                created_at=now,
                updated_at=now,
            )
        )
        await session.flush()
    try:
        with pytest.raises(AuthorizationError):
            await service.test_connection(connection.id)
        with pytest.raises(AuthorizationError):
            await service.trigger_provider(
                connection.id,
                ProviderTriggerPayload(ref="main"),
            )
        with pytest.raises(AuthorizationError):
            await service.get_provider_run(connection.id, run_id)
        with pytest.raises(AuthorizationError):
            await service.cancel_provider_run(connection.id, run_id)
        assert dns_calls == 0
        assert http_calls == 0
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_runtime_rechecks_self_hosted_ownership_before_building_provider(
    tmp_path: Path,
) -> None:
    """Service safety cannot depend only on startup-time Settings validation."""

    builder_calls = 0

    def forbidden_builder(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("ownership must be checked before provider construction")

    unvalidated_settings = SimpleNamespace(
        provider_runtime_mode="self_hosted_lab",
        provider_self_hosted_ownership_acknowledged=False,
        provider_allowed_hosts=("ci.lab.test",),
        provider_allowed_ports=(443,),
        provider_allowed_networks=("10.20.30.40/32",),
        provider_allow_loopback_http=False,
        provider_secret_env_names=("QA_PROVIDER_SECRET_LAB_TOKEN",),
    )
    database = Database(_sqlite_url(tmp_path / "ownership-defense.db"))
    service = create_runtime_service(
        database,
        unvalidated_settings,
        environ={"QA_PROVIDER_SECRET_LAB_TOKEN": "test-only-value"},
        provider_builder=forbidden_builder,
    )
    connection = await service.create_connection(
        ProviderConnectionCreate(
            name="Unacknowledged self-hosted lab",
            kind="gitlab",
            base_url="https://ci.lab.test",
            definition_ref="learning/qa",
            config={"project_id": "learning/qa"},
            secret_env_var="QA_PROVIDER_SECRET_LAB_TOKEN",
            enabled=True,
        )
    )
    try:
        with pytest.raises(AuthorizationError, match="所有权|自建"):
            await service.test_connection(connection.id)
        with pytest.raises(AuthorizationError, match="所有权|自建"):
            await service.trigger_provider(
                connection.id,
                ProviderTriggerPayload(ref="main"),
            )
        assert builder_calls == 0
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_runtime_rechecks_host_topology_before_building_provider(
    tmp_path: Path,
) -> None:
    builder_calls = 0

    def forbidden_builder(*_args: object, **_kwargs: object) -> NoReturn:
        nonlocal builder_calls
        builder_calls += 1
        raise AssertionError("host topology must be checked before provider construction")

    unvalidated_settings = SimpleNamespace(
        app_env="local",
        provider_runtime_mode="self_hosted_lab",
        provider_self_hosted_ownership_acknowledged=True,
        provider_allowed_hosts=("ci.lab.test",),
        provider_allowed_ports=(443,),
        provider_allowed_networks=("10.20.30.40/32",),
        provider_allow_loopback_http=False,
        provider_secret_env_names=("QA_PROVIDER_SECRET_LAB_TOKEN",),
    )
    database = Database(_sqlite_url(tmp_path / "topology-defense.db"))
    service = create_runtime_service(
        database,
        unvalidated_settings,
        environ={"QA_PROVIDER_SECRET_LAB_TOKEN": "test-only-value"},
        provider_builder=forbidden_builder,
    )
    connection = await service.create_connection(
        ProviderConnectionCreate(
            name="Private target from host process",
            kind="gitlab",
            base_url="https://ci.lab.test",
            definition_ref="learning/qa",
            config={"project_id": "learning/qa"},
            secret_env_var="QA_PROVIDER_SECRET_LAB_TOKEN",
            enabled=True,
        )
    )
    try:
        with pytest.raises(AuthorizationError, match="local-container|环回"):
            await service.test_connection(connection.id)
        with pytest.raises(AuthorizationError, match="local-container|环回"):
            await service.trigger_provider(
                connection.id,
                ProviderTriggerPayload(ref="main"),
            )
        assert builder_calls == 0
    finally:
        await database.shutdown()


@pytest.mark.asyncio
async def test_allowlisted_hostname_still_rejects_a_public_dns_result() -> None:
    policy = OutboundPolicy(
        allowed_hosts=("ci.lab.test",),
        allowed_ports=(443,),
        allowed_networks=("10.20.30.40/32",),
    )

    async def public_resolver(_host: str, _port: int) -> tuple[str, ...]:
        return ("93.184.216.34",)

    with pytest.raises(ProviderSecurityError):
        await validate_resolved_addresses(
            "ci.lab.test",
            443,
            policy,
            resolver=public_resolver,
        )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "app_env",
        "base_url",
        "host",
        "port",
        "network",
        "address",
        "allow_http",
    ),
    [
        (
            "local-container",
            "https://ci.lab.test",
            "ci.lab.test",
            443,
            "10.20.30.40/32",
            "10.20.30.40",
            False,
        ),
        (
            "local",
            "http://127.0.0.1:23020",
            "127.0.0.1",
            23020,
            "127.0.0.1/32",
            "127.0.0.1",
            True,
        ),
    ],
)
async def test_explicit_self_hosted_addresses_pass_only_through_mock_transport(
    app_env: str,
    base_url: str,
    host: str,
    port: int,
    network: str,
    address: str,
    allow_http: bool,
) -> None:
    requests: list[str] = []

    _self_hosted_settings(
        app_env=app_env,
        provider_allowed_hosts=(host,),
        provider_allowed_ports=(port,),
        provider_allowed_networks=(network,),
        provider_allow_loopback_http=allow_http,
    ).validate_local_safety()

    async def resolver(resolved_host: str, resolved_port: int) -> tuple[str, ...]:
        assert (resolved_host, resolved_port) == (host, port)
        return (address,)

    async def handler(request: httpx.Request) -> httpx.Response:
        requests.append(str(request.url))
        return httpx.Response(200, json={"lab": "self-hosted"})

    policy = OutboundPolicy(
        allowed_hosts=(host,),
        allowed_ports=(port,),
        allowed_networks=(network,),
        allow_loopback_http=allow_http,
    )
    assert validate_base_url(base_url, policy) == base_url
    assert await validate_resolved_addresses(
        host,
        port,
        policy,
        resolver=resolver,
    ) == (address,)

    client = SafeHttpClient(
        base_url,
        policy,
        resolver=resolver,
        transport=httpx.MockTransport(handler),
    )
    try:
        response = await client.request("GET", "/health")
        assert response.status_code == 200
        assert response.json() == {"lab": "self-hosted"}
        assert requests == [f"{base_url}/health"]
    finally:
        await client.aclose()
