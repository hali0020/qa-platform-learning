"""Strict public and internal models for the local CI Lab."""

from __future__ import annotations

import json
import re
from datetime import datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, field_validator


_REF = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,127}\Z")
_VARIABLE_NAME = re.compile(r"[A-Z][A-Z0-9_]{0,63}\Z")
_SENSITIVE_SEGMENTS = frozenset(
    {
        "AUTH",
        "AUTHORIZATION",
        "COOKIE",
        "CREDENTIAL",
        "KEY",
        "PASS",
        "PASSWD",
        "PASSWORD",
        "PRIVATE",
        "SECRET",
        "TOKEN",
    }
)
_MAX_VARIABLES = 32
_MAX_VARIABLE_VALUE_BYTES = 512
_MAX_VARIABLES_JSON_BYTES = 8 * 1024


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid")


class RunStatus(str, Enum):
    QUEUED = "queued"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    CANCELLED = "cancelled"

    @property
    def is_terminal(self) -> bool:
        return self in {
            RunStatus.SUCCEEDED,
            RunStatus.FAILED,
            RunStatus.CANCELLED,
        }


class TriggerRunRequest(StrictModel):
    ref: str | None = Field(default=None, max_length=128)
    variables: dict[str, str] = Field(default_factory=dict, repr=False)

    @field_validator("ref")
    @classmethod
    def validate_ref(cls, value: str | None) -> str | None:
        if value is None:
            return None
        if _REF.fullmatch(value) is None:
            raise ValueError(
                "ref must contain only ASCII letters, digits, dot, underscore, or dash"
            )
        return value

    @field_validator("variables")
    @classmethod
    def validate_variables(cls, value: dict[str, str]) -> dict[str, str]:
        if len(value) > _MAX_VARIABLES:
            raise ValueError(f"at most {_MAX_VARIABLES} variables are allowed")

        normalized: dict[str, str] = {}
        for key, item in value.items():
            if _VARIABLE_NAME.fullmatch(key) is None:
                raise ValueError(
                    "variable names must use 1-64 uppercase ASCII letters, digits, or underscores"
                )
            if _SENSITIVE_SEGMENTS.intersection(key.split("_")):
                raise ValueError("secret-like variable names are not allowed")
            if not item or len(item.encode("utf-8")) > _MAX_VARIABLE_VALUE_BYTES:
                raise ValueError(
                    f"variable values must contain 1-{_MAX_VARIABLE_VALUE_BYTES} UTF-8 bytes"
                )
            if any(ord(character) < 32 or ord(character) == 127 for character in item):
                raise ValueError("variable values cannot contain control characters")
            if "://" in item or "../" in item or "..\\" in item:
                raise ValueError("URLs and parent-relative paths are not allowed in variables")
            normalized[key] = item

        canonical = json.dumps(
            normalized,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        ).encode("utf-8")
        if len(canonical) > _MAX_VARIABLES_JSON_BYTES:
            raise ValueError(
                f"the canonical variable document cannot exceed {_MAX_VARIABLES_JSON_BYTES} bytes"
            )
        return normalized


class JobView(StrictModel):
    key: str
    name: str
    status: RunStatus
    duration_ms: int
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None


class StageView(StrictModel):
    key: str
    name: str
    status: RunStatus
    jobs: list[JobView]
    started_at: datetime | None
    finished_at: datetime | None
    message: str | None


class RunView(StrictModel):
    id: str
    definition: str
    definition_revision: int
    status: RunStatus
    web_url: str | None = None
    message: str | None
    metadata: dict[str, Any]
    created_at: datetime
    updated_at: datetime
    started_at: datetime | None
    finished_at: datetime | None
    stages: list[StageView]
    replayed: bool = False


class DefinitionJobView(StrictModel):
    key: str
    name: str
    duration_ms: int
    should_fail: bool


class DefinitionStageView(StrictModel):
    key: str
    name: str
    jobs: list[DefinitionJobView]


class DefinitionView(StrictModel):
    key: str
    name: str
    revision: int
    queue_delay_ms: int
    stages: list[DefinitionStageView]


__all__ = [
    "DefinitionJobView",
    "DefinitionStageView",
    "DefinitionView",
    "JobView",
    "RunStatus",
    "RunView",
    "StageView",
    "StrictModel",
    "TriggerRunRequest",
]
