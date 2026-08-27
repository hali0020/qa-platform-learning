"""增加本机身份、系统角色、评论和安全附件元数据。"""

from collections.abc import Sequence

from alembic import op
import sqlalchemy as sa


revision: str = "20260827_0004"
down_revision: str | Sequence[str] | None = "20260827_0003"
branch_labels: str | Sequence[str] | None = None
depends_on: str | Sequence[str] | None = None


PERMISSIONS = {
    "qa.read": "读取 QA 业务数据",
    "qa.write": "创建和变更 QA 业务数据",
    "defects.manage": "管理缺陷及其状态",
    "pipeline.read": "读取流水线",
    "pipeline.manage": "触发和取消流水线",
    "users.read": "读取用户与角色",
    "users.manage": "创建、禁用用户和分配系统角色",
    "collaboration.write": "创建评论和附件",
    "imports.manage": "导入和导出业务数据",
    "reports.read": "读取质量报表",
    "integrations.read": "读取集成配置",
    "integrations.manage": "管理外部集成",
    "devices.read": "读取设备资源",
    "devices.manage": "管理设备资源",
    "schedules.read": "读取调度任务",
    "schedules.manage": "管理调度任务",
    "user.read": "读取用户",
    "user.manage": "管理用户",
    "project.read": "读取项目",
    "project.manage": "管理项目",
    "test.read": "读取用例、套件、快照",
    "test.manage": "管理用例、套件、快照",
    "execution.read": "读取计划和执行",
    "execution.manage": "管理计划和执行",
    "defect.read": "读取缺陷",
    "defect.manage": "管理缺陷",
    "audit.read": "读取审计事件",
    "comment.read": "读取评论",
    "comment.write": "创建或编辑自己的评论",
    "comment.moderate": "管理所有评论",
    "attachment.read": "读取附件",
    "attachment.write": "创建或删除自己的附件",
    "attachment.moderate": "管理所有附件",
}

ROLES = {
    "system_admin": (
        "系统管理员",
        "本机教学平台的全部权限",
        set(PERMISSIONS),
    ),
    "qa_lead": (
        "QA 负责人",
        "管理测试设计、执行、缺陷、协作和本地流水线",
        set(PERMISSIONS)
        - {"users.manage", "user.manage", "integrations.manage", "devices.manage", "schedules.manage"},
    ),
    "tester": (
        "测试工程师",
        "编写用例、执行测试、管理缺陷并参与协作",
        {
            "qa.read", "qa.write", "defects.manage", "pipeline.read",
            "users.read", "collaboration.write", "imports.manage",
            "reports.read", "integrations.read", "devices.read",
            "schedules.read", "user.read", "project.read", "test.read",
            "test.manage", "execution.read", "execution.manage", "defect.read",
            "defect.manage", "audit.read", "comment.read", "comment.write",
            "attachment.read", "attachment.write",
        },
    ),
    "developer": (
        "开发工程师",
        "读取测试资产、处理缺陷并参与协作",
        {
            "qa.read", "defects.manage", "pipeline.read", "users.read",
            "collaboration.write", "reports.read", "integrations.read",
            "devices.read", "schedules.read", "user.read", "project.read",
            "test.read", "execution.read", "defect.read", "defect.manage",
            "audit.read", "comment.read", "comment.write", "attachment.read",
            "attachment.write",
        },
    ),
    "viewer": (
        "只读成员",
        "只读查看 QA 数据、报表和附件",
        {
            "qa.read", "pipeline.read", "users.read", "reports.read",
            "integrations.read", "devices.read", "schedules.read", "user.read",
            "project.read", "test.read", "execution.read", "defect.read",
            "audit.read", "comment.read", "attachment.read",
        },
    ),
}


