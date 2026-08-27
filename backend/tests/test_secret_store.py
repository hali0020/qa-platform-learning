from __future__ import annotations

import asyncio
import gzip
import socket
from pathlib import Path
from types import SimpleNamespace

import httpx
import pytest

from app.secrets import (
    BUILTIN_SECRET_NAMES,
    EnvironmentSecretStore,
    SecretNotFoundError,
    SecretStoreConfigurationError,
    SecretStoreResponseError,
    SecretStoreUnavailableError,
    VAULT_APP_TOKEN_FILE,
    VaultSecretStore,
    build_secret_store,
    read_vault_app_token_file,
    validate_secret_store_runtime_target,
)
from app.core.config import (
    Settings,
    VAULT_LOCAL_APP_TOKEN_FILE,
    VAULT_LOCAL_ENDPOINT,
    VAULT_LOCAL_KV_MOUNT,
)
from app.database import Database
from app.runtime.schemas import ProviderConnectionCreate
from app.runtime.service import create_runtime_service


TOKEN = "hvs.test-only-app-token"
PROVIDER_NAME = "QA_PROVIDER_SECRET_GITLAB_LESSON_TOKEN"


def vault_store(
    handler: httpx.AsyncBaseTransport | httpx.MockTransport,
    *,
    max_attempts: int = 3,
    timeout: float = 0.2,
) -> VaultSecretStore:
    return VaultSecretStore(
        app_env="local-container",
        endpoint_url="http://vault:8200",
        kv_mount="qa-platform",
        token=TOKEN,
        max_concurrency=2,
        operation_timeout_seconds=timeout,
        max_attempts=max_attempts,
        allowed_names=(PROVIDER_NAME,),
        transport=handler,
    )


