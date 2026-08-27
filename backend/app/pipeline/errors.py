from dataclasses import dataclass


class PipelineError(Exception):
    """Base class for errors safe to expose from the pipeline API."""


class PipelineNotFoundError(PipelineError):
    pass


class PipelineTransitionError(PipelineError):
    pass


class PipelineInvariantError(PipelineTransitionError):
    """A requested change would make parent and child states disagree."""


class PipelineIdempotencyConflictError(PipelineError):
    pass


class PipelineTargetNotFoundError(PipelineError):
    pass


class PipelineServiceClosedError(PipelineError):
    pass


@dataclass(frozen=True, slots=True)
class PipelineErrorMapping:
    """Transport-neutral error information for API exception handlers."""

    status_code: int
    code: int
    message: str


def map_pipeline_error(error: PipelineError) -> PipelineErrorMapping:
    """Map pipeline errors without coupling the domain service to FastAPI.

    The application can use this from a route-local handler today and from a
    global exception handler later without duplicating status/code decisions.
    """

    if isinstance(error, (PipelineNotFoundError, PipelineTargetNotFoundError)):
        return PipelineErrorMapping(status_code=404, code=40420, message=str(error))
    if isinstance(error, PipelineServiceClosedError):
        return PipelineErrorMapping(status_code=503, code=50320, message=str(error))
    if isinstance(error, PipelineInvariantError):
        return PipelineErrorMapping(status_code=409, code=40921, message=str(error))
    if isinstance(error, PipelineTransitionError):
        return PipelineErrorMapping(status_code=409, code=40920, message=str(error))
    if isinstance(error, PipelineIdempotencyConflictError):
        return PipelineErrorMapping(status_code=409, code=40922, message=str(error))
    return PipelineErrorMapping(status_code=500, code=50020, message=str(error))
