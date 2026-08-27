from __future__ import annotations

import contextvars
import json
import logging
from datetime import datetime, timezone
from typing import Any


request_id_context: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "request_id",
    default=None,
)

_EXTRA_FIELDS = (
    "event",
    "request_id",
    "method",
    "route",
    "status_code",
    "duration_ms",
    "error_type",
)


class JsonLogFormatter(logging.Formatter):
    """One-line JSON logs without request bodies, query strings or headers."""

    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, Any] = {
            "timestamp": datetime.fromtimestamp(
                record.created,
                tz=timezone.utc,
            ).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for field in _EXTRA_FIELDS:
            value = getattr(record, field, None)
            if value is not None:
                payload[field] = value

        if "request_id" not in payload:
            request_id = request_id_context.get()
            if request_id:
                payload["request_id"] = request_id
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_json_logger(
    logger: logging.Logger,
    *,
    level: int = logging.INFO,
) -> logging.Logger:
    """Configure a dedicated logger idempotently.

    This function never changes the root/Uvicorn logging configuration. A
    marker on the handler avoids duplicate output when an app is created more
    than once in the same test process.
    """

    for handler in logger.handlers:
        if getattr(handler, "_qa_json_handler", False):
            return logger

    handler = logging.StreamHandler()
    handler.setFormatter(JsonLogFormatter())
    handler._qa_json_handler = True  # type: ignore[attr-defined]
    logger.addHandler(handler)
    logger.setLevel(level)
    logger.propagate = False
    return logger
