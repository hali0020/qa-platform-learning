from __future__ import annotations

import asyncio
import hashlib
import secrets
from asyncio import Lock
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from uuid import UUID

from argon2 import PasswordHasher
from argon2.exceptions import InvalidHashError, VerificationError, VerifyMismatchError

from app.core.config import OIDC_LOCAL_ISSUER, Settings
from app.core.errors import (
    AuthenticationError,
    AuthorizationError,
    BusinessValidationError,
    ConflictError,
    NotFoundError,
)
from app.domain.identity import (
    AuthSession,
    OidcIdentity,
    PermissionCode,
    Principal,
    RoleDefinition,
    UserAccount,
    UserStatus,
)
from app.domain.models import utc_now
from app.repositories.identity import IdentityRepository
from app.schemas.auth import (
    AuthStatus,
    ChangePasswordRequest,
    LoginRequest,
    PasswordResetRequest,
    RoleView,
    SetupRequest,
    UserCreate,
    UserUpdate,
    UserView,
)


@dataclass(frozen=True, slots=True)
class IssuedSession:
    raw_token: str = field(repr=False)
    csrf_token: str = field(repr=False)
    principal: Principal


class IdentityService:
    _MAX_FAILED_LOGINS = 5
    _LOCKOUT_MINUTES = 15

    def __init__(
        self,
        repository: IdentityRepository,
        settings: Settings,
        business_lock: Lock,
    ) -> None:
        self._repository = repository
        self._settings = settings
        self._business_lock = business_lock
        self._password_hasher = PasswordHasher(
            time_cost=settings.password_time_cost,
            memory_cost=settings.password_memory_cost_kib,
            parallelism=settings.password_parallelism,
            hash_len=32,
            salt_len=16,
        )
        self._hash_slots = asyncio.Semaphore(2)
        self._dummy_hash: str | None = None
        self._dummy_lock = asyncio.Lock()

    async def status(self, raw_token: str | None) -> AuthStatus:
        user_count = await self._repository.count_users()
        authenticated = False
        if raw_token:
            try:
                await self.authenticate_session(raw_token, touch=False)
                authenticated = True
            except AuthenticationError:
                pass
        return AuthStatus(
            enabled=self._settings.auth_enabled,
            authentication_method=(
                "oidc"
                if self._settings.auth_runtime_mode
                == "keycloak_local_container"
                else "local_accounts"
            ),
            setup_required=user_count == 0,
            setup_allowed=(
                self._settings.auth_runtime_mode == "local_accounts"
                and self._settings.local_only
                and user_count == 0
            ),
            authenticated=authenticated,
            csrf_cookie_name=self._settings.csrf_cookie_name,
        )

    async def setup(self, payload: SetupRequest) -> IssuedSession:
        self._require_local_accounts()
        if not self._settings.local_only:
            raise AuthorizationError("网页初始化只允许在本机教学模式使用")
        async with self._business_lock:
            if await self._repository.count_users() != 0:
                raise ConflictError("平台已经完成初始化")
            user = await self._new_user(
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
                role_key="system_admin",
            )
            await self._repository.create_user(user)
        return await self._issue_session(user)

    async def login(self, payload: LoginRequest) -> IssuedSession:
        self._require_local_accounts()
        normalized = self._normalize_username(payload.username)
        user = await self._repository.get_user_by_username(normalized)
        if user is None:
            await self._verify_dummy(payload.password)
            raise AuthenticationError("用户名或密码错误")

        verified = await self._verify_password(user.password_hash, payload.password)
        now = utc_now()
        if user.status != UserStatus.ACTIVE or (
            user.locked_until is not None and user.locked_until > now
        ):
            raise AuthenticationError("用户名或密码错误")
        if not verified:
            await self._repository.record_failed_login(
                user,
                failed_at=now,
                max_failed_logins=self._MAX_FAILED_LOGINS,
                locked_until=now + timedelta(minutes=self._LOCKOUT_MINUTES),
            )
            raise AuthenticationError("用户名或密码错误")

        replacement_password_hash: str | None = None
        if self._password_hasher.check_needs_rehash(user.password_hash):
            replacement_password_hash = await self._hash_password(payload.password)

        issued_at = utc_now()
        raw_token, csrf_token, auth_session = self._new_session(user.id, issued_at)
        current = await self._repository.complete_login(
            user,
            auth_session,
            authenticated_at=issued_at,
            replacement_password_hash=replacement_password_hash,
        )
        if current is None:
            # The password was verified against a stale account snapshot.  Do
            # not disclose whether an administrator disabled, demoted, reset,
            # or locked the account while Argon2 was running.
            raise AuthenticationError("用户名或密码错误")
        return IssuedSession(
            raw_token=raw_token,
            csrf_token=csrf_token,
            principal=await self._principal(current),
        )

    async def login_oidc(
        self,
        *,
        issuer: str,
        subject: str,
    ) -> IssuedSession:
        """Map a verified Keycloak subject to an existing local RBAC user.

        Login resolves only the stable ``(issuer, subject)`` pair. Realm
        usernames, email addresses, and roles are deliberately ignored;
        authorization remains owned by this application's database. An
        administrator must create the binding explicitly before switching
        from local-password mode.
        """

        if self._settings.auth_runtime_mode != "keycloak_local_container":
            raise AuthenticationError("OIDC 登录未启用")
        if issuer != self._settings.oidc_issuer or not self._valid_oidc_value(
            subject,
            max_length=255,
        ):
            raise AuthenticationError("OIDC 登录失败")

        identity = await self._repository.get_oidc_identity(
            issuer=issuer,
            subject=subject,
        )
        if identity is None:
            raise AuthenticationError("OIDC 账号未获平台授权")
        user = await self._repository.get_user(identity.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            raise AuthenticationError("OIDC 账号未获平台授权")

        issued_at = utc_now()
        raw_token, csrf_token, auth_session = self._new_session(
            user.id,
            issued_at,
        )
        current = await self._repository.complete_oidc_login(
            user,
            identity,
            auth_session,
            authenticated_at=issued_at,
        )
        if current is None:
            raise AuthenticationError("OIDC 账号未获平台授权")
        return IssuedSession(
            raw_token=raw_token,
            csrf_token=csrf_token,
            principal=await self._principal(current),
        )

    async def bind_oidc_identity(
        self,
        *,
        user_id: UUID,
        subject: str,
    ) -> None:
        """Explicitly bind one local user to one local Keycloak subject."""

        if not self._valid_oidc_value(subject, max_length=255):
            raise BusinessValidationError("OIDC subject 格式无效")
        user = await self._require_user(user_id)
        if user.status != UserStatus.ACTIVE:
            raise ConflictError("只能为启用的本地用户绑定 OIDC")
        now = utc_now()
        created = await self._repository.bind_oidc_identity(
            OidcIdentity(
                user_id=user.id,
                issuer=OIDC_LOCAL_ISSUER,
                subject=subject,
                created_at=now,
            )
        )
        if not created:
            raise ConflictError("OIDC subject 或本地用户已经绑定")

    async def authenticate_session(
        self,
        raw_token: str,
        *,
        touch: bool = True,
    ) -> Principal:
        if not raw_token or len(raw_token) > 200:
            raise AuthenticationError()
        token_hash = self._token_hash(raw_token)
        auth_session = await self._repository.get_session_by_token_hash(token_hash)
        now = utc_now()
        if (
            auth_session is None
            or auth_session.revoked_at is not None
            or auth_session.expires_at <= now
        ):
            raise AuthenticationError()
        user = await self._repository.get_user(auth_session.user_id)
        if user is None or user.status != UserStatus.ACTIVE:
            await self._repository.revoke_session(auth_session.id, now)
            raise AuthenticationError()
        if touch and now - auth_session.last_seen_at >= timedelta(minutes=5):
            if not await self._repository.touch_session(auth_session.id, now):
                # A concurrent logout/password reset won the race.  A touch is
                # never allowed to clear revoked_at or authenticate afterwards.
                raise AuthenticationError()
        return await self._principal(user)

    async def logout(self, raw_token: str) -> None:
        session = await self._repository.get_session_by_token_hash(
            self._token_hash(raw_token)
        )
        if session is not None:
            await self._repository.revoke_session(session.id, utc_now())

    async def change_password(
        self,
        principal: Principal,
        payload: ChangePasswordRequest,
    ) -> None:
        self._require_local_accounts()
        async with self._business_lock:
            user = await self._require_user(principal.user_id)
            if not await self._verify_password(
                user.password_hash,
                payload.current_password,
            ):
                raise AuthenticationError("当前密码错误")
            now = utc_now()
            updated = user.model_copy(
                update={
                    "password_hash": await self._hash_password(payload.new_password),
                    "password_changed_at": now,
                    "failed_login_count": 0,
                    "locked_until": None,
                    "updated_at": now,
                }
            )
            await self._repository.update_user(updated)
            await self._repository.revoke_user_sessions(user.id, now)

    async def list_users(self) -> list[UserView]:
        users = await self._repository.list_users()
        return [await self.user_view(user) for user in users]

    async def create_user(self, payload: UserCreate) -> UserView:
        async with self._business_lock:
            user = await self._new_user(
                username=payload.username,
                display_name=payload.display_name,
                password=payload.password,
                role_key=payload.role,
            )
            created = await self._repository.create_user(user)
        return await self.user_view(created)

    async def update_user(
        self,
        user_id: UUID,
        payload: UserUpdate,
        actor: Principal,
    ) -> UserView:
        async with self._business_lock:
            user = await self._require_user(user_id)
            changes = payload.model_dump(exclude_unset=True, exclude_none=True)
            if "role" in changes:
                role_key = str(changes.pop("role"))
                if not await self._repository.role_exists(role_key):
                    raise BusinessValidationError("角色不存在")
                changes["role_key"] = role_key
            if user.id == actor.user_id and (
                changes.get("status") == UserStatus.DISABLED
                or (
                    "role_key" in changes
                    and changes["role_key"] != "system_admin"
                )
            ):
                raise ConflictError("不能禁用当前账号或移除自己的管理员角色")
            losing_admin = (
                user.role_key == "system_admin"
                and user.status == UserStatus.ACTIVE
                and (
                    changes.get("status") == UserStatus.DISABLED
                    or changes.get("role_key", user.role_key) != "system_admin"
                )
            )
            if losing_admin and await self._repository.count_active_admins() <= 1:
                raise ConflictError("至少必须保留一个启用的系统管理员")
            changes["updated_at"] = utc_now()
            updated = await self._repository.update_user(user.model_copy(update=changes))
            if updated.status != UserStatus.ACTIVE or updated.role_key != user.role_key:
                await self._repository.revoke_user_sessions(updated.id, utc_now())
        return await self.user_view(updated)

    async def reset_password(
        self,
        user_id: UUID,
        payload: PasswordResetRequest,
    ) -> None:
        async with self._business_lock:
            user = await self._require_user(user_id)
            now = utc_now()
            await self._repository.update_user(
                user.model_copy(
                    update={
                        "password_hash": await self._hash_password(payload.new_password),
                        "password_changed_at": now,
                        "failed_login_count": 0,
                        "locked_until": None,
                        "updated_at": now,
                    }
                )
            )
            await self._repository.revoke_user_sessions(user.id, now)

    async def revoke_sessions(self, user_id: UUID) -> int:
        await self._require_user(user_id)
        return await self._repository.revoke_user_sessions(user_id, utc_now())

    async def list_roles(self) -> list[RoleView]:
        roles = await self._repository.list_roles()
        return [self._role_view(role) for role in roles]

    async def user_view(
        self,
        user: UserAccount,
        permissions: tuple[str, ...] | None = None,
    ) -> UserView:
        current_permissions = permissions or await self._repository.permissions_for_role(
            user.role_key
        )
        return UserView(
            id=user.id,
            username=user.username,
            display_name=user.display_name,
            status=user.status,
            roles=[user.role_key],
            permissions=list(current_permissions),
            last_login_at=(user.last_login_at.isoformat() if user.last_login_at else None),
            created_at=user.created_at.isoformat(),
        )

    async def principal_view(self, principal: Principal) -> UserView:
        user = await self._require_user(principal.user_id)
        return await self.user_view(user, tuple(sorted(principal.permissions)))

    def validate_csrf(self, cookie_value: str | None, header_value: str | None) -> None:
        if (
            cookie_value is None
            or header_value is None
            or len(cookie_value) < 32
            or len(cookie_value) > 200
            or not secrets.compare_digest(cookie_value, header_value)
        ):
            raise AuthorizationError("CSRF 校验失败")

    @staticmethod
    def require_permission(principal: Principal, permission: str | PermissionCode) -> None:
        if not principal.has_permission(permission):
            raise AuthorizationError()

    async def _new_user(
        self,
        *,
        username: str,
        display_name: str,
        password: str,
        role_key: str,
    ) -> UserAccount:
        if not await self._repository.role_exists(role_key):
            raise BusinessValidationError("角色不存在")
        return UserAccount(
            username=username,
            username_normalized=self._normalize_username(username),
            display_name=display_name,
            password_hash=await self._hash_password(password),
            role_key=role_key,
        )

    async def _issue_session(self, user: UserAccount) -> IssuedSession:
        now = utc_now()
        raw_token, csrf_token, auth_session = self._new_session(user.id, now)
        await self._repository.create_session(auth_session)
        return IssuedSession(
            raw_token=raw_token,
            csrf_token=csrf_token,
            principal=await self._principal(user),
        )

    def _new_session(
        self,
        user_id: UUID,
        now: datetime,
    ) -> tuple[str, str, AuthSession]:
        raw_token = secrets.token_urlsafe(32)
        csrf_token = secrets.token_urlsafe(32)
        auth_session = AuthSession(
            user_id=user_id,
            token_hash=self._token_hash(raw_token),
            created_at=now,
            expires_at=now + timedelta(minutes=self._settings.session_ttl_minutes),
            last_seen_at=now,
        )
        return raw_token, csrf_token, auth_session

    def _require_local_accounts(self) -> None:
        if self._settings.auth_runtime_mode != "local_accounts":
            raise AuthorizationError("当前身份模式不允许本地密码登录")

    @staticmethod
    def _valid_oidc_value(value: str, *, max_length: int) -> bool:
        return (
            bool(value)
            and value == value.strip()
            and len(value) <= max_length
            and not any(
                character.isspace()
                or ord(character) < 32
                or ord(character) == 127
                for character in value
            )
        )

    async def _principal(self, user: UserAccount) -> Principal:
        permissions = await self._repository.permissions_for_role(user.role_key)
        return Principal(
            user_id=user.id,
            username=user.username,
            display_name=user.display_name,
            roles=(user.role_key,),
            permissions=frozenset(permissions),
        )

    async def _require_user(self, user_id: UUID) -> UserAccount:
        user = await self._repository.get_user(user_id)
        if user is None:
            raise NotFoundError("用户", user_id)
        return user

    async def _hash_password(self, password: str) -> str:
        async with self._hash_slots:
            return await asyncio.to_thread(self._password_hasher.hash, password)

    async def _verify_password(self, encoded: str, password: str) -> bool:
        async with self._hash_slots:
            try:
                return bool(
                    await asyncio.to_thread(
                        self._password_hasher.verify,
                        encoded,
                        password,
                    )
                )
            except (VerifyMismatchError, VerificationError, InvalidHashError):
                return False

    async def _verify_dummy(self, password: str) -> None:
        if self._dummy_hash is None:
            async with self._dummy_lock:
                if self._dummy_hash is None:
                    self._dummy_hash = await self._hash_password(
                        "dummy-password-never-authenticates"
                    )
        await self._verify_password(self._dummy_hash, password)

    @staticmethod
    def _normalize_username(username: str) -> str:
        return username.strip().lower()

    @staticmethod
    def _token_hash(raw_token: str) -> str:
        return hashlib.sha256(raw_token.encode("utf-8")).hexdigest()

    @staticmethod
    def _role_view(role: RoleDefinition) -> RoleView:
        return RoleView(
            key=role.key,
            name=role.name,
            description=role.description,
            permissions=list(role.permissions),
        )
