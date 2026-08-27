from __future__ import annotations

import asyncio
import base64
import hashlib
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import parse_qs, urlparse
from uuid import UUID

import httpx
import jwt
import pytest
from cryptography.hazmat.primitives.asymmetric import rsa
from httpx import ASGITransport, AsyncClient

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
from app.core.errors import AuthenticationError
from app.database.session import Database
from app.domain.identity import OidcLoginTransaction
from app.domain.models import utc_now
from app.main import create_app
from app.repositories.identity import IdentityRepository
from app.services.oidc import OidcService


ADMIN_PASSWORD = "LocalOidcAdmin!123"
VIEWER_PASSWORD = "LocalOidcViewer!123"
BOUND_SUBJECT = "b37cbb2a-2244-4d43-9859-f78d78f357f3"


def local_settings(database_path: Path, upload_root: Path) -> Settings:
    return Settings(
        app_env="test",
        database_url=(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        ),
        upload_root=str(upload_root),
        password_time_cost=1,
        password_memory_cost_kib=1024,
        password_parallelism=1,
    )


def keycloak_settings(database_path: Path, upload_root: Path) -> Settings:
    return Settings(
        app_env="local-container",
        database_url=(
            f"sqlite+aiosqlite:///{database_path.as_posix()}"
        ),
        upload_root=str(upload_root),
        auth_runtime_mode="keycloak_local_container",
        oidc_issuer=OIDC_LOCAL_ISSUER,
        oidc_browser_authorization_endpoint=(
            OIDC_LOCAL_BROWSER_AUTHORIZATION_ENDPOINT
        ),
        oidc_token_endpoint=OIDC_LOCAL_TOKEN_ENDPOINT,
        oidc_jwks_endpoint=OIDC_LOCAL_JWKS_ENDPOINT,
        oidc_client_id=OIDC_LOCAL_CLIENT_ID,
        oidc_redirect_uri=OIDC_LOCAL_REDIRECT_URI,
        oidc_post_login_redirect_uri=(
            OIDC_LOCAL_POST_LOGIN_REDIRECT_URI
        ),
    )


