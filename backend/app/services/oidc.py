from __future__ import annotations

import asyncio
import base64
import hashlib
import json
import secrets
import time
from dataclasses import dataclass, field
from datetime import timedelta
from typing import Any
from urllib.parse import urlencode

import httpx
import jwt
from jwt import PyJWK

from app.core.config import Settings
from app.core.errors import AuthenticationError, AuthorizationError
from app.domain.identity import OidcLoginTransaction
from app.domain.models import utc_now
from app.repositories.identity import IdentityRepository
from app.services.identity import IdentityService, IssuedSession


_MAX_TOKEN_RESPONSE_BYTES = 64 * 1024
_MAX_JWKS_RESPONSE_BYTES = 256 * 1024
_MAX_ID_TOKEN_BYTES = 16 * 1024
_MAX_JWKS_KEYS = 20


class _OidcProtocolError(Exception):
    """Internal error whose details must never cross the API boundary."""


@dataclass(frozen=True, slots=True)
class OidcAuthorizationStart:
    authorization_url: str = field(repr=False)
    browser_binding: str = field(repr=False)


@dataclass(frozen=True, slots=True)
class _VerifiedIdentity:
    issuer: str
    subject: str


class OidcService:
    """Authorization-code + PKCE client for the isolated local Keycloak.

    All network targets were validated as exact values by ``Settings``.  The
    HTTP client ignores proxy environment variables and never follows a
    redirect, so neither a host environment nor a malicious response can
    move backend traffic outside the Compose-internal Keycloak endpoint.
    """

    def __init__(
        self,
        repository: IdentityRepository,
        identity: IdentityService,
        settings: Settings,
        *,
        transport: httpx.AsyncBaseTransport | None = None,
    ) -> None:
        if settings.auth_runtime_mode != "keycloak_local_container":
            raise RuntimeError("OIDC service requires keycloak_local_container")
        settings.validate_local_safety()
        self._repository = repository
        self._identity = identity
        self._settings = settings
        timeout = httpx.Timeout(settings.oidc_operation_timeout_seconds)
        self._http = httpx.AsyncClient(
            transport=transport,
            timeout=timeout,
            follow_redirects=False,
            trust_env=False,
            limits=httpx.Limits(
                max_connections=4,
                max_keepalive_connections=2,
            ),
            headers={"User-Agent": "qa-platform-learning-oidc/1"},
        )
        self._jwks: dict[str, Any] = {}
        self._jwks_expires_at = 0.0
        self._jwks_lock = asyncio.Lock()

    async def start_authorization(self) -> OidcAuthorizationStart:
        self._require_enabled()
        state = secrets.token_urlsafe(32)
        nonce = secrets.token_urlsafe(32)
        browser_binding = secrets.token_urlsafe(32)
        code_verifier = secrets.token_urlsafe(64)
        code_challenge = self._base64url(
            hashlib.sha256(code_verifier.encode("ascii")).digest()
        )
        now = utc_now()
        await self._repository.create_oidc_login_transaction(
            OidcLoginTransaction(
                state_hash=self._hash_secret(state),
                browser_binding_hash=self._hash_secret(browser_binding),
                nonce_hash=self._hash_secret(nonce),
                code_verifier=code_verifier,
                created_at=now,
                expires_at=now
                + timedelta(
                    seconds=self._settings.oidc_transaction_ttl_seconds
                ),
            )
        )
        query = urlencode(
            {
                "client_id": self._settings.oidc_client_id,
                "redirect_uri": self._settings.oidc_redirect_uri,
                "response_type": "code",
                "scope": "openid profile",
                "state": state,
                "nonce": nonce,
                "code_challenge": code_challenge,
                "code_challenge_method": "S256",
            }
        )
        return OidcAuthorizationStart(
            authorization_url=(
                f"{self._settings.oidc_browser_authorization_endpoint}?{query}"
            ),
            browser_binding=browser_binding,
        )

    async def complete_authorization(
        self,
        *,
        code: str,
        state: str,
        browser_binding: str,
    ) -> IssuedSession:
        self._require_enabled()
        if not self._valid_protocol_value(code, minimum=1, maximum=2048):
            raise AuthenticationError("OIDC 登录失败")
        if not self._valid_protocol_value(state, minimum=32, maximum=200):
            raise AuthenticationError("OIDC 登录失败")
        if not self._valid_protocol_value(
            browser_binding,
            minimum=32,
            maximum=200,
        ):
            raise AuthenticationError("OIDC 登录失败")

        transaction = await self._repository.consume_oidc_login_transaction(
            state_hash=self._hash_secret(state),
            browser_binding_hash=self._hash_secret(browser_binding),
            consumed_at=utc_now(),
        )
        if transaction is None:
            raise AuthenticationError("OIDC 登录失败")

        try:
            token_response = await self._exchange_code(
                code=code,
                code_verifier=transaction.code_verifier,
            )
            verified = await self._verify_id_token(
                token_response,
                expected_nonce_hash=transaction.nonce_hash,
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # Do not leak Keycloak URLs, response bodies, codes, JWTs, or key
            # material through a nested exception or user-facing error.
            raise AuthenticationError("OIDC 登录失败") from None

        return await self._identity.login_oidc(
            issuer=verified.issuer,
            subject=verified.subject,
        )

    async def aclose(self) -> None:
        await self._http.aclose()

    async def _exchange_code(
        self,
        *,
        code: str,
        code_verifier: str,
    ) -> dict[str, Any]:
        payload = await self._request_json(
            "POST",
            self._settings.oidc_token_endpoint,
            max_bytes=_MAX_TOKEN_RESPONSE_BYTES,
            form={
                "grant_type": "authorization_code",
                "client_id": self._settings.oidc_client_id,
                "redirect_uri": self._settings.oidc_redirect_uri,
                "code": code,
                "code_verifier": code_verifier,
            },
        )
        id_token = payload.get("id_token")
        if not isinstance(id_token, str) or not 1 <= len(id_token) <= _MAX_ID_TOKEN_BYTES:
            raise _OidcProtocolError("invalid token response")
        token_type = payload.get("token_type")
        if token_type is not None and (
            not isinstance(token_type, str)
            or token_type.lower() != "bearer"
        ):
            raise _OidcProtocolError("invalid token type")
        return payload

    async def _verify_id_token(
        self,
        token_response: dict[str, Any],
        *,
        expected_nonce_hash: str,
    ) -> _VerifiedIdentity:
        encoded = token_response["id_token"]
        try:
            header = jwt.get_unverified_header(encoded)
        except jwt.PyJWTError as exc:
            raise _OidcProtocolError("invalid JWT header") from exc
        kid = header.get("kid")
        if (
            header.get("alg") != "RS256"
            or not isinstance(kid, str)
            or not self._valid_protocol_value(kid, minimum=1, maximum=128)
        ):
            raise _OidcProtocolError("unsupported JWT header")
        token_type = header.get("typ")
        if token_type is not None and token_type not in {"JWT", "ID"}:
            raise _OidcProtocolError("invalid JWT type")

        claims: dict[str, Any] | None = None
        for attempt in range(2):
            key = await self._signing_key(kid, force_refresh=attempt == 1)
            try:
                claims = jwt.decode(
                    encoded,
                    key=key,
                    algorithms=["RS256"],
                    audience=self._settings.oidc_client_id,
                    issuer=self._settings.oidc_issuer,
                    leeway=30,
                    options={
                        "require": [
                            "exp",
                            "iat",
                            "iss",
                            "aud",
                            "sub",
                            "nonce",
                        ]
                    },
                )
                break
            except jwt.InvalidSignatureError:
                if attempt == 0:
                    continue
                raise
        if claims is None:  # pragma: no cover - loop either breaks or raises
            raise _OidcProtocolError("invalid ID token")

        audience = claims.get("aud")
        authorized_party = claims.get("azp")
        if isinstance(audience, list) and len(audience) > 1:
            if authorized_party != self._settings.oidc_client_id:
                raise _OidcProtocolError("invalid authorized party")
        elif authorized_party is not None and (
            authorized_party != self._settings.oidc_client_id
        ):
            raise _OidcProtocolError("invalid authorized party")

        nonce = claims.get("nonce")
        if (
            not isinstance(nonce, str)
            or not self._valid_protocol_value(nonce, minimum=32, maximum=200)
            or not secrets.compare_digest(
                self._hash_secret(nonce),
                expected_nonce_hash,
            )
        ):
            raise _OidcProtocolError("invalid nonce")

        subject = claims.get("sub")
        issuer = claims.get("iss")
        if not all(isinstance(value, str) for value in (issuer, subject)):
            raise _OidcProtocolError("missing identity claim")

        at_hash = claims.get("at_hash")
        if at_hash is not None:
            access_token = token_response.get("access_token")
            if (
                not isinstance(at_hash, str)
                or not isinstance(access_token, str)
                or not self._valid_protocol_value(
                    access_token,
                    minimum=1,
                    maximum=_MAX_ID_TOKEN_BYTES,
                )
                or not secrets.compare_digest(
                    at_hash,
                    self._oidc_hash(access_token),
                )
            ):
                raise _OidcProtocolError("invalid access token hash")

        return _VerifiedIdentity(
            issuer=issuer,
            subject=subject,
        )

    async def _signing_key(
        self,
        kid: str,
        *,
        force_refresh: bool,
    ) -> Any:
        now = time.monotonic()
        if (
            not force_refresh
            and now < self._jwks_expires_at
            and kid in self._jwks
        ):
            return self._jwks[kid]
        async with self._jwks_lock:
            now = time.monotonic()
            if (
                not force_refresh
                and now < self._jwks_expires_at
                and kid in self._jwks
            ):
                return self._jwks[kid]
            payload = await self._request_json(
                "GET",
                self._settings.oidc_jwks_endpoint,
                max_bytes=_MAX_JWKS_RESPONSE_BYTES,
            )
            raw_keys = payload.get("keys")
            if (
                not isinstance(raw_keys, list)
                or not 1 <= len(raw_keys) <= _MAX_JWKS_KEYS
            ):
                raise _OidcProtocolError("invalid JWKS")
            parsed: dict[str, Any] = {}
            for raw_key in raw_keys:
                if not isinstance(raw_key, dict):
                    raise _OidcProtocolError("invalid JWK")
                key_id = raw_key.get("kid")
                if (
                    not isinstance(key_id, str)
                    or not self._valid_protocol_value(
                        key_id,
                        minimum=1,
                        maximum=128,
                    )
                    or key_id in parsed
                    or raw_key.get("kty") != "RSA"
                    or raw_key.get("use") not in {None, "sig"}
                    or raw_key.get("alg") not in {None, "RS256"}
                ):
                    raise _OidcProtocolError("invalid JWK metadata")
                pyjwk = PyJWK.from_dict(raw_key, algorithm="RS256")
                public_key = pyjwk.key
                if getattr(public_key, "key_size", 0) < 2048:
                    raise _OidcProtocolError("weak JWK")
                parsed[key_id] = public_key
            self._jwks = parsed
            self._jwks_expires_at = (
                time.monotonic() + self._settings.oidc_jwks_cache_seconds
            )
            key = parsed.get(kid)
            if key is None:
                raise _OidcProtocolError("unknown signing key")
            return key

    async def _request_json(
        self,
        method: str,
        url: str,
        *,
        max_bytes: int,
        form: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        headers = {
            "Accept": "application/json",
            "Accept-Encoding": "identity",
        }
        try:
            async with self._http.stream(
                method,
                url,
                data=form,
                headers=headers,
            ) as response:
                if response.status_code != 200:
                    raise _OidcProtocolError("identity provider rejected request")
                content_type = response.headers.get("content-type", "")
                media_type = content_type.split(";", maxsplit=1)[0].strip().lower()
                if media_type not in {
                    "application/json",
                    "application/jwk-set+json",
                }:
                    raise _OidcProtocolError("invalid response media type")
                content_encoding = response.headers.get(
                    "content-encoding",
                    "",
                ).strip().lower()
                if content_encoding not in {"", "identity"}:
                    raise _OidcProtocolError("encoded response is forbidden")
                declared_length = response.headers.get("content-length")
                if declared_length is not None:
                    try:
                        parsed_length = int(declared_length)
                        if parsed_length < 0 or parsed_length > max_bytes:
                            raise _OidcProtocolError("response too large")
                    except ValueError as exc:
                        raise _OidcProtocolError(
                            "invalid response length"
                        ) from exc
                chunks: list[bytes] = []
                size = 0
                if response.is_stream_consumed:
                    # MockTransport may provide already-buffered content. It
                    # is safe here because encoded responses were rejected
                    # above and the same byte limit is applied immediately.
                    buffered = response.content
                    if len(buffered) > max_bytes:
                        raise _OidcProtocolError("response too large")
                    chunks.append(buffered)
                else:
                    async for chunk in response.aiter_raw():
                        size += len(chunk)
                        if size > max_bytes:
                            raise _OidcProtocolError("response too large")
                        chunks.append(chunk)
        except _OidcProtocolError:
            raise
        except httpx.HTTPError as exc:
            raise _OidcProtocolError("identity provider unavailable") from exc
        try:
            payload = json.loads(b"".join(chunks))
        except (json.JSONDecodeError, UnicodeDecodeError) as exc:
            raise _OidcProtocolError("invalid JSON response") from exc
        if not isinstance(payload, dict):
            raise _OidcProtocolError("invalid JSON object")
        return payload

    def _require_enabled(self) -> None:
        if (
            not self._settings.auth_enabled
            or self._settings.auth_runtime_mode
            != "keycloak_local_container"
        ):
            raise AuthorizationError("OIDC 登录未启用")

    @staticmethod
    def _hash_secret(value: str) -> str:
        return hashlib.sha256(value.encode("utf-8")).hexdigest()

    @staticmethod
    def _base64url(value: bytes) -> str:
        return base64.urlsafe_b64encode(value).rstrip(b"=").decode("ascii")

    @classmethod
    def _oidc_hash(cls, value: str) -> str:
        digest = hashlib.sha256(value.encode("ascii")).digest()
        return cls._base64url(digest[: len(digest) // 2])

    @staticmethod
    def _valid_protocol_value(
        value: str,
        *,
        minimum: int,
        maximum: int,
    ) -> bool:
        return (
            isinstance(value, str)
            and minimum <= len(value) <= maximum
            and value == value.strip()
            and not any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in value
            )
        )
