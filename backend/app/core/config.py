import ipaddress
import os
import re
from dataclasses import dataclass, field
from functools import lru_cache
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import load_dotenv
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


PROJECT_ROOT = Path(__file__).resolve().parents[3]
LOCAL_ENV_FILE = PROJECT_ROOT / ".env"
SKIP_LOCAL_ENV_VARIABLE = "QA_PLATFORM_SKIP_LOCAL_ENV"


def _load_local_environment() -> bool:
    """Load only this repository's ignored root environment file.

    Passing an exact path is intentional.  ``load_dotenv()`` without one walks
    parent directories and could silently import an unrelated personal or
    organization environment file when this repository has no local ``.env``.
    Existing process variables keep precedence so launch scripts can enforce
    a narrower safety mode.
    """

    if os.environ.get(SKIP_LOCAL_ENV_VARIABLE) == "1":
        return False
    return load_dotenv(dotenv_path=LOCAL_ENV_FILE, override=False)


_load_local_environment()


PROVIDER_RUNTIME_MODES = frozenset(
    {"local_lab", "ci_lab_local", "self_hosted_lab"}
)
CI_LAB_PROVIDER_SECRET_NAME = "QA_PROVIDER_SECRET_CI_LAB"
CI_LAB_HOST_BASE_URL = "http://127.0.0.1:23020"
CI_LAB_CONTAINER_BASE_URL = "http://172.30.60.2:8080"
CI_LAB_HOST_ADDRESS = "127.0.0.1"
CI_LAB_CONTAINER_ADDRESS = "172.30.60.2"
DATABASE_RUNTIME_MODES = frozenset(
    {"sqlite_local", "postgres_local_container"}
)
POSTGRES_LOCAL_CONTAINER_HOST = "postgres"
POSTGRES_DEFAULT_PORT = 5432
BROKER_RUNTIME_MODES = frozenset(
    {"disabled_local", "rabbitmq_local_container"}
)
RABBITMQ_LOCAL_CONTAINER_HOST = "rabbitmq"
RABBITMQ_DEFAULT_PORT = 5672
RABBITMQ_DEDICATED_VHOST = "qa_platform_learning"
OBJECT_STORAGE_RUNTIME_MODES = frozenset(
    {"local_filesystem", "s3_local_container"}
)
OBJECT_STORAGE_LOCAL_ENDPOINT = "http://seaweedfs:8333"
OBJECT_STORAGE_DEDICATED_BUCKET = "qa-artifacts"
OBJECT_STORAGE_DEDICATED_REGION = "us-east-1"
AUTH_RUNTIME_MODES = frozenset(
    {"local_accounts", "keycloak_local_container"}
)
OIDC_LOCAL_ISSUER = (
    "http://127.0.0.1:23010/identity/realms/qa-learning"
)
OIDC_LOCAL_BROWSER_AUTHORIZATION_ENDPOINT = (
    f"{OIDC_LOCAL_ISSUER}/protocol/openid-connect/auth"
)
OIDC_LOCAL_INTERNAL_BASE = (
    "http://keycloak:8080/identity/realms/qa-learning"
)
OIDC_LOCAL_TOKEN_ENDPOINT = (
    f"{OIDC_LOCAL_INTERNAL_BASE}/protocol/openid-connect/token"
)
OIDC_LOCAL_JWKS_ENDPOINT = (
    f"{OIDC_LOCAL_INTERNAL_BASE}/protocol/openid-connect/certs"
)
OIDC_LOCAL_CLIENT_ID = "qa-platform-web"
OIDC_LOCAL_REDIRECT_URI = (
    "http://127.0.0.1:23010/api/v1/auth/oidc/callback"
)
OIDC_LOCAL_POST_LOGIN_REDIRECT_URI = "http://127.0.0.1:23010/"
SECRET_STORE_RUNTIME_MODES = frozenset(
    {"env_local", "vault_local_container"}
)
VAULT_LOCAL_ENDPOINT = "http://vault:8200"
VAULT_LOCAL_KV_MOUNT = "qa-platform"
VAULT_LOCAL_APP_TOKEN_FILE = "/run/secrets/vault_app_token"
_SELF_HOSTED_NETWORK_BOUNDARIES = (
    ipaddress.ip_network("10.0.0.0/8"),
    ipaddress.ip_network("172.16.0.0/12"),
    ipaddress.ip_network("192.168.0.0/16"),
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("fc00::/7"),
    ipaddress.ip_network("::1/128"),
)
_LOOPBACK_NETWORK_BOUNDARIES = (
    ipaddress.ip_network("127.0.0.0/8"),
    ipaddress.ip_network("::1/128"),
)


def _is_self_hosted_network(value: str) -> bool:
    """Return whether a CIDR is limited to a lab-owned private/loopback range."""

    network = ipaddress.ip_network(value, strict=True)
    inside_lab_boundary = any(
        network.version == boundary.version and network.subnet_of(boundary)
        for boundary in _SELF_HOSTED_NETWORK_BOUNDARIES
    )
    minimum_prefix = 24 if network.version == 4 else 64
    return inside_lab_boundary and network.prefixlen >= minimum_prefix


