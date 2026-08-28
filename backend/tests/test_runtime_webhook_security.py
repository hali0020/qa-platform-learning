from __future__ import annotations

import hashlib
from datetime import datetime, timedelta, timezone

import pytest

from app.runtime.webhook_security import (
    EVENT_ID_HEADER,
    MAX_WEBHOOK_BODY_BYTES,
    SIGNATURE_HEADER,
    TIMESTAMP_HEADER,
    RawBodyBuffer,
    WebhookSecurityError,
    WebhookVerifier,
    canonical_webhook_message,
    collect_bounded_body,
    parse_webhook_headers,
    sign_webhook,
)


SECRET = b"local-webhook-secret-value-32bytes!"
NOW = datetime(2026, 8, 28, 12, 0, tzinfo=timezone.utc)
TIMESTAMP = int(NOW.timestamp())


def _headers(
    body: bytes,
    *,
    event_id: str = "event-001",
    timestamp: int = TIMESTAMP,
    secret: bytes = SECRET,
) -> list[tuple[bytes, bytes]]:
    signature = sign_webhook(
        secret,
        timestamp=timestamp,
        event_id=event_id,
        raw_body=body,
    )
    return [
        (b"content-type", b"application/json"),
        (EVENT_ID_HEADER, event_id.encode("ascii")),
        (TIMESTAMP_HEADER, str(timestamp).encode("ascii")),
        (SIGNATURE_HEADER, signature.encode("ascii")),
    ]


def _verifier(secret: bytes = SECRET, *, now: datetime = NOW) -> WebhookVerifier:
    return WebhookVerifier(secret, clock=lambda: now)


def test_collects_exactly_bounded_raw_bytes_and_rejects_overflow() -> None:
    first = b"a" * (MAX_WEBHOOK_BODY_BYTES - 1)
    assert collect_bounded_body((first, b"b")) == first + b"b"

    buffer = RawBodyBuffer()
    buffer.append(first)
    with pytest.raises(WebhookSecurityError) as captured:
        buffer.append(b"bc")
    assert captured.value.code == "webhook_body_too_large"
    assert buffer.size == len(first)
    assert buffer.finish() == first


def test_canonical_message_and_valid_signature_are_exact() -> None:
    body = b'{"status":"running"}'
    body_sha = hashlib.sha256(body).hexdigest()
    expected = f"v1\n{TIMESTAMP}\nevent-001\n{body_sha}".encode("ascii")
    assert canonical_webhook_message(
        timestamp=TIMESTAMP,
        event_id="event-001",
        raw_body=body,
    ) == expected

    verified = _verifier().verify(raw_body=body, raw_headers=_headers(body))
    assert verified.event_id == "event-001"
    assert verified.timestamp == TIMESTAMP
    assert verified.body_sha256 == body_sha


def test_header_names_are_case_insensitive_but_duplicates_are_rejected() -> None:
    body = b"{}"
    headers = _headers(body)
    headers[1] = (b"X-QA-Webhook-Event-ID", b"event-001")
    assert parse_webhook_headers(headers).event_id == "event-001"

    headers.append((b"X-QA-WEBHOOK-EVENT-ID", b"event-001"))
    with pytest.raises(WebhookSecurityError) as captured:
        parse_webhook_headers(headers)
    assert captured.value.code == "webhook_header_invalid"


@pytest.mark.parametrize(
    "event_id",
    ["", " event-1", "event 1", "event/1", "event\n1", "évent-1", "a" * 201],
)
def test_event_id_parser_is_strict(event_id: str) -> None:
    body = b"{}"
    headers = _headers(body)
    headers[1] = (EVENT_ID_HEADER, event_id.encode("utf-8"))
    with pytest.raises(WebhookSecurityError) as captured:
        parse_webhook_headers(headers)
    assert captured.value.code == "webhook_header_invalid"


@pytest.mark.parametrize(
    "timestamp",
    [b"", b"-1787899200", b"+1787899200", b" 1787899200", b"01787899200", b"17878992000", b"not-a-time"],
)
def test_timestamp_parser_is_strict(timestamp: bytes) -> None:
    body = b"{}"
    headers = _headers(body)
    headers[2] = (TIMESTAMP_HEADER, timestamp)
    with pytest.raises(WebhookSecurityError) as captured:
        parse_webhook_headers(headers)
    assert captured.value.code == "webhook_header_invalid"


