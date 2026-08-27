from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import os
import unicodedata
import warnings
import zipfile
from pathlib import Path
from uuid import UUID

import aiofiles
from fastapi import UploadFile
from PIL import Image, ImageOps, UnidentifiedImageError

from app.core.errors import BusinessValidationError, NotFoundError
from app.services.attachment_storage import (
    AttachmentStorageIntegrityError,
    QuarantineReceipt,
    StoredContent,
    StoredUpload,
    quarantine_key,
    validate_quarantine_key,
    validate_sha256,
    validate_storage_key,
)
_WINDOWS_RESERVED = {
    "con",
    "prn",
    "aux",
    "nul",
    *(f"com{number}" for number in range(1, 10)),
    *(f"lpt{number}" for number in range(1, 10)),
}

_ALLOWED_EXTENSIONS = {
    "image/jpeg": {".jpg", ".jpeg"},
    "image/png": {".png"},
    "image/webp": {".webp"},
    "application/pdf": {".pdf"},
    "text/plain": {".txt", ".log", ".md"},
    "text/csv": {".csv"},
    "application/json": {".json"},
    "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet": {
        ".xlsx"
    },
}
_IMAGE_FORMATS = {
    "JPEG": ("image/jpeg", "JPEG"),
    "PNG": ("image/png", "PNG"),
    "WEBP": ("image/webp", "WEBP"),
}

