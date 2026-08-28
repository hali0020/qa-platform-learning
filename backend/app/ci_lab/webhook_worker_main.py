"""Fail-closed process entry point for the CI Lab webhook worker."""

from __future__ import annotations

import asyncio
import logging
import signal

from app.ci_lab.webhook_worker import build_worker, load_worker_config


async def _run() -> None:
    config = load_worker_config()
    worker, service = build_worker(config)
    stop_event = asyncio.Event()
    loop = asyncio.get_running_loop()
    for selected_signal in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(selected_signal, stop_event.set)
        except (NotImplementedError, RuntimeError):
            # Windows console delivery and test loops do not always implement
            # add_signal_handler. Process termination still runs finalizers.
            pass
    try:
        await service.initialize()
        await worker.run_forever(
            stop_event,
            poll_seconds=config.poll_seconds,
        )
    finally:
        await worker.close()
        await service.close()


def main() -> None:
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    asyncio.run(_run())


if __name__ == "__main__":
    main()


__all__ = ["main"]