def _is_self_hosted_ip_literal(host: str) -> bool:
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return True  # Exact hostnames are checked again after DNS resolution.
    return any(
        address.version == boundary.version and address in boundary
        for boundary in _SELF_HOSTED_NETWORK_BOUNDARIES
    )


def _is_loopback_network(value: str) -> bool:
    network = ipaddress.ip_network(value, strict=True)
    return any(
        network.version == boundary.version and network.subnet_of(boundary)
        for boundary in _LOOPBACK_NETWORK_BOUNDARIES
    )


def _to_bool(name: str, value: str, default: bool = False) -> bool:
    normalized = value.strip().lower()
    if not normalized:
        return default
    if normalized in {"1", "true", "yes", "on"}:
        return True
    if normalized in {"0", "false", "no", "off"}:
        return False
    raise ValueError(f"{name} 必须是 true/false，当前值: {value!r}")


def _is_loopback_host(host: str) -> bool:
    normalized = host.strip().lower()
    if normalized == "localhost":
        return True
    try:
        return ipaddress.ip_address(normalized).is_loopback
    except ValueError:
        return False


def _is_loopback_url(value: str) -> bool:
    parsed = urlparse(value)
    return parsed.scheme in {"http", "https"} and _is_loopback_host(
        parsed.hostname or ""
    )


def _is_local_sqlite_url(value: str) -> bool:
    """只接受本地 aiosqlite URL，并拒绝可能指向共享目录的 UNC 路径。"""

    prefix = "sqlite+aiosqlite:///"
    if not value.startswith(prefix):
        return False
    database_path = value.removeprefix(prefix).split("?", maxsplit=1)[0]
    if not database_path:
        return False
    if database_path == ":memory:":
        return True
    return not database_path.startswith(("//", "\\\\", "file:"))


def _is_local_postgres_container_url(
    value: str,
) -> bool:
    """Only accept the project-owned PostgreSQL service on the internal network.

    The explicit driver and exact service name prevent a copied ``DATABASE_URL``
    from silently reaching a company/public database. Query parameters are
    rejected because libpq-style options can introduce alternate hosts.
    """

    try:
        parsed = make_url(value)
        parsed_port = parsed.port
    except (ArgumentError, TypeError, ValueError):
        return False
    return all(
        (
            parsed.drivername == "postgresql+asyncpg",
            parsed.host == POSTGRES_LOCAL_CONTAINER_HOST,
            parsed_port == POSTGRES_DEFAULT_PORT,
            bool(parsed.username),
            bool(parsed.password),
            bool(parsed.database),
            not parsed.query,
        )
    )


def validate_database_runtime_target(
    *,
    database_url: str,
    runtime_mode: str,
    app_env: str,
) -> None:
    """Validate the database URL against the selected isolated lab topology."""

    if runtime_mode not in DATABASE_RUNTIME_MODES:
        raise RuntimeError(
            "DATABASE_RUNTIME_MODE 只能是 sqlite_local 或 "
            "postgres_local_container"
        )
    if runtime_mode == "sqlite_local":
        if not _is_local_sqlite_url(database_url):
            raise RuntimeError(
                "sqlite_local 只允许本地 sqlite+aiosqlite 数据库"
            )
        return
    if app_env != "local-container":
        raise RuntimeError(
            "postgres_local_container 只允许用于 "
            "APP_ENV=local-container"
        )
    if not _is_local_postgres_container_url(database_url):
        raise RuntimeError(
            "postgres_local_container 只允许通过 postgresql+asyncpg "
            "连接自建内网服务 postgres:5432"
        )


def _decode_non_empty_url_part(value: str | None) -> str | None:
    """Decode a URL component and reject empty, whitespace, or control values."""

    if value is None:
        return None
    try:
        decoded = unquote(value, errors="strict")
    except UnicodeDecodeError:
        return None
    if not decoded or not decoded.strip() or any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in decoded
    ):
        return None
    return decoded


def _is_local_rabbitmq_container_url(value: str) -> bool:
    """Accept only the project-owned RabbitMQ service and dedicated vhost."""

    if (
        value != value.strip()
        or not value.startswith("amqp://")
        or "?" in value
        or "#" in value
        or "\\" in value
        or re.search(r"%(?![0-9A-Fa-f]{2})", value) is not None
    ):
        return False
    try:
        parsed = urlparse(value)
        parsed_port = parsed.port
        username = _decode_non_empty_url_part(parsed.username)
        password = _decode_non_empty_url_part(parsed.password)
        vhost_path = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        return False
    return all(
        (
            parsed.scheme == "amqp",
            parsed.hostname == RABBITMQ_LOCAL_CONTAINER_HOST,
            parsed_port == RABBITMQ_DEFAULT_PORT,
            username is not None,
            password is not None,
            vhost_path == f"/{RABBITMQ_DEDICATED_VHOST}",
            not parsed.params,
            not parsed.query,
            not parsed.fragment,
        )
    )


