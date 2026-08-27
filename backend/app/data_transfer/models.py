from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from app.schemas.data_transfer import TransferEntity, TransferFormat


@dataclass(frozen=True, slots=True)
class RawImportRow:
    sheet: str
    row: int
    values: dict[str, Any]


@dataclass(frozen=True, slots=True)
class ParsedImport:
    entity: TransferEntity
    filename: str
    transfer_format: TransferFormat
    sha256: str
    template_version: str
    main_sheet: str
    main_headers: tuple[str, ...]
    main_rows: tuple[RawImportRow, ...]
    child_sheet: str | None = None
    child_headers: tuple[str, ...] = ()
    child_rows: tuple[RawImportRow, ...] = ()
    metadata: dict[str, str] = field(default_factory=dict)


class DataTransferFileError(ValueError):
    """Fatal file-level validation error suitable for a 4xx response."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


class SpreadsheetDependencyError(RuntimeError):
    def __init__(self) -> None:
        super().__init__(
            "XLSX 功能需要安装 openpyxl；CSV 功能不受影响"
        )
