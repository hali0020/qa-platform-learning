from __future__ import annotations

import secrets
from datetime import datetime, timezone
from uuid import UUID

from sqlalchemy import case, delete, func, or_, select, update
from sqlalchemy.exc import IntegrityError

from app.core.errors import ConflictError, NotFoundError
from app.database.models import (
    AuthSessionRecord,
    OidcIdentityRecord,
    OidcLoginTransactionRecord,
    PermissionRecord,
    RolePermissionRecord,
    RoleRecord,
    UserRecord,
)
from app.database.session import Database
from app.domain.identity import (
    AuthSession,
    OidcIdentity,
    OidcLoginTransaction,
    RoleDefinition,
    UserAccount,
    UserStatus,
)


def _as_utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


class IdentityRepository:
    def __init__(self, database: Database) -> None:
        self._database = database

    async def count_users(self) -> int:
        async with self._database.session() as session:
            return int(await session.scalar(select(func.count(UserRecord.id))) or 0)

    async def count_active_admins(self) -> int:
        statement = select(func.count(UserRecord.id)).where(
            UserRecord.role_key == "system_admin",
            UserRecord.status == UserStatus.ACTIVE.value,
        )
        async with self._database.session() as session:
            return int(await session.scalar(statement) or 0)

    async def create_user(self, user: UserAccount) -> UserAccount:
        record = self._to_user_record(user)
        async with self._database.session() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("用户名已存在或角色无效") from exc
        return user.model_copy(deep=True)

    async def update_user(self, user: UserAccount) -> UserAccount:
        async with self._database.session() as session:
            record = await session.get(UserRecord, str(user.id))
            if record is None:
                raise NotFoundError("用户", user.id)
            replacement = self._to_user_record(user)
            for field in (
                "username",
                "username_normalized",
                "display_name",
                "password_hash",
                "role_key",
                "status",
                "failed_login_count",
                "locked_until",
                "last_login_at",
                "password_changed_at",
                "created_at",
                "updated_at",
            ):
                setattr(record, field, getattr(replacement, field))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("用户更新违反唯一性或角色约束") from exc
        return user.model_copy(deep=True)

    async def record_failed_login(
        self,
        user: UserAccount,
        *,
        failed_at: datetime,
        max_failed_logins: int,
        locked_until: datetime,
    ) -> bool:
        """Atomically count one failed attempt without replacing account state.

        The password/status predicates make an attempt verified against an old
        password snapshot a no-op after an administrator resets or disables the
        account.  The counter expression is evaluated by SQLite so concurrent
        failures cannot collapse into one lost update.
        """

        next_count = UserRecord.failed_login_count + 1
        statement = (
            update(UserRecord)
            .where(
                UserRecord.id == str(user.id),
                UserRecord.password_hash == user.password_hash,
                UserRecord.status == UserStatus.ACTIVE.value,
                or_(
                    UserRecord.locked_until.is_(None),
                    UserRecord.locked_until <= failed_at,
                ),
            )
            .values(
                failed_login_count=next_count,
                locked_until=case(
                    (next_count >= max_failed_logins, locked_until),
                    else_=UserRecord.locked_until,
                ),
                updated_at=failed_at,
            )
        )
        async with self._database.session() as session:
            result = await session.execute(statement)
            await session.commit()
            return result.rowcount == 1

    async def complete_login(
        self,
        user: UserAccount,
        auth_session: AuthSession,
        *,
        authenticated_at: datetime,
        replacement_password_hash: str | None = None,
    ) -> UserAccount | None:
        """CAS the verified account snapshot and create its session atomically."""

        locked_snapshot = (
            UserRecord.locked_until.is_(None)
            if user.locked_until is None
            else UserRecord.locked_until == user.locked_until
        )
        statement = (
            update(UserRecord)
            .where(
                UserRecord.id == str(user.id),
                UserRecord.updated_at == user.updated_at,
                UserRecord.password_hash == user.password_hash,
                UserRecord.password_changed_at == user.password_changed_at,
                UserRecord.role_key == user.role_key,
                UserRecord.status == UserStatus.ACTIVE.value,
                UserRecord.failed_login_count == user.failed_login_count,
                locked_snapshot,
                or_(
                    UserRecord.locked_until.is_(None),
                    UserRecord.locked_until <= authenticated_at,
                ),
            )
            .values(
                password_hash=(replacement_password_hash or UserRecord.password_hash),
                failed_login_count=0,
                locked_until=None,
                last_login_at=authenticated_at,
                updated_at=authenticated_at,
            )
        )
        async with self._database.session() as session:
            result = await session.execute(statement)
            if result.rowcount != 1:
                await session.rollback()
                return None

            record = await session.get(UserRecord, str(user.id))
            if record is None:  # pragma: no cover - guarded by the successful CAS
                await session.rollback()
                return None
            session.add(self._to_session_record(auth_session))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("无法创建登录会话") from exc
            return self._to_user(record)

    async def get_user(self, user_id: UUID) -> UserAccount | None:
        async with self._database.session() as session:
            record = await session.get(UserRecord, str(user_id))
            return self._to_user(record) if record is not None else None

    async def get_user_by_username(self, normalized: str) -> UserAccount | None:
        statement = select(UserRecord).where(
            UserRecord.username_normalized == normalized
        )
        async with self._database.session() as session:
            record = (await session.scalars(statement)).one_or_none()
            return self._to_user(record) if record is not None else None

    async def list_users(self) -> list[UserAccount]:
        statement = select(UserRecord).order_by(UserRecord.created_at, UserRecord.id)
        async with self._database.session() as session:
            records = (await session.scalars(statement)).all()
            return [self._to_user(record) for record in records]

    async def role_exists(self, role_key: str) -> bool:
        async with self._database.session() as session:
            return await session.get(RoleRecord, role_key) is not None

    async def permissions_for_role(self, role_key: str) -> tuple[str, ...]:
        statement = (
            select(RolePermissionRecord.permission_code)
            .where(RolePermissionRecord.role_key == role_key)
            .order_by(RolePermissionRecord.permission_code)
        )
        async with self._database.session() as session:
            return tuple((await session.scalars(statement)).all())

    async def list_roles(self) -> list[RoleDefinition]:
        async with self._database.session() as session:
            roles = (await session.scalars(select(RoleRecord).order_by(RoleRecord.key))).all()
            permission_rows = (
                await session.execute(
                    select(
                        RolePermissionRecord.role_key,
                        RolePermissionRecord.permission_code,
                    ).order_by(
                        RolePermissionRecord.role_key,
                        RolePermissionRecord.permission_code,
                    )
                )
            ).all()
        by_role: dict[str, list[str]] = {}
        for role_key, permission in permission_rows:
            by_role.setdefault(role_key, []).append(permission)
        return [
            RoleDefinition(
                key=role.key,
                name=role.name,
                description=role.description,
                permissions=tuple(by_role.get(role.key, [])),
            )
            for role in roles
        ]

    async def create_session(self, auth_session: AuthSession) -> AuthSession:
        record = self._to_session_record(auth_session)
        async with self._database.session() as session:
            session.add(record)
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("无法创建登录会话") from exc
        return auth_session.model_copy(deep=True)

    async def create_oidc_login_transaction(
        self,
        transaction: OidcLoginTransaction,
    ) -> None:
        async with self._database.session() as session:
            await session.execute(
                delete(OidcLoginTransactionRecord).where(
                    OidcLoginTransactionRecord.expires_at
                    <= transaction.created_at
                )
            )
            session.add(self._to_oidc_transaction_record(transaction))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("无法创建 OIDC 登录事务") from exc

    async def consume_oidc_login_transaction(
        self,
        *,
        state_hash: str,
        browser_binding_hash: str,
        consumed_at: datetime,
    ) -> OidcLoginTransaction | None:
        """Atomically consume a browser-bound, unexpired login transaction."""

        async with self._database.session() as session:
            record = await session.get(
                OidcLoginTransactionRecord,
                state_hash,
            )
            if (
                record is None
                or record.consumed_at is not None
                or _as_utc(record.expires_at) <= consumed_at
                or not secrets.compare_digest(
                    record.browser_binding_hash,
                    browser_binding_hash,
                )
            ):
                return None
            statement = (
                update(OidcLoginTransactionRecord)
                .where(
                    OidcLoginTransactionRecord.state_hash == state_hash,
                    OidcLoginTransactionRecord.browser_binding_hash
                    == browser_binding_hash,
                    OidcLoginTransactionRecord.consumed_at.is_(None),
                    OidcLoginTransactionRecord.expires_at > consumed_at,
                )
                .values(consumed_at=consumed_at)
                .execution_options(synchronize_session=False)
            )
            result = await session.execute(statement)
            if result.rowcount != 1:
                await session.rollback()
                return None
            await session.commit()
            return self._to_oidc_transaction(record).model_copy(
                update={"consumed_at": consumed_at}
            )

    async def get_oidc_identity(
        self,
        *,
        issuer: str,
        subject: str,
    ) -> OidcIdentity | None:
        statement = select(OidcIdentityRecord).where(
            OidcIdentityRecord.issuer == issuer,
            OidcIdentityRecord.subject == subject,
        )
        async with self._database.session() as session:
            record = (await session.scalars(statement)).one_or_none()
            return (
                self._to_oidc_identity(record)
                if record is not None
                else None
            )

    async def bind_oidc_identity(self, identity: OidcIdentity) -> bool:
        """Bind once; unique subject and user constraints prevent takeover."""

        async with self._database.session() as session:
            session.add(self._to_oidc_identity_record(identity))
            try:
                await session.commit()
            except IntegrityError:
                await session.rollback()
                return False
        return True

    async def complete_oidc_login(
        self,
        user: UserAccount,
        identity: OidcIdentity,
        auth_session: AuthSession,
        *,
        authenticated_at: datetime,
    ) -> UserAccount | None:
        """Issue a session only while both account and binding remain valid."""

        user_statement = (
            update(UserRecord)
            .where(
                UserRecord.id == str(user.id),
                UserRecord.status == UserStatus.ACTIVE.value,
            )
            .values(
                failed_login_count=0,
                locked_until=None,
                last_login_at=authenticated_at,
                updated_at=authenticated_at,
            )
        )
        identity_statement = (
            update(OidcIdentityRecord)
            .where(
                OidcIdentityRecord.id == str(identity.id),
                OidcIdentityRecord.user_id == str(user.id),
                OidcIdentityRecord.issuer == identity.issuer,
                OidcIdentityRecord.subject == identity.subject,
            )
            .values(last_login_at=authenticated_at)
        )
        async with self._database.session() as session:
            user_result = await session.execute(user_statement)
            identity_result = await session.execute(identity_statement)
            if user_result.rowcount != 1 or identity_result.rowcount != 1:
                await session.rollback()
                return None
            record = await session.get(UserRecord, str(user.id))
            if record is None:  # pragma: no cover - guarded by update above
                await session.rollback()
                return None
            session.add(self._to_session_record(auth_session))
            try:
                await session.commit()
            except IntegrityError as exc:
                await session.rollback()
                raise ConflictError("无法创建登录会话") from exc
            return self._to_user(record)

    async def get_session_by_token_hash(self, token_hash: str) -> AuthSession | None:
        statement = select(AuthSessionRecord).where(
            AuthSessionRecord.token_hash == token_hash
        )
        async with self._database.session() as session:
            record = (await session.scalars(statement)).one_or_none()
            return self._to_session(record) if record is not None else None

    async def touch_session(self, session_id: UUID, last_seen_at: datetime) -> bool:
        statement = (
            update(AuthSessionRecord)
            .where(
                AuthSessionRecord.id == str(session_id),
                AuthSessionRecord.revoked_at.is_(None),
                AuthSessionRecord.expires_at > last_seen_at,
            )
            .values(last_seen_at=last_seen_at)
        )
        async with self._database.session() as session:
            result = await session.execute(statement)
            await session.commit()
            return result.rowcount == 1

    async def revoke_session(self, session_id: UUID, revoked_at: datetime) -> bool:
        statement = (
            update(AuthSessionRecord)
            .where(
                AuthSessionRecord.id == str(session_id),
                AuthSessionRecord.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        async with self._database.session() as session:
            result = await session.execute(statement)
            await session.commit()
            return result.rowcount == 1

    async def revoke_user_sessions(
        self,
        user_id: UUID,
        revoked_at: datetime,
    ) -> int:
        statement = (
            update(AuthSessionRecord)
            .where(
                AuthSessionRecord.user_id == str(user_id),
                AuthSessionRecord.revoked_at.is_(None),
            )
            .values(revoked_at=revoked_at)
        )
        async with self._database.session() as session:
            result = await session.execute(statement)
            await session.commit()
            return max(0, result.rowcount)

    @staticmethod
    def _to_user_record(user: UserAccount) -> UserRecord:
        return UserRecord(
            id=str(user.id),
            username=user.username,
            username_normalized=user.username_normalized,
            display_name=user.display_name,
            password_hash=user.password_hash,
            role_key=user.role_key,
            status=user.status.value,
            failed_login_count=user.failed_login_count,
            locked_until=user.locked_until,
            last_login_at=user.last_login_at,
            password_changed_at=user.password_changed_at,
            created_at=user.created_at,
            updated_at=user.updated_at,
        )

    @staticmethod
    def _to_session_record(auth_session: AuthSession) -> AuthSessionRecord:
        return AuthSessionRecord(
            id=str(auth_session.id),
            user_id=str(auth_session.user_id),
            token_hash=auth_session.token_hash,
            created_at=auth_session.created_at,
            expires_at=auth_session.expires_at,
            last_seen_at=auth_session.last_seen_at,
            revoked_at=auth_session.revoked_at,
        )

    @staticmethod
    def _to_oidc_identity_record(
        identity: OidcIdentity,
    ) -> OidcIdentityRecord:
        return OidcIdentityRecord(
            id=str(identity.id),
            user_id=str(identity.user_id),
            issuer=identity.issuer,
            subject=identity.subject,
            created_at=identity.created_at,
            last_login_at=identity.last_login_at,
        )

    @staticmethod
    def _to_oidc_transaction_record(
        transaction: OidcLoginTransaction,
    ) -> OidcLoginTransactionRecord:
        return OidcLoginTransactionRecord(
            state_hash=transaction.state_hash,
            browser_binding_hash=transaction.browser_binding_hash,
            nonce_hash=transaction.nonce_hash,
            code_verifier=transaction.code_verifier,
            created_at=transaction.created_at,
            expires_at=transaction.expires_at,
            consumed_at=transaction.consumed_at,
        )

    @staticmethod
    def _to_user(record: UserRecord) -> UserAccount:
        return UserAccount(
            id=UUID(record.id),
            username=record.username,
            username_normalized=record.username_normalized,
            display_name=record.display_name,
            password_hash=record.password_hash,
            role_key=record.role_key,
            status=UserStatus(record.status),
            failed_login_count=record.failed_login_count,
            locked_until=_as_utc(record.locked_until),
            last_login_at=_as_utc(record.last_login_at),
            password_changed_at=_as_utc(record.password_changed_at),
            created_at=_as_utc(record.created_at),
            updated_at=_as_utc(record.updated_at),
        )

    @staticmethod
    def _to_session(record: AuthSessionRecord) -> AuthSession:
        return AuthSession(
            id=UUID(record.id),
            user_id=UUID(record.user_id),
            token_hash=record.token_hash,
            created_at=_as_utc(record.created_at),
            expires_at=_as_utc(record.expires_at),
            last_seen_at=_as_utc(record.last_seen_at),
            revoked_at=_as_utc(record.revoked_at),
        )

    @staticmethod
    def _to_oidc_identity(record: OidcIdentityRecord) -> OidcIdentity:
        return OidcIdentity(
            id=UUID(record.id),
            user_id=UUID(record.user_id),
            issuer=record.issuer,
            subject=record.subject,
            created_at=_as_utc(record.created_at),
            last_login_at=_as_utc(record.last_login_at),
        )

    @staticmethod
    def _to_oidc_transaction(
        record: OidcLoginTransactionRecord,
    ) -> OidcLoginTransaction:
        return OidcLoginTransaction(
            state_hash=record.state_hash,
            browser_binding_hash=record.browser_binding_hash,
            nonce_hash=record.nonce_hash,
            code_verifier=record.code_verifier,
            created_at=_as_utc(record.created_at),
            expires_at=_as_utc(record.expires_at),
            consumed_at=_as_utc(record.consumed_at),
        )