def upgrade() -> None:
    op.create_table(
        "roles",
        sa.Column("key", sa.String(length=50), nullable=False),
        sa.Column("name", sa.String(length=100), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.Column("is_builtin", sa.Boolean(), nullable=False),
        sa.PrimaryKeyConstraint("key"),
    )
    op.create_table(
        "permissions",
        sa.Column("code", sa.String(length=80), nullable=False),
        sa.Column("description", sa.Text(), nullable=False),
        sa.PrimaryKeyConstraint("code"),
    )
    op.create_table(
        "role_permissions",
        sa.Column("role_key", sa.String(length=50), nullable=False),
        sa.Column("permission_code", sa.String(length=80), nullable=False),
        sa.ForeignKeyConstraint(
            ["permission_code"], ["permissions.code"], ondelete="CASCADE"
        ),
        sa.ForeignKeyConstraint(["role_key"], ["roles.key"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("role_key", "permission_code"),
    )
    op.bulk_insert(
        sa.table(
            "roles",
            sa.column("key", sa.String),
            sa.column("name", sa.String),
            sa.column("description", sa.Text),
            sa.column("is_builtin", sa.Boolean),
        ),
        [
            {"key": key, "name": value[0], "description": value[1], "is_builtin": True}
            for key, value in ROLES.items()
        ],
    )
    op.bulk_insert(
        sa.table(
            "permissions",
            sa.column("code", sa.String),
            sa.column("description", sa.Text),
        ),
        [
            {"code": code, "description": description}
            for code, description in PERMISSIONS.items()
        ],
    )
    op.bulk_insert(
        sa.table(
            "role_permissions",
            sa.column("role_key", sa.String),
            sa.column("permission_code", sa.String),
        ),
        [
            {"role_key": role_key, "permission_code": permission}
            for role_key, (_, _, permissions) in ROLES.items()
            for permission in sorted(permissions)
        ],
    )

    op.create_table(
        "users",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("username", sa.String(length=50), nullable=False),
        sa.Column("username_normalized", sa.String(length=50), nullable=False),
        sa.Column("display_name", sa.String(length=100), nullable=False),
        sa.Column("password_hash", sa.Text(), nullable=False),
        sa.Column("role_key", sa.String(length=50), nullable=False),
        sa.Column("status", sa.String(length=20), nullable=False),
        sa.Column("failed_login_count", sa.Integer(), nullable=False),
        sa.Column("locked_until", sa.DateTime(timezone=True), nullable=True),
        sa.Column("last_login_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("password_changed_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.ForeignKeyConstraint(["role_key"], ["roles.key"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("username_normalized"),
    )
    op.create_index("ix_users_role_key", "users", ["role_key"])
    op.create_index("ix_users_status", "users", ["status"])
    op.create_index(
        "ix_users_username_normalized", "users", ["username_normalized"], unique=True
    )

    op.create_table(
        "auth_sessions",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("user_id", sa.String(length=36), nullable=False),
        sa.Column("token_hash", sa.String(length=64), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("expires_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("last_seen_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("revoked_at", sa.DateTime(timezone=True), nullable=True),
        sa.ForeignKeyConstraint(["user_id"], ["users.id"], ondelete="CASCADE"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("token_hash"),
    )
    op.create_index("ix_auth_sessions_expires_at", "auth_sessions", ["expires_at"])
    op.create_index("ix_auth_sessions_revoked_at", "auth_sessions", ["revoked_at"])
    op.create_index(
        "ix_auth_sessions_token_hash", "auth_sessions", ["token_hash"], unique=True
    )
    op.create_index("ix_auth_sessions_user_id", "auth_sessions", ["user_id"])

    with op.batch_alter_table("audit_events") as batch:
        batch.add_column(sa.Column("actor_user_id", sa.String(length=36), nullable=True))
        batch.create_foreign_key(
            "fk_audit_events_actor_user_id_users",
            "users",
            ["actor_user_id"],
            ["id"],
            ondelete="RESTRICT",
        )
        batch.create_index("ix_audit_events_actor_user_id", ["actor_user_id"])

    op.create_table(
        "comments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("parent_id", sa.String(length=36), nullable=True),
        sa.Column("author_id", sa.String(length=36), nullable=False),
        sa.Column("author_name", sa.String(length=100), nullable=False),
        sa.Column("body", sa.Text(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("updated_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("edited_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["author_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["deleted_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["parent_id"], ["comments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("id", "project_id", name="uq_comments_id_project"),
    )
    op.create_index("ix_comments_author_id", "comments", ["author_id"])
    op.create_index("ix_comments_deleted_at", "comments", ["deleted_at"])
    op.create_index("ix_comments_parent_id", "comments", ["parent_id"])
    op.create_index("ix_comments_project_id", "comments", ["project_id"])
    op.create_index("ix_comments_target", "comments", ["entity_type", "entity_id"])

    op.create_table(
        "attachments",
        sa.Column("id", sa.String(length=36), nullable=False),
        sa.Column("project_id", sa.String(length=36), nullable=False),
        sa.Column("entity_type", sa.String(length=32), nullable=False),
        sa.Column("entity_id", sa.String(length=36), nullable=False),
        sa.Column("comment_id", sa.String(length=36), nullable=True),
        sa.Column("uploader_id", sa.String(length=36), nullable=False),
        sa.Column("uploader_name", sa.String(length=100), nullable=False),
        sa.Column("original_filename", sa.String(length=255), nullable=False),
        sa.Column("storage_key", sa.String(length=100), nullable=False),
        sa.Column("media_type", sa.String(length=150), nullable=False),
        sa.Column("size_bytes", sa.Integer(), nullable=False),
        sa.Column("sha256", sa.String(length=64), nullable=False),
        sa.Column("is_image", sa.Boolean(), nullable=False),
        sa.Column("created_at", sa.DateTime(timezone=True), nullable=False),
        sa.Column("deleted_at", sa.DateTime(timezone=True), nullable=True),
        sa.Column("deleted_by_id", sa.String(length=36), nullable=True),
        sa.ForeignKeyConstraint(["comment_id"], ["comments.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["deleted_by_id"], ["users.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["project_id"], ["projects.id"], ondelete="RESTRICT"),
        sa.ForeignKeyConstraint(["uploader_id"], ["users.id"], ondelete="RESTRICT"),
        sa.PrimaryKeyConstraint("id"),
        sa.UniqueConstraint("storage_key"),
    )
    op.create_index("ix_attachments_comment_id", "attachments", ["comment_id"])
    op.create_index("ix_attachments_deleted_at", "attachments", ["deleted_at"])
    op.create_index("ix_attachments_project_id", "attachments", ["project_id"])
    op.create_index("ix_attachments_sha256", "attachments", ["sha256"])
    op.create_index("ix_attachments_target", "attachments", ["entity_type", "entity_id"])
    op.create_index("ix_attachments_uploader_id", "attachments", ["uploader_id"])


def downgrade() -> None:
    op.drop_index("ix_attachments_uploader_id", table_name="attachments")
    op.drop_index("ix_attachments_target", table_name="attachments")
    op.drop_index("ix_attachments_sha256", table_name="attachments")
    op.drop_index("ix_attachments_project_id", table_name="attachments")
    op.drop_index("ix_attachments_deleted_at", table_name="attachments")
    op.drop_index("ix_attachments_comment_id", table_name="attachments")
    op.drop_table("attachments")
    op.drop_index("ix_comments_target", table_name="comments")
    op.drop_index("ix_comments_project_id", table_name="comments")
    op.drop_index("ix_comments_parent_id", table_name="comments")
    op.drop_index("ix_comments_deleted_at", table_name="comments")
    op.drop_index("ix_comments_author_id", table_name="comments")
    op.drop_table("comments")
    with op.batch_alter_table("audit_events") as batch:
        batch.drop_index("ix_audit_events_actor_user_id")
        batch.drop_constraint("fk_audit_events_actor_user_id_users", type_="foreignkey")
        batch.drop_column("actor_user_id")
    op.drop_index("ix_auth_sessions_user_id", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_token_hash", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_revoked_at", table_name="auth_sessions")
    op.drop_index("ix_auth_sessions_expires_at", table_name="auth_sessions")
    op.drop_table("auth_sessions")
    op.drop_index("ix_users_username_normalized", table_name="users")
    op.drop_index("ix_users_status", table_name="users")
    op.drop_index("ix_users_role_key", table_name="users")
    op.drop_table("users")
    op.drop_table("role_permissions")
    op.drop_table("permissions")
    op.drop_table("roles")
