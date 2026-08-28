"""Provider-neutral primitives for authenticating machine webhooks.

The verifier deliberately operates on the exact request bytes and raw ASGI
headers.  A router can therefore enforce a body limit before JSON parsing and
detect duplicate security headers before a framework joins or normalizes them.
No exception message contains request data, a signature, or the shared secret.
"""

from __future__ import annotations

import hashlib
import hmac
import re
from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Final, Protocol


MAX_WEBHOOK_BODY_BYTES: Final = 16 * 1024
WEBHOOK_TIMESTAMP_WINDOW_SECONDS: Final = 300

EVENT_ID_HEADER: Final = b"x-qa-webhook-event-id"
TIMESTAMP_HEADER: Final = b"x-qa-webhook-timestamp"
SIGNATURE_HEADER: Final = b"x-qa-webhook-signature"

_EVENT_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._:-]{0,199}\Z")
_TIMESTAMP = re.compile(r"[1-9][0-9]{9}\Z")
_SIGNATURE = re.compile(r"v1=([0-9a-f]{64})\Z")
_REQUIRED_HEADERS: Final = frozenset(
    {EVENT_ID_HEADER, TIMESTAMP_HEADER, SIGNATURE_HEADER}
)


class Clock(Protocol):
    def __call__(self) -> datetime: ...


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


class WebhookSecurityError(ValueError):
    """Safe-to-report rejection with a stable, non-sensitive code."""

    def __init__(self, code: str) -> None:
        super().__init__("webhook request was rejected")
        self.code = code


class RawBodyBuffer:
    """Accumulate streamed request bytes without exceeding a fixed bound."""

    def __init__(self, max_bytes: int = MAX_WEBHOOK_BODY_BYTES) -> None:
        if not 1 <= max_bytes <= MAX_WEBHOOK_BODY_BYTES:
            raise ValueError("webhook body limit is invalid")
        self.max_bytes = max_bytes
        self._parts: list[bytes] = []
        self._size = 0

    @property
    def size(self) -> int:
        return self._size

    def append(self, chunk: bytes | bytearray | memoryview) -> None:
        if not isinstance(chunk, (bytes, bytearray, memoryview)):
            raise TypeError("webhook body chunks must be bytes-like")
        selected = bytes(chunk)
        if len(selected) > self.max_bytes - self._size:
            raise WebhookSecurityError("webhook_body_too_large")
        if selected:
            self._parts.append(selected)
            self._size += len(selected)

    def finish(self) -> bytes:
        return b"".join(self._parts)


def collect_bounded_body(
    chunks: Iterable[bytes | bytearray | memoryview],
    *,
    max_bytes: int = MAX_WEBHOOK_BODY_BYTES,
) -> bytes:
    """Collect body chunks while checking the limit before retaining a chunk."""

    buffer = RawBodyBuffer(max_bytes=max_bytes)
    for chunk in chunks:
        buffer.append(chunk)
    return buffer.finish()


@dataclass(frozen=True, slots=True)
class ParsedWebhookHeaders:
    event_id: str
    timestamp: int
    signature: bytes


@dataclass(frozen=True, slots=True)
class VerifiedWebhook:
    event_id: str
    timestamp: int
    body_sha256: str


def _decode_ascii(value: bytes) -> str:
    try:
        return value.decode("ascii", errors="strict")
    except UnicodeDecodeError:
        raise WebhookSecurityError("webhook_header_invalid") from None


def _parse_event_id(value: str) -> str:
    if _EVENT_ID.fullmatch(value) is None:
        raise WebhookSecurityError("webhook_header_invalid")
    return value


def _parse_timestamp(value: str) -> int:
    if _TIMESTAMP.fullmatch(value) is None:
        raise WebhookSecurityError("webhook_header_invalid")
    return int(value)


def _parse_signature(value: str) -> bytes:
    matched = _SIGNATURE.fullmatch(value)
    if matched is None:
        raise WebhookSecurityError("webhook_header_invalid")
    # The regular expression fixes the length and alphabet before decoding.
    return bytes.fromhex(matched.group(1))


