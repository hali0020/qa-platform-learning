from __future__ import annotations

from app.broker.base import WakeupPublisher, WakeupSource
from app.broker.local import DisabledLocalWakeupBroker
from app.broker.rabbitmq import (
    AioPikaLoader,
    RabbitMQWakeupPublisher,
    RabbitMQWakeupSource,
)
from app.core.config import Settings, validate_broker_runtime_target


def _validate_settings(settings: Settings) -> None:
    validate_broker_runtime_target(
        broker_url=settings.broker_url,
        runtime_mode=settings.broker_runtime_mode,
        app_env=settings.app_env,
    )


def build_wakeup_publisher(
    settings: Settings,
    *,
    aio_pika_loader: AioPikaLoader | None = None,
) -> WakeupPublisher:
    _validate_settings(settings)
    if settings.broker_runtime_mode == "disabled_local":
        return DisabledLocalWakeupBroker()
    return RabbitMQWakeupPublisher(
        settings.broker_url,
        app_env=settings.app_env,
        aio_pika_loader=aio_pika_loader,
    )


def build_wakeup_source(
    settings: Settings,
    *,
    aio_pika_loader: AioPikaLoader | None = None,
) -> WakeupSource:
    _validate_settings(settings)
    if settings.broker_runtime_mode == "disabled_local":
        return DisabledLocalWakeupBroker()
    return RabbitMQWakeupSource(
        settings.broker_url,
        app_env=settings.app_env,
        aio_pika_loader=aio_pika_loader,
    )


__all__ = ["build_wakeup_publisher", "build_wakeup_source"]
