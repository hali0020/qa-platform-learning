"""Small Alembic boundary shared by the migration Job and local startup.

The target is validated again by ``alembic/env.py`` before a connection is
opened.  Keeping configuration construction here avoids subtly different
rules between the standalone Job and the backwards-compatible source mode.
"""

from __future__ import annotations

from pathlib import Path

from alembic import command
from alembic.config import Config
from alembic.script import ScriptDirectory


SCHEMA_MODE_UPGRADE = "upgrade"
SCHEMA_MODE_VERIFY = "verify"
SCHEMA_MODES = frozenset({SCHEMA_MODE_UPGRADE, SCHEMA_MODE_VERIFY})


def build_alembic_config(
    *,
    database_url: str,
    runtime_mode: str,
    app_env: str,
    configure_logger: bool,
) -> Config:
    backend_root = Path(__file__).resolve().parents[2]
    config = Config(str(backend_root / "alembic.ini"))
    config.set_main_option("script_location", str(backend_root / "alembic"))
    config.attributes["database_url"] = database_url
    config.attributes["runtime_mode"] = runtime_mode
    config.attributes["app_env"] = app_env
    config.attributes["configure_logger"] = configure_logger
    return config


def expected_schema_heads(config: Config) -> tuple[str, ...]:
    """Return repository heads without opening the configured database."""

    return tuple(sorted(ScriptDirectory.from_config(config).get_heads()))


def upgrade_schema(
    *,
    database_url: str,
    runtime_mode: str,
    app_env: str,
    configure_logger: bool = False,
) -> tuple[str, ...]:
    """Upgrade one already-validated local database and return expected heads."""

    config = build_alembic_config(
        database_url=database_url,
        runtime_mode=runtime_mode,
        app_env=app_env,
        configure_logger=configure_logger,
    )
    command.upgrade(config, "head")
    return expected_schema_heads(config)


__all__ = [
    "SCHEMA_MODES",
    "SCHEMA_MODE_UPGRADE",
    "SCHEMA_MODE_VERIFY",
    "build_alembic_config",
    "expected_schema_heads",
    "upgrade_schema",
]
