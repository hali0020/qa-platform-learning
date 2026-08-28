from pathlib import Path

import pytest

import app.core.config as config_module
from app.core.config import (
    CI_LAB_PROVIDER_SECRET_NAME,
    Settings,
    _is_local_postgres_container_url,
    _is_local_sqlite_url,
    _to_bool,
)


def test_dotenv_loader_uses_only_the_repository_root_file(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls: list[tuple[Path, bool]] = []

    def fake_load_dotenv(*, dotenv_path: Path, override: bool) -> bool:
        calls.append((dotenv_path, override))
        return True

    monkeypatch.delenv(config_module.SKIP_LOCAL_ENV_VARIABLE, raising=False)
    monkeypatch.setattr(config_module, "load_dotenv", fake_load_dotenv)

    assert config_module._load_local_environment()
    assert calls == [(config_module.PROJECT_ROOT / ".env", False)]
    assert config_module.LOCAL_ENV_FILE == config_module.PROJECT_ROOT / ".env"


def test_dotenv_loader_can_be_disabled_for_isolated_tests(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv(config_module.SKIP_LOCAL_ENV_VARIABLE, "1")
    monkeypatch.setattr(
        config_module,
        "load_dotenv",
        lambda **_kwargs: pytest.fail("disabled loader must not read .env"),
    )

    assert not config_module._load_local_environment()


def test_ci_lab_local_accepts_only_the_fixed_secret_reference() -> None:
    Settings(
        provider_runtime_mode="ci_lab_local",
        provider_secret_env_names=(CI_LAB_PROVIDER_SECRET_NAME,),
    ).validate_local_safety()


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("provider_allowed_hosts", ("127.0.0.1",)),
        ("provider_allowed_networks", ("127.0.0.1/32",)),
        ("provider_allowed_ports", (23020,)),
        ("provider_allow_loopback_http", True),
        ("provider_self_hosted_ownership_acknowledged", True),
        ("provider_secret_env_names", ("QA_PROVIDER_SECRET_OTHER",)),
        ("app_env", "production"),
    ],
)
def test_ci_lab_local_rejects_every_freely_configurable_egress_field(
    field: str,
    value: object,
) -> None:
    values: dict[str, object] = {
        "provider_runtime_mode": "ci_lab_local",
        "provider_secret_env_names": (CI_LAB_PROVIDER_SECRET_NAME,),
        field: value,
    }

    with pytest.raises(RuntimeError, match="ci_lab_local|Learning|CI_LAB"):
        Settings(**values).validate_local_safety()


def test_local_only_accepts_loopback() -> None:
    Settings(host="127.0.0.1", local_only=True).validate_local_safety()


def test_local_only_rejects_external_binding() -> None:
    with pytest.raises(RuntimeError):
        Settings(host="0.0.0.0", local_only=True).validate_local_safety()


def test_local_only_rejects_external_cors_origin() -> None:
    with pytest.raises(RuntimeError):
        Settings(
            local_only=True,
            cors_origins=("https://forbidden-origin.invalid",),
        ).validate_local_safety()


def test_invalid_safety_boolean_is_rejected() -> None:
    with pytest.raises(ValueError):
        _to_bool("LOCAL_ONLY", "treu", default=True)


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://forbidden-db.invalid/qa",
        "mysql+aiomysql://forbidden-db.invalid/qa",
        "sqlite+aiosqlite://///untrusted-share/shared/qa.db",
        "sqlite+aiosqlite:///file:qa.db?mode=rw",
    ],
)
def test_local_only_rejects_external_or_ambiguous_database_url(
    database_url: str,
) -> None:
    with pytest.raises(RuntimeError):
        Settings(local_only=True, database_url=database_url).validate_local_safety()


@pytest.mark.parametrize(
    "database_url",
    [
        "sqlite+aiosqlite:///./.data/qa.db",
        "sqlite+aiosqlite:///:memory:",
        "sqlite+aiosqlite:///D:/temp/qa.db",
    ],
)
def test_local_sqlite_database_urls_are_accepted(database_url: str) -> None:
    assert _is_local_sqlite_url(database_url)
    Settings(local_only=True, database_url=database_url).validate_local_safety()


def test_database_path_resolves_relative_sqlite_file() -> None:
    settings = Settings(database_url="sqlite+aiosqlite:///./.data/qa.db")

    assert settings.database_path.is_absolute()
    assert settings.database_path.name == "qa.db"
    assert settings.database_path.parent.name == ".data"


def test_database_path_rejects_memory_database() -> None:
    settings = Settings(database_url="sqlite+aiosqlite:///:memory:")

    with pytest.raises(RuntimeError, match="内存 SQLite"):
        _ = settings.database_path


def test_external_database_is_rejected_even_if_http_local_only_is_disabled() -> None:
    with pytest.raises(RuntimeError, match="sqlite_local"):
        Settings(
            local_only=False,
            database_url="postgresql+asyncpg://forbidden-db.invalid/qa",
        ).validate_local_safety()


def test_postgres_local_container_mode_accepts_only_internal_service() -> None:
    database_url = "postgresql+asyncpg://qa:secret@postgres:5432/qa"

    assert _is_local_postgres_container_url(database_url)
    Settings(
        app_env="local-container",
        database_runtime_mode="postgres_local_container",
        database_url=database_url,
    ).validate_local_safety()


