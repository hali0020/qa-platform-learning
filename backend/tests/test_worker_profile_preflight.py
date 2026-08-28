from __future__ import annotations

from pathlib import Path

import pytest

from app.ops.worker_profile_preflight import (
    WorkerProfilePreflightError,
    load_worker_profile_environment,
    main,
    validate_worker_profile_environment,
)


def valid_environment() -> dict[str, str]:
    return {
        "COMPOSE_DATABASE_RUNTIME_MODE": "postgres_local_container",
        "COMPOSE_DATABASE_URL": (
            "postgresql+asyncpg://qa_user:db-local-pass@postgres:5432/qa_db"
        ),
        "POSTGRES_USER": "qa_user",
        "POSTGRES_PASSWORD": "db-local-pass",
        "POSTGRES_DB": "qa_db",
        "COMPOSE_BROKER_URL": (
            "amqp://qa_worker:rabbit%2Flocal%3Apass@rabbitmq:5672/"
            "qa_platform_learning"
        ),
        "RABBITMQ_DEFAULT_USER": "qa_worker",
        "RABBITMQ_DEFAULT_PASS": "rabbit/local:pass",
    }


def test_worker_profile_preflight_accepts_exact_local_targets() -> None:
    validate_worker_profile_environment(valid_environment())


def test_worker_profile_preflight_resolves_only_same_file_references(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("POSTGRES_PASSWORD", "ambient-secret-must-not-win")
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "COMPOSE_DATABASE_RUNTIME_MODE=postgres_local_container",
                "POSTGRES_USER=qa_user",
                "POSTGRES_PASSWORD=db-local-pass",
                "POSTGRES_DB=qa_db",
                "COMPOSE_DATABASE_URL=postgresql+asyncpg://${POSTGRES_USER}:"
                "${POSTGRES_PASSWORD}@postgres:5432/${POSTGRES_DB}",
                "RABBITMQ_DEFAULT_USER=qa_worker",
                "RABBITMQ_DEFAULT_PASS=rabbit-local-pass",
                "COMPOSE_BROKER_URL=amqp://${RABBITMQ_DEFAULT_USER}:"
                "${RABBITMQ_DEFAULT_PASS}@rabbitmq:5672/qa_platform_learning",
            )
        ),
        encoding="utf-8",
    )

    values = load_worker_profile_environment(env_file)
    validate_worker_profile_environment(values)


@pytest.mark.parametrize(
    ("name", "replacement", "message"),
    [
        (
            "COMPOSE_DATABASE_URL",
            "postgresql+asyncpg://qa_user:db-local-pass@localhost:5432/qa_db",
            "postgres:5432",
        ),
        ("POSTGRES_PASSWORD", "different-db-pass", "PostgreSQL"),
        (
            "COMPOSE_BROKER_URL",
            "amqp://qa_worker:rabbit-local-pass@localhost:5672/"
            "qa_platform_learning",
            "rabbitmq:5672",
        ),
        ("RABBITMQ_DEFAULT_PASS", "different-rabbit-pass", "RabbitMQ"),
    ],
)
def test_worker_profile_preflight_rejects_remote_or_mismatched_settings(
    name: str,
    replacement: str,
    message: str,
) -> None:
    values = valid_environment()
    values[name] = replacement

    with pytest.raises(WorkerProfilePreflightError, match=message):
        validate_worker_profile_environment(values)


def test_worker_profile_preflight_cli_never_prints_secret_values(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database_secret = "database-secret-never-print"
    broker_secret = "broker-secret-never-print"
    env_file = tmp_path / ".env"
    env_file.write_text(
        "\n".join(
            (
                "COMPOSE_DATABASE_RUNTIME_MODE=postgres_local_container",
                "POSTGRES_USER=qa_user",
                f"POSTGRES_PASSWORD={database_secret}",
                "POSTGRES_DB=qa_db",
                "COMPOSE_DATABASE_URL=postgresql+asyncpg://qa_user:wrong@"
                "postgres:5432/qa_db",
                "RABBITMQ_DEFAULT_USER=qa_worker",
                f"RABBITMQ_DEFAULT_PASS={broker_secret}",
                "COMPOSE_BROKER_URL=amqp://qa_worker:wrong@rabbitmq:5672/"
                "qa_platform_learning",
            )
        ),
        encoding="utf-8",
    )

    assert main(["--env-file", str(env_file)]) == 1
    captured = capsys.readouterr()
    combined = captured.out + captured.err
    assert database_secret not in combined
    assert broker_secret not in combined