def validate_broker_runtime_target(
    *,
    broker_url: str,
    runtime_mode: str,
    app_env: str,
) -> None:
    """Validate the broker against the isolated local-container topology."""

    if runtime_mode not in BROKER_RUNTIME_MODES:
        raise RuntimeError(
            "BROKER_RUNTIME_MODE 只能是 disabled_local 或 "
            "rabbitmq_local_container"
        )
    if runtime_mode == "disabled_local":
        if broker_url:
            raise RuntimeError("disabled_local 禁止配置 BROKER_URL")
        return
    if app_env != "local-container":
        raise RuntimeError(
            "rabbitmq_local_container 只允许用于 "
            "APP_ENV=local-container"
        )
    if not _is_local_rabbitmq_container_url(broker_url):
        raise RuntimeError(
            "rabbitmq_local_container 只允许通过 amqp 连接自建内网服务 "
            "rabbitmq:5672/qa_platform_learning"
        )


def _valid_object_storage_credential(value: str) -> bool:
    return bool(value) and value == value.strip() and len(value) <= 256 and not any(
        character.isspace() or ord(character) < 32 or ord(character) == 127
        for character in value
    )


def validate_object_storage_runtime_target(
    *,
    runtime_mode: str,
    app_env: str,
    endpoint_url: str,
    bucket: str,
    region: str,
    access_key: str,
    secret_key: str,
    max_concurrency: int,
    operation_timeout_seconds: float,
) -> None:
    """Allow only the project-owned S3 service on the internal lab network."""

    if runtime_mode not in OBJECT_STORAGE_RUNTIME_MODES:
        raise RuntimeError(
            "OBJECT_STORAGE_RUNTIME_MODE 只能是 local_filesystem 或 "
            "s3_local_container"
        )
    if not 1 <= max_concurrency <= 16:
        raise RuntimeError("对象存储并发上限必须在 1 到 16 之间")
    if not 0.1 <= operation_timeout_seconds <= 60:
        raise RuntimeError("对象存储操作超时必须在 0.1 到 60 秒之间")
    if runtime_mode == "local_filesystem":
        if endpoint_url or bucket or access_key or secret_key:
            raise RuntimeError(
                "local_filesystem 禁止配置对象存储 Endpoint、Bucket 或凭据"
            )
        return
    if app_env != "local-container":
        raise RuntimeError(
            "s3_local_container 只允许用于 APP_ENV=local-container"
        )
    if endpoint_url != OBJECT_STORAGE_LOCAL_ENDPOINT:
        raise RuntimeError(
            "s3_local_container 只允许连接自建内网服务 "
            "http://seaweedfs:8333"
        )
    if bucket != OBJECT_STORAGE_DEDICATED_BUCKET:
        raise RuntimeError("对象存储 bucket 必须是 qa-artifacts")
    if region != OBJECT_STORAGE_DEDICATED_REGION:
        raise RuntimeError("对象存储 region 必须是 us-east-1")
    if not _valid_object_storage_credential(access_key):
        raise RuntimeError("OBJECT_STORAGE_ACCESS_KEY 必须是专用非空凭据")
    if not _valid_object_storage_credential(secret_key):
        raise RuntimeError("OBJECT_STORAGE_SECRET_KEY 必须是专用非空凭据")
    if access_key == secret_key:
        raise RuntimeError("对象存储 Access Key 与 Secret Key 不能相同")


def validate_auth_runtime_target(
    *,
    runtime_mode: str,
    app_env: str,
    issuer: str,
    browser_authorization_endpoint: str,
    token_endpoint: str,
    jwks_endpoint: str,
    client_id: str,
    redirect_uri: str,
    post_login_redirect_uri: str,
    operation_timeout_seconds: float,
    transaction_ttl_seconds: int,
    jwks_cache_seconds: int,
) -> None:
    """Restrict OIDC to the project-owned Keycloak and loopback gateway.

    The logical issuer is the browser-visible loopback gateway because
    Keycloak embeds it in signed tokens and browser redirects.  Token and
    JWKS traffic uses a separate, exact Compose-internal endpoint so the
    backend cannot be redirected to a public or company identity provider.
    """

    if runtime_mode not in AUTH_RUNTIME_MODES:
        raise RuntimeError(
            "AUTH_RUNTIME_MODE 只能是 local_accounts 或 "
            "keycloak_local_container"
        )
    if not 0.1 <= operation_timeout_seconds <= 15:
        raise RuntimeError("OIDC 操作超时必须在 0.1 到 15 秒之间")
    if not 60 <= transaction_ttl_seconds <= 600:
        raise RuntimeError("OIDC 登录事务有效期必须在 60 到 600 秒之间")
    if not 30 <= jwks_cache_seconds <= 600:
        raise RuntimeError("OIDC JWKS 缓存时间必须在 30 到 600 秒之间")

    oidc_values = (
        issuer,
        browser_authorization_endpoint,
        token_endpoint,
        jwks_endpoint,
        client_id,
        redirect_uri,
        post_login_redirect_uri,
    )
    if runtime_mode == "local_accounts":
        if any(oidc_values):
            raise RuntimeError("local_accounts 禁止配置 OIDC 地址或客户端")
        return
    if app_env != "local-container":
        raise RuntimeError(
            "keycloak_local_container 只允许用于 APP_ENV=local-container"
        )

    expected = (
        OIDC_LOCAL_ISSUER,
        OIDC_LOCAL_BROWSER_AUTHORIZATION_ENDPOINT,
        OIDC_LOCAL_TOKEN_ENDPOINT,
        OIDC_LOCAL_JWKS_ENDPOINT,
        OIDC_LOCAL_CLIENT_ID,
        OIDC_LOCAL_REDIRECT_URI,
        OIDC_LOCAL_POST_LOGIN_REDIRECT_URI,
    )
    if oidc_values != expected:
        raise RuntimeError(
            "keycloak_local_container 只能使用自建 Keycloak、固定环回网关、"
            "固定客户端和回调地址"
        )


