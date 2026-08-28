"""Process entry point for transactional task wake-up publication.

The database outbox is authoritative.  RabbitMQ receives only the existing
content-free hint, so a publish-confirm/database-finalize crash window can
produce a harmless duplicate without leaking task data into the broker.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from collections.abc import Mapping
from dataclasses import dataclass

from app.broker.base import WakeupPublisher
from app.broker.factory import build_wakeup_publisher
from app.core.config import Settings, get_settings
from app.database.session import Database
from app.runtime.service import PersistentRuntimeService, create_runtime_service


@dataclass(frozen=True, slots=True)
class OutboxDispatcherOptions:
    dispatcher_id: str
    lease_seconds: int = 30
    poll_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.dispatcher_id.strip() or len(self.dispatcher_id) > 200:
            raise ValueError("outbox dispatcher id must contain 1-200 characters")
        if not 5 <= self.lease_seconds <= 3600:
            raise ValueError("outbox lease must be between 5 and 3600 seconds")
        if not 0.05 <= self.poll_seconds <= 60:
            raise ValueError("outbox poll interval must be between 0.05 and 60 seconds")


def build_outbox_dispatcher_options(
    environ: Mapping[str, str] | None = None,
) -> OutboxDispatcherOptions:
    values = environ if environ is not None else os.environ
    dispatcher_id = values.get("OUTBOX_DISPATCHER_ID", "").strip() or values.get(
        "HOSTNAME", ""
    ).strip() or "task-outbox-local"
    try:
        lease_seconds = int(values.get("OUTBOX_LEASE_SECONDS", "30"))
        poll_seconds = float(values.get("OUTBOX_POLL_SECONDS", "2"))
    except ValueError as error:
        raise RuntimeError("outbox dispatcher 环境参数格式无效") from error
    return OutboxDispatcherOptions(
        dispatcher_id=dispatcher_id,
        lease_seconds=lease_seconds,
        poll_seconds=poll_seconds,
    )


class TaskWakeupOutboxDispatcher:
    def __init__(
        self,
        service: PersistentRuntimeService,
        publisher: WakeupPublisher,
        database: Database,
        options: OutboxDispatcherOptions,
    ) -> None:
        self.service = service
        self.publisher = publisher
        self.database = database
        self.options = options
        self._stop = asyncio.Event()
        self._logger = logging.getLogger("qa.task_wakeup_outbox")

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    dispatched = await self.service.dispatch_task_wakeup_once(
                        dispatcher_id=self.options.dispatcher_id,
                        publisher=self.publisher,
                        lease_seconds=self.options.lease_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Database/transport exceptions can contain connection
                    # strings.  Keep process logs metadata-only.
                    self._logger.error(
                        "task wake-up outbox dispatch failed error_type=%s",
                        type(error).__name__,
                    )
                    dispatched = None
                if not dispatched:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.options.poll_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            try:
                await self.publisher.close()
            finally:
                try:
                    await self.service.shutdown()
                finally:
                    await self.database.shutdown()


async def build_outbox_dispatcher(
    settings: Settings | None = None,
    *,
    environ: Mapping[str, str] | None = None,
    publisher: WakeupPublisher | None = None,
) -> TaskWakeupOutboxDispatcher:
    current = settings or get_settings()
    current.validate_local_safety()
    if current.app_env != "local-container":
        raise RuntimeError("独立 Outbox Dispatcher 只允许 APP_ENV=local-container")
    if current.database_runtime_mode != "postgres_local_container":
        raise RuntimeError("独立 Outbox Dispatcher 只允许 postgres_local_container")
    if current.broker_runtime_mode != "rabbitmq_local_container":
        raise RuntimeError("独立 Outbox Dispatcher 只允许 rabbitmq_local_container")

    options = build_outbox_dispatcher_options(environ)
    database = Database(
        current.database_url,
        runtime_mode=current.database_runtime_mode,
        app_env=current.app_env,
        schema_mode="verify",
    )
    service: PersistentRuntimeService | None = None
    selected_publisher: WakeupPublisher | None = None
    try:
        service = create_runtime_service(database, current)
        await service.initialize()
        selected_publisher = publisher or build_wakeup_publisher(current)
        return TaskWakeupOutboxDispatcher(
            service,
            selected_publisher,
            database,
            options,
        )
    except BaseException:
        try:
            if selected_publisher is not None and publisher is None:
                await selected_publisher.close()
        finally:
            try:
                if service is not None:
                    await service.shutdown()
            finally:
                await database.shutdown()
        raise


def _install_signal_handlers(dispatcher: TaskWakeupOutboxDispatcher) -> None:
    loop = asyncio.get_running_loop()
    for selected in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected, dispatcher.request_stop)
        except NotImplementedError:
            signal.signal(
                selected,
                lambda *_args: loop.call_soon_threadsafe(dispatcher.request_stop),
            )


async def _run() -> None:
    dispatcher = await build_outbox_dispatcher()
    _install_signal_handlers(dispatcher)
    await dispatcher.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()


__all__ = [
    "OutboxDispatcherOptions",
    "TaskWakeupOutboxDispatcher",
    "build_outbox_dispatcher",
    "build_outbox_dispatcher_options",
    "main",
]
