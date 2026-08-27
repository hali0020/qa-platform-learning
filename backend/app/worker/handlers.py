"""Fixed, side-effect-free teaching handlers.

Task payloads can only select an exact key in this registry.  They cannot name
Python modules, callables, commands, URLs or executables.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Mapping
from types import MappingProxyType
from typing import Any

TaskHandler = Callable[[Mapping[str, Any]], Awaitable[dict[str, Any]]]


class HandlerFailure(Exception):
    def __init__(self, error_code: str, *, retryable: bool) -> None:
        super().__init__(error_code)
        self.error_code = error_code
        self.retryable = retryable


class UnknownTaskType(HandlerFailure):
    def __init__(self) -> None:
        super().__init__("worker_unknown_task_type", retryable=False)


class FixedHandlerRegistry:
    def __init__(self, handlers: Mapping[str, TaskHandler]) -> None:
        if not handlers or any(not name.strip() for name in handlers):
            raise ValueError("worker handler registry cannot be empty")
        self._handlers = MappingProxyType(dict(handlers))

    def resolve(self, task_type: str) -> TaskHandler:
        try:
            return self._handlers[task_type]
        except KeyError as error:
            raise UnknownTaskType() from error

    @property
    def task_types(self) -> tuple[str, ...]:
        return tuple(sorted(self._handlers))


def _non_negative_int(payload: Mapping[str, Any], name: str) -> int:
    value = payload.get(name)
    if isinstance(value, bool) or not isinstance(value, int) or value < 0:
        raise HandlerFailure("worker_invalid_payload", retryable=False)
    return value


async def validate_inline_import(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a bounded inline row set without opening files or URLs."""

    rows = payload.get("rows")
    if not isinstance(rows, list) or len(rows) > 1_000:
        raise HandlerFailure("worker_invalid_payload", retryable=False)
    if any(not isinstance(row, dict) or len(row) > 128 for row in rows):
        raise HandlerFailure("worker_invalid_payload", retryable=False)
    return {
        "handler": "qa.import.validate",
        "valid": True,
        "row_count": len(rows),
    }


async def calculate_quality_summary(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Calculate a small deterministic summary from already-aggregated counts."""

    passed = _non_negative_int(payload, "passed")
    failed = _non_negative_int(payload, "failed")
    skipped = _non_negative_int(payload, "skipped")
    total = passed + failed + skipped
    pass_rate = round(passed / total, 6) if total else 0.0
    return {
        "handler": "qa.quality.generate",
        "passed": passed,
        "failed": failed,
        "skipped": skipped,
        "total": total,
        "pass_rate": pass_rate,
    }


async def simulate_pipeline_poll(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Normalize a supplied teaching status without contacting a CI server."""

    observed = payload.get("observed_status")
    allowed = {
        "queued",
        "running",
        "succeeded",
        "failed",
        "cancelled",
    }
    if not isinstance(observed, str) or observed not in allowed:
        raise HandlerFailure("worker_invalid_payload", retryable=False)
    return {
        "handler": "qa.pipeline.poll",
        "simulated": True,
        "status": observed,
    }


async def simulate_device_execution(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Validate a fixed action vocabulary; never launch a command or device tool."""

    steps = payload.get("steps")
    allowed_steps = {"launch", "tap", "swipe", "wait", "assert", "screenshot"}
    if (
        not isinstance(steps, list)
        or len(steps) > 1_000
        or any(not isinstance(step, str) or step not in allowed_steps for step in steps)
    ):
        raise HandlerFailure("worker_invalid_payload", retryable=False)
    return {
        "handler": "qa.device.execute",
        "simulated": True,
        "step_count": len(steps),
    }


def build_safe_handler_registry() -> FixedHandlerRegistry:
    return FixedHandlerRegistry(
        {
            "qa.import.validate": validate_inline_import,
            "qa.quality.generate": calculate_quality_summary,
            "qa.pipeline.poll": simulate_pipeline_poll,
            "qa.device.execute": simulate_device_execution,
        }
    )


__all__ = [
    "FixedHandlerRegistry",
    "HandlerFailure",
    "TaskHandler",
    "UnknownTaskType",
    "build_safe_handler_registry",
    "calculate_quality_summary",
    "simulate_device_execution",
    "simulate_pipeline_poll",
    "validate_inline_import",
]