def _base64url_int(value: int) -> str:
    raw = value.to_bytes((value.bit_length() + 7) // 8, "big")
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode("ascii")


class FakeKeycloak:
    def __init__(self) -> None:
        self.private_key = rsa.generate_private_key(
            public_exponent=65_537,
            key_size=2048,
        )
        public_numbers = self.private_key.public_key().public_numbers()
        self.jwk = {
            "kty": "RSA",
            "use": "sig",
            "alg": "RS256",
            "kid": "local-key-1",
            "n": _base64url_int(public_numbers.n),
            "e": _base64url_int(public_numbers.e),
        }
        self.nonce = ""
        self.subject = BOUND_SUBJECT
        self.claim_overrides: dict[str, object] = {}
        self.jwks_content_encoding = ""
        self.jwks_content_length = ""
        self.requests: list[httpx.Request] = []
        self.token_forms: list[dict[str, list[str]]] = []

    async def handle(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        if str(request.url) == OIDC_LOCAL_TOKEN_ENDPOINT:
            form = parse_qs(request.content.decode("ascii"))
            self.token_forms.append(form)
            now = datetime.now(timezone.utc)
            claims: dict[str, object] = {
                "iss": OIDC_LOCAL_ISSUER,
                "sub": self.subject,
                "aud": OIDC_LOCAL_CLIENT_ID,
                "azp": OIDC_LOCAL_CLIENT_ID,
                "iat": int(now.timestamp()),
                "exp": int((now + timedelta(minutes=5)).timestamp()),
                "nonce": self.nonce,
            }
            claims.update(self.claim_overrides)
            id_token = jwt.encode(
                claims,
                self.private_key,
                algorithm="RS256",
                headers={"kid": "local-key-1", "typ": "JWT"},
            )
            return httpx.Response(
                200,
                json={"id_token": id_token, "token_type": "Bearer"},
            )
        if str(request.url) == OIDC_LOCAL_JWKS_ENDPOINT:
            headers: dict[str, str] = {}
            if self.jwks_content_encoding:
                headers["Content-Encoding"] = self.jwks_content_encoding
            if self.jwks_content_length:
                headers["Content-Length"] = self.jwks_content_length
            return httpx.Response(
                200,
                json={"keys": [self.jwk]},
                headers=headers or None,
            )
        raise AssertionError("OIDC requested an unexpected network target")


async def _setup_and_bind_admin(
    database_path: Path,
    upload_root: Path,
) -> UUID:
    application = create_app(local_settings(database_path, upload_root))
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://127.0.0.1:23010",
        ) as client:
            auth_status = await client.get("/api/v1/auth/status")
            assert auth_status.status_code == 200
            assert (
                auth_status.json()["data"]["authentication_method"]
                == "local_accounts"
            )
            assert auth_status.json()["data"]["setup_allowed"] is True
            setup = await client.post(
                "/api/v1/auth/setup",
                json={
                    "username": "oidc-admin",
                    "display_name": "OIDC Admin",
                    "password": ADMIN_PASSWORD,
                },
            )
            assert setup.status_code == 200, setup.text
            admin_id = UUID(setup.json()["data"]["user"]["id"])
            csrf = setup.json()["data"]["csrf_token"]

            bound = await client.post(
                f"/api/v1/users/{admin_id}/oidc-binding",
                headers={"X-CSRF-Token": csrf},
                json={"subject": BOUND_SUBJECT},
            )
            assert bound.status_code == 200, bound.text

            duplicate = await client.post(
                f"/api/v1/users/{admin_id}/oidc-binding",
                headers={"X-CSRF-Token": csrf},
                json={"subject": BOUND_SUBJECT},
            )
            assert duplicate.status_code == 409

            unknown = await client.post(
                "/api/v1/users/00000000-0000-0000-0000-000000000099/"
                "oidc-binding",
                headers={"X-CSRF-Token": csrf},
                json={"subject": "unknown-user-subject"},
            )
            assert unknown.status_code == 404

            viewer = await client.post(
                "/api/v1/users",
                headers={"X-CSRF-Token": csrf},
                json={
                    "username": "disabled-viewer",
                    "display_name": "Disabled Viewer",
                    "password": VIEWER_PASSWORD,
                    "role": "viewer",
                },
            )
            assert viewer.status_code == 201, viewer.text
            viewer_id = viewer.json()["data"]["id"]
            disabled = await client.patch(
                f"/api/v1/users/{viewer_id}",
                headers={"X-CSRF-Token": csrf},
                json={"status": "disabled"},
            )
            assert disabled.status_code == 200, disabled.text
            disabled_binding = await client.post(
                f"/api/v1/users/{viewer_id}/oidc-binding",
                headers={"X-CSRF-Token": csrf},
                json={"subject": "disabled-viewer-subject"},
            )
            assert disabled_binding.status_code == 409
            return admin_id
    finally:
        await application.state.container.shutdown()


async def _install_fake_keycloak(
    application,
    settings: Settings,
    fake: FakeKeycloak,
) -> None:
    original = application.state.container.oidc
    assert original is not None
    await original.aclose()
    application.state.container.oidc = OidcService(
        IdentityRepository(application.state.container.database),
        application.state.container.identity,
        settings,
        transport=httpx.MockTransport(fake.handle),
    )


def _read_authorization(response: httpx.Response) -> dict[str, str]:
    assert response.status_code == 302, response.text
    location = response.headers["location"]
    parsed = urlparse(location)
    assert location.startswith(
        f"{OIDC_LOCAL_BROWSER_AUTHORIZATION_ENDPOINT}?"
    )
    values = {
        key: entries[0]
        for key, entries in parse_qs(parsed.query).items()
    }
    assert values["client_id"] == OIDC_LOCAL_CLIENT_ID
    assert values["redirect_uri"] == OIDC_LOCAL_REDIRECT_URI
    assert values["response_type"] == "code"
    assert values["scope"] == "openid profile"
    assert values["code_challenge_method"] == "S256"
    return values


@pytest.mark.asyncio
async def test_oidc_pkce_callback_persistent_binding_and_safe_replay(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "oidc.db"
    upload_root = tmp_path / "uploads"
    await _setup_and_bind_admin(database_path, upload_root)

    settings = keycloak_settings(database_path, upload_root)
    application = create_app(settings)
    fake = FakeKeycloak()
    await _install_fake_keycloak(application, settings, fake)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://127.0.0.1:23010",
            follow_redirects=False,
        ) as client:
            started = await client.get("/api/v1/auth/oidc/start")
            authorization = _read_authorization(started)
            fake.nonce = authorization["nonce"]
            assert "HttpOnly" in started.headers["set-cookie"]
            assert "SameSite=lax" in started.headers["set-cookie"]
            assert "Path=/api/v1/auth/oidc" in started.headers["set-cookie"]

            with sqlite3.connect(database_path) as connection:
                stored = connection.execute(
                    "SELECT state_hash, nonce_hash, consumed_at "
                    "FROM oidc_login_transactions"
                ).fetchone()
            assert stored is not None
            assert stored[0] == hashlib.sha256(
                authorization["state"].encode()
            ).hexdigest()
            assert stored[1] == hashlib.sha256(
                authorization["nonce"].encode()
            ).hexdigest()
            assert stored[0] != authorization["state"]
            assert stored[1] != authorization["nonce"]
            assert stored[2] is None

            callback = await client.get(
                "/api/v1/auth/oidc/callback",
                params={"code": "one-time-code", "state": authorization["state"]},
            )
            assert callback.status_code == 303, callback.text
            assert callback.headers["location"] == OIDC_LOCAL_POST_LOGIN_REDIRECT_URI
            assert callback.headers["cache-control"] == "no-store"
            assert callback.headers["referrer-policy"] == "no-referrer"
            me = await client.get("/api/v1/auth/me")
            assert me.status_code == 200, me.text
            assert me.json()["data"]["username"] == "oidc-admin"
            assert me.json()["data"]["roles"] == ["system_admin"]

            token_form = fake.token_forms[0]
            verifier = token_form["code_verifier"][0]
            challenge = base64.urlsafe_b64encode(
                hashlib.sha256(verifier.encode("ascii")).digest()
            ).rstrip(b"=").decode("ascii")
            assert challenge == authorization["code_challenge"]
            assert token_form["grant_type"] == ["authorization_code"]
            assert token_form["client_id"] == [OIDC_LOCAL_CLIENT_ID]
            assert token_form["redirect_uri"] == [OIDC_LOCAL_REDIRECT_URI]
            assert [str(request.url) for request in fake.requests] == [
                OIDC_LOCAL_TOKEN_ENDPOINT,
                OIDC_LOCAL_JWKS_ENDPOINT,
            ]
            assert all(
                request.headers["accept-encoding"] == "identity"
                for request in fake.requests
            )

            replay = await client.get(
                "/api/v1/auth/oidc/callback",
                params={"code": "replay-code", "state": authorization["state"]},
            )
            assert replay.status_code == 401
            assert replay.json()["message"] == "OIDC 登录失败"
            assert len(fake.requests) == 2

            fake.subject = "unbound-subject"
            next_start = await client.get("/api/v1/auth/oidc/start")
            next_authorization = _read_authorization(next_start)
            fake.nonce = next_authorization["nonce"]
            unbound = await client.get(
                "/api/v1/auth/oidc/callback",
                params={
                    "code": "unbound-code",
                    "state": next_authorization["state"],
                },
            )
            assert unbound.status_code == 401
            assert unbound.json()["message"] == "OIDC 账号未获平台授权"
            assert "unbound-subject" not in unbound.text
    finally:
        await application.state.container.shutdown()


@pytest.mark.asyncio
async def test_oidc_state_is_browser_bound_without_consuming_valid_flow(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "browser-bound.db"
    upload_root = tmp_path / "uploads"
    await _setup_and_bind_admin(database_path, upload_root)
    settings = keycloak_settings(database_path, upload_root)
    application = create_app(settings)
    fake = FakeKeycloak()
    await _install_fake_keycloak(application, settings, fake)
    transport = ASGITransport(app=application)
    try:
        async with AsyncClient(
            transport=transport,
            base_url="http://127.0.0.1:23010",
        ) as owner:
            started = await owner.get("/api/v1/auth/oidc/start")
            authorization = _read_authorization(started)
            fake.nonce = authorization["nonce"]
            async with AsyncClient(
                transport=transport,
                base_url="http://127.0.0.1:23010",
            ) as another_browser:
                rejected = await another_browser.get(
                    "/api/v1/auth/oidc/callback",
                    params={
                        "code": "stolen-code",
                        "state": authorization["state"],
                    },
                )
            assert rejected.status_code == 401
            assert fake.requests == []

            completed = await owner.get(
                "/api/v1/auth/oidc/callback",
                params={
                    "code": "owner-code",
                    "state": authorization["state"],
                },
            )
            assert completed.status_code == 303
            assert len(fake.requests) == 2
    finally:
        await application.state.container.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("content_encoding", "content_length"),
    [("gzip", ""), ("", "-1")],
)
async def test_oidc_rejects_encoded_identity_provider_responses(
    tmp_path: Path,
    content_encoding: str,
    content_length: str,
) -> None:
    database_path = tmp_path / "encoded-jwks.db"
    upload_root = tmp_path / "uploads"
    await _setup_and_bind_admin(database_path, upload_root)
    settings = keycloak_settings(database_path, upload_root)
    application = create_app(settings)
    fake = FakeKeycloak()
    fake.jwks_content_encoding = content_encoding
    fake.jwks_content_length = content_length
    await _install_fake_keycloak(application, settings, fake)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://127.0.0.1:23010",
        ) as client:
            started = await client.get("/api/v1/auth/oidc/start")
            authorization = _read_authorization(started)
            fake.nonce = authorization["nonce"]
            callback = await client.get(
                "/api/v1/auth/oidc/callback",
                params={
                    "code": "encoded-response-code",
                    "state": authorization["state"],
                },
            )
            assert callback.status_code == 401
            assert callback.json()["message"] == "OIDC 登录失败"
    finally:
        await application.state.container.shutdown()


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "claim_overrides",
    [
        {"iss": "https://identity.example.test/realm"},
        {"aud": "another-client", "azp": "another-client"},
        {"exp": 1},
        {"nonce": "wrong-nonce-value-that-is-long-enough-123456789"},
    ],
)
async def test_oidc_rejects_invalid_issuer_audience_expiry_and_nonce(
    tmp_path: Path,
    claim_overrides: dict[str, object],
) -> None:
    database_path = tmp_path / "invalid-token.db"
    upload_root = tmp_path / "uploads"
    await _setup_and_bind_admin(database_path, upload_root)
    settings = keycloak_settings(database_path, upload_root)
    application = create_app(settings)
    fake = FakeKeycloak()
    fake.claim_overrides = claim_overrides
    await _install_fake_keycloak(application, settings, fake)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://127.0.0.1:23010",
            follow_redirects=False,
        ) as client:
            started = await client.get("/api/v1/auth/oidc/start")
            authorization = _read_authorization(started)
            fake.nonce = authorization["nonce"]
            callback = await client.get(
                "/api/v1/auth/oidc/callback",
                params={"code": "invalid-code", "state": authorization["state"]},
            )
            assert callback.status_code == 401
            assert callback.json()["message"] == "OIDC 登录失败"
            assert "identity.example.test" not in callback.text
            assert "invalid-code" not in callback.text
    finally:
        await application.state.container.shutdown()


