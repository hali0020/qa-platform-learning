from datetime import datetime
from enum import Enum
import re
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator

from app.pipeline.models import PipelineStatus


class ProviderKind(str, Enum):
    LOCAL = "local"
    LEARNING_CI = "learning_ci"
    JENKINS = "jenkins"
    GITLAB = "gitlab"
    BK_CI = "bk_ci"


class ProviderGateDecision(str, Enum):
    APPROVE = "approve"
    REJECT = "reject"


class ProviderQualityGateStatus(str, Enum):
    NOT_REQUIRED = "not_required"
    EVALUATING = "evaluating"
    WAITING_APPROVAL = "waiting_approval"
    APPROVED = "approved"
    REJECTED = "rejected"
    FAILED = "failed"
    CANCELLED = "cancelled"


class ProviderGateDecisionRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")

    event_id: str = Field(min_length=1, max_length=200)
    decision: ProviderGateDecision
    actor_id: str = Field(min_length=1, max_length=100)
    actor_name: str = Field(min_length=1, max_length=100)
    comment: str = Field(default="", max_length=1000)

    @field_validator("event_id")
    @classmethod
    def valid_event_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}", value) is None:
            raise ValueError("approval event id must contain safe ASCII characters")
        return value

    @field_validator("actor_id")
    @classmethod
    def valid_actor_id(cls, value: str) -> str:
        if re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9._:@-]{0,99}", value) is None:
            raise ValueError("approval actor id must contain safe ASCII characters")
        return value

    @field_validator("actor_name")
    @classmethod
    def valid_actor_name(cls, value: str) -> str:
        normalized = value.strip()
        if not normalized or any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        ):
            raise ValueError("approval actor name is invalid")
        return normalized

    @field_validator("comment")
    @classmethod
    def valid_comment(cls, value: str) -> str:
        normalized = value.strip()
        if any(
            ord(character) < 32 or ord(character) == 127
            for character in normalized
        ):
            raise ValueError("approval comment is invalid")
        return normalized


class ProviderApproval(BaseModel):
    id: str
    event_id: str
    decision: ProviderGateDecision
    actor_id: str
    actor_name: str
    comment: str
    created_at: datetime


class ProviderQualityGate(BaseModel):
    required: bool = False
    status: ProviderQualityGateStatus = ProviderQualityGateStatus.NOT_REQUIRED
    policy_revision: int | None = None
    reached_at: datetime | None = None
    decided_at: datetime | None = None


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
    quality_gate: ProviderQualityGate = Field(default_factory=ProviderQualityGate)
    approvals: list[ProviderApproval] = Field(default_factory=list, max_length=1)


__all__ = [
    "ProviderApproval",
    "ProviderGateDecision",
    "ProviderGateDecisionRequest",
    "ProviderKind",
    "ProviderQualityGate",
    "ProviderQualityGateStatus",
    "ProviderRun",
    "ProviderTriggerRequest",
]