@pytest.mark.asyncio
async def test_environment_store_reads_only_explicit_names_without_network_or_writes(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("env_local must not use DNS or sockets")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    values = {
        "DATABASE_URL": "sqlite+aiosqlite:///./.data/qa.db",
        PROVIDER_NAME: "provider-test-secret",
        "UNRELATED_SECRET": "must-not-be-readable",
    }
    before = dict(values)
    store = EnvironmentSecretStore(values, allowed_names=(PROVIDER_NAME,))

    assert await store.read("DATABASE_URL") == values["DATABASE_URL"]
    assert await store.read(PROVIDER_NAME) == "provider-test-secret"
    assert values == before
    assert "provider-test-secret" not in repr(store)
    assert not hasattr(store, "list")
    assert not hasattr(store, "write")

    with pytest.raises(SecretStoreConfigurationError):
        await store.read("UNRELATED_SECRET")
    with pytest.raises(SecretStoreConfigurationError):
        await store.read("QA_PROVIDER_SECRET_NOT_IN_CALLER_ALLOWLIST")

    await store.aclose()
    with pytest.raises(SecretNotFoundError):
        await store.read("DATABASE_URL")


def test_both_adapters_reject_invalid_provider_allowlists() -> None:
    with pytest.raises(SecretStoreConfigurationError):
        EnvironmentSecretStore({}, allowed_names=("provider_secret",))

    with pytest.raises(SecretStoreConfigurationError):
        VaultSecretStore(
            app_env="local-container",
            endpoint_url="http://vault:8200",
            kv_mount="qa-platform",
            token=TOKEN,
            allowed_names=("QA_PROVIDER_SECRET_lowercase",),
            transport=httpx.MockTransport(lambda _request: httpx.Response(500)),
        )


@pytest.mark.asyncio
async def test_factory_keeps_environment_default_socket_free() -> None:
    settings = SimpleNamespace(
        secret_store_runtime_mode="env_local",
        provider_secret_env_names=(PROVIDER_NAME,),
    )

    def forbidden_token_read() -> str:
        raise AssertionError("env_local must not read the Vault token file")

    store = build_secret_store(
        settings,
        environ={PROVIDER_NAME: "provider-test-secret"},
        token_reader=forbidden_token_read,
    )
    try:
        assert await store.read(PROVIDER_NAME) == "provider-test-secret"
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_factory_constructs_only_the_exact_local_vault_adapter() -> None:
    settings = SimpleNamespace(
        secret_store_runtime_mode="vault_local_container",
        provider_secret_env_names=(PROVIDER_NAME,),
        app_env="local-container",
        vault_endpoint_url="http://vault:8200",
        vault_kv_mount="qa-platform",
        vault_max_concurrency=2,
        vault_operation_timeout_seconds=0.2,
        vault_max_attempts=1,
    )
    token_reads = 0

    def token_reader() -> str:
        nonlocal token_reads
        token_reads += 1
        return TOKEN

    store = build_secret_store(settings, token_reader=token_reader)
    try:
        assert isinstance(store, VaultSecretStore)
        assert token_reads == 1
    finally:
        await store.aclose()

    settings.vault_endpoint_url = "https://vault.example.test"
    with pytest.raises(SecretStoreConfigurationError):
        build_secret_store(settings, token_reader=lambda: TOKEN)


def test_runtime_topology_is_exact_and_env_mode_rejects_dormant_vault() -> None:
    validate_secret_store_runtime_target(
        runtime_mode="env_local",
        app_env="local",
        endpoint_url="",
        kv_mount="",
        token="",
    )
    invalid = (
        {"runtime_mode": "company_vault"},
        {"runtime_mode": "env_local", "endpoint_url": "http://vault:8200"},
        {"runtime_mode": "vault_local_container", "app_env": "local"},
        {
            "runtime_mode": "vault_local_container",
            "endpoint_url": "https://vault.example.test",
        },
        {"runtime_mode": "vault_local_container", "kv_mount": "company"},
        {"runtime_mode": "vault_local_container", "token": "token with spaces"},
        {"runtime_mode": "vault_local_container", "max_concurrency": 17},
        {
            "runtime_mode": "vault_local_container",
            "operation_timeout_seconds": 60,
        },
        {"runtime_mode": "vault_local_container", "max_attempts": 4},
    )
    defaults: dict[str, object] = {
        "runtime_mode": "vault_local_container",
        "app_env": "local-container",
        "endpoint_url": "http://vault:8200",
        "kv_mount": "qa-platform",
        "token": TOKEN,
        "max_concurrency": 4,
        "operation_timeout_seconds": 3,
        "max_attempts": 3,
    }
    for override in invalid:
        arguments = defaults | override
        with pytest.raises(SecretStoreConfigurationError) as captured:
            validate_secret_store_runtime_target(**arguments)  # type: ignore[arg-type]
        assert TOKEN not in repr(captured.value)
        assert "vault.example.test" not in repr(captured.value)


def test_application_settings_select_only_the_local_vault_topology() -> None:
    Settings().validate_local_safety()
    with pytest.raises(RuntimeError, match="env_local"):
        Settings(vault_endpoint_url=VAULT_LOCAL_ENDPOINT).validate_local_safety()

    local_vault = Settings(
        app_env="local-container",
        secret_store_runtime_mode="vault_local_container",
        vault_endpoint_url=VAULT_LOCAL_ENDPOINT,
        vault_kv_mount=VAULT_LOCAL_KV_MOUNT,
        vault_app_token_file=VAULT_LOCAL_APP_TOKEN_FILE,
    )
    local_vault.validate_local_safety()

    with pytest.raises(RuntimeError, match="自建 Vault"):
        Settings(
            app_env="local-container",
            secret_store_runtime_mode="vault_local_container",
            vault_endpoint_url="https://vault.example.test",
            vault_kv_mount=VAULT_LOCAL_KV_MOUNT,
            vault_app_token_file=VAULT_LOCAL_APP_TOKEN_FILE,
        ).validate_local_safety()


@pytest.mark.asyncio
async def test_vault_uses_two_fixed_documents_and_field_allowlist_without_dns(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def forbidden_network(*_args: object, **_kwargs: object) -> None:
        raise AssertionError("Vault fake tests must not use DNS or sockets")

    monkeypatch.setattr(socket, "getaddrinfo", forbidden_network)
    monkeypatch.setattr(socket, "create_connection", forbidden_network)
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        assert request.method == "GET"
        assert request.headers["X-Vault-Token"] == TOKEN
        assert request.headers["Accept"] == "application/json"
        assert request.headers["Accept-Encoding"] == "identity"
        if request.url.path.endswith("/runtime"):
            data = {
                "DATABASE_URL": "postgresql+asyncpg://qa:local@postgres:5432/qa",
                "OBJECT_STORAGE_SECRET_KEY": "storage-secret",
            }
        else:
            data = {PROVIDER_NAME: "provider-secret"}
        return httpx.Response(200, json={"data": {"data": data}})

    store = vault_store(httpx.MockTransport(handler))
    try:
        assert await store.read("DATABASE_URL") == (
            "postgresql+asyncpg://qa:local@postgres:5432/qa"
        )
        assert await store.read(PROVIDER_NAME) == "provider-secret"
        with pytest.raises(SecretStoreConfigurationError):
            await store.read("QA_PROVIDER_SECRET_NOT_IN_CALLER_ALLOWLIST")
        assert paths == [
            "/v1/qa-platform/data/runtime",
            "/v1/qa-platform/data/providers",
        ]
        assert all(PROVIDER_NAME not in path for path in paths)
        assert TOKEN not in repr(store)
        assert not hasattr(store, "list")
        assert not hasattr(store, "write")
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_vault_retries_only_bounded_transient_failures() -> None:
    attempts = 0

    async def handler(_request: httpx.Request) -> httpx.Response:
        nonlocal attempts
        attempts += 1
        if attempts < 3:
            return httpx.Response(503, text=f"remote leaked {TOKEN}")
        return httpx.Response(
            200,
            json={"data": {"data": {"DATABASE_URL": "local-database-url"}}},
        )

    store = vault_store(httpx.MockTransport(handler))
    try:
        assert await store.read("DATABASE_URL") == "local-database-url"
        assert attempts == 3
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_vault_never_follows_redirects_or_retries_non_transient_status() -> None:
    paths: list[str] = []

    async def handler(request: httpx.Request) -> httpx.Response:
        paths.append(request.url.path)
        return httpx.Response(
            302,
            headers={"Location": "https://vault.example.test/v1/secrets"},
        )

    store = vault_store(httpx.MockTransport(handler))
    try:
        with pytest.raises(SecretStoreResponseError) as captured:
            await store.read("DATABASE_URL")
        assert paths == ["/v1/qa-platform/data/runtime"]
        assert "vault.example.test" not in repr(captured.value)
    finally:
        await store.aclose()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("response", "error_type"),
    [
        (httpx.Response(404, text="missing secret-value"), SecretNotFoundError),
        (httpx.Response(403, text="token rejected"), SecretStoreUnavailableError),
        (httpx.Response(200, text="not-json"), SecretStoreResponseError),
        (
            httpx.Response(200, json={"data": {"data": {"OTHER": "secret"}}}),
            SecretStoreResponseError,
        ),
        (httpx.Response(200, content=b"x" * 16_385), SecretStoreResponseError),
    ],
)
async def test_vault_response_errors_are_generic_and_body_is_bounded(
    response: httpx.Response,
    error_type: type[Exception],
) -> None:
    store = vault_store(
        httpx.MockTransport(lambda _request: response),
        max_attempts=1,
    )
    try:
        with pytest.raises(error_type) as captured:
            await store.read("DATABASE_URL")
        rendered = repr(captured.value)
        assert TOKEN not in rendered
        assert "secret-value" not in rendered
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
    finally:
        await store.aclose()


@pytest.mark.asyncio
async def test_vault_rejects_compressed_bodies_before_decoding() -> None:
    expanded = (
        b'{"data":{"data":{"DATABASE_URL":"'
        + (b"x" * (8 * 1024 * 1024))
        + b'"}}}'
    )
    compressed = gzip.compress(expanded)
    assert len(compressed) < 16 * 1024
    store = vault_store(
        httpx.MockTransport(
            lambda _request: httpx.Response(
                200,
                content=compressed,
                headers={"Content-Encoding": "gzip"},
            )
        ),
        max_attempts=1,
    )
    try:
        with pytest.raises(SecretStoreResponseError):
            await store.read("DATABASE_URL")
    finally:
        await store.aclose()
    assert len(expanded) > 8 * 1024 * 1024


@pytest.mark.asyncio
async def test_transport_exception_and_timeout_drop_the_secret_exception_chain() -> None:
    async def leaking_handler(_request: httpx.Request) -> httpx.Response:
        raise RuntimeError(f"request failed with {TOKEN} and database-password")

    store = vault_store(httpx.MockTransport(leaking_handler), max_attempts=1)
    try:
        with pytest.raises(SecretStoreUnavailableError) as captured:
            await store.read("DATABASE_URL")
        assert TOKEN not in repr(captured.value)
        assert "database-password" not in repr(captured.value)
        assert captured.value.__cause__ is None
        assert captured.value.__context__ is None
    finally:
        await store.aclose()

    started = asyncio.Event()

    async def blocking_handler(_request: httpx.Request) -> httpx.Response:
        started.set()
        await asyncio.Event().wait()
        raise AssertionError("unreachable")

    timed = vault_store(
        httpx.MockTransport(blocking_handler),
        max_attempts=1,
        timeout=0.1,
    )
    try:
        with pytest.raises(SecretStoreUnavailableError):
            await timed.read("DATABASE_URL")
        assert started.is_set()
    finally:
        await timed.aclose()


class BlockingBody(httpx.AsyncByteStream):
    def __init__(self) -> None:
        self.started = asyncio.Event()
        self.closed = asyncio.Event()

    async def __aiter__(self):
        self.started.set()
        await asyncio.Event().wait()
        yield b"unreachable"

    async def aclose(self) -> None:
        self.closed.set()


@pytest.mark.asyncio
async def test_cancelled_read_closes_stream_and_releases_concurrency_permit() -> None:
    stream = BlockingBody()

    async def handler(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, stream=stream)

    store = vault_store(httpx.MockTransport(handler), timeout=1)
    operation = asyncio.create_task(store.read("DATABASE_URL"))
    await asyncio.wait_for(stream.started.wait(), timeout=1)
    operation.cancel()
    with pytest.raises(asyncio.CancelledError):
        await operation

    assert stream.closed.is_set()
    assert store._semaphore._value == 2
    await store.aclose()


class BlockingCloseTransport(httpx.AsyncBaseTransport):
    def __init__(self) -> None:
        self.close_started = asyncio.Event()
        self.release_close = asyncio.Event()
        self.closed = False

    async def handle_async_request(self, _request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"data": {"data": {"DATABASE_URL": "local"}}},
        )

    async def aclose(self) -> None:
        self.close_started.set()
        await self.release_close.wait()
        self.closed = True


