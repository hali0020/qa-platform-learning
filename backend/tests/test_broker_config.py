import pytest

from app.core.config import (
    Settings,
    _is_local_rabbitmq_container_url,
    validate_broker_runtime_target,
)


VALID_BROKER_URL = (
    "amqp://qa_learning:lesson-secret@rabbitmq:5672/qa_platform_learning"
)


def test_broker_is_disabled_and_has_no_url_by_default() -> None:
    settings = Settings()

    assert settings.broker_runtime_mode == "disabled_local"
    assert settings.broker_url == ""
    settings.validate_local_safety()


def test_rabbitmq_mode_accepts_only_dedicated_internal_service_and_vhost() -> None:
    assert _is_local_rabbitmq_container_url(VALID_BROKER_URL)

    settings = Settings(
        app_env="local-container",
        broker_runtime_mode="rabbitmq_local_container",
        broker_url=VALID_BROKER_URL,
    )
    settings.validate_local_safety()


def test_percent_encoded_non_empty_credentials_are_accepted() -> None:
    encoded_url = (
        "amqp://qa%5Flearning:s%40fe@rabbitmq:5672/"
        "%71a_platform_learning"
    )

    assert _is_local_rabbitmq_container_url(encoded_url)


@pytest.mark.parametrize(
    "broker_url",
    [
        "amqps://qa:secret@rabbitmq:5672/qa_platform_learning",
        "AMQP://qa:secret@rabbitmq:5672/qa_platform_learning",
        "amqp://test_user:test_password@broker.example.test:5672/qa_platform_learning",
        "amqp://qa:secret@127.0.0.1:5672/qa_platform_learning",
        "amqp://qa:secret@localhost:5672/qa_platform_learning",
        "amqp://qa:secret@rabbitmq/qa_platform_learning",
        "amqp://qa:secret@rabbitmq:5673/qa_platform_learning",
        "amqp://qa:secret@rabbitmq:not-a-port/qa_platform_learning",
        "amqp://:secret@rabbitmq:5672/qa_platform_learning",
        "amqp://qa@rabbitmq:5672/qa_platform_learning",
        "amqp://%20:secret@rabbitmq:5672/qa_platform_learning",
        "amqp://qa:%09@rabbitmq:5672/qa_platform_learning",
        "amqp://qa:%00@rabbitmq:5672/qa_platform_learning",
        "amqp://qa:secret@rabbitmq:5672/",
        "amqp://qa:secret@rabbitmq:5672/%2F",
        "amqp://qa:secret@rabbitmq:5672/another_vhost",
        "amqp://qa:secret@rabbitmq:5672/qa_platform_learning;other",
        "amqp://qa:secret@rabbitmq:5672/qa_platform_learning?heartbeat=30",
        "amqp://qa:secret@rabbitmq:5672/qa_platform_learning#fragment",
        "amqp://qa:secret@rabbitmq:5672/qa_platform_learning?",
        "amqp://qa:secret@rabbitmq:5672/qa_platform_learning#",
        "amqp://qa:sec\\ret@rabbitmq:5672/qa_platform_learning",
        "amqp://qa:%ZZ@rabbitmq:5672/qa_platform_learning",
        " amqp://qa:secret@rabbitmq:5672/qa_platform_learning",
        "amqp://qa:secret@rabbitmq:5672/qa_platform_learning ",
    ],
)
def test_rabbitmq_mode_rejects_every_other_topology(broker_url: str) -> None:
    assert not _is_local_rabbitmq_container_url(broker_url)

    with pytest.raises(RuntimeError) as caught:
        validate_broker_runtime_target(
            broker_url=broker_url,
            runtime_mode="rabbitmq_local_container",
            app_env="local-container",
        )

    assert "lesson-secret" not in str(caught.value)
    assert broker_url not in str(caught.value)


def test_rabbitmq_mode_requires_explicit_local_container_environment() -> None:
    with pytest.raises(RuntimeError, match="APP_ENV=local-container"):
        validate_broker_runtime_target(
            broker_url=VALID_BROKER_URL,
            runtime_mode="rabbitmq_local_container",
            app_env="local",
        )


def test_disabled_mode_rejects_a_dormant_url() -> None:
    with pytest.raises(RuntimeError, match="禁止配置 BROKER_URL"):
        validate_broker_runtime_target(
            broker_url=VALID_BROKER_URL,
            runtime_mode="disabled_local",
            app_env="local",
        )


def test_unknown_broker_mode_fails_closed() -> None:
    with pytest.raises(RuntimeError, match="BROKER_RUNTIME_MODE"):
        validate_broker_runtime_target(
            broker_url=VALID_BROKER_URL,
            runtime_mode="external",
            app_env="local-container",
        )


def test_broker_settings_are_loaded_from_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "local-container")
    monkeypatch.setenv("BROKER_RUNTIME_MODE", "RABBITMQ_LOCAL_CONTAINER")
    monkeypatch.setenv("BROKER_URL", VALID_BROKER_URL)

    settings = Settings.from_environment()

    assert settings.broker_runtime_mode == "rabbitmq_local_container"
    assert settings.broker_url == VALID_BROKER_URL
    settings.validate_local_safety()


def test_broker_url_secret_is_excluded_from_settings_repr() -> None:
    settings = Settings(
        app_env="local-container",
        broker_runtime_mode="rabbitmq_local_container",
        broker_url=VALID_BROKER_URL,
    )

    rendered = repr(settings)

    assert "lesson-secret" not in rendered
    assert VALID_BROKER_URL not in rendered


def test_database_url_secret_is_excluded_from_settings_repr() -> None:
    database_url = (
        "postgresql+asyncpg://qa_learning:local-pg-test-secret@"
        "postgres:5432/qa_platform_learning"
    )
    settings = Settings(
        app_env="local-container",
        database_runtime_mode="postgres_local_container",
        database_url=database_url,
    )

    rendered = repr(settings)

    assert "local-pg-test-secret" not in rendered
    assert database_url not in rendered


def test_remote_broker_is_rejected_even_when_http_local_only_is_disabled() -> None:
    with pytest.raises(RuntimeError, match="rabbitmq:5672"):
        Settings(
            app_env="local-container",
            local_only=False,
            broker_runtime_mode="rabbitmq_local_container",
            broker_url=(
                "amqp://test_user:test_password@broker.example.test:5672/"
                "qa_platform_learning"
            ),
        ).validate_local_safety()
