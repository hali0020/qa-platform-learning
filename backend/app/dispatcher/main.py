"""Process entry point for provider trigger intents.

The database claim and finalization transactions are deliberately separated
from provider HTTP.  A crashed dispatcher leaves a lease that another process
can recover; Learning CI retries reuse the same idempotency key.
"""

from __future__ import annotations

import asyncio
import logging
import os
import signal
from dataclasses import dataclass

from app.core.config import Settings, get_settings
from app.database.session import Database
from app.runtime.service import PersistentRuntimeService, create_runtime_service
from app.secrets import build_secret_store


@dataclass(frozen=True, slots=True)
class DispatcherOptions:
    worker_id: str
    lease_seconds: int = 30
    poll_seconds: float = 2.0

    def __post_init__(self) -> None:
        if not self.worker_id.strip() or len(self.worker_id) > 200:
            raise ValueError("dispatcher worker_id must contain 1-200 characters")
        if not 5 <= self.lease_seconds <= 3600:
            raise ValueError("dispatcher lease must be between 5 and 3600 seconds")
        if not 0.05 <= self.poll_seconds <= 60:
            raise ValueError("dispatcher poll interval must be between 0.05 and 60 seconds")


def dispatcher_options() -> DispatcherOptions:
    worker_id = os.environ.get("PROVIDER_DISPATCHER_ID", "").strip() or os.environ.get(
        "HOSTNAME", ""
    ).strip() or "provider-dispatcher-local"
    try:
        lease = int(os.environ.get("PROVIDER_DISPATCHER_LEASE_SECONDS", "30"))
        poll = float(os.environ.get("PROVIDER_DISPATCHER_POLL_SECONDS", "2"))
    except ValueError as error:
        raise RuntimeError("dispatcher 环境参数格式无效") from error
    return DispatcherOptions(worker_id, lease, poll)


class ProviderDispatcher:
    def __init__(
        self,
        service: PersistentRuntimeService,
        database: Database,
        options: DispatcherOptions,
    ) -> None:
        self.service = service
        self.database = database
        self.options = options
        self._stop = asyncio.Event()
        self._logger = logging.getLogger("qa.provider_dispatcher")

    def request_stop(self) -> None:
        self._stop.set()

    async def run(self) -> None:
        try:
            while not self._stop.is_set():
                try:
                    dispatched = await self.service.dispatch_provider_trigger_once(
                        self.options.worker_id,
                        self.options.lease_seconds,
                    )
                except asyncio.CancelledError:
                    raise
                except Exception as error:
                    # Provider errors can contain URLs or credential metadata.
                    self._logger.error(
                        "provider dispatch failed error_type=%s",
                        type(error).__name__,
                    )
                    dispatched = None
                if dispatched is None:
                    try:
                        await asyncio.wait_for(
                            self._stop.wait(),
                            timeout=self.options.poll_seconds,
                        )
                    except asyncio.TimeoutError:
                        pass
        finally:
            try:
                await self.service.shutdown()
            finally:
                await self.database.shutdown()


async def build_dispatcher(
    settings: Settings | None = None,
) -> ProviderDispatcher:
    current = settings or get_settings()
    current.validate_local_safety()
    if current.app_env != "local-container":
        raise RuntimeError("独立 Dispatcher 只允许 APP_ENV=local-container")
    if current.database_runtime_mode != "postgres_local_container":
        raise RuntimeError("独立 Dispatcher 只允许 postgres_local_container")
    database = Database(
        current.database_url,
        runtime_mode=current.database_runtime_mode,
        app_env=current.app_env,
        schema_mode="verify",
    )
    try:
        service = create_runtime_service(
            database,
            current,
            secret_store=build_secret_store(current),
        )
        await service.initialize()
        return ProviderDispatcher(service, database, dispatcher_options())
    except BaseException:
        await database.shutdown()
        raise


def _install_signal_handlers(dispatcher: ProviderDispatcher) -> None:
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
    dispatcher = await build_dispatcher()
    _install_signal_handlers(dispatcher)
    await dispatcher.run()


def main() -> None:
    logging.basicConfig(level=logging.INFO)
    asyncio.run(_run())


if __name__ == "__main__":
    main()


__all__ = [
    "DispatcherOptions",
    "ProviderDispatcher",
    "build_dispatcher",
    "dispatcher_options",
    "main",
]
