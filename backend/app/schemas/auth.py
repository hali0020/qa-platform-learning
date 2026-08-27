import re
from typing import Literal
from uuid import UUID

from pydantic import BaseModel, ConfigDict, Field, field_validator, model_validator

from app.domain.identity import UserStatus


USERNAME_PATTERN = re.compile(r"^[A-Za-z][A-Za-z0-9_.-]{2,49}$")


def validate_username(value: str) -> str:
    normalized = value.strip()
    if not USERNAME_PATTERN.fullmatch(normalized):
        raise ValueError(
            "用户名须以字母开头，仅包含字母、数字、点、下划线或连字符，长度 3-50"
        )
    return normalized


def validate_password(value: str) -> str:
    if len(value) < 12 or len(value) > 128:
        raise ValueError("密码长度必须在 12 到 128 个字符之间")
    if value.isspace():
        raise ValueError("密码不能全部为空白字符")
    return value


class LoginRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    password: str = Field(min_length=1, max_length=128)

    _username = field_validator("username")(validate_username)


class SetupRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    display_name: str = Field(min_length=1, max_length=100)
    password: str

    _username = field_validator("username")(validate_username)
    _password = field_validator("password")(validate_password)

    @field_validator("display_name")
    @classmethod
    def strip_display_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("显示名称不能为空")
        return normalized


class ChangePasswordRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    current_password: str
    new_password: str

    _current = field_validator("current_password")(validate_password)
    _new = field_validator("new_password")(validate_password)

    @model_validator(mode="after")
    def passwords_must_differ(self) -> "ChangePasswordRequest":
        if self.current_password == self.new_password:
            raise ValueError("新密码不能与当前密码相同")
        return self


class UserCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    username: str
    display_name: str = Field(min_length=1, max_length=100)
    password: str
    role: str = Field(min_length=1, max_length=50)

    _username = field_validator("username")(validate_username)
    _password = field_validator("password")(validate_password)

    @field_validator("display_name", "role")
    @classmethod
    def strip_required(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized


class UserUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    display_name: str | None = Field(default=None, min_length=1, max_length=100)
    role: str | None = Field(default=None, min_length=1, max_length=50)
    status: UserStatus | None = None

    @field_validator("display_name", "role")
    @classmethod
    def strip_optional(cls, value: str | None) -> str | None:
        if value is None:
            return None
        normalized = value.strip()
        if not normalized:
            raise ValueError("字段不能为空")
        return normalized


class PasswordResetRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    new_password: str

    _password = field_validator("new_password")(validate_password)


class OidcBindingRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    subject: str = Field(min_length=1, max_length=255)

    @field_validator("subject")
    @classmethod
    def validate_subject(cls, value: str) -> str:
        if value != value.strip() or any(
            character.isspace()
            or ord(character) < 32
            or ord(character) == 127
            for character in value
        ):
            raise ValueError("OIDC subject 不能包含空白或控制字符")
        return value


class UserView(BaseModel):
    id: UUID
    username: str
    display_name: str
    status: UserStatus
    roles: list[str]
    permissions: list[str]
    last_login_at: str | None
    created_at: str


class RoleView(BaseModel):
    key: str
    name: str
    description: str
    permissions: list[str]


class AuthResult(BaseModel):
    user: UserView
    csrf_token: str


class AuthStatus(BaseModel):
    enabled: bool
    authentication_method: Literal["local_accounts", "oidc"]
    setup_required: bool
    setup_allowed: bool
    authenticated: bool
    csrf_cookie_name: str
