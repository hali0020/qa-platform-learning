from __future__ import annotations

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    JSON,
    String,
    Text,
    UniqueConstraint,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from app.database.base import Base


class ProjectRecord(Base):
    __tablename__ = "projects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    key: Mapped[str] = mapped_column(String(50), nullable=False, unique=True, index=True)
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TestSuiteRecord(Base):
    __tablename__ = "test_suites"
    __table_args__ = (
        UniqueConstraint(
            "project_id",
            "parent_id",
            "name",
            name="uq_test_suites_sibling_name",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_suites.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    position: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TestCaseRecord(Base):
    __tablename__ = "test_cases"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    suite_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_suites.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    preconditions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    steps: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False, default=list)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)
    case_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class TestPlanRecord(Base):
    __tablename__ = "test_plans"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    name: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    case_links: Mapped[list[TestPlanCaseRecord]] = relationship(
        back_populates="plan",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TestPlanCaseRecord.position",
    )


class TestPlanCaseRecord(Base):
    __tablename__ = "test_plan_cases"

    plan_id: Mapped[str] = mapped_column(
        ForeignKey("test_plans.id", ondelete="CASCADE"),
        primary_key=True,
    )
    case_id: Mapped[str] = mapped_column(
        ForeignKey("test_cases.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    plan: Mapped[TestPlanRecord] = relationship(back_populates="case_links")


class TestExecutionRecord(Base):
    __tablename__ = "test_executions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    plan_id: Mapped[str] = mapped_column(
        ForeignKey("test_plans.id", ondelete="RESTRICT"),
        nullable=False,
        unique=True,
        index=True,
    )
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    started_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    completed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    results: Mapped[list[CaseExecutionResultRecord]] = relationship(
        back_populates="execution",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="CaseExecutionResultRecord.position",
    )


class CaseExecutionResultRecord(Base):
    __tablename__ = "case_execution_results"

    execution_id: Mapped[str] = mapped_column(
        ForeignKey("test_executions.id", ondelete="CASCADE"),
        primary_key=True,
    )
    case_id: Mapped[str] = mapped_column(
        ForeignKey("test_cases.id", ondelete="RESTRICT"),
        primary_key=True,
    )
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    case_title: Mapped[str] = mapped_column(String(200), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    actual_result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    executed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    execution: Mapped[TestExecutionRecord] = relationship(back_populates="results")


class DefectRecord(Base):
    __tablename__ = "defects"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    case_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_cases.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    execution_id: Mapped[str | None] = mapped_column(
        ForeignKey("test_executions.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    severity: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    reporter: Mapped[str] = mapped_column(String(100), nullable=False)
    assignee: Mapped[str] = mapped_column(String(100), nullable=False, default="", index=True)
    environment: Mapped[str] = mapped_column(String(200), nullable=False, default="")
    reproduction_steps: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    expected_result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    actual_result: Mapped[str] = mapped_column(Text, nullable=False, default="")
    resolution: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    resolved_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))


class AuditEventRecord(Base):
    __tablename__ = "audit_events"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str | None] = mapped_column(String(36), nullable=True, index=True)
    entity_type: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    entity_id: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    action: Mapped[str] = mapped_column(String(32), nullable=False)
    actor: Mapped[str] = mapped_column(String(100), nullable=False)
    actor_user_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    changes: Mapped[dict[str, dict[str, object]]] = mapped_column(
        JSON,
        nullable=False,
        default=dict,
    )
    comment: Mapped[str] = mapped_column(Text, nullable=False, default="")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        nullable=False,
        index=True,
    )


