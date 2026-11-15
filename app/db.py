"""SQLite access layer.

The database is shared by two API instances over the shared volume.
WAL mode allows concurrent readers while one writer holds the lock, and
every multi-statement mutation runs inside a single `BEGIN IMMEDIATE`
transaction so cross-instance races are serialised by SQLite itself.
"""
from __future__ import annotations

import sqlite3
import threading
from contextlib import contextmanager
from typing import Iterator

from . import config

# One connection per thread. The background worker thread and the request
# handler threads therefore never share a connection.
_local = threading.local()


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(
        config.DB_PATH,
        timeout=30.0,
        isolation_level=None,  # autocommit; transactions are managed explicitly
        check_same_thread=False,
    )
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=FULL")
    conn.execute("PRAGMA busy_timeout=30000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def get_conn() -> sqlite3.Connection:
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = _connect()
        _local.conn = conn
    return conn


@contextmanager
def locked_tx() -> Iterator[sqlite3.Connection]:
    """Serialised write transaction (one writer across all instances)."""
    conn = get_conn()
    conn.execute("BEGIN IMMEDIATE")
    try:
        yield conn
        conn.execute("COMMIT")
    except Exception:
        conn.execute("ROLLBACK")
        raise


SCHEMA = """
CREATE TABLE IF NOT EXISTS packages (
    package_id   TEXT PRIMARY KEY,
    status       TEXT NOT NULL,            -- uploading|rewriting|ready|failed
    line_count   INTEGER NOT NULL DEFAULT 0,
    updated_at   TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS records (
    package_id    TEXT NOT NULL REFERENCES packages(package_id),
    record_id     TEXT NOT NULL,
    respondent_id TEXT NOT NULL,
    text          TEXT NOT NULL,
    PRIMARY KEY (package_id, record_id)
);
CREATE INDEX IF NOT EXISTS idx_records_respondent
    ON records(respondent_id);

CREATE TABLE IF NOT EXISTS withdrawals (
    respondent_id TEXT PRIMARY KEY,
    reason        TEXT,
    created_at    TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);

CREATE TABLE IF NOT EXISTS rewrite_jobs (
    id               INTEGER PRIMARY KEY AUTOINCREMENT,
    package_id       TEXT NOT NULL REFERENCES packages(package_id),
    status           TEXT NOT NULL,        -- pending|running|succeeded|failed
    attempts         INTEGER NOT NULL DEFAULT 0,
    last_error       TEXT,
    run_after        TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    locked_by        TEXT,
    locked_at        TEXT,
    created_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
    updated_at       TEXT NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now'))
);
CREATE INDEX IF NOT EXISTS idx_jobs_claim
    ON rewrite_jobs(status, run_after);
CREATE INDEX IF NOT EXISTS idx_jobs_package
    ON rewrite_jobs(package_id, status);
"""


def init_db() -> None:
    config.ensure_dirs()
    conn = get_conn()
    conn.executescript(SCHEMA)


def is_withdrawn(conn: sqlite3.Connection, respondent_id: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM withdrawals WHERE respondent_id = ?", (respondent_id,)
    ).fetchone()
    return row is not None