@pytest.mark.asyncio
async def test_cancelled_close_finishes_bounded_client_cleanup() -> None:
    transport = BlockingCloseTransport()
    store = vault_store(transport, timeout=1)
    closing = asyncio.create_task(store.aclose())
    await asyncio.wait_for(transport.close_started.wait(), timeout=1)

    closing.cancel()
    transport.release_close.set()
    with pytest.raises(asyncio.CancelledError):
        await closing

    assert transport.closed is True
    await store.aclose()


def test_fixed_token_file_reader_returns_only_a_valid_dedicated_token(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    token_file = tmp_path / "vault_app_token"
    token_file.write_text(f"{TOKEN}\n", encoding="utf-8")
    monkeypatch.setattr("app.secrets.vault.VAULT_APP_TOKEN_FILE", str(token_file))

    assert read_vault_app_token_file() == TOKEN
    assert VAULT_APP_TOKEN_FILE == "/run/secrets/vault_app_token"

    token_file.write_text(" leading-space-token\n", encoding="utf-8")
    with pytest.raises(SecretStoreConfigurationError) as captured:
        read_vault_app_token_file()
    assert "leading-space-token" not in repr(captured.value)


def test_builtin_secret_names_are_deliberately_small() -> None:
    assert BUILTIN_SECRET_NAMES == {
        "DATABASE_URL",
        "BROKER_URL",
        "OBJECT_STORAGE_ACCESS_KEY",
        "OBJECT_STORAGE_SECRET_KEY",
        "OIDC_CLIENT_SECRET",
    }


@pytest.mark.asyncio
async def test_runtime_reads_provider_credentials_through_injected_store(
    tmp_path: Path,
) -> None:
    class RecordingSecretStore:
        runtime_mode = "vault_local_container"

        def __init__(self) -> None:
            self.reads: list[str] = []
            self.closed = False

        async def read(self, name: str) -> str:
            self.reads.append(name)
            return "local-vault-provider-token"

        async def aclose(self) -> None:
            self.closed = True

    settings = SimpleNamespace(
        app_env="local-container",
        provider_runtime_mode="self_hosted_lab",
        provider_self_hosted_ownership_acknowledged=True,
        provider_allowed_hosts=("ci.lab.test",),
        provider_allowed_ports=(443,),
        provider_allowed_networks=("10.20.30.40/32",),
        provider_allow_loopback_http=False,
        provider_secret_env_names=(PROVIDER_NAME,),
    )
    database = Database(
        f"sqlite+aiosqlite:///{(tmp_path / 'vault-runtime.db').as_posix()}"
    )
    store = RecordingSecretStore()
    service = create_runtime_service(
        database,
        settings,
        environ={},
        secret_store=store,
    )
    connection = await service.create_connection(
        ProviderConnectionCreate(
            name="Self-owned GitLab lesson",
            kind="gitlab",
            base_url="https://ci.lab.test",
            definition_ref="learning/qa",
            config={"project_id": "learning/qa"},
            secret_env_var=PROVIDER_NAME,
            enabled=True,
        )
    )
    try:
        result = await service.test_connection(connection.id)
        assert result.ready is True
        assert store.reads == [PROVIDER_NAME]
    finally:
        await service.shutdown()
        await database.shutdown()
    assert store.closed is True
