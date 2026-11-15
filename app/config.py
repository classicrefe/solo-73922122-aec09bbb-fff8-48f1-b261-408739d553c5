"""Runtime configuration.

All state lives under DATA_DIR so two API instances can share one volume
(SQLite database + corpus JSONL files) in docker-compose.
"""
from __future__ import annotations

import os
from pathlib import Path

DATA_DIR = Path(os.environ.get("CORPUS_DATA_DIR", "/data")).resolve()
DB_PATH = DATA_DIR / "corpus.db"
PACKAGES_DIR = DATA_DIR / "packages"
TMP_DIR = DATA_DIR / "tmp"

MAX_LINES_PER_UPLOAD = 100
# Hard cap on raw upload body size (100 short transcriptions is well under this).
MAX_UPLOAD_BYTES = 10 * 1024 * 1024

# A job whose lock is older than this is considered orphaned (instance died
# while rewriting) and may be claimed again by any instance.
JOB_STALE_SECONDS = int(os.environ.get("JOB_STALE_SECONDS", "30"))
WORKER_POLL_INTERVAL = float(os.environ.get("WORKER_POLL_INTERVAL", "0.5"))
MAX_BACKOFF_SECONDS = 30.0

# Package / respondent identifiers are used as part of file paths and in URLs,
# so restrict them to a safe charset. This also blocks path traversal.
ID_PATTERN = r"[A-Za-z0-9._-]{1,64}"

STATUS_UPLOADING = "uploading"
STATUS_REWRITING = "rewriting"
STATUS_READY = "ready"
STATUS_FAILED = "failed"

JOB_PENDING = "pending"
JOB_RUNNING = "running"
JOB_SUCCEEDED = "succeeded"
JOB_FAILED = "failed"


def ensure_dirs() -> None:
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    PACKAGES_DIR.mkdir(parents=True, exist_ok=True)
    TMP_DIR.mkdir(parents=True, exist_ok=True)
