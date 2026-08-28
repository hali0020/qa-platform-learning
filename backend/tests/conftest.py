"""Keep tests independent from the developer's ignored local environment."""

from __future__ import annotations

import os


_APPLICATION_ENV_NAMES = {
    "AWS_EC2_METADATA_DISABLED",
    "CORS_ORIGINS",
    "DEBUG",
    "HOST",
    "LOCAL_ONLY",
    "PORT",
}
_APPLICATION_ENV_PREFIXES = (
    "APP_",
    "AUTH_",
    "BROKER_",
    "COMPOSE_",
    "CSRF_",
    "DATABASE_",
    "IMAGE_",
    "KEYCLOAK_",
    "METRICS_",
    "OBJECT_STORAGE_",
    "OIDC_",
    "PASSWORD_",
    "POSTGRES_",
    "PROVIDER_",
    "QA_PROVIDER_SECRET_",
    "RABBITMQ_",
    "REQUEST_LOGGING_",
    "SECRET_STORE_",
    "SESSION_",
    "UPLOAD_",
    "VAULT_",
    "WORKER_",
)

# A developer may have exported private lab settings in their shell or IDE.
# Remove only this application's namespace before test modules are imported;
# individual tests use monkeypatch when they need a specific environment.
for _name in tuple(os.environ):
    if _name in _APPLICATION_ENV_NAMES or _name.startswith(
        _APPLICATION_ENV_PREFIXES
    ):
        os.environ.pop(_name, None)

os.environ["QA_PLATFORM_SKIP_LOCAL_ENV"] = "1"