class TestCaseSnapshotRecord(Base):
    __tablename__ = "test_case_snapshots"
    __table_args__ = (
        UniqueConstraint(
            "scope_type",
            "scope_id",
            "version",
            name="uq_test_case_snapshots_scope_version",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    scope_type: Mapped[str] = mapped_column(String(32), nullable=False, index=True)
    scope_id: Mapped[str] = mapped_column(String(36), nullable=False, index=True)
    scope_name: Mapped[str] = mapped_column(String(150), nullable=False)
    version: Mapped[int] = mapped_column(Integer, nullable=False)
    label: Mapped[str] = mapped_column(String(150), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    case_count: Mapped[int] = mapped_column(Integer, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    items: Mapped[list[TestCaseSnapshotItemRecord]] = relationship(
        back_populates="snapshot",
        cascade="all, delete-orphan",
        lazy="selectin",
        order_by="TestCaseSnapshotItemRecord.position",
    )


class TestCaseSnapshotItemRecord(Base):
    __tablename__ = "test_case_snapshot_items"

    snapshot_id: Mapped[str] = mapped_column(
        ForeignKey("test_case_snapshots.id", ondelete="CASCADE"),
        primary_key=True,
    )
    source_case_id: Mapped[str] = mapped_column(String(36), primary_key=True)
    source_suite_id: Mapped[str | None] = mapped_column(String(36), nullable=True)
    suite_path: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    position: Mapped[int] = mapped_column(Integer, nullable=False)
    title: Mapped[str] = mapped_column(String(200), nullable=False)
    preconditions: Mapped[str] = mapped_column(Text, nullable=False, default="")
    steps: Mapped[list[dict[str, str]]] = mapped_column(JSON, nullable=False, default=list)
    priority: Mapped[str] = mapped_column(String(8), nullable=False)
    case_type: Mapped[str] = mapped_column(String(32), nullable=False)
    status: Mapped[str] = mapped_column(String(32), nullable=False)
    tags: Mapped[list[str]] = mapped_column(JSON, nullable=False, default=list)
    source_created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    source_updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    snapshot: Mapped[TestCaseSnapshotRecord] = relationship(back_populates="items")


class RoleRecord(Base):
    __tablename__ = "roles"

    key: Mapped[str] = mapped_column(String(50), primary_key=True)
    name: Mapped[str] = mapped_column(String(100), nullable=False)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")
    is_builtin: Mapped[bool] = mapped_column(Boolean, nullable=False, default=True)


class PermissionRecord(Base):
    __tablename__ = "permissions"

    code: Mapped[str] = mapped_column(String(80), primary_key=True)
    description: Mapped[str] = mapped_column(Text, nullable=False, default="")


class RolePermissionRecord(Base):
    __tablename__ = "role_permissions"

    role_key: Mapped[str] = mapped_column(
        ForeignKey("roles.key", ondelete="CASCADE"),
        primary_key=True,
    )
    permission_code: Mapped[str] = mapped_column(
        ForeignKey("permissions.code", ondelete="CASCADE"),
        primary_key=True,
    )


class UserRecord(Base):
    __tablename__ = "users"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    username: Mapped[str] = mapped_column(String(50), nullable=False)
    username_normalized: Mapped[str] = mapped_column(
        String(50), nullable=False, unique=True, index=True
    )
    display_name: Mapped[str] = mapped_column(String(100), nullable=False)
    password_hash: Mapped[str] = mapped_column(Text, nullable=False)
    role_key: Mapped[str] = mapped_column(
        ForeignKey("roles.key", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(20), nullable=False, index=True)
    failed_login_count: Mapped[int] = mapped_column(Integer, nullable=False, default=0)
    locked_until: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    last_login_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    password_changed_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)


class AuthSessionRecord(Base):
    __tablename__ = "auth_sessions"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        index=True,
    )
    token_hash: Mapped[str] = mapped_column(
        String(64), nullable=False, unique=True, index=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    revoked_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class OidcIdentityRecord(Base):
    __tablename__ = "oidc_identities"
    __table_args__ = (
        UniqueConstraint(
            "issuer",
            "subject",
            name="uq_oidc_identities_issuer_subject",
        ),
        CheckConstraint(
            "issuer = 'http://127.0.0.1:23010/identity/realms/qa-learning'",
            name="ck_oidc_identities_local_issuer",
        ),
        CheckConstraint(
            "length(subject) >= 1 AND length(subject) <= 255",
            name="ck_oidc_identities_subject_length",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    user_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"),
        nullable=False,
        unique=True,
        index=True,
    )
    issuer: Mapped[str] = mapped_column(String(200), nullable=False)
    subject: Mapped[str] = mapped_column(String(255), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    last_login_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), nullable=True
    )


class OidcLoginTransactionRecord(Base):
    __tablename__ = "oidc_login_transactions"
    __table_args__ = (
        CheckConstraint(
            "length(state_hash) = 64",
            name="ck_oidc_login_transactions_state_hash",
        ),
        CheckConstraint(
            "length(browser_binding_hash) = 64",
            name="ck_oidc_login_transactions_browser_hash",
        ),
        CheckConstraint(
            "length(nonce_hash) = 64",
            name="ck_oidc_login_transactions_nonce_hash",
        ),
        CheckConstraint(
            "length(code_verifier) >= 43 AND length(code_verifier) <= 128",
            name="ck_oidc_login_transactions_verifier_length",
        ),
    )

    state_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    browser_binding_hash: Mapped[str] = mapped_column(
        String(64), nullable=False
    )
    nonce_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    code_verifier: Mapped[str] = mapped_column(String(128), nullable=False)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False
    )
    expires_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), nullable=False, index=True
    )
    consumed_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )


