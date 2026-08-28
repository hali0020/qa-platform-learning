"""Storage port shared by local and self-hosted attachment backends.

The public application works with generated storage keys and one-shot async
content streams.  Backend-specific paths, buckets, clients, and delete tokens
stay behind this boundary.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import re
from collections.abc import AsyncIterator, Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import Protocol, runtime_checkable
from uuid import UUID

from fastapi import UploadFile

from app.core.errors import BusinessValidationError, DomainError


_STORAGE_KEY = re.compile(r"^[0-9a-f]{2}/[0-9a-f]{32}$")
_QUARANTINE_KEY = re.compile(r"^\.trash/[0-9a-f]{32}$")
_SHA256 = re.compile(r"^[0-9a-f]{64}$")


class AttachmentValidationProfile(str, Enum):
    """Select the bounded content contract applied before persistence."""

    GENERIC = "generic"
    TEST_REPORT = "test_report"


class AttachmentStorageUnavailableError(DomainError):
    """A safe, non-secret error for unavailable storage infrastructure."""

    def __init__(self, message: str = "附件存储暂不可用") -> None:
        super().__init__(message, status_code=503, code=50310)


class AttachmentStorageIntegrityError(DomainError):
    """Stored bytes do not match their trusted relational metadata."""

    def __init__(self, message: str = "附件存储内容完整性校验失败") -> None:
        super().__init__(message, status_code=500, code=50010)


@dataclass(frozen=True, slots=True)
class StoredUpload:
    original_filename: str
    storage_key: str
    media_type: str
    size_bytes: int
    sha256: str
    is_image: bool


@dataclass(frozen=True, slots=True)
class QuarantineReceipt:
    """Opaque compensation token returned by a storage implementation."""

    _backend_name: str = field(repr=False)
    _namespace: str = field(repr=False)
    _original_key: str = field(repr=False)
    _quarantine_key: str = field(repr=False)


CloseCallback = Callable[[], Awaitable[None]]


class StoredContent:
    """A bounded, one-shot async stream which always releases its resource."""

    __slots__ = (
        "size_bytes",
        "sha256",
        "_iterator",
        "_close_callback",
        "_close_lock",
        "_closed",
        "_expected_sha256",
        "_started",
    )

    def __init__(
        self,
        *,
        body: AsyncIterator[bytes],
        size_bytes: int,
        sha256: str | None = None,
        expected_sha256: str | None = None,
        close: CloseCallback | None = None,
    ) -> None:
        if size_bytes < 0:
            raise ValueError("stored content size cannot be negative")
        if sha256 is not None:
            validate_sha256(sha256)
        if expected_sha256 is not None:
            expected_sha256 = validate_sha256(expected_sha256)
        self.size_bytes = size_bytes
        self.sha256 = sha256
        self._expected_sha256 = expected_sha256
        self._iterator = body
        self._close_callback = close
        self._close_lock = asyncio.Lock()
        self._closed = False
        self._started = False

    def __aiter__(self) -> AsyncIterator[bytes]:
        return self.iter_bytes()

    async def iter_bytes(self) -> AsyncIterator[bytes]:
        if self._started:
            raise RuntimeError("stored content streams are one-shot")
        if self._closed:
            raise RuntimeError("stored content stream is closed")
        self._started = True
        emitted = 0
        digest = hashlib.sha256() if self._expected_sha256 is not None else None
        try:
            async for chunk in self._iterator:
                if not isinstance(chunk, bytes):
                    raise AttachmentStorageIntegrityError()
                if not chunk:
                    continue
                emitted += len(chunk)
                if emitted > self.size_bytes:
                    raise AttachmentStorageIntegrityError()
                if digest is not None:
                    digest.update(chunk)
                yield chunk
            if emitted != self.size_bytes:
                raise AttachmentStorageIntegrityError()
            if digest is not None and not hmac.compare_digest(
                digest.hexdigest(),
                self._expected_sha256 or "",
            ):
                raise AttachmentStorageIntegrityError()
        finally:
            await self.aclose()

    async def aclose(self) -> None:
        async with self._close_lock:
            if self._closed:
                return
            self._closed = True
            iterator_close = getattr(self._iterator, "aclose", None)
            try:
                if iterator_close is not None:
                    await iterator_close()
            finally:
                if self._close_callback is not None:
                    await self._close_callback()


@runtime_checkable
class AttachmentStorage(Protocol):
    """Port used by attachment application services."""

    @property
    def backend_name(self) -> str: ...

    @property
    def namespace(self) -> str: ...

    async def save(
        self,
        upload: UploadFile,
        attachment_id: UUID,
        *,
        validation_profile: AttachmentValidationProfile = (
            AttachmentValidationProfile.GENERIC
        ),
    ) -> StoredUpload: ...

    async def open(
        self,
        storage_key: str,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredContent: ...

    async def discard(self, storage_key: str) -> None: ...

    async def quarantine(
        self, storage_key: str, attachment_id: UUID
    ) -> QuarantineReceipt: ...

    async def restore(self, receipt: QuarantineReceipt) -> None: ...

    async def aclose(self) -> None: ...


def validate_storage_key(value: str) -> str:
    if _STORAGE_KEY.fullmatch(value) is None:
        raise BusinessValidationError("附件存储键格式无效")
    return value


def quarantine_key(attachment_id: UUID) -> str:
    return f".trash/{attachment_id.hex}"


def validate_quarantine_key(value: str) -> str:
    if _QUARANTINE_KEY.fullmatch(value) is None:
        raise BusinessValidationError("附件隔离键格式无效")
    return value


def validate_sha256(value: str) -> str:
    normalized = value.casefold()
    if _SHA256.fullmatch(normalized) is None:
        raise BusinessValidationError("附件 SHA-256 格式无效")
    return normalized


__all__ = [
    "AttachmentStorage",
    "AttachmentStorageIntegrityError",
    "AttachmentStorageUnavailableError",
    "AttachmentValidationProfile",
    "QuarantineReceipt",
    "StoredContent",
    "StoredUpload",
    "quarantine_key",
    "validate_quarantine_key",
    "validate_sha256",
    "validate_storage_key",
]
