"""Fail-closed deployment entry point for ``app.ci_lab.main:app``."""

from __future__ import annotations

import os
from pathlib import Path

from app.ci_lab.app import create_ci_lab_app
from app.ci_lab.database import require_local_filesystem_path


_DATABASE_PATH_ENV = "CI_LAB_DATABASE_PATH"
_TOKEN_FILE_ENV = "CI_LAB_MACHINE_TOKEN_FILE"
_MAX_TOKEN_FILE_BYTES = 4096


def _required_absolute_path(name: str) -> Path:
    raw = os.environ.get(name, "")
    if not raw:
        raise RuntimeError(f"{name} is required")
    try:
        selected = require_local_filesystem_path(raw)
    except ValueError as error:
        raise RuntimeError(f"{name} must be a local filesystem path") from error
    if not selected.is_absolute():
        raise RuntimeError(f"{name} must be an absolute local path")
    return selected


def _load_machine_token(path: Path) -> str:
    if path.is_symlink() or not path.is_file():
        raise RuntimeError("CI Lab machine token file must be a regular file")
    size = path.stat().st_size
    if not 1 <= size <= _MAX_TOKEN_FILE_BYTES:
        raise RuntimeError("CI Lab machine token file has an invalid size")
    try:
        # A normal secret file may end in newlines. Other surrounding
        # whitespace remains visible to the strict token validator and fails
        # closed instead of silently changing the credential.
        return path.read_text(encoding="utf-8").rstrip("\r\n")
    except UnicodeError as error:
        raise RuntimeError("CI Lab machine token file must be UTF-8") from error


app = create_ci_lab_app(
    database_path=_required_absolute_path(_DATABASE_PATH_ENV),
    machine_token=_load_machine_token(_required_absolute_path(_TOKEN_FILE_ENV)),
)


__all__ = ["app"]