def validate_secret_store_runtime_target(
    *,
    runtime_mode: str,
    app_env: str,
    endpoint_url: str,
    kv_mount: str,
    app_token_file: str,
    max_concurrency: int,
    operation_timeout_seconds: float,
    max_attempts: int,
) -> None:
    """Restrict Secret reads to env or the project-owned Vault gateway."""

    if runtime_mode not in SECRET_STORE_RUNTIME_MODES:
        raise RuntimeError(
            "SECRET_STORE_RUNTIME_MODE 只能是 env_local 或 "
            "vault_local_container"
        )
    if not 1 <= max_concurrency <= 16:
        raise RuntimeError("Vault 并发上限必须在 1 到 16 之间")
    if not 0.1 <= operation_timeout_seconds <= 30:
        raise RuntimeError("Vault 操作超时必须在 0.1 到 30 秒之间")
    if not 1 <= max_attempts <= 3:
        raise RuntimeError("Vault 重试次数必须在 1 到 3 之间")
    vault_values = (endpoint_url, kv_mount, app_token_file)
    if runtime_mode == "env_local":
        if any(vault_values):
            raise RuntimeError("env_local 禁止保留 Vault 地址、路径或 Token 文件")
        return
    if app_env != "local-container":
        raise RuntimeError(
            "vault_local_container 只允许用于 APP_ENV=local-container"
        )
    if vault_values != (
        VAULT_LOCAL_ENDPOINT,
        VAULT_LOCAL_KV_MOUNT,
        VAULT_LOCAL_APP_TOKEN_FILE,
    ):
        raise RuntimeError(
            "vault_local_container 只能使用自建 Vault 网关、"
            "固定 KV mount 和固定容器 Secret 文件"
        )