@pytest.mark.asyncio
async def test_oidc_transaction_is_atomic_across_database_instances(
    tmp_path: Path,
) -> None:
    database_path = tmp_path / "multi-instance.db"
    database_url = f"sqlite+aiosqlite:///{database_path.as_posix()}"
    first_database = Database(database_url)
    second_database = Database(database_url)
    await first_database.initialize()
    await second_database.initialize()
    first = IdentityRepository(first_database)
    second = IdentityRepository(second_database)
    now = utc_now()
    transaction = OidcLoginTransaction(
        state_hash="a" * 64,
        browser_binding_hash="b" * 64,
        nonce_hash="c" * 64,
        code_verifier="v" * 64,
        created_at=now,
        expires_at=now + timedelta(minutes=5),
    )
    try:
        await first.create_oidc_login_transaction(transaction)
        results = await asyncio.gather(
            first.consume_oidc_login_transaction(
                state_hash="a" * 64,
                browser_binding_hash="b" * 64,
                consumed_at=now + timedelta(seconds=1),
            ),
            second.consume_oidc_login_transaction(
                state_hash="a" * 64,
                browser_binding_hash="b" * 64,
                consumed_at=now + timedelta(seconds=1),
            ),
        )
        assert sum(result is not None for result in results) == 1
    finally:
        await first_database.shutdown()
        await second_database.shutdown()


@pytest.mark.asyncio
async def test_local_password_endpoints_are_closed_in_keycloak_mode(
    tmp_path: Path,
) -> None:
    settings = keycloak_settings(tmp_path / "closed.db", tmp_path / "uploads")
    application = create_app(settings)
    try:
        async with AsyncClient(
            transport=ASGITransport(app=application),
            base_url="http://127.0.0.1:23010",
        ) as client:
            auth_status = await client.get("/api/v1/auth/status")
            assert auth_status.status_code == 200
            assert auth_status.json()["data"]["authentication_method"] == "oidc"
            assert auth_status.json()["data"]["setup_allowed"] is False
            setup = await client.post(
                "/api/v1/auth/setup",
                json={
                    "username": "admin",
                    "display_name": "Admin",
                    "password": ADMIN_PASSWORD,
                },
            )
            login = await client.post(
                "/api/v1/auth/login",
                json={"username": "admin", "password": ADMIN_PASSWORD},
            )
            assert setup.status_code == 403
            assert login.status_code == 403
    finally:
        await application.state.container.shutdown()
