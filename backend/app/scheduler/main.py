"""Process entry point for the PostgreSQL-backed independent Scheduler."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.database.session import Database
from app.runtime.service import PersistentRuntimeService, create_runtime_service
from app.scheduler.backend import RuntimeScheduleBackend
from app.scheduler.runner import SchedulerOptions, SchedulerRunner


def _float_setting(
    environ: Mapping[str, str],
    name: str,
    default: str,
) -> float:
    try:
        return float(environ.get(name, default))
    except ValueError as error:
        raise RuntimeError(f"{name} 必须是数字") from error


def _int_setting(
    environ: Mapping[str, str],
    name: str,
    default: str,
) -> int:
    try:
        return int(environ.get(name, default))
    except ValueError as error:
        raise RuntimeError(f"{name} 必须是整数") from error


def build_scheduler_options(
    environ: Mapping[str, str] | None = None,
) -> SchedulerOptions:
    values = environ if environ is not None else os.environ
    scheduler_id = values.get("SCHEDULER_ID", "").strip() or values.get(
        "HOSTNAME", ""
    ).strip()
    return SchedulerOptions(
        scheduler_id=scheduler_id,
        lease_seconds=_int_setting(values, "SCHEDULER_LEASE_SECONDS", "30"),
        poll_interval_seconds=_float_setting(
            values,
            "SCHEDULER_POLL_SECONDS",
            "5",
        ),
    )


@dataclass(slots=True)
class SchedulerApplication:
    runner: SchedulerRunner
    service: PersistentRuntimeService
    database: Database

    def request_stop(self) -> None:
        self.runner.request_stop()

    async def run(self) -> None:
        try:
            await self.runner.run()
        finally:
            await self.service.shutdown()
            await self.database.shutdown()


async def build_scheduler_from_settings(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
) -> SchedulerApplication:
    current = settings or get_settings()
    current.validate_local_safety()
    if current.app_env != "local-container":
        raise RuntimeError("独立 Scheduler 只允许 APP_ENV=local-container")
    if current.database_runtime_mode != "postgres_local_container":
        raise RuntimeError(
            "独立 Scheduler 只允许 postgres_local_container；"
            "SQLite 仅保留教学单进程 tick"
        )

    options = build_scheduler_options(environ)
    database = Database(
        current.database_url,
        echo=False,
        runtime_mode=current.database_runtime_mode,
        app_env=current.app_env,
        schema_mode="verify",
    )
    try:
        service = create_runtime_service(database, current)
        await service.initialize()
        runner = SchedulerRunner(RuntimeScheduleBackend(service), options)
        return SchedulerApplication(
            runner=runner,
            service=service,
            database=database,
        )
    except BaseException:
        await database.shutdown()
        raise


def _install_signal_handlers(application: SchedulerApplication) -> None:
    loop = asyncio.get_running_loop()
    for selected_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected_signal, application.request_stop)
        except NotImplementedError:
            signal.signal(
                selected_signal,
                lambda *_args: loop.call_soon_threadsafe(
                    application.request_stop
                ),
            )


async def _run() -> None:
    application = await build_scheduler_from_settings()
    _install_signal_handlers(application)
    await application.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()


__all__ = [
    "SchedulerApplication",
    "build_scheduler_from_settings",
    "build_scheduler_options",
    "main",
]