@dataclass(frozen=True, slots=True)
class Settings:
    app_name: str = "qa-platform-learning"
    app_env: str = "local"
    debug: bool = True
    host: str = "127.0.0.1"
    port: int = 23100
    local_only: bool = True
    auth_enabled: bool = True
    auth_runtime_mode: str = "local_accounts"
    oidc_issuer: str = ""
    oidc_browser_authorization_endpoint: str = ""
    oidc_token_endpoint: str = ""
    oidc_jwks_endpoint: str = ""
    oidc_client_id: str = ""
    oidc_redirect_uri: str = ""
    oidc_post_login_redirect_uri: str = ""
    oidc_transaction_cookie_name: str = "qa_oidc_txn"
    oidc_operation_timeout_seconds: float = 5.0
    oidc_transaction_ttl_seconds: int = 300
    oidc_jwks_cache_seconds: int = 300
    secret_store_runtime_mode: str = "env_local"
    vault_endpoint_url: str = ""
    vault_kv_mount: str = ""
    vault_app_token_file: str = ""
    vault_max_concurrency: int = 4
    vault_operation_timeout_seconds: float = 3.0
    vault_max_attempts: int = 3
    database_runtime_mode: str = "sqlite_local"
    database_url: str = field(
        default="sqlite+aiosqlite:///./.data/qa.db",
        repr=False,
    )
    broker_runtime_mode: str = "disabled_local"
    broker_url: str = field(default="", repr=False)
    local_data_root: str = "./.data"
    session_cookie_name: str = "qa_session"
    csrf_cookie_name: str = "qa_csrf"
    session_ttl_minutes: int = 480
    session_cookie_secure: bool = False
    password_time_cost: int = 2
    password_memory_cost_kib: int = 19_456
    password_parallelism: int = 1
    upload_root: str = "./.data/uploads"
    upload_max_bytes: int = 10 * 1024 * 1024
    image_max_pixels: int = 25_000_000
    object_storage_runtime_mode: str = "local_filesystem"
    object_storage_endpoint_url: str = ""
    object_storage_bucket: str = ""
    object_storage_region: str = OBJECT_STORAGE_DEDICATED_REGION
    object_storage_access_key: str = field(default="", repr=False)
    object_storage_secret_key: str = field(default="", repr=False)
    object_storage_max_concurrency: int = 4
    object_storage_operation_timeout_seconds: float = 10.0
    provider_runtime_mode: str = "local_lab"
    provider_self_hosted_ownership_acknowledged: bool = False
    provider_allowed_hosts: tuple[str, ...] = ()
    provider_allowed_ports: tuple[int, ...] = (443,)
    provider_allowed_networks: tuple[str, ...] = ()
    provider_allow_loopback_http: bool = False
    provider_secret_env_names: tuple[str, ...] = ()
    metrics_enabled: bool = True
    request_logging_enabled: bool = True
    cors_origins: tuple[str, ...] = (
        "http://127.0.0.1:5173",
        "http://localhost:5173",
    )

    @classmethod
    def from_environment(cls) -> "Settings":
        # The old switch could enable arbitrary external providers. It is now a
        # retired fail-closed tripwire rather than a compatibility escape hatch.
        legacy_external_switch = os.getenv("EXTERNAL_PROVIDERS_ENABLED", "").strip()
        if legacy_external_switch and _to_bool(
            "EXTERNAL_PROVIDERS_ENABLED", legacy_external_switch
        ):
            raise ValueError(
                "EXTERNAL_PROVIDERS_ENABLED 已停用；只能使用 PROVIDER_RUNTIME_MODE="
                "local_lab 或 self_hosted_lab"
            )
        return cls(
            app_name=os.getenv("APP_NAME", "qa-platform-learning").strip(),
            app_env=os.getenv("APP_ENV", "local").strip(),
            debug=_to_bool("DEBUG", os.getenv("DEBUG", "true"), default=True),
            host=os.getenv("HOST", "127.0.0.1").strip(),
            port=int(os.getenv("PORT", "23100")),
            local_only=_to_bool(
                "LOCAL_ONLY", os.getenv("LOCAL_ONLY", "true"), default=True
            ),
            auth_enabled=_to_bool(
                "AUTH_ENABLED", os.getenv("AUTH_ENABLED", "true"), default=True
            ),
            auth_runtime_mode=os.getenv(
                "AUTH_RUNTIME_MODE", "local_accounts"
            ).strip().lower(),
            oidc_issuer=os.getenv("OIDC_ISSUER", "").strip(),
            oidc_browser_authorization_endpoint=os.getenv(
                "OIDC_BROWSER_AUTHORIZATION_ENDPOINT", ""
            ).strip(),
            oidc_token_endpoint=os.getenv(
                "OIDC_TOKEN_ENDPOINT", ""
            ).strip(),
            oidc_jwks_endpoint=os.getenv(
                "OIDC_JWKS_ENDPOINT", ""
            ).strip(),
            oidc_client_id=os.getenv("OIDC_CLIENT_ID", "").strip(),
            oidc_redirect_uri=os.getenv("OIDC_REDIRECT_URI", "").strip(),
            oidc_post_login_redirect_uri=os.getenv(
                "OIDC_POST_LOGIN_REDIRECT_URI", ""
            ).strip(),
            oidc_transaction_cookie_name=os.getenv(
                "OIDC_TRANSACTION_COOKIE_NAME", "qa_oidc_txn"
            ).strip(),
            oidc_operation_timeout_seconds=float(
                os.getenv("OIDC_OPERATION_TIMEOUT_SECONDS", "5")
            ),
            oidc_transaction_ttl_seconds=int(
                os.getenv("OIDC_TRANSACTION_TTL_SECONDS", "300")
            ),
            oidc_jwks_cache_seconds=int(
                os.getenv("OIDC_JWKS_CACHE_SECONDS", "300")
            ),
            secret_store_runtime_mode=os.getenv(
                "SECRET_STORE_RUNTIME_MODE", "env_local"
            ).strip().lower(),
            vault_endpoint_url=os.getenv(
                "VAULT_ENDPOINT_URL", ""
            ).strip(),
            vault_kv_mount=os.getenv("VAULT_KV_MOUNT", "").strip(),
            vault_app_token_file=os.getenv(
                "VAULT_APP_TOKEN_FILE", ""
            ).strip(),
            vault_max_concurrency=int(
                os.getenv("VAULT_MAX_CONCURRENCY", "4")
            ),
            vault_operation_timeout_seconds=float(
                os.getenv("VAULT_OPERATION_TIMEOUT_SECONDS", "3")
            ),
            vault_max_attempts=int(os.getenv("VAULT_MAX_ATTEMPTS", "3")),
            database_runtime_mode=os.getenv(
                "DATABASE_RUNTIME_MODE", "sqlite_local"
            ).strip().lower(),
            database_url=os.getenv(
                "DATABASE_URL",
                "sqlite+aiosqlite:///./.data/qa.db",
            ).strip(),
            broker_runtime_mode=os.getenv(
                "BROKER_RUNTIME_MODE", "disabled_local"
            ).strip().lower(),
            broker_url=os.getenv("BROKER_URL", "").strip(),
            local_data_root=os.getenv("LOCAL_DATA_ROOT", "./.data").strip(),
            session_cookie_name=os.getenv(
                "SESSION_COOKIE_NAME", "qa_session"
            ).strip(),
            csrf_cookie_name=os.getenv("CSRF_COOKIE_NAME", "qa_csrf").strip(),
            session_ttl_minutes=int(os.getenv("SESSION_TTL_MINUTES", "480")),
            session_cookie_secure=_to_bool(
                "SESSION_COOKIE_SECURE",
                os.getenv("SESSION_COOKIE_SECURE", "false"),
                default=False,
            ),
            password_time_cost=int(os.getenv("PASSWORD_TIME_COST", "2")),
            password_memory_cost_kib=int(
                os.getenv("PASSWORD_MEMORY_COST_KIB", "19456")
            ),
            password_parallelism=int(os.getenv("PASSWORD_PARALLELISM", "1")),
            upload_root=os.getenv("UPLOAD_ROOT", "./.data/uploads").strip(),
            upload_max_bytes=int(
                os.getenv("UPLOAD_MAX_BYTES", str(10 * 1024 * 1024))
            ),
            image_max_pixels=int(os.getenv("IMAGE_MAX_PIXELS", "25000000")),
            object_storage_runtime_mode=os.getenv(
                "OBJECT_STORAGE_RUNTIME_MODE", "local_filesystem"
            ).strip().lower(),
            object_storage_endpoint_url=os.getenv(
                "OBJECT_STORAGE_ENDPOINT_URL", ""
            ).strip(),
            object_storage_bucket=os.getenv(
                "OBJECT_STORAGE_BUCKET", ""
            ).strip(),
            object_storage_region=os.getenv(
                "OBJECT_STORAGE_REGION", OBJECT_STORAGE_DEDICATED_REGION
            ).strip(),
            object_storage_access_key=os.getenv(
                "OBJECT_STORAGE_ACCESS_KEY", ""
            ),
            object_storage_secret_key=os.getenv(
                "OBJECT_STORAGE_SECRET_KEY", ""
            ),
            object_storage_max_concurrency=int(
                os.getenv("OBJECT_STORAGE_MAX_CONCURRENCY", "4")
            ),
            object_storage_operation_timeout_seconds=float(
                os.getenv("OBJECT_STORAGE_OPERATION_TIMEOUT_SECONDS", "10")
            ),
            provider_runtime_mode=os.getenv(
                "PROVIDER_RUNTIME_MODE", "local_lab"
            ).strip().lower(),
            provider_self_hosted_ownership_acknowledged=_to_bool(
                "PROVIDER_SELF_HOSTED_OWNERSHIP_ACKNOWLEDGED",
                os.getenv(
                    "PROVIDER_SELF_HOSTED_OWNERSHIP_ACKNOWLEDGED", "false"
                ),
                default=False,
            ),
            provider_allowed_hosts=tuple(
                item.strip().lower()
                for item in os.getenv("PROVIDER_ALLOWED_HOSTS", "").split(",")
                if item.strip()
            ),
            provider_allowed_ports=tuple(
                int(item.strip())
                for item in os.getenv("PROVIDER_ALLOWED_PORTS", "443").split(",")
                if item.strip()
            ),
            provider_allowed_networks=tuple(
                item.strip()
                for item in os.getenv("PROVIDER_ALLOWED_NETWORKS", "").split(",")
                if item.strip()
            ),
            provider_allow_loopback_http=_to_bool(
                "PROVIDER_ALLOW_LOOPBACK_HTTP",
                os.getenv("PROVIDER_ALLOW_LOOPBACK_HTTP", "false"),
                default=False,
            ),
            provider_secret_env_names=tuple(
                item.strip()
                for item in os.getenv("PROVIDER_SECRET_ENV_ALLOWLIST", "").split(",")
                if item.strip()
            ),
            metrics_enabled=_to_bool(
                "METRICS_ENABLED",
                os.getenv("METRICS_ENABLED", "true"),
                default=True,
            ),
            request_logging_enabled=_to_bool(
                "REQUEST_LOGGING_ENABLED",
                os.getenv("REQUEST_LOGGING_ENABLED", "true"),
                default=True,
            ),
            cors_origins=tuple(
                item.strip()
                for item in os.getenv(
                    "CORS_ORIGINS",
                    "http://127.0.0.1:5173,http://localhost:5173",
                ).split(",")
                if item.strip()
            ),
        )

    def validate_local_safety(self) -> None:
        # Database topology is an independent boundary. Disabling the HTTP
        # local_only switch must never authorize a remote database.
        validate_database_runtime_target(
            database_url=self.database_url,
            runtime_mode=self.database_runtime_mode,
            app_env=self.app_env,
        )
        validate_broker_runtime_target(
            broker_url=self.broker_url,
            runtime_mode=self.broker_runtime_mode,
            app_env=self.app_env,
        )
        validate_object_storage_runtime_target(
            runtime_mode=self.object_storage_runtime_mode,
            app_env=self.app_env,
            endpoint_url=self.object_storage_endpoint_url,
            bucket=self.object_storage_bucket,
            region=self.object_storage_region,
            access_key=self.object_storage_access_key,
            secret_key=self.object_storage_secret_key,
            max_concurrency=self.object_storage_max_concurrency,
            operation_timeout_seconds=(
                self.object_storage_operation_timeout_seconds
            ),
        )
        validate_auth_runtime_target(
            runtime_mode=self.auth_runtime_mode,
            app_env=self.app_env,
            issuer=self.oidc_issuer,
            browser_authorization_endpoint=(
                self.oidc_browser_authorization_endpoint
            ),
            token_endpoint=self.oidc_token_endpoint,
            jwks_endpoint=self.oidc_jwks_endpoint,
            client_id=self.oidc_client_id,
            redirect_uri=self.oidc_redirect_uri,
            post_login_redirect_uri=self.oidc_post_login_redirect_uri,
            operation_timeout_seconds=self.oidc_operation_timeout_seconds,
            transaction_ttl_seconds=self.oidc_transaction_ttl_seconds,
            jwks_cache_seconds=self.oidc_jwks_cache_seconds,
        )
        validate_secret_store_runtime_target(
            runtime_mode=self.secret_store_runtime_mode,
            app_env=self.app_env,
            endpoint_url=self.vault_endpoint_url,
            kv_mount=self.vault_kv_mount,
            app_token_file=self.vault_app_token_file,
            max_concurrency=self.vault_max_concurrency,
            operation_timeout_seconds=(
                self.vault_operation_timeout_seconds
            ),
            max_attempts=self.vault_max_attempts,
        )
        if self.local_only and not _is_loopback_host(self.host):
            raise RuntimeError(
                f"LOCAL_ONLY=true，拒绝绑定非本机地址: {self.host}"
            )
        if self.local_only:
            unsafe_origins = [
                origin
                for origin in self.cors_origins
                if not _is_loopback_url(origin)
            ]
            if unsafe_origins:
                raise RuntimeError(
                    "LOCAL_ONLY=true，拒绝非本机 CORS 来源: "
                    + ", ".join(unsafe_origins)
                )
        if not self.auth_enabled and self.app_env != "test":
            raise RuntimeError("AUTH_ENABLED=false 只允许用于隔离测试")
        local_container_demo = self.app_env == "local-container" and all(
            _is_loopback_url(origin) for origin in self.cors_origins
        )
        if (
            not self.local_only
            and not self.session_cookie_secure
            and not local_container_demo
        ):
            raise RuntimeError("非本机模式必须启用安全 Cookie")
        if not self.session_cookie_name or not self.csrf_cookie_name:
            raise RuntimeError("Session 与 CSRF Cookie 名称不能为空")
        if not self.oidc_transaction_cookie_name:
            raise RuntimeError("OIDC 登录事务 Cookie 名称不能为空")
        if len(
            {
                self.session_cookie_name,
                self.csrf_cookie_name,
                self.oidc_transaction_cookie_name,
            }
        ) != 3:
            raise RuntimeError("Session、CSRF 与 OIDC Cookie 必须使用不同名称")
        if not 5 <= self.session_ttl_minutes <= 24 * 60:
            raise RuntimeError("Session 有效期必须在 5 分钟到 24 小时之间")
        if self.upload_max_bytes < 1024 or self.upload_max_bytes > 100 * 1024 * 1024:
            raise RuntimeError("单个附件上限必须在 1 KiB 到 100 MiB 之间")
        if not 1_000_000 <= self.image_max_pixels <= 100_000_000:
            raise RuntimeError("图片像素上限必须在 100 万到 1 亿之间")
        if self.app_env != "test":
            if self.password_time_cost < 2:
                raise RuntimeError("Argon2 time_cost 不能小于 2")
            if self.password_memory_cost_kib < 19_456:
                raise RuntimeError("Argon2 memory_cost 不能小于 19456 KiB")
        if self.password_parallelism < 1:
            raise RuntimeError("Argon2 parallelism 不能小于 1")
        if self.provider_runtime_mode not in PROVIDER_RUNTIME_MODES:
            raise RuntimeError(
                "PROVIDER_RUNTIME_MODE 只能是 local_lab、ci_lab_local "
                "或 self_hosted_lab"
            )
        if not self.provider_allowed_ports or any(
            port < 1 or port > 65_535 for port in self.provider_allowed_ports
        ):
            raise RuntimeError("Provider 端口白名单无效")
        if any(
            not host
            or "://" in host
            or "/" in host
            or "*" in host
            or not _is_self_hosted_ip_literal(host)
            for host in self.provider_allowed_hosts
        ):
            raise RuntimeError(
                "Provider 主机白名单只能包含精确主机名或私网/环回 IP"
            )
        try:
            for network in self.provider_allowed_networks:
                if not _is_self_hosted_network(network):
                    raise RuntimeError(
                        "Provider 网络白名单只能包含自建私网或环回 CIDR"
                    )
        except ValueError as exc:
            raise RuntimeError("Provider 网络白名单必须使用规范 CIDR") from exc
        if any(
            re.fullmatch(r"QA_PROVIDER_SECRET_[A-Z0-9_]{1,109}", name) is None
            for name in self.provider_secret_env_names
        ):
            raise RuntimeError(
                "Provider Secret 白名单只能包含 QA_PROVIDER_SECRET_ 前缀的环境变量名"
            )
        if self.provider_runtime_mode == "ci_lab_local":
            if self.app_env not in {"local", "local-container", "test"}:
                raise RuntimeError(
                    "ci_lab_local 只允许本机、本地容器或隔离测试环境"
                )
            if self.provider_self_hosted_ownership_acknowledged:
                raise RuntimeError(
                    "ci_lab_local 不使用通用自建 Provider 归属确认"
                )
            if self.provider_allowed_hosts or self.provider_allowed_networks:
                raise RuntimeError(
                    "ci_lab_local 的主机和 CIDR 由代码固定，禁止自由白名单"
                )
            if self.provider_allowed_ports != (443,):
                raise RuntimeError(
                    "ci_lab_local 禁止自由端口白名单"
                )
            if self.provider_allow_loopback_http:
                raise RuntimeError(
                    "ci_lab_local 的 HTTP 例外由代码固定，禁止通用开关"
                )
            if self.provider_secret_env_names != (
                CI_LAB_PROVIDER_SECRET_NAME,
            ):
                raise RuntimeError(
                    "ci_lab_local 只允许 QA_PROVIDER_SECRET_CI_LAB"
                )
        if self.provider_runtime_mode == "self_hosted_lab":
            if not self.provider_self_hosted_ownership_acknowledged:
                raise RuntimeError("自建 Provider 实验室必须显式确认环境所有权")
            if not self.provider_allowed_hosts:
                raise RuntimeError("自建 Provider 实验室必须配置精确主机白名单")
            if not self.provider_allowed_networks:
                raise RuntimeError("自建 Provider 实验室必须配置精确 CIDR 白名单")
            if not self.provider_secret_env_names:
                raise RuntimeError(
                    "自建 Provider 实验室必须配置 Secret 环境变量白名单"
                )
            if self.app_env != "local-container":
                if any(
                    not _is_loopback_host(host)
                    for host in self.provider_allowed_hosts
                ):
                    raise RuntimeError(
                        "宿主机 self_hosted_lab 只允许 localhost 或环回 IP"
                    )
                if any(
                    not _is_loopback_network(network)
                    for network in self.provider_allowed_networks
                ):
                    raise RuntimeError(
                        "宿主机 self_hosted_lab 只允许环回 CIDR；"
                        "自建私网只能用于 local-container"
                    )
        _ = self.upload_root_path

    @property
    def database_path(self) -> Path:
        """返回供 SQLite 与流水线存储共用的规范化本机文件路径。"""

        if not _is_local_sqlite_url(self.database_url):
            raise RuntimeError("无法从非本机 SQLite URL 提取数据库路径")
        database = make_url(self.database_url).database
        if not database or database == ":memory:":
            raise RuntimeError("内存 SQLite 没有可供流水线复用的文件路径")
        return Path(database).expanduser().resolve()

    @property
    def upload_root_path(self) -> Path:
        """返回限制在本机数据目录内的附件根目录。"""

        raw = self.upload_root.strip()
        if not raw or raw.startswith(("\\\\", "//", "file:")):
            raise RuntimeError("UPLOAD_ROOT 必须是本机文件路径")
        if self.database_runtime_mode == "sqlite_local":
            try:
                storage_boundary = self.database_path.parent
            except RuntimeError:
                # In-memory SQLite is test-only and has no file parent.
                storage_boundary = (Path.cwd() / ".data").resolve()
        else:
            # PostgreSQL owns relational data, while phase 2 attachments remain
            # beneath one explicit local volume boundary.
            storage_boundary = self.local_data_root_path
        if raw == "./.data/uploads":
            return (storage_boundary / "uploads").resolve()

        resolved = Path(raw).expanduser().resolve()
        if (
            resolved == storage_boundary
            or not resolved.is_relative_to(storage_boundary)
        ):
            raise RuntimeError("UPLOAD_ROOT 必须位于本机数据目录的子目录中")
        return resolved

    @property
    def local_data_root_path(self) -> Path:
        """Resolve the non-relational local storage boundary without network paths."""

        raw = self.local_data_root.strip()
        if not raw or raw.startswith(("\\\\", "//", "file:")):
            raise RuntimeError("LOCAL_DATA_ROOT 必须是本机文件路径")
        return Path(raw).expanduser().resolve()


@lru_cache
def get_settings() -> Settings:
    return Settings.from_environment()