@pytest.mark.parametrize(
    "database_url",
    [
        "postgresql+asyncpg://qa:secret@forbidden-db.invalid:5432/qa",
        "postgresql+asyncpg://qa:secret@127.0.0.1:5432/qa",
        "postgresql+asyncpg://qa:secret@postgres:6432/qa",
        "postgresql+asyncpg://qa:secret@postgres/qa",
        "postgresql+asyncpg://qa:secret@postgres:not-a-port/qa",
        "postgresql://qa:secret@postgres:5432/qa",
        "postgresql+asyncpg://qa@postgres:5432/qa",
        "postgresql+asyncpg://qa:secret@postgres:5432/qa?host=evil.example",
    ],
)
def test_postgres_local_container_rejects_other_topologies(
    database_url: str,
) -> None:
    assert not _is_local_postgres_container_url(database_url)
    with pytest.raises(RuntimeError, match="自建内网服务 postgres"):
        Settings(
            app_env="local-container",
            database_runtime_mode="postgres_local_container",
            database_url=database_url,
        ).validate_local_safety()


def test_postgres_mode_requires_explicit_local_container_environment() -> None:
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        Settings(
            app_env="local",
            database_runtime_mode="postgres_local_container",
            database_url=(
                "postgresql+asyncpg://qa:secret@postgres:5432/qa"
            ),
        ).validate_local_safety()


def test_sqlite_url_cannot_be_used_under_postgres_mode() -> None:
    with pytest.raises(RuntimeError, match=r"postgresql\+asyncpg"):
        Settings(
            app_env="local-container",
            database_runtime_mode="postgres_local_container",
            database_url="sqlite+aiosqlite:///./.data/qa.db",
        ).validate_local_safety()


def test_database_runtime_mode_is_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "local-container")
    monkeypatch.setenv("DATABASE_RUNTIME_MODE", "postgres_local_container")
    monkeypatch.setenv(
        "DATABASE_URL",
        "postgresql+asyncpg://qa:secret@postgres:5432/qa",
    )

    settings = Settings.from_environment()

    assert settings.database_runtime_mode == "postgres_local_container"
    settings.validate_local_safety()


def test_postgres_mode_keeps_uploads_in_project_local_data_directory(
    tmp_path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        app_env="local-container",
        database_runtime_mode="postgres_local_container",
        database_url="postgresql+asyncpg://qa:secret@postgres:5432/qa",
    )

    assert settings.upload_root_path == (tmp_path / ".data" / "uploads").resolve()

    container_data = tmp_path / "container-data"
    mounted = Settings(
        app_env="local-container",
        database_runtime_mode="postgres_local_container",
        database_url="postgresql+asyncpg://qa:secret@postgres:5432/qa",
        local_data_root=str(container_data),
        upload_root=str(container_data / "uploads"),
    )
    mounted.validate_local_safety()
    assert mounted.upload_root_path == (container_data / "uploads").resolve()

    with pytest.raises(RuntimeError, match="本机数据目录"):
        Settings(
            app_env="local-container",
            database_runtime_mode="postgres_local_container",
            database_url="postgresql+asyncpg://qa:secret@postgres:5432/qa",
            upload_root=str(tmp_path / "outside" / "uploads"),
        ).validate_local_safety()

    with pytest.raises(RuntimeError, match="LOCAL_DATA_ROOT"):
        Settings(
            app_env="local-container",
            database_runtime_mode="postgres_local_container",
            database_url="postgresql+asyncpg://qa:secret@postgres:5432/qa",
            local_data_root=r"\\untrusted-share\qa-data",
        ).validate_local_safety()


def test_self_hosted_provider_mode_requires_exact_host_allowlist() -> None:
    with pytest.raises(RuntimeError, match="主机白名单"):
        Settings(
            provider_runtime_mode="self_hosted_lab",
            provider_self_hosted_ownership_acknowledged=True,
            provider_allowed_networks=("10.20.30.40/32",),
            provider_secret_env_names=("QA_PROVIDER_SECRET_LAB_TOKEN",),
        ).validate_local_safety()


def test_external_provider_requires_dedicated_secret_allowlist() -> None:
    with pytest.raises(RuntimeError, match="Secret 环境变量白名单"):
        Settings(
            provider_runtime_mode="self_hosted_lab",
            provider_self_hosted_ownership_acknowledged=True,
            provider_allowed_hosts=("ci.example.test",),
            provider_allowed_networks=("10.20.30.40/32",),
        ).validate_local_safety()

    with pytest.raises(RuntimeError, match="QA_PROVIDER_SECRET_"):
        Settings(provider_secret_env_names=("PATH",)).validate_local_safety()


@pytest.mark.parametrize(
    "hosts,networks",
    [
        (("*.lab.test",), ()),
        (("https://owned-ci-lab.test",), ()),
        (("owned-ci-lab.test/path",), ()),
        (("owned-ci-lab.test",), ("10.0.0.1/24",)),
    ],
)
def test_provider_allowlists_reject_wildcards_urls_and_noncanonical_cidr(
    hosts: tuple[str, ...],
    networks: tuple[str, ...],
) -> None:
    with pytest.raises(RuntimeError):
        Settings(
            provider_allowed_hosts=hosts,
            provider_allowed_networks=networks,
        ).validate_local_safety()


def test_insecure_cookie_exception_is_only_for_loopback_container_demo() -> None:
    Settings(
        app_env="local-container",
        local_only=False,
        host="0.0.0.0",
        session_cookie_secure=False,
    ).validate_local_safety()

    with pytest.raises(RuntimeError, match="安全 Cookie"):
        Settings(
            app_env="production",
            local_only=False,
            host="0.0.0.0",
            session_cookie_secure=False,
        ).validate_local_safety()
