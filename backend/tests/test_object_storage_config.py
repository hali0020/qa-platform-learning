from __future__ import annotations

import builtins

import pytest

from app.core.config import Settings


def s3_settings(**overrides: object) -> Settings:
    values: dict[str, object] = {
        "app_env": "local-container",
        "object_storage_runtime_mode": "s3_local_container",
        "object_storage_endpoint_url": "http://seaweedfs:8333",
        "object_storage_bucket": "qa-artifacts",
        "object_storage_region": "us-east-1",
        "object_storage_access_key": "qa-storage-access",
        "object_storage_secret_key": "qa-storage-secret",
    }
    values.update(overrides)
    return Settings(**values)


def test_default_local_storage_never_imports_s3_sdk(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    original_import = builtins.__import__

    def guarded_import(name: str, *args: object, **kwargs: object):
        if name.startswith(("aiobotocore", "botocore")):
            raise AssertionError("local_filesystem must not import an S3 SDK")
        return original_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", guarded_import)
    settings = Settings()

    settings.validate_local_safety()
    assert settings.object_storage_runtime_mode == "local_filesystem"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_storage_runtime_mode", "external_s3"),
        ("app_env", "local"),
        ("app_env", "test"),
        ("object_storage_endpoint_url", "https://s3.amazonaws.com"),
        ("object_storage_endpoint_url", "http://127.0.0.1:8333"),
        ("object_storage_endpoint_url", "http://seaweedfs:8333/"),
        ("object_storage_bucket", "other-bucket"),
        ("object_storage_region", "cn-north-1"),
        ("object_storage_access_key", ""),
        ("object_storage_secret_key", ""),
        ("object_storage_access_key", " qa-storage-access"),
        ("object_storage_secret_key", "qa-storage-secret\n"),
        ("object_storage_max_concurrency", 0),
        ("object_storage_max_concurrency", 17),
        ("object_storage_operation_timeout_seconds", 0),
        ("object_storage_operation_timeout_seconds", 61),
    ],
)
def test_s3_mode_rejects_every_other_topology_or_unsafe_value(
    field: str,
    value: object,
) -> None:
    with pytest.raises(RuntimeError):
        s3_settings(**{field: value}).validate_local_safety()


def test_s3_mode_accepts_only_the_dedicated_internal_service() -> None:
    settings = s3_settings()

    settings.validate_local_safety()
    assert settings.object_storage_endpoint_url == "http://seaweedfs:8333"
    assert settings.object_storage_bucket == "qa-artifacts"


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("object_storage_endpoint_url", "http://seaweedfs:8333"),
        ("object_storage_bucket", "qa-artifacts"),
        ("object_storage_access_key", "dormant-access"),
        ("object_storage_secret_key", "dormant-secret"),
    ],
)
def test_local_mode_rejects_dormant_s3_configuration(
    field: str,
    value: str,
) -> None:
    with pytest.raises(RuntimeError, match="local_filesystem"):
        Settings(**{field: value}).validate_local_safety()


def test_object_storage_credentials_are_hidden_from_settings_repr() -> None:
    settings = s3_settings(
        object_storage_access_key="repr-access-marker",
        object_storage_secret_key="repr-secret-marker",
    )

    rendered = repr(settings)
    assert "repr-access-marker" not in rendered
    assert "repr-secret-marker" not in rendered


def test_object_storage_environment_names_load_without_trimming_credentials(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("APP_ENV", "local-container")
    monkeypatch.setenv("OBJECT_STORAGE_RUNTIME_MODE", "s3_local_container")
    monkeypatch.setenv("OBJECT_STORAGE_ENDPOINT_URL", "http://seaweedfs:8333")
    monkeypatch.setenv("OBJECT_STORAGE_BUCKET", "qa-artifacts")
    monkeypatch.setenv("OBJECT_STORAGE_ACCESS_KEY", " leading-space")
    monkeypatch.setenv("OBJECT_STORAGE_SECRET_KEY", "qa-storage-secret")

    settings = Settings.from_environment()

    assert settings.object_storage_access_key == " leading-space"
    with pytest.raises(RuntimeError, match="ACCESS_KEY"):
        settings.validate_local_safety()
