from datetime import datetime
from enum import Enum
from uuid import UUID, uuid4

from pydantic import BaseModel, Field

from app.domain.models import utc_now


class UserStatus(str, Enum):
    ACTIVE = "active"
    DISABLED = "disabled"


class PermissionCode(str, Enum):
    QA_READ = "qa.read"
    QA_WRITE = "qa.write"
    DEFECTS_MANAGE = "defects.manage"
    USERS_READ = "users.read"
    USERS_MANAGE = "users.manage"
    COLLABORATION_WRITE = "collaboration.write"
    IMPORTS_MANAGE = "imports.manage"
    REPORTS_READ = "reports.read"
    INTEGRATIONS_READ = "integrations.read"
    INTEGRATIONS_MANAGE = "integrations.manage"
    DEVICES_READ = "devices.read"
    DEVICES_MANAGE = "devices.manage"
    SCHEDULES_READ = "schedules.read"
    SCHEDULES_MANAGE = "schedules.manage"
    USER_READ = "user.read"
    USER_MANAGE = "user.manage"
    PROJECT_READ = "project.read"
    PROJECT_MANAGE = "project.manage"
    TEST_READ = "test.read"
    TEST_MANAGE = "test.manage"
    EXECUTION_READ = "execution.read"
    EXECUTION_MANAGE = "execution.manage"
    DEFECT_READ = "defect.read"
    DEFECT_MANAGE = "defect.manage"
    AUDIT_READ = "audit.read"
    COMMENT_READ = "comment.read"
    COMMENT_WRITE = "comment.write"
    COMMENT_MODERATE = "comment.moderate"
    ATTACHMENT_READ = "attachment.read"
    ATTACHMENT_WRITE = "attachment.write"
    ATTACHMENT_MODERATE = "attachment.moderate"
    PIPELINE_READ = "pipeline.read"
    PIPELINE_MANAGE = "pipeline.manage"
    PIPELINE_APPROVE = "pipeline.approve"


class UserAccount(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    username: str
    username_normalized: str
    display_name: str
    password_hash: str = Field(repr=False)
    role_key: str
    status: UserStatus = UserStatus.ACTIVE
    failed_login_count: int = 0
    locked_until: datetime | None = None
    last_login_at: datetime | None = None
    password_changed_at: datetime = Field(default_factory=utc_now)
    created_at: datetime = Field(default_factory=utc_now)
    updated_at: datetime = Field(default_factory=utc_now)


class AuthSession(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    token_hash: str = Field(repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    last_seen_at: datetime = Field(default_factory=utc_now)
    revoked_at: datetime | None = None


class OidcIdentity(BaseModel):
    id: UUID = Field(default_factory=uuid4)
    user_id: UUID
    issuer: str
    subject: str
    created_at: datetime = Field(default_factory=utc_now)
    last_login_at: datetime | None = None


class OidcLoginTransaction(BaseModel):
    state_hash: str = Field(repr=False)
    browser_binding_hash: str = Field(repr=False)
    nonce_hash: str = Field(repr=False)
    code_verifier: str = Field(repr=False)
    created_at: datetime = Field(default_factory=utc_now)
    expires_at: datetime
    consumed_at: datetime | None = None


class Principal(BaseModel):
    user_id: UUID
    username: str
    display_name: str
    roles: tuple[str, ...]
    permissions: frozenset[str]

    def has_permission(self, permission: str | PermissionCode) -> bool:
        value = permission.value if isinstance(permission, PermissionCode) else permission
        return "*" in self.permissions or value in self.permissions

    @classmethod
    def test_admin(cls) -> "Principal":
        return cls(
            user_id=UUID("00000000-0000-0000-0000-000000000001"),
            username="test-system-admin",
            display_name="Test System Admin",
            roles=("system_admin",),
            permissions=frozenset({"*"}),
        )


class RoleDefinition(BaseModel):
    key: str
    name: str
    description: str
    permissions: tuple[str, ...]
