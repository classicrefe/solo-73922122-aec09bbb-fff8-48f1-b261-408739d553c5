"""Corpus file storage on the shared volume.

The authoritative deliverable for each package is `<packages>/<package_id>.jsonl`.
Writes always go through a unique temporary file + fsync + os.replace, which is
atomic on a single filesystem: a downloader either sees the complete old file
or the complete new file, never a half-written one.
"""
from __future__ import annotations

import json
import os
import re
import uuid
from pathlib import Path

from . import config

_ID_RE = re.compile(f"^{config.ID_PATTERN}$")


class InvalidId(ValueError):
    pass


def validate_id(value: str, kind: str = "id") -> str:
    if not isinstance(value, str) or not _ID_RE.match(value):
        raise InvalidId(
            f"invalid {kind}: must be 1-64 chars of letters, digits, '.', '_' or '-'"
        )
    return value


def package_file(package_id: str) -> Path:
    validate_id(package_id, "package_id")
    return config.PACKAGES_DIR / f"{package_id}.jsonl"


def _fsync_dir(path: Path) -> None:
    fd = os.open(path, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def atomic_write_jsonl(path: Path, rows: list[dict]) -> None:
    """Write rows as JSONL and atomically replace path.

    A unique temp name per call makes concurrent writers (upload vs rewrite,
    or two instances) never collide. The directory fsync guarantees the
    replacement survives a host crash.
    """
    tmp = config.TMP_DIR / f".{path.stem}.{uuid.uuid4().hex}.tmp"
    payload = "".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
    except BaseException:
        tmp.unlink(missing_ok=True)
        raise
    os.replace(tmp, path)
    _fsync_dir(path.parent)


def read_jsonl(path: Path) -> list[dict]:
    with open(path, "r", encoding="utf-8") as fh:
        return [json.loads(line) for line in fh if line.strip()]