class LocalAttachmentStorage:
    backend_name = "local_filesystem"
    namespace = ""

    def __init__(
        self,
        root: Path,
        *,
        max_bytes: int,
        max_image_pixels: int,
    ) -> None:
        self._root = root.resolve()
        self._max_bytes = max_bytes
        self._max_image_pixels = max_image_pixels

    async def save(self, upload: UploadFile, attachment_id: UUID) -> StoredUpload:
        filename = self.validate_filename(upload.filename or "")
        declared_type = (upload.content_type or "").split(";", maxsplit=1)[0].lower()
        self._require_declared_type_and_extension(filename, declared_type)
        await self._ensure_directories()
        temp_path = self._root / ".tmp" / f"{attachment_id.hex}.part"
        safe_temp_path = self._resolve_under_root(temp_path)
        size = 0
        try:
            async with aiofiles.open(safe_temp_path, "xb") as handle:
                while True:
                    chunk = await upload.read(1024 * 1024)
                    if not chunk:
                        break
                    size += len(chunk)
                    if size > self._max_bytes:
                        raise BusinessValidationError(
                            f"附件不能超过 {self._max_bytes} 字节"
                        )
                    await handle.write(chunk)
            if size == 0:
                raise BusinessValidationError("附件不能为空文件")
            media_type, is_image = await asyncio.to_thread(
                self._validate_and_normalize,
                safe_temp_path,
                declared_type,
                filename,
            )
            normalized_size = safe_temp_path.stat().st_size
            if normalized_size > self._max_bytes:
                raise BusinessValidationError(
                    f"规范化后的附件不能超过 {self._max_bytes} 字节"
                )
            digest = await asyncio.to_thread(self._sha256_file, safe_temp_path)
            storage_key = f"{attachment_id.hex[:2]}/{attachment_id.hex}"
            final_path = self.path_for_key(storage_key, must_exist=False)
            await asyncio.to_thread(final_path.parent.mkdir, parents=True, exist_ok=True)
            await asyncio.to_thread(os.replace, safe_temp_path, final_path)
            return StoredUpload(
                original_filename=filename,
                storage_key=storage_key,
                media_type=media_type,
                size_bytes=normalized_size,
                sha256=digest,
                is_image=is_image,
            )
        except BaseException:
            await self._unlink_if_exists(safe_temp_path)
            raise
        finally:
            await upload.close()

    def path_for_key(self, storage_key: str, *, must_exist: bool = True) -> Path:
        validate_storage_key(storage_key)
        path = self._resolve_under_root(self._root / storage_key)
        if must_exist and (not path.is_file() or path.is_symlink()):
            raise NotFoundError("附件文件", storage_key)
        return path

    async def open(
        self,
        storage_key: str,
        *,
        expected_size: int | None = None,
        expected_sha256: str | None = None,
    ) -> StoredContent:
        path = self.path_for_key(storage_key)
        size = await asyncio.to_thread(lambda: path.stat().st_size)
        if expected_size is not None and size != expected_size:
            raise AttachmentStorageIntegrityError()
        digest: str | None = None
        if expected_sha256 is not None:
            trusted_digest = validate_sha256(expected_sha256)
            digest = await asyncio.to_thread(self._sha256_file, path)
            if not hmac.compare_digest(digest, trusted_digest):
                raise AttachmentStorageIntegrityError()

        handle = await aiofiles.open(path, "rb")

        async def chunks():
            while True:
                chunk = await handle.read(1024 * 1024)
                if not chunk:
                    return
                yield chunk

        return StoredContent(
            body=chunks(),
            size_bytes=size,
            sha256=digest,
            expected_sha256=digest,
            close=handle.close,
        )

    async def discard(self, storage_key: str) -> None:
        await self._unlink_if_exists(self.path_for_key(storage_key, must_exist=False))

    async def move_to_trash(self, storage_key: str, attachment_id: UUID) -> Path:
        source = self.path_for_key(storage_key)
        await self._ensure_directories()
        target = self._resolve_under_root(self._root / ".trash" / attachment_id.hex)
        await asyncio.to_thread(os.replace, source, target)
        return target

    async def quarantine(
        self,
        storage_key: str,
        attachment_id: UUID,
    ) -> QuarantineReceipt:
        validate_storage_key(storage_key)
        await self.move_to_trash(storage_key, attachment_id)
        return QuarantineReceipt(
            _backend_name=self.backend_name,
            _namespace=self.namespace,
            _original_key=storage_key,
            _quarantine_key=quarantine_key(attachment_id),
        )

    async def restore_from_trash(self, trash_path: Path, storage_key: str) -> None:
        source = self._resolve_under_root(trash_path)
        destination = self.path_for_key(storage_key, must_exist=False)
        await asyncio.to_thread(destination.parent.mkdir, parents=True, exist_ok=True)
        if source.is_file():
            await asyncio.to_thread(os.replace, source, destination)

    async def restore(self, receipt: QuarantineReceipt) -> None:
        if (
            receipt._backend_name != self.backend_name
            or receipt._namespace != self.namespace
        ):
            raise BusinessValidationError("附件隔离凭据不属于当前存储")
        original_key = validate_storage_key(receipt._original_key)
        isolated_key = validate_quarantine_key(receipt._quarantine_key)
        trash_path = self._resolve_under_root(self._root / isolated_key)
        if not trash_path.is_file() or trash_path.is_symlink():
            raise NotFoundError("附件隔离文件", isolated_key)
        await self.restore_from_trash(trash_path, original_key)

    async def aclose(self) -> None:
        return None

    @staticmethod
    def validate_filename(value: str) -> str:
        normalized = unicodedata.normalize("NFC", value)
        if not normalized or len(normalized) > 255:
            raise BusinessValidationError("附件文件名长度必须在 1 到 255 之间")
        if normalized in {".", ".."} or normalized.endswith((".", " ")):
            raise BusinessValidationError("附件文件名无效")
        if any(character in normalized for character in ("/", "\\", ":")):
            raise BusinessValidationError("附件文件名不能包含路径字符")
        if any(ord(character) < 32 or ord(character) == 127 for character in normalized):
            raise BusinessValidationError("附件文件名不能包含控制字符")
        stem = normalized.split(".", maxsplit=1)[0].casefold()
        if stem in _WINDOWS_RESERVED:
            raise BusinessValidationError("附件文件名是 Windows 保留名称")
        return normalized

    async def _ensure_directories(self) -> None:
        await asyncio.to_thread(self._root.mkdir, parents=True, exist_ok=True)
        if self._root.is_symlink():
            raise BusinessValidationError("附件根目录不能是符号链接")
        await asyncio.to_thread(
            (self._root / ".tmp").mkdir,
            parents=True,
            exist_ok=True,
        )
        await asyncio.to_thread(
            (self._root / ".trash").mkdir,
            parents=True,
            exist_ok=True,
        )

    def _resolve_under_root(self, path: Path) -> Path:
        resolved = path.resolve()
        if resolved == self._root or not resolved.is_relative_to(self._root):
            raise BusinessValidationError("附件路径越过本机存储根目录")
        return resolved

    def _validate_and_normalize(
        self,
        path: Path,
        declared_type: str,
        filename: str,
    ) -> tuple[str, bool]:
        if declared_type.startswith("image/"):
            actual_type = self._normalize_image(path)
            self._require_declared_type_and_extension(filename, actual_type)
            return actual_type, True
        if declared_type == "application/pdf":
            with path.open("rb") as handle:
                if handle.read(5) != b"%PDF-":
                    raise BusinessValidationError("文件内容不是有效 PDF")
            return declared_type, False
        if declared_type == "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet":
            self._validate_xlsx(path)
            return declared_type, False
        data = path.read_bytes()
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise BusinessValidationError("文本附件必须使用 UTF-8 编码") from exc
        if declared_type == "application/json":
            try:
                json.loads(text)
            except json.JSONDecodeError as exc:
                raise BusinessValidationError("JSON 附件内容无效") from exc
        return declared_type, False

    def _normalize_image(self, path: Path) -> str:
        normalized_path = path.with_suffix(".normalized")
        try:
            with warnings.catch_warnings():
                warnings.simplefilter("error", Image.DecompressionBombWarning)
                with Image.open(path) as probe:
                    image_format = probe.format or ""
                    width, height = probe.size
                    probe.verify()
                if image_format not in _IMAGE_FORMATS:
                    raise BusinessValidationError("只允许 JPEG、PNG 或 WEBP 图片")
                if width <= 0 or height <= 0 or width * height > self._max_image_pixels:
                    raise BusinessValidationError(
                        f"图片像素数不能超过 {self._max_image_pixels}"
                    )
                with Image.open(path) as source:
                    normalized = ImageOps.exif_transpose(source)
                    normalized.load()
                    media_type, output_format = _IMAGE_FORMATS[image_format]
                    save_options: dict[str, object] = {}
                    if output_format == "JPEG":
                        if normalized.mode not in {"RGB", "L"}:
                            normalized = normalized.convert("RGB")
                        save_options = {"quality": 92, "optimize": True}
                    elif output_format == "PNG":
                        save_options = {"optimize": True}
                    elif output_format == "WEBP":
                        save_options = {"quality": 90, "method": 4}
                    normalized.save(normalized_path, format=output_format, **save_options)
            os.replace(normalized_path, path)
            return media_type
        except BusinessValidationError:
            raise
        except (UnidentifiedImageError, OSError, ValueError) as exc:
            raise BusinessValidationError("图片内容损坏或格式不受支持") from exc
        finally:
            if normalized_path.exists():
                normalized_path.unlink()

    @staticmethod
    def _validate_xlsx(path: Path) -> None:
        try:
            with zipfile.ZipFile(path) as workbook:
                entries = workbook.infolist()
                if len(entries) > 1000:
                    raise BusinessValidationError("XLSX 内部文件数量过多")
                names = {entry.filename for entry in entries}
                if "[Content_Types].xml" not in names or "xl/workbook.xml" not in names:
                    raise BusinessValidationError("文件内容不是有效 XLSX")
                expanded_size = 0
                for entry in entries:
                    pure_name = Path(entry.filename.replace("\\", "/"))
                    if pure_name.is_absolute() or ".." in pure_name.parts:
                        raise BusinessValidationError("XLSX 包含不安全路径")
                    expanded_size += entry.file_size
                    if expanded_size > 100 * 1024 * 1024:
                        raise BusinessValidationError("XLSX 解压后内容过大")
        except zipfile.BadZipFile as exc:
            raise BusinessValidationError("文件内容不是有效 XLSX") from exc

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    @staticmethod
    def _require_declared_type_and_extension(filename: str, media_type: str) -> None:
        extensions = _ALLOWED_EXTENSIONS.get(media_type)
        if extensions is None:
            raise BusinessValidationError("附件 MIME 类型不在允许列表中")
        if Path(filename).suffix.casefold() not in extensions:
            raise BusinessValidationError("附件扩展名与 MIME 类型不匹配")

    @staticmethod
    async def _unlink_if_exists(path: Path) -> None:
        try:
            await asyncio.to_thread(path.unlink, missing_ok=True)
        except OSError:
            # 原始业务异常优先；残留 .part 文件仍位于隔离的本机临时目录。
            pass
