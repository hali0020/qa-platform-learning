"""Local-only CI/CD pipeline simulator.

The default service is in-memory and an optional standard-library SQLite
snapshot adapter provides local persistence. It is the first implementation
behind the future ``PipelineProvider`` abstraction.
"""

from app.pipeline.models import (
    CallbackTarget,
    PipelineCallbackRequest,
    PipelineJobSpec,
    PipelineRun,
    PipelineStageSpec,
    PipelineStatus,
    PipelineTriggerRequest,
)
from app.pipeline.service import (
    InMemoryPipelineService,
    PipelineService,
    create_pipeline_service,
)
from app.pipeline.persistence import SQLitePipelinePersistence

__all__ = [
    "CallbackTarget",
    "InMemoryPipelineService",
    "PipelineCallbackRequest",
    "PipelineJobSpec",
    "PipelineRun",
    "PipelineService",
    "PipelineStageSpec",
    "PipelineStatus",
    "PipelineTriggerRequest",
    "SQLitePipelinePersistence",
    "create_pipeline_service",
]
