"""One-shot Alembic migration Job for the isolated Compose deployment."""

from __future__ import annotations

import logging

from sqlalchemy.engine import make_url

from app.core.config import Settings, get_settings
from app.migrations.runner import upgrade_schema


LOGGER = logging.getLogger("qa.migration_job")


def run_migration_job(settings: Settings | None = None) -> tuple[str, ...]:
    """Apply schema revisions only inside an explicitly local container.

    ``Settings`` and Alembic independently validate the exact SQLite or
    project-owned ``postgres:5432`` target.  A memory database is rejected
    because its schema would disappear when this one-shot process exits.
    """

    current = settings or get_settings()
    current.validate_local_safety()
    if current.app_env != "local-container":
        raise RuntimeError("migration Job 只允许 APP_ENV=local-container")
    parsed = make_url(current.database_url)
    if parsed.get_backend_name() == "sqlite" and parsed.database == ":memory:":
        raise RuntimeError("migration Job 不允许内存 SQLite")
    return upgrade_schema(
        database_url=current.database_url,
        runtime_mode=current.database_runtime_mode,
        app_env=current.app_env,
        # Preserve this Job's redacted logger instead of letting Alembic's
        # fileConfig disable loggers created before env.py is loaded.
        configure_logger=False,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    # Alembic's ``fileConfig`` may have disabled loggers that were created by
    # an earlier in-process migration (notably in the full pytest suite).
    # Re-enable this stable, redacted process logger before emitting status.
    LOGGER.disabled = False
    try:
        heads = run_migration_job()
    except Exception as error:
        # Connection exceptions can contain URLs or credentials.  Emit only a
        # stable error class and return a failed Job exit code.
        LOGGER.error(
            "database migration failed error_type=%s",
            type(error).__name__,
        )
        raise SystemExit(1) from None
    LOGGER.info("database migration completed heads=%s", ",".join(heads))


if __name__ == "__main__":
    main()


__all__ = ["main", "run_migration_job"]