class CommentRecord(Base):
    __tablename__ = "comments"

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    parent_id: Mapped[str | None] = mapped_column(
        ForeignKey("comments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    author_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    author_name: Mapped[str] = mapped_column(String(100), nullable=False)
    body: Mapped[str] = mapped_column(Text, nullable=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    edited_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    deleted_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
    __table_args__ = (
        UniqueConstraint("id", "project_id", name="uq_comments_id_project"),
        Index("ix_comments_target", "entity_type", "entity_id"),
    )


class AttachmentRecord(Base):
    __tablename__ = "attachments"
    __table_args__ = (
        Index("ix_attachments_target", "entity_type", "entity_id"),
        CheckConstraint(
            "length(trim(storage_backend)) >= 1 "
            "AND length(storage_backend) <= 50",
            name="ck_attachments_storage_backend_length",
        ),
        CheckConstraint(
            "length(storage_namespace) <= 200",
            name="ck_attachments_storage_namespace_length",
        ),
        CheckConstraint(
            "storage_backend IN ('local_filesystem', 's3_local_container')",
            name="ck_attachments_storage_backend_allowed",
        ),
        CheckConstraint(
            "(storage_backend = 'local_filesystem' AND storage_namespace = '') "
            "OR (storage_backend = 's3_local_container' "
            "AND storage_namespace = 'qa-artifacts')",
            name="ck_attachments_storage_route",
        ),
    )

    id: Mapped[str] = mapped_column(String(36), primary_key=True)
    project_id: Mapped[str] = mapped_column(
        ForeignKey("projects.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    entity_type: Mapped[str] = mapped_column(String(32), nullable=False)
    entity_id: Mapped[str] = mapped_column(String(36), nullable=False)
    comment_id: Mapped[str | None] = mapped_column(
        ForeignKey("comments.id", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    uploader_id: Mapped[str] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"),
        nullable=False,
        index=True,
    )
    uploader_name: Mapped[str] = mapped_column(String(100), nullable=False)
    original_filename: Mapped[str] = mapped_column(String(255), nullable=False)
    storage_backend: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
        default="local_filesystem",
        server_default="local_filesystem",
    )
    storage_namespace: Mapped[str] = mapped_column(
        String(200), nullable=False, default="", server_default=""
    )
    storage_key: Mapped[str] = mapped_column(
        String(100), nullable=False, unique=True
    )
    media_type: Mapped[str] = mapped_column(String(150), nullable=False)
    size_bytes: Mapped[int] = mapped_column(Integer, nullable=False)
    sha256: Mapped[str] = mapped_column(String(64), nullable=False, index=True)
    is_image: Mapped[bool] = mapped_column(Boolean, nullable=False, default=False)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), nullable=False)
    deleted_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True), index=True
    )
    deleted_by_id: Mapped[str | None] = mapped_column(
        ForeignKey("users.id", ondelete="RESTRICT"), nullable=True
    )
