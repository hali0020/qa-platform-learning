from dataclasses import replace

import pytest

from app.container import build_container
from app.core.config import (
    OIDC_LOCAL_BROWSER_AUTHORIZATION_ENDPOINT,
    OIDC_LOCAL_CLIENT_ID,
    OIDC_LOCAL_ISSUER,
    OIDC_LOCAL_JWKS_ENDPOINT,
    OIDC_LOCAL_POST_LOGIN_REDIRECT_URI,
    OIDC_LOCAL_REDIRECT_URI,
    OIDC_LOCAL_TOKEN_ENDPOINT,
    Settings,
)
from app.database.session import Database


def keycloak_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "local-container",
        "auth_runtime_mode": "keycloak_local_container",
        "oidc_issuer": OIDC_LOCAL_ISSUER,
        "oidc_browser_authorization_endpoint": (
            OIDC_LOCAL_BROWSER_AUTHORIZATION_ENDPOINT
        ),
        "oidc_token_endpoint": OIDC_LOCAL_TOKEN_ENDPOINT,
        "oidc_jwks_endpoint": OIDC_LOCAL_JWKS_ENDPOINT,
        "oidc_client_id": OIDC_LOCAL_CLIENT_ID,
        "oidc_redirect_uri": OIDC_LOCAL_REDIRECT_URI,
        "oidc_post_login_redirect_uri": (
            OIDC_LOCAL_POST_LOGIN_REDIRECT_URI
        ),
    }
    values.update(overrides)
    return Settings(**values)


@pytest.mark.asyncio
async def test_default_local_accounts_keep_oidc_dormant(
    tmp_path,
) -> None:
    settings = Settings(
        app_env="test",
        database_url=(
            f"sqlite+aiosqlite:///{(tmp_path / 'local.db').as_posix()}"
        ),
    )
    settings.validate_local_safety()
    database = Database(settings.database_url)
    container = build_container(database, settings)

    assert settings.auth_runtime_mode == "local_accounts"
    assert container.oidc is None
    await container.shutdown()


def test_local_accounts_reject_dormant_oidc_targets() -> None:
    settings = Settings(oidc_issuer="https://identity.example.test/realm")

    with pytest.raises(RuntimeError, match="禁止配置 OIDC"):
        settings.validate_local_safety()


@pytest.mark.parametrize(
    ("field_name", "unsafe_value"),
    [
        ("oidc_issuer", "https://identity.example.test/realm"),
        (
            "oidc_browser_authorization_endpoint",
            "https://identity.example.test/authorize",
        ),
        ("oidc_token_endpoint", "https://identity.example.test/token"),
        ("oidc_jwks_endpoint", "https://identity.example.test/jwks"),
        ("oidc_client_id", "company-client"),
        (
            "oidc_redirect_uri",
            "https://attacker.example/callback",
        ),
        (
            "oidc_post_login_redirect_uri",
            "https://attacker.example/",
        ),
    ],
)
def test_keycloak_mode_rejects_every_arbitrary_target(
    field_name: str,
    unsafe_value: str,
) -> None:
    settings = replace(
        keycloak_settings(),
        **{field_name: unsafe_value},
    )

    with pytest.raises(RuntimeError, match="只能使用自建 Keycloak"):
        settings.validate_local_safety()


def test_keycloak_mode_requires_local_container_and_bounded_timeouts() -> None:
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        replace(keycloak_settings(), app_env="test").validate_local_safety()
    with pytest.raises(RuntimeError, match="OIDC 操作超时"):
        replace(
            keycloak_settings(),
            oidc_operation_timeout_seconds=30,
        ).validate_local_safety()


def test_keycloak_exact_topology_is_accepted() -> None:
    settings = keycloak_settings()
    settings.validate_local_safety()

    assert settings.oidc_issuer.startswith("http://127.0.0.1:23010/")
    assert settings.oidc_token_endpoint.startswith("http://keycloak:8080/")
    assert settings.oidc_jwks_endpoint.startswith("http://keycloak:8080/")
