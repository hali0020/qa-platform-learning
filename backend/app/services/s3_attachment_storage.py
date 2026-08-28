"""Strict async S3 adapter for the project-owned SeaweedFS lab service.

There is deliberately no generic AWS/public mode.  The production client is
imported lazily, while tests inject the narrow client protocol below and never
open a socket.
"""

from __future__ import annotations

import asyncio
import inspect
from collections.abc import AsyncIterator, Awaitable, Callable, Mapping
from pathlib import Path
from typing import Any, AsyncContextManager, Protocol, cast
from uuid import UUID

import aiofiles
from fastapi import UploadFile

from app.core.config import validate_object_storage_runtime_target
from app.core.errors import BusinessValidationError
from app.services.attachment_storage import (
    AttachmentStorageIntegrityError,
    AttachmentStorageUnavailableError,
    AttachmentValidationProfile,
    QuarantineReceipt,
    StoredContent,
    StoredUpload,
    quarantine_key,
    validate_quarantine_key,
    validate_sha256,
    validate_storage_key,
)
from app.services.local_attachment_storage import LocalAttachmentStorage


class S3ResponseBody(Protocol):
    async def read(self, amount: int = -1) -> bytes: ...

    def close(self) -> object: ...


class S3Client(Protocol):
    async def create_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def upload_part(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def complete_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def abort_multipart_upload(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def get_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def head_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def copy_object(self, **kwargs: Any) -> Mapping[str, Any]: ...

    async def delete_object(self, **kwargs: Any) -> Mapping[str, Any]: ...


S3ClientFactory = Callable[[], AsyncContextManager[S3Client]]

_MULTIPART_CHUNK_BYTES = 5 * 1024 * 1024


class S3AttachmentStorage:
    backend_name = "s3_local_container"

    def __init__(
        self,
        *,
        app_env: str,
        endpoint_url: str,
        bucket: str,
        region: str,
        access_key: str,
        secret_key: str,
        staging_root: Path,
        max_bytes: int,
        max_image_pixels: int,
        max_concurrency: int = 4,
        operation_timeout_seconds: float = 10.0,
        client_factory: S3ClientFactory | None = None,
    ) -> None:
        validate_object_storage_runtime_target(
            runtime_mode=self.backend_name,
            app_env=app_env,
            endpoint_url=endpoint_url,
            bucket=bucket,
            region=region,
            access_key=access_key,
            secret_key=secret_key,
            max_concurrency=max_concurrency,
            operation_timeout_seconds=operation_timeout_seconds,
        )
        self.namespace = bucket
        self._endpoint_url = endpoint_url
        self._bucket = bucket
        self._region = region
        self._access_key = access_key
        self._secret_key = secret_key
        self._max_bytes = max_bytes
        self._timeout = operation_timeout_seconds
        self._max_concurrency = max_concurrency
        self._semaphore = asyncio.Semaphore(max_concurrency)
        self._client_lock = asyncio.Lock()
        self._client_factory = client_factory or self._default_client_context
        self._client_context: AsyncContextManager[S3Client] | None = None
        self._client: S3Client | None = None
        self._closed = False
        self._staging = LocalAttachmentStorage(
            staging_root,
            max_bytes=max_bytes,
            max_image_pixels=max_image_pixels,
        )

    async def save(
        self,
        upload: UploadFile,
        attachment_id: UUID,
        *,
        validation_profile: AttachmentValidationProfile = (
            AttachmentValidationProfile.GENERIC
        ),
    ) -> StoredUpload:
        staged = await self._staging.save(
            upload,
            attachment_id,
            validation_profile=validation_profile,
        )
        storage_key = validate_storage_key(staged.storage_key)
        staged_path = self._staging.path_for_key(storage_key)
        upload_id: str | None = None
        try:
            async with self._semaphore:
                client = await self._require_client()
                created = await self._request(
                    client.create_multipart_upload(
                        Bucket=self._bucket,
                        Key=storage_key,
                        ContentType=staged.media_type,
                        Metadata={"sha256": staged.sha256},
                    ),
                    operation="create multipart upload",
                    storage_key=storage_key,
                )
                raw_upload_id = created.get("UploadId")
                if (
                    not isinstance(raw_upload_id, str)
                    or not raw_upload_id
                    or len(raw_upload_id) > 1024
                    or any(ord(character) < 32 for character in raw_upload_id)
                ):
                    raise AttachmentStorageIntegrityError()
                upload_id = raw_upload_id
                parts: list[dict[str, object]] = []
                async with aiofiles.open(staged_path, "rb") as handle:
                    part_number = 1
                    while True:
                        chunk = await handle.read(_MULTIPART_CHUNK_BYTES)
                        if not chunk:
                            break
                        uploaded = await self._request(
                            client.upload_part(
                                Bucket=self._bucket,
                                Key=storage_key,
                                UploadId=upload_id,
                                PartNumber=part_number,
                                Body=chunk,
                                ContentLength=len(chunk),
                            ),
                            operation="upload part",
                            storage_key=storage_key,
                        )
                        etag = uploaded.get("ETag")
                        if (
                            not isinstance(etag, str)
                            or not etag
                            or len(etag) > 1024
                            or any(ord(character) < 32 for character in etag)
                        ):
                            raise AttachmentStorageIntegrityError()
                        parts.append({"ETag": etag, "PartNumber": part_number})
                        part_number += 1
                if not parts:
                    raise AttachmentStorageIntegrityError()
                await self._request(
                    client.complete_multipart_upload(
                        Bucket=self._bucket,
                        Key=storage_key,
                        UploadId=upload_id,
                        MultipartUpload={"Parts": parts},
                    ),
                    operation="complete multipart upload",
                    storage_key=storage_key,
                )
                head = await self._request(
                    client.head_object(Bucket=self._bucket, Key=storage_key),
                    operation="head",
                    storage_key=storage_key,
                )
                self._verify_head(
                    head,
                    expected_size=staged.size_bytes,
                    expected_sha256=staged.sha256,
                )
            return staged
        except BaseException:
            if upload_id is not None:
                await self._shielded_abort_and_delete(storage_key, upload_id)
            raise
        finally:
            await self._staging.discard(storage_key)

    async def open(
        self,
        storage_key: str,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredContent:
        key = validate_storage_key(storage_key)
        trusted_digest = (
            validate_sha256(expected_sha256)
            if expected_sha256 is not None
            else None
        )
        await self._semaphore.acquire()
        body: S3ResponseBody | None = None
        released = False
        close_lock = asyncio.Lock()

        async def close_lease() -> None:
            nonlocal released
            async with close_lock:
                if released:
                    return
                released = True
                try:
                    if body is not None:
                        await self._close_body(body)
                finally:
                    self._semaphore.release()

        try:
            client = await self._require_client()
            response = await self._request(
                client.get_object(Bucket=self._bucket, Key=key),
                operation="get",
                storage_key=key,
            )
            candidate = response.get("Body")
            if candidate is None or not hasattr(candidate, "read"):
                raise AttachmentStorageIntegrityError()
            body = cast(S3ResponseBody, candidate)
            size, digest = self._verified_object_metadata(
                response,
                expected_size=expected_size,
                expected_sha256=trusted_digest,
            )

            async def chunks() -> AsyncIterator[bytes]:
                assert body is not None
                while True:
                    chunk = await self._read_body(body)
                    if not chunk:
                        return
                    yield chunk

            return StoredContent(
                body=chunks(),
                size_bytes=size,
                sha256=digest,
                expected_sha256=digest,
                close=close_lease,
            )
        except BaseException:
            await close_lease()
            raise

    async def discard(self, storage_key: str) -> None:
        key = validate_storage_key(storage_key)
        async with self._semaphore:
            client = await self._require_client()
            await self._request(
                client.delete_object(Bucket=self._bucket, Key=key),
                operation="delete",
                storage_key=key,
            )

    async def quarantine(
        self,
        storage_key: str,
        attachment_id: UUID,
    ) -> QuarantineReceipt:
        original_key = validate_storage_key(storage_key)
        isolated_key = quarantine_key(attachment_id)
        copy_started = False
        copy_verified = False
        async with self._semaphore:
            client = await self._require_client()
            source = await self._request(
                client.head_object(Bucket=self._bucket, Key=original_key),
                operation="head",
                storage_key=original_key,
            )
            size, digest = self._verified_object_metadata(source)
            try:
                copy_started = True
                await self._request(
                    client.copy_object(
                        Bucket=self._bucket,
                        Key=isolated_key,
                        CopySource={"Bucket": self._bucket, "Key": original_key},
                        MetadataDirective="COPY",
                    ),
                    operation="copy",
                    storage_key=isolated_key,
                )
                copied = await self._request(
                    client.head_object(Bucket=self._bucket, Key=isolated_key),
                    operation="head",
                    storage_key=isolated_key,
                )
                self._verify_head(
                    copied,
                    expected_size=size,
                    expected_sha256=digest,
                )
                copy_verified = True
            except BaseException:
                # The source has not been deleted yet, so removing a partial
                # quarantine copy is safe.
                if copy_started:
                    await self._best_effort_delete_with_client(client, isolated_key)
                raise
            assert copy_verified
            # A failed/uncertain delete leaves a duplicate source for later GC;
            # the verified quarantine copy is already sufficient compensation.
            await self._best_effort_delete_with_client(client, original_key)
        return QuarantineReceipt(
            _backend_name=self.backend_name,
            _namespace=self.namespace,
            _original_key=original_key,
            _quarantine_key=isolated_key,
        )

    async def restore(self, receipt: QuarantineReceipt) -> None:
        self._validate_receipt(receipt)
        original_key = validate_storage_key(receipt._original_key)
        isolated_key = validate_quarantine_key(receipt._quarantine_key)
        copy_started = False
        async with self._semaphore:
            client = await self._require_client()
            source = await self._request(
                client.head_object(Bucket=self._bucket, Key=isolated_key),
                operation="head",
                storage_key=isolated_key,
            )
            size, digest = self._verified_object_metadata(source)
            try:
                copy_started = True
                await self._request(
                    client.copy_object(
                        Bucket=self._bucket,
                        Key=original_key,
                        CopySource={"Bucket": self._bucket, "Key": isolated_key},
                        MetadataDirective="COPY",
                    ),
                    operation="copy",
                    storage_key=original_key,
                )
                restored = await self._request(
                    client.head_object(Bucket=self._bucket, Key=original_key),
                    operation="head",
                    storage_key=original_key,
                )
                self._verify_head(
                    restored,
                    expected_size=size,
                    expected_sha256=digest,
                )
            except BaseException:
                if copy_started:
                    await self._best_effort_delete_with_client(client, original_key)
                raise
            # Restore is complete once the original has been verified. A trash
            # duplicate is safe and can be removed by reconciliation later.
            await self._best_effort_delete_with_client(client, isolated_key)

    async def aclose(self) -> None:
        async with self._client_lock:
            if self._closed:
                return
            self._closed = True
            context = self._client_context
            self._client = None
            self._client_context = None
        if context is not None:
            await self._bounded_lifecycle(
                context.__aexit__(None, None, None)
            )

    async def _require_client(self) -> S3Client:
        if self._closed:
            raise AttachmentStorageUnavailableError("附件存储已经关闭")
        if self._client is not None:
            return self._client
        async with self._client_lock:
            if self._closed:
                raise AttachmentStorageUnavailableError("附件存储已经关闭")
            if self._client is not None:
                return self._client
            context: AsyncContextManager[S3Client] | None = None
            failed = False
            try:
                context = self._client_factory()
                client = await self._bounded_lifecycle(context.__aenter__())
            except asyncio.CancelledError:
                if context is not None:
                    await self._best_effort_close_context(context)
                raise
            except Exception:
                failed = True
                if context is not None:
                    await self._best_effort_close_context(context)
            if failed:
                raise AttachmentStorageUnavailableError()
            assert context is not None
            self._client_context = context
            self._client = client
            return client

    def _default_client_context(self) -> AsyncContextManager[S3Client]:
        # Lazy imports are a hard boundary: local_filesystem mode never imports
        # an S3 SDK or constructs a client. Explicit credentials bypass the AWS
        # provider chain and therefore cannot fall back to instance metadata.
        try:
            from aiobotocore.config import AioConfig
            from aiobotocore.session import get_session
        except ImportError:  # pragma: no cover - dependency error path
            dependency_missing = True
        else:
            dependency_missing = False
        if dependency_missing:
            raise AttachmentStorageUnavailableError("异步对象存储依赖未安装")

        config = AioConfig(
            connect_timeout=self._timeout,
            read_timeout=self._timeout,
            max_pool_connections=self._max_concurrency,
            retries={"max_attempts": 2, "mode": "standard"},
            signature_version="s3v4",
            s3={"addressing_style": "path"},
            proxies={},
        )
        session = get_session()
        context = session.create_client(
            "s3",
            endpoint_url=self._endpoint_url,
            region_name=self._region,
            aws_access_key_id=self._access_key,
            aws_secret_access_key=self._secret_key,
            use_ssl=False,
            config=config,
        )
        return cast(AsyncContextManager[S3Client], context)

    async def _bounded_lifecycle(self, awaitable: Awaitable[Any]) -> Any:
        """Finish or cancel SDK lifecycle work within a fixed deadline."""

        task = asyncio.ensure_future(awaitable)
        failed = False
        timed_out = False
        try:
            return await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._timeout,
            )
        except asyncio.CancelledError:
            await self._settle_lifecycle_task(task)
            raise
        except asyncio.TimeoutError:
            task.cancel()
            await self._settle_lifecycle_task(task)
            timed_out = True
        except Exception:
            failed = True
        if failed or timed_out:
            raise AttachmentStorageUnavailableError()

    async def _settle_lifecycle_task(self, task: asyncio.Future[Any]) -> None:
        if task.done():
            try:
                task.result()
            except BaseException:
                pass
            return
        try:
            await asyncio.wait_for(
                asyncio.shield(task),
                timeout=self._timeout,
            )
        except BaseException:
            task.cancel()

    async def _best_effort_close_context(
        self,
        context: AsyncContextManager[S3Client],
    ) -> None:
        try:
            await self._bounded_lifecycle(context.__aexit__(None, None, None))
        except BaseException:
            pass

    async def _request(
        self,
        request: Awaitable[Mapping[str, Any]],
        *,
        operation: str,
        storage_key: str,
    ) -> Mapping[str, Any]:
        not_found = False
        timed_out = False
        failed = False
        try:
            return await asyncio.wait_for(request, timeout=self._timeout)
        except asyncio.TimeoutError:
            timed_out = True
        except Exception as error:
            if self._is_not_found(error):
                not_found = True
            else:
                failed = True
        if not_found:
            raise AttachmentStorageIntegrityError()
        if timed_out:
            raise AttachmentStorageUnavailableError()
        if failed:
            # Never include endpoint, bucket, key, signed headers, or the remote
            # response body in a user-visible error.
            raise AttachmentStorageUnavailableError(
                f"附件存储 {operation} 操作失败"
            )
        raise AttachmentStorageUnavailableError()

    async def _read_body(self, body: S3ResponseBody) -> bytes:
        timed_out = False
        failed = False
        try:
            chunk = await asyncio.wait_for(
                body.read(1024 * 1024),
                timeout=self._timeout,
            )
        except asyncio.TimeoutError:
            timed_out = True
        except Exception:
            failed = True
        if timed_out:
            raise AttachmentStorageUnavailableError()
        if failed:
            raise AttachmentStorageUnavailableError("附件存储读取失败")
        if not isinstance(chunk, bytes):
            raise AttachmentStorageIntegrityError()
        return chunk

    async def _close_body(self, body: S3ResponseBody) -> None:
        try:
            result = body.close()
            if inspect.isawaitable(result):
                await self._bounded_lifecycle(result)
        except asyncio.CancelledError:
            raise
        except Exception:
            # Closing a response is best effort and must not replace the
            # operation's original validation/transport error.
            pass

    def _verified_object_metadata(
        self,
        response: Mapping[str, Any],
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> tuple[int, str]:
        try:
            size = int(response["ContentLength"])
        except (KeyError, TypeError, ValueError) as error:
            raise AttachmentStorageIntegrityError() from error
        if size < 0 or size > self._max_bytes:
            raise AttachmentStorageIntegrityError()
        if expected_size is not None and size != expected_size:
            raise AttachmentStorageIntegrityError()
        metadata = response.get("Metadata")
        if not isinstance(metadata, Mapping):
            raise AttachmentStorageIntegrityError()
        raw_digest = metadata.get("sha256")
        if not isinstance(raw_digest, str):
            raise AttachmentStorageIntegrityError()
        try:
            digest = validate_sha256(raw_digest)
        except BusinessValidationError as error:
            raise AttachmentStorageIntegrityError() from error
        if expected_sha256 is not None and digest != validate_sha256(expected_sha256):
            raise AttachmentStorageIntegrityError()
        return size, digest

    def _verify_head(
        self,
        response: Mapping[str, Any],
        *,
        expected_size: int,
        expected_sha256: str,
    ) -> None:
        self._verified_object_metadata(
            response,
            expected_size=expected_size,
            expected_sha256=expected_sha256,
        )

    def _validate_receipt(self, receipt: QuarantineReceipt) -> None:
        if (
            receipt._backend_name != self.backend_name
            or receipt._namespace != self.namespace
        ):
            raise BusinessValidationError("附件隔离凭据不属于当前存储")

    async def _shielded_abort_and_delete(
        self,
        storage_key: str,
        upload_id: str,
    ) -> None:
        cleanup = asyncio.create_task(
            self._abort_and_delete(storage_key, upload_id)
        )
        try:
            await asyncio.wait_for(
                asyncio.shield(cleanup),
                timeout=min(self._timeout * 3, 60),
            )
        except BaseException:
            if not cleanup.done():
                cleanup.cancel()

    async def _abort_and_delete(self, storage_key: str, upload_id: str) -> None:
        try:
            async with self._semaphore:
                client = await self._require_client()
                try:
                    await asyncio.wait_for(
                        client.abort_multipart_upload(
                            Bucket=self._bucket,
                            Key=storage_key,
                            UploadId=upload_id,
                        ),
                        timeout=self._timeout,
                    )
                except BaseException:
                    # Abort failure must never replace the upload's original
                    # validation, transport, or cancellation error.
                    pass
                await self._best_effort_delete_with_client(client, storage_key)
        except BaseException:
            pass

    async def _best_effort_delete_with_client(
        self,
        client: S3Client,
        storage_key: str,
    ) -> None:
        try:
            await asyncio.wait_for(
                client.delete_object(Bucket=self._bucket, Key=storage_key),
                timeout=self._timeout,
            )
        except BaseException:
            pass

    @staticmethod
    def _is_not_found(error: Exception) -> bool:
        response = getattr(error, "response", None)
        if not isinstance(response, Mapping):
            return False
        error_data = response.get("Error")
        if isinstance(error_data, Mapping):
            code = str(error_data.get("Code", ""))
            if code in {"404", "NoSuchKey", "NotFound"}:
                return True
        metadata = response.get("ResponseMetadata")
        return isinstance(metadata, Mapping) and metadata.get("HTTPStatusCode") == 404


__all__ = ["S3AttachmentStorage", "S3Client", "S3ClientFactory", "S3ResponseBody"]
