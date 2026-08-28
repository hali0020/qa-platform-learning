"""Validate the ignored Worker-profile environment without opening sockets.

The check deliberately reads one explicit dotenv file instead of importing the
application Settings object.  That keeps ambient shell variables out of the
comparison and prevents the application's automatic dotenv loader from
silently changing which credentials are being compared.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from urllib.parse import unquote, urlparse

from dotenv import dotenv_values
from sqlalchemy.engine import make_url
from sqlalchemy.exc import ArgumentError


DATABASE_RUNTIME_MODE = "postgres_local_container"
POSTGRES_HOST = "postgres"
POSTGRES_PORT = 5432
RABBITMQ_HOST = "rabbitmq"
RABBITMQ_PORT = 5672
RABBITMQ_VHOST = "qa_platform_learning"
_REFERENCE_PATTERN = re.compile(r"\$\{([A-Za-z_][A-Za-z0-9_]*)\}")
_INVALID_PERCENT_PATTERN = re.compile(r"%(?![0-9A-Fa-f]{2})")


class WorkerProfilePreflightError(RuntimeError):
    """A safe-to-print description of an invalid local profile setting."""


def _resolve_value(
    values: Mapping[str, str | None],
    name: str,
    stack: tuple[str, ...] = (),
) -> str:
    if name in stack:
        raise WorkerProfilePreflightError(
            f"{name} 包含循环的 dotenv 变量引用"
        )
    raw = values.get(name)
    if raw is None or raw == "":
        raise WorkerProfilePreflightError(f"{name} 未配置")

    def replace_reference(match: re.Match[str]) -> str:
        return _resolve_value(values, match.group(1), (*stack, name))

    resolved = _REFERENCE_PATTERN.sub(replace_reference, raw)
    if "$" in resolved:
        raise WorkerProfilePreflightError(
            f"{name} 含不受支持的 dotenv 变量语法"
        )
    if resolved != resolved.strip() or any(
        character in resolved for character in ("\r", "\n", "\x00")
    ):
        raise WorkerProfilePreflightError(f"{name} 包含空白或控制字符")
    return resolved


def load_worker_profile_environment(path: Path) -> dict[str, str | None]:
    if not path.is_file():
        raise WorkerProfilePreflightError(
            "未找到仓库根目录 .env；请先复制 .env.example"
        )
    try:
        # Resolve ${NAME} ourselves from this file only.  python-dotenv's
        # interpolation also consults ambient os.environ, which is not an
        # acceptable source of truth for this credential consistency check.
        return dict(
            dotenv_values(
                dotenv_path=path,
                encoding="utf-8",
                interpolate=False,
            )
        )
    except Exception:
        raise WorkerProfilePreflightError(".env 无法安全解析") from None


def _validate_database(values: Mapping[str, str | None]) -> None:
    if _resolve_value(values, "COMPOSE_DATABASE_RUNTIME_MODE") != (
        DATABASE_RUNTIME_MODE
    ):
        raise WorkerProfilePreflightError(
            "COMPOSE_DATABASE_RUNTIME_MODE 必须是 postgres_local_container"
        )

    database_url = _resolve_value(values, "COMPOSE_DATABASE_URL")
    try:
        parsed = make_url(database_url)
        port = parsed.port
    except (ArgumentError, TypeError, ValueError):
        raise WorkerProfilePreflightError(
            "COMPOSE_DATABASE_URL 格式无效"
        ) from None
    if not all(
        (
            parsed.drivername == "postgresql+asyncpg",
            parsed.host == POSTGRES_HOST,
            port == POSTGRES_PORT,
            parsed.username,
            parsed.password,
            parsed.database,
            not parsed.query,
        )
    ):
        raise WorkerProfilePreflightError(
            "COMPOSE_DATABASE_URL 必须精确指向 postgres:5432"
        )

    expected_user = _resolve_value(values, "POSTGRES_USER")
    expected_password = _resolve_value(values, "POSTGRES_PASSWORD")
    expected_database = _resolve_value(values, "POSTGRES_DB")
    if (
        parsed.username != expected_user
        or parsed.password != expected_password
        or parsed.database != expected_database
    ):
        raise WorkerProfilePreflightError(
            "PostgreSQL 服务端与客户端账号、密码或数据库名不一致"
        )


def _validate_broker(values: Mapping[str, str | None]) -> None:
    broker_url = _resolve_value(values, "COMPOSE_BROKER_URL")
    if (
        broker_url != broker_url.strip()
        or not broker_url.startswith("amqp://")
        or "?" in broker_url
        or "#" in broker_url
        or "\\" in broker_url
        or _INVALID_PERCENT_PATTERN.search(broker_url) is not None
    ):
        raise WorkerProfilePreflightError("COMPOSE_BROKER_URL 格式无效")
    try:
        parsed = urlparse(broker_url)
        port = parsed.port
        username = unquote(parsed.username or "", errors="strict")
        password = unquote(parsed.password or "", errors="strict")
        vhost = unquote(parsed.path, errors="strict")
    except (UnicodeDecodeError, ValueError):
        raise WorkerProfilePreflightError(
            "COMPOSE_BROKER_URL 格式无效"
        ) from None
    if not all(
        (
            parsed.scheme == "amqp",
            parsed.hostname == RABBITMQ_HOST,
            port == RABBITMQ_PORT,
            username,
            password,
            vhost == f"/{RABBITMQ_VHOST}",
            not parsed.params,
            not parsed.query,
            not parsed.fragment,
        )
    ):
        raise WorkerProfilePreflightError(
            "COMPOSE_BROKER_URL 必须精确指向 "
            "rabbitmq:5672/qa_platform_learning"
        )

    expected_user = _resolve_value(values, "RABBITMQ_DEFAULT_USER")
    expected_password = _resolve_value(values, "RABBITMQ_DEFAULT_PASS")
    if username != expected_user or password != expected_password:
        raise WorkerProfilePreflightError(
            "RabbitMQ 服务端与客户端账号或密码不一致"
        )


def validate_worker_profile_environment(
    values: Mapping[str, str | None],
) -> None:
    """Validate exact local targets and decoded credential equality."""

    _validate_database(values)
    _validate_broker(values)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Validate the isolated local Worker profile environment.",
    )
    parser.add_argument("--env-file", type=Path, required=True)
    arguments = parser.parse_args(argv)
    try:
        values = load_worker_profile_environment(arguments.env_file)
        validate_worker_profile_environment(values)
    except WorkerProfilePreflightError as error:
        print(f"Worker profile 预检失败：{error}", file=sys.stderr)
        return 1
    except Exception:
        # Parser/URL exceptions may embed credentials.  Keep the unexpected
        # path generic too, rather than printing exception text or repr(values).
        print("Worker profile 预检失败：本机配置无法安全验证", file=sys.stderr)
        return 1
    print("Worker profile 预检通过：数据库与 Broker 均为本机容器边界。")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())


__all__ = [
    "WorkerProfilePreflightError",
    "load_worker_profile_environment",
    "main",
    "validate_worker_profile_environment",
]
