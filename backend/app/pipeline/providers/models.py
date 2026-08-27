from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, field_validator

from app.pipeline.models import PipelineStatus


class ProviderKind(str, Enum):
    LOCAL = "local"
    LEARNING_CI = "learning_ci"
    JENKINS = "jenkins"
    GITLAB = "gitlab"
    BK_CI = "bk_ci"


class ProviderTriggerRequest(BaseModel):
    """Provider-neutral trigger intent.

    ``definition_ref`` is an identifier already bound to a configured
    provider. It is never interpreted as a URL. Secrets must not be placed in
    ``variables`` because requests can be persisted by the orchestration layer.
    """

    definition_ref: str = Field(min_length=1, max_length=300)
    ref: str | None = Field(default=None, min_length=1, max_length=300)
    variables: dict[str, str] = Field(default_factory=dict, repr=False)
    correlation_id: str | None = Field(default=None, min_length=1, max_length=200)

    @field_validator("definition_ref", "ref", "correlation_id")
    @classmethod
    def no_control_characters(cls, value: str | None) -> str | None:
        if value is not None and any(ord(character) < 32 for character in value):
            raise ValueError("provider identifiers cannot contain control characters")
        return value

    @field_validator("variables")
    @classmethod
    def bounded_variables(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > 100:
            raise ValueError("at most 100 provider variables are allowed")
        sensitive = (
            "secret",
            "token",
            "password",
            "passwd",
            "credential",
            "api_key",
            "apikey",
            "private_key",
            "authorization",
        )
        for key, item in value.items():
            if any(marker in key.casefold() for marker in sensitive):
                raise ValueError(
                    "provider variables cannot contain credential-like keys"
                )
            if not key or len(key) > 128 or len(item) > 4096:
                raise ValueError("provider variable key or value is too large")
            if any(ord(character) < 32 for character in key):
                raise ValueError("provider variable keys cannot contain control characters")
        return value


class ProviderRun(BaseModel):
    provider: ProviderKind
    external_id: str = Field(min_length=1, max_length=300)
    status: PipelineStatus
    raw_status: str = Field(default="", max_length=100)
    web_url: str | None = Field(default=None, max_length=2048)
    message: str | None = Field(default=None, max_length=500)
    metadata: dict[str, Any] = Field(default_factory=dict)


__all__ = ["ProviderKind", "ProviderRun", "ProviderTriggerRequest"]
