"""Thin process entry point for the isolated, scalable Worker service."""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.database.session import Database
from app.runtime.service import create_runtime_service
from app.worker.backend import RuntimeTaskLeaseBackend
from app.worker.contracts import WakeupSource
from app.worker.handlers import build_safe_handler_registry
from app.worker.runner import WorkerOptions, WorkerRunner


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


def build_worker_options(
    environ: Mapping[str, str] | None = None,
) -> WorkerOptions:
    values = environ if environ is not None else os.environ
    worker_id = values.get("WORKER_ID", "").strip() or values.get(
        "HOSTNAME", ""
    ).strip()
    queues = tuple(
        item.strip()
        for item in values.get("WORKER_QUEUES", "default").split(",")
        if item.strip()
    )
    return WorkerOptions(
        worker_id=worker_id,
        queues=queues,
        lease_seconds=_int_setting(values, "WORKER_LEASE_SECONDS", "30"),
        heartbeat_interval_seconds=_float_setting(
            values,
            "WORKER_HEARTBEAT_SECONDS",
            "10",
        ),
        poll_interval_seconds=_float_setting(
            values,
            "WORKER_POLL_SECONDS",
            "5",
        ),
        shutdown_grace_seconds=_float_setting(
            values,
            "WORKER_SHUTDOWN_GRACE_SECONDS",
            "30",
        ),
    )


def _build_broker_wakeup_source(settings: Settings) -> WakeupSource:
    """Single integration seam for the fixed local RabbitMQ adapter."""

    from app.broker.factory import build_wakeup_source

    return build_wakeup_source(settings)


@dataclass(slots=True)
class WorkerApplication:
    runner: WorkerRunner
    database: Database

    def request_stop(self) -> None:
        self.runner.request_stop()

    async def run(self) -> None:
        try:
            await self.runner.run()
        finally:
            await self.database.shutdown()


async def build_worker_from_settings(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    wakeup_source: WakeupSource | None = None,
) -> WorkerApplication:
    current = settings or get_settings()
    current.validate_local_safety()
    if current.app_env != "local-container":
        raise RuntimeError("Worker 只允许 APP_ENV=local-container")
    if current.database_runtime_mode != "postgres_local_container":
        raise RuntimeError("Worker 只允许 postgres_local_container")
    if current.broker_runtime_mode != "rabbitmq_local_container":
        raise RuntimeError("Worker 只允许 rabbitmq_local_container")

    database = Database(
        current.database_url,
        # SQL echo can expose task payloads; Worker logs remain metadata-only.
        echo=False,
        runtime_mode=current.database_runtime_mode,
        app_env=current.app_env,
        schema_mode="verify",
    )
    try:
        service = create_runtime_service(database, current)
        await service.initialize()
        source = (
            wakeup_source
            if wakeup_source is not None
            else _build_broker_wakeup_source(current)
        )
        runner = WorkerRunner(
            RuntimeTaskLeaseBackend(service),
            source,
            build_safe_handler_registry(),
            build_worker_options(environ),
        )
        return WorkerApplication(runner=runner, database=database)
    except BaseException:
        await database.shutdown()
        raise


def _install_signal_handlers(application: WorkerApplication) -> None:
    loop = asyncio.get_running_loop()
    for selected_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected_signal, application.request_stop)
        except NotImplementedError:
            signal.signal(
                selected_signal,
                lambda *_args: loop.call_soon_threadsafe(application.request_stop),
            )


async def _run() -> None:
    application = await build_worker_from_settings()
    _install_signal_handlers(application)
    await application.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()


__all__ = [
    "WorkerApplication",
    "build_worker_from_settings",
    "build_worker_options",
    "main",
]