def parse_webhook_headers(
    raw_headers: Iterable[tuple[bytes, bytes]],
) -> ParsedWebhookHeaders:
    """Parse exact ASGI headers and reject duplicate security headers."""

    selected: dict[bytes, bytes] = {}
    for item in raw_headers:
        if (
            not isinstance(item, tuple)
            or len(item) != 2
            or not isinstance(item[0], bytes)
            or not isinstance(item[1], bytes)
        ):
            raise WebhookSecurityError("webhook_header_invalid")
        name, value = item
        normalized_name = name.lower()
        if normalized_name not in _REQUIRED_HEADERS:
            continue
        if normalized_name in selected:
            raise WebhookSecurityError("webhook_header_invalid")
        selected[normalized_name] = value

    if selected.keys() != _REQUIRED_HEADERS:
        raise WebhookSecurityError("webhook_header_invalid")

    event_id = _parse_event_id(_decode_ascii(selected[EVENT_ID_HEADER]))
    timestamp = _parse_timestamp(_decode_ascii(selected[TIMESTAMP_HEADER]))
    signature = _parse_signature(_decode_ascii(selected[SIGNATURE_HEADER]))
    return ParsedWebhookHeaders(
        event_id=event_id,
        timestamp=timestamp,
        signature=signature,
    )


def canonical_webhook_message(
    *,
    timestamp: int,
    event_id: str,
    raw_body: bytes,
) -> bytes:
    """Return ``v1\n{timestamp}\n{event_id}\n{sha256(raw_body)}`` as ASCII."""

    selected_event_id = _parse_event_id(event_id)
    selected_timestamp = _parse_timestamp(str(timestamp))
    body = collect_bounded_body((raw_body,))
    body_digest = hashlib.sha256(body).hexdigest()
    return (
        f"v1\n{selected_timestamp}\n{selected_event_id}\n{body_digest}"
    ).encode("ascii")


def _validated_secret(secret: bytes) -> bytes:
    if not isinstance(secret, bytes) or not 32 <= len(secret) <= 512:
        raise ValueError("webhook secret must contain 32-512 bytes")
    return secret


def sign_webhook(
    secret: bytes,
    *,
    timestamp: int,
    event_id: str,
    raw_body: bytes,
) -> str:
    """Create the strict ``v1=<lowercase hex>`` signature used by the verifier."""

    digest = hmac.digest(
        _validated_secret(secret),
        canonical_webhook_message(
            timestamp=timestamp,
            event_id=event_id,
            raw_body=raw_body,
        ),
        "sha256",
    )
    return f"v1={digest.hex()}"


class WebhookVerifier:
    """Verify one signed request with an injected wall clock."""

    def __init__(
        self,
        secret: bytes,
        *,
        clock: Clock = utc_now,
        timestamp_window_seconds: int = WEBHOOK_TIMESTAMP_WINDOW_SECONDS,
    ) -> None:
        if timestamp_window_seconds != WEBHOOK_TIMESTAMP_WINDOW_SECONDS:
            # Keep the security contract fixed instead of allowing a deployment
            # setting to silently widen replay acceptance.
            raise ValueError("webhook timestamp window must be 300 seconds")
        self._secret = _validated_secret(secret)
        self._clock = clock
        self._timestamp_window_seconds = timestamp_window_seconds

    def verify(
        self,
        *,
        raw_body: bytes,
        raw_headers: Iterable[tuple[bytes, bytes]],
    ) -> VerifiedWebhook:
        body = collect_bounded_body((raw_body,))
        headers = parse_webhook_headers(raw_headers)
        current = self._clock()
        if current.tzinfo is None or current.utcoffset() is None:
            raise RuntimeError("webhook clock must return a timezone-aware datetime")
        current_timestamp = int(current.timestamp())
        if (
            abs(current_timestamp - headers.timestamp)
            > self._timestamp_window_seconds
        ):
            raise WebhookSecurityError("webhook_timestamp_out_of_window")

        expected = hmac.digest(
            self._secret,
            canonical_webhook_message(
                timestamp=headers.timestamp,
                event_id=headers.event_id,
                raw_body=body,
            ),
            "sha256",
        )
        if not hmac.compare_digest(expected, headers.signature):
            raise WebhookSecurityError("webhook_signature_invalid")
        return VerifiedWebhook(
            event_id=headers.event_id,
            timestamp=headers.timestamp,
            body_sha256=hashlib.sha256(body).hexdigest(),
        )


__all__ = [
    "EVENT_ID_HEADER",
    "MAX_WEBHOOK_BODY_BYTES",
    "ParsedWebhookHeaders",
    "RawBodyBuffer",
    "SIGNATURE_HEADER",
    "TIMESTAMP_HEADER",
    "VerifiedWebhook",
    "WEBHOOK_TIMESTAMP_WINDOW_SECONDS",
    "WebhookSecurityError",
    "WebhookVerifier",
    "canonical_webhook_message",
    "collect_bounded_body",
    "parse_webhook_headers",
    "sign_webhook",
]