@pytest.mark.parametrize(
    "signature",
    [
        b"",
        b"v2=" + b"0" * 64,
        b"v1=" + b"0" * 63,
        b"v1=" + b"0" * 65,
        b"v1=" + b"G" * 64,
        b"V1=" + b"0" * 64,
        b"v1=" + b"A" * 64,
        b"v1=" + b"0" * 64 + b",v1=" + b"0" * 64,
    ],
)
def test_signature_parser_accepts_only_v1_lowercase_fixed_hex(signature: bytes) -> None:
    body = b"{}"
    headers = _headers(body)
    headers[3] = (SIGNATURE_HEADER, signature)
    with pytest.raises(WebhookSecurityError) as captured:
        parse_webhook_headers(headers)
    assert captured.value.code == "webhook_header_invalid"


def test_missing_and_malformed_raw_headers_are_rejected() -> None:
    body = b"{}"
    headers = _headers(body)
    with pytest.raises(WebhookSecurityError):
        parse_webhook_headers(headers[:-1])
    with pytest.raises(WebhookSecurityError):
        parse_webhook_headers([("not-bytes", b"value")])  # type: ignore[list-item]


def test_wrong_signature_is_rejected_without_echoing_sensitive_values() -> None:
    body = b'{"external_id":"run-1"}'
    headers = _headers(body, secret=b"different-local-secret-value-32b!")
    with pytest.raises(WebhookSecurityError) as captured:
        _verifier().verify(raw_body=body, raw_headers=headers)
    assert captured.value.code == "webhook_signature_invalid"
    message = str(captured.value)
    assert "run-1" not in message
    assert headers[-1][1].decode("ascii") not in message
    assert SECRET.decode("ascii") not in message


@pytest.mark.parametrize("offset", [-300, 300])
def test_timestamp_window_includes_exact_boundaries(offset: int) -> None:
    body = b"{}"
    timestamp = TIMESTAMP + offset
    verified = _verifier().verify(
        raw_body=body,
        raw_headers=_headers(body, timestamp=timestamp),
    )
    assert verified.timestamp == timestamp


@pytest.mark.parametrize("offset", [-301, 301])
def test_timestamp_window_rejects_past_and_future_replays(offset: int) -> None:
    body = b"{}"
    timestamp = TIMESTAMP + offset
    with pytest.raises(WebhookSecurityError) as captured:
        _verifier().verify(
            raw_body=body,
            raw_headers=_headers(body, timestamp=timestamp),
        )
    assert captured.value.code == "webhook_timestamp_out_of_window"


def test_injected_clock_is_used_for_replay_window() -> None:
    body = b"{}"
    later = NOW + timedelta(seconds=301)
    with pytest.raises(WebhookSecurityError):
        _verifier(now=later).verify(raw_body=body, raw_headers=_headers(body))


def test_signature_binds_exact_unicode_bytes_not_parsed_json_semantics() -> None:
    utf8_body = '{"message":"游戏"}'.encode("utf-8")
    escaped_body = b'{"message":"\\u6e38\\u620f"}'
    headers = _headers(utf8_body)

    assert _verifier().verify(raw_body=utf8_body, raw_headers=headers)
    with pytest.raises(WebhookSecurityError) as captured:
        _verifier().verify(raw_body=escaped_body, raw_headers=headers)
    assert captured.value.code == "webhook_signature_invalid"


def test_body_limit_is_checked_before_authentication() -> None:
    body = b"x" * (MAX_WEBHOOK_BODY_BYTES + 1)
    with pytest.raises(WebhookSecurityError) as captured:
        _verifier().verify(raw_body=body, raw_headers=[])
    assert captured.value.code == "webhook_body_too_large"


def test_secret_and_clock_configuration_fail_closed_without_echoing_secret() -> None:
    weak = b"weak-secret"
    with pytest.raises(ValueError) as captured:
        WebhookVerifier(weak)
    assert weak.decode("ascii") not in str(captured.value)

    body = b"{}"
    verifier = WebhookVerifier(SECRET, clock=lambda: NOW.replace(tzinfo=None))
    with pytest.raises(RuntimeError, match="timezone-aware"):
        verifier.verify(raw_body=body, raw_headers=_headers(body))
