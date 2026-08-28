"""Keep committed configuration private and every runtime endpoint local."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from urllib.parse import urlsplit


ROOT = Path(__file__).resolve().parents[2]

_RUNTIME_PATHS = (
    ROOT / ".env.example",
    ROOT / "backend" / "app",
    ROOT / "backend" / "alembic",
    ROOT / "frontend" / "src",
    ROOT / "frontend" / "vite.config.ts",
    ROOT / ".github",
    ROOT / "infra" / "compose.phase2.yaml",
    ROOT / "infra" / "docker",
    ROOT / "infra" / "keycloak",
    ROOT / "infra" / "vault",
    ROOT / "scripts",
)
_RUNTIME_SUFFIXES = {
    ".conf",
    ".dockerfile",
    ".example",
    ".hcl",
    ".json",
    ".ps1",
    ".py",
    ".ts",
    ".vue",
    ".yaml",
    ".yml",
}
_TEMPLATE_VALUE = re.compile(r"\$\$?\{[^}\r\n]+\}|<[^>\r\n]+>")
_ENDPOINT = re.compile(
    r"\b(?:https?|wss?|amqps?|postgresql(?:\+[a-z0-9_]+)?|"
    r"mysql(?:\+[a-z0-9_]+)?|mariadb|mssql|oracle|sqlite\+aiosqlite|"
    r"rediss?|mongodb(?:\+srv)?|s?ftp|file|ldaps?|nats|kafka|s3)://"
    r"[A-Za-z0-9._~:/?#\[\]@!$&*+;=%()-]+",
    re.IGNORECASE,
)

# Exact endpoints owned by this repository. ``None`` is a local SQLite path.
_ALLOWED_TARGETS: set[tuple[str, str | None, int | None]] = {
    ("http", "127.0.0.1", 5173),
    ("http", "127.0.0.1", 8080),
    ("http", "127.0.0.1", 8200),
    ("http", "127.0.0.1", 23010),
    ("http", "127.0.0.1", 23020),
    ("http", "127.0.0.1", 23100),
    ("http", "localhost", 5173),
    ("http", "localhost", 23010),
    ("http", "172.30.60.2", 8080),
    ("http", "172.30.60.3", 23100),
    ("http", "backend", 23100),
    ("http", "keycloak", 8080),
    ("http", "keycloak_core", None),
    ("http", "seaweedfs", 8333),
    ("http", "seaweedfs_s3", None),
    ("http", "vault", 8200),
    ("http", "vault-core", 8200),
    ("http", "vault_core", None),
    ("https", "ci.lab.test", None),
    ("amqp", "rabbitmq", 5672),
    ("postgresql+asyncpg", "postgres", 5432),
}
_ALLOWED_SQLITE_LITERALS = {
    "sqlite+aiosqlite:///",
    "sqlite+aiosqlite:///./.data/qa.db",
    "sqlite+aiosqlite:////data/qa.db",
}


def _runtime_files() -> Iterable[Path]:
    for candidate in _RUNTIME_PATHS:
        if candidate.is_file():
            yield candidate
            continue
        for path in candidate.rglob("*"):
            if path.is_file() and path.suffix.lower() in _RUNTIME_SUFFIXES:
                yield path


def test_private_values_have_one_ignored_local_file() -> None:
    patterns = {
        line.strip()
        for line in (ROOT / ".gitignore").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#")
    }

    assert ".env*" in patterns
    assert "!/.env.example" in patterns
    assert "!.env.example" not in patterns
    assert not (ROOT / "frontend" / ".env.example").exists()

    example_values = {
        key.strip(): value.strip()
        for line in (ROOT / ".env.example").read_text(encoding="utf-8").splitlines()
        if line.strip() and not line.lstrip().startswith("#") and "=" in line
        for key, value in (line.split("=", 1),)
    }
    assert example_values["COMPOSE_DATABASE_RUNTIME_MODE"] == "sqlite_local"
    assert example_values["COMPOSE_DATABASE_URL"] == (
        "sqlite+aiosqlite:////data/qa.db"
    )
    for secret_name in (
        "POSTGRES_PASSWORD",
        "RABBITMQ_DEFAULT_PASS",
        "KEYCLOAK_BOOTSTRAP_ADMIN_PASSWORD",
        "VAULT_APP_TOKEN",
        "QA_PROVIDER_SECRET_CI_LAB",
        "OBJECT_STORAGE_ACCESS_KEY",
        "OBJECT_STORAGE_SECRET_KEY",
    ):
        assert example_values[secret_name] == ""


def test_committed_runtime_endpoints_are_project_owned_and_local() -> None:
    discovered: list[tuple[Path, str, tuple[str, str | None, int | None]]] = []

    for path in _runtime_files():
        content = path.read_text(encoding="utf-8")
        normalized = _TEMPLATE_VALUE.sub("placeholder", content)
        for match in _ENDPOINT.finditer(normalized):
            endpoint = match.group(0).rstrip(".;}")
            parsed = urlsplit(endpoint)
            target = (parsed.scheme.lower(), parsed.hostname, parsed.port)
            discovered.append((path, endpoint, target))

    assert discovered, "runtime endpoint inventory unexpectedly found nothing"
    unexpected: list[str] = []
    for path, endpoint, target in discovered:
        parsed = urlsplit(endpoint)
        is_script_local_sqlite = (
            path == ROOT / "scripts" / "start-ci-lab-source.ps1"
            and endpoint.startswith("sqlite+aiosqlite:///$($QaDatabasePath")
        )
        is_allowed_sqlite = (
            parsed.scheme.lower() == "sqlite+aiosqlite"
            and (
                endpoint in _ALLOWED_SQLITE_LITERALS
                or is_script_local_sqlite
            )
        )
        has_http_userinfo = (
            parsed.scheme.lower() in {"http", "https"}
            and (parsed.username is not None or parsed.password is not None)
        )
        if (
            not has_http_userinfo
            and (target in _ALLOWED_TARGETS or is_allowed_sqlite)
        ):
            continue
        unexpected.append(
            f"{path.relative_to(ROOT)}: {endpoint} -> {target}"
        )

    assert not unexpected, "unexpected committed runtime endpoint(s):\n" + "\n".join(
        unexpected
    )
