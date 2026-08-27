from pydantic import BaseModel, ConfigDict, Field

from app.domain.models import CaseResultStatus, ExecutionStatus


class ExecutionCreate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    plan_id: str


class ExecutionTransition(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: ExecutionStatus


class CaseResultUpdate(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: CaseResultStatus
    actual_result: str = Field(default="", max_length=2000)
    comment: str = Field(default="", max_length=1000)
