from __future__ import annotations

import hashlib
import re
import zipfile
from io import BytesIO
from pathlib import PurePosixPath

from app.data_transfer.models import DataTransferFileError
from app.schemas.data_transfer import TransferFormat

MAX_UPLOAD_BYTES = 5 * 1024 * 1024
MAX_PRIMARY_ROWS = 5_000
MAX_TOTAL_ROWS = 100_000
MAX_XLSX_UNCOMPRESSED_BYTES = 50 * 1024 * 1024
MAX_XLSX_COMPRESSION_RATIO = 250
MAX_XLSX_ARCHIVE_MEMBERS = 256
MAX_XLSX_COLUMNS = 128
MAX_XLSX_METADATA_ROWS = 100
MAX_EXPOSED_VALUE_CHARS = 160

_DANGEROUS_SPREADSHEET_PREFIXES = ("=", "+", "-", "@", "\t", "\r")
_SAFE_FILENAME = re.compile(r"[^0-9A-Za-z._\-\u4e00-\u9fff]+")


def sha256_hex(content: bytes) -> str:
    return hashlib.sha256(content).hexdigest()


def detect_format(filename: str) -> TransferFormat:
    suffix = PurePosixPath(filename.replace("\\", "/")).suffix.casefold()
    if suffix == ".csv":
        return TransferFormat.CSV
    if suffix == ".xlsx":
        return TransferFormat.XLSX
    raise DataTransferFileError(
        "unsupported_file_type",
        "只支持 .csv 与 .xlsx 文件；不接受 .xls、.xlsm 或其他格式",
    )


def validate_upload(filename: str, content: bytes) -> TransferFormat:
    transfer_format = detect_format(filename)
    if not content:
        raise DataTransferFileError("empty_file", "上传文件不能为空")
    if len(content) > MAX_UPLOAD_BYTES:
        raise DataTransferFileError(
            "file_too_large",
            f"上传文件不能超过 {MAX_UPLOAD_BYTES // (1024 * 1024)} MiB",
        )
    if transfer_format == TransferFormat.CSV:
        if b"\x00" in content:
            raise DataTransferFileError(
                "invalid_csv_bytes",
                "CSV 不能包含 NUL 字节",
            )
    else:
        _validate_xlsx_archive(content)
    return transfer_format


def _validate_xlsx_archive(content: bytes) -> None:
    if not content.startswith(b"PK"):
        raise DataTransferFileError(
            "invalid_xlsx_signature",
            "XLSX 文件签名无效",
        )
    try:
        with zipfile.ZipFile(BytesIO(content)) as archive:
            infos = archive.infolist()
            if not infos:
                raise DataTransferFileError("empty_xlsx", "XLSX 压缩包为空")
            if len(infos) > MAX_XLSX_ARCHIVE_MEMBERS:
                raise DataTransferFileError(
                    "xlsx_too_many_members",
                    f"XLSX 内部文件不能超过 {MAX_XLSX_ARCHIVE_MEMBERS} 个",
                )
            total_uncompressed = 0
            for info in infos:
                normalized = PurePosixPath(info.filename)
                if normalized.is_absolute() or ".." in normalized.parts:
                    raise DataTransferFileError(
                        "unsafe_xlsx_path",
                        "XLSX 包含不安全的内部路径",
                    )
                lowered = info.filename.casefold()
                if (
                    "vbaproject.bin" in lowered
                    or lowered.startswith("xl/externallinks/")
                    or lowered.startswith("xl/embeddings/")
                    or lowered.startswith("xl/oleobjects/")
                ):
                    raise DataTransferFileError(
                        "unsafe_xlsx_content",
                        "XLSX 不能包含宏、外部链接或嵌入对象",
                    )
                total_uncompressed += info.file_size
                if total_uncompressed > MAX_XLSX_UNCOMPRESSED_BYTES:
                    raise DataTransferFileError(
                        "xlsx_uncompressed_too_large",
                        "XLSX 解压后内容过大",
                    )
                if info.compress_size and (
                    info.file_size / info.compress_size
                    > MAX_XLSX_COMPRESSION_RATIO
                ):
                    raise DataTransferFileError(
                        "xlsx_suspicious_compression",
                        "XLSX 压缩比异常",
                    )
            bad_member = archive.testzip()
            if bad_member is not None:
                raise DataTransferFileError(
                    "corrupt_xlsx",
                    f"XLSX 内部文件损坏: {bad_member}",
                )
    except zipfile.BadZipFile as exc:
        raise DataTransferFileError(
            "invalid_xlsx_archive",
            "XLSX 不是有效的 ZIP 文档",
        ) from exc


def neutralize_spreadsheet_text(value: object) -> object:
    """Prevent formula execution when an exported file is opened in Excel."""

    if not isinstance(value, str) or not value:
        return value
    if value.startswith(_DANGEROUS_SPREADSHEET_PREFIXES):
        return "'" + value
    return value


def exposed_value(value: object) -> object:
    """Return a bounded issue value; never echo a whole uploaded cell/file."""

    if value is None or isinstance(value, (bool, int, float)):
        return value
    rendered = str(value)
    if len(rendered) <= MAX_EXPOSED_VALUE_CHARS:
        return rendered
    return rendered[:MAX_EXPOSED_VALUE_CHARS] + "…"


def safe_download_name(value: str) -> str:
    normalized = _SAFE_FILENAME.sub("-", value.strip()).strip(".-")
    return normalized[:120] or "qa-export"
