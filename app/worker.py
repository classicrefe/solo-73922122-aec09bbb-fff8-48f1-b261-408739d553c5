"""Background rewrite worker.

Every API instance runs one worker thread. Jobs are claimed through a
`BEGIN IMMEDIATE` transaction so only one instance works on a given job at a
time. A lock lease (JOB_STALE_SECONDS) makes an instance that dies mid-rewrite
recoverable: another instance (or the same one after restart) reclaims the job.

Failure handling: a failed job stays scheduled with exponential backoff and the
package stays in 'rewriting' state, so downloads return "processing" rather
than stale content, and the job is retried automatically.
"""
from __future__ import annotations

import json
import logging
import os
import socket
import threading
import time
import uuid

from . import config, db, storage

log = logging.getLogger("corpus.worker")
INSTANCE_ID = f"{socket.gethostname()}:{uuid.uuid4().hex[:8]}"

_CLAIM_SQL = """
SELECT * FROM rewrite_jobs
WHERE (
        (status IN ('pending', 'failed')
         AND run_after <= strftime('%Y-%m-%dT%H:%M:%fZ','now'))
       OR (status = 'running'
           AND locked_at < strftime('%Y-%m-%dT%H:%M:%fZ', 'now', ?))
      )
ORDER BY id
LIMIT 1
"""

_BACKOFF = [1, 2, 5, 10, 20, 30]


def _backoff(attempts: int) -> str:
    idx = min(attempts, len(_BACKOFF) - 1)
    return time.strftime(
        "%Y-%m-%dT%H:%M:%fZ", time.gmtime(time.time() + _BACKOFF[idx])
    )


def _stale_modifier() -> str:
    return f"-{config.JOB_STALE_SECONDS} seconds"


def claim_job() -> dict | None:
    """Atomically take the next due job, reclaiming stale leases."""
    with db.locked_tx() as conn:
        row = conn.execute(_CLAIM_SQL, (_stale_modifier(),)).fetchone()
        if row is None:
            return None
        conn.execute(
            """
            UPDATE rewrite_jobs
            SET status='running', attempts=attempts+1, locked_by=?,
                locked_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                last_error=NULL
            WHERE id=?
            """,
            (INSTANCE_ID, row["id"]),
        )
        conn.execute(
            "UPDATE packages SET status='rewriting', "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
            "WHERE package_id=? AND status != 'rewriting'",
            (row["package_id"],),
        )
        return dict(row)


def _execute_job(job: dict) -> None:
    """Rewrite one package file, dropping every withdrawn respondent."""
    package_id = job["package_id"]
    path = storage.package_file(package_id)
    conn = db.get_conn()

    # Fault injection for self-tests/ops drills: a marker file on the shared
    # volume forces the first N attempts of one package's rewrite to fail,
    # exercising the failed -> backoff -> retry path. The marker is removed
    # by the test to let the automatic retry succeed.
    marker = config.TMP_DIR / f"force-fail.{package_id}"
    if marker.exists():
        try:
            limit = int(marker.read_text().strip() or "1")
        except ValueError:
            limit = 1
        if job["attempts"] <= limit:
            raise RuntimeError(
                f"injected rewrite failure for {package_id} (attempt {job['attempts']})"
            )

    pkg = conn.execute(
        "SELECT status FROM packages WHERE package_id=?", (package_id,)
    ).fetchone()
    if pkg is None:
        raise RuntimeError("package no longer exists")
    if pkg["status"] == "uploading":
        # Upload transaction committed but the file is not there yet; wait.
        raise RuntimeError("package upload has not finished writing its file")

    # The database is the authoritative record set. If the deliverable is
    # missing (e.g. host died after deleting/renaming the old file), rebuild
    # from the DB instead of failing forever.
    if path.exists():
        rows = storage.read_jsonl(path)
    else:
        log.warning("package file %s missing; rebuilding from database", path.name)
        rows = [
            dict(r)
            for r in conn.execute(
                "SELECT record_id, respondent_id, text FROM records "
                "WHERE package_id=? ORDER BY rowid",
                (package_id,),
            ).fetchall()
        ]

    withdrawn = {
        r["respondent_id"]
        for r in conn.execute("SELECT respondent_id FROM withdrawals").fetchall()
    }
    kept = [r for r in rows if r.get("respondent_id") not in withdrawn]
    # Rewrite is idempotent: running it twice yields the same file.
    storage.atomic_write_jsonl(path, kept)

    with db.locked_tx() as tx:
        tx.execute(
            """
            DELETE FROM records
            WHERE package_id=?
              AND respondent_id IN (SELECT respondent_id FROM withdrawals)
            """,
            (package_id,),
        )
        remaining = tx.execute(
            "SELECT COUNT(*) AS n FROM records WHERE package_id=?", (package_id,)
        ).fetchone()["n"]

        # If a newer withdrawal already scheduled another job, leave the
        # package in 'rewriting' so no download can observe stale content.
        active = tx.execute(
            "SELECT 1 FROM rewrite_jobs WHERE package_id=? "
            "AND status IN ('pending','running','failed') AND id != ? LIMIT 1",
            (package_id, job["id"]),
        ).fetchone()
        if active is None:
            tx.execute(
                "UPDATE packages SET status='ready', line_count=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE package_id=? AND status='rewriting'",
                (remaining, package_id),
            )
        else:
            tx.execute(
                "UPDATE packages SET line_count=?, "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE package_id=?",
                (remaining, package_id),
            )
        tx.execute(
            "UPDATE rewrite_jobs SET status='succeeded', "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (job["id"],),
        )


def _fail_job(job_id: int, attempts: int, exc: Exception) -> None:
    with db.locked_tx() as conn:
        conn.execute(
            """
            UPDATE rewrite_jobs
            SET status='failed', last_error=?, run_after=?,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE id=?
            """,
            (f"{type(exc).__name__}: {exc}"[:500], _backoff(attempts), job_id),
        )


def _recover_on_startup() -> None:
    """Handle state left by a previous process that exited mid-operation."""
    with db.locked_tx() as conn:
        # Orphaned running jobs: reschedule immediately for retry.
        conn.execute(
            """
            UPDATE rewrite_jobs
            SET status='failed',
                last_error=COALESCE(last_error, 'interrupted by instance restart'),
                run_after=strftime('%Y-%m-%dT%H:%M:%fZ','now'),
                locked_by=NULL, locked_at=NULL,
                updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now')
            WHERE status='running'
            """
        )

        # Uploads interrupted between DB commit and file replacement:
        # rebuild the deliverable from the committed records.
        stuck = conn.execute(
            "SELECT package_id FROM packages WHERE status='uploading'"
        ).fetchall()

    for row in stuck:
        package_id = row["package_id"]
        try:
            with db.locked_tx() as conn:
                records = conn.execute(
                    """
                    SELECT r.record_id, r.respondent_id, r.text
                    FROM records r
                    WHERE r.package_id=?
                      AND r.respondent_id NOT IN
                          (SELECT respondent_id FROM withdrawals)
                    ORDER BY r.rowid
                    """,
                    (package_id,),
                ).fetchall()
                rows = [dict(r) for r in records]
            storage.atomic_write_jsonl(storage.package_file(package_id), rows)
            with db.locked_tx() as conn:
                active_job = conn.execute(
                    "SELECT 1 FROM rewrite_jobs WHERE package_id=? "
                    "AND status IN ('pending','running','failed') LIMIT 1",
                    (package_id,),
                ).fetchone()
                new_status = "rewriting" if active_job else "ready"
                conn.execute(
                    "UPDATE packages SET status=?, line_count=?, "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE package_id=? AND status='uploading'",
                    (new_status, len(rows), package_id),
                )
            log.info("recovered interrupted upload %s -> %s", package_id, new_status)
        except Exception:
            log.exception("failed to recover uploading package %s", package_id)
            with db.locked_tx() as conn:
                conn.execute(
                    "UPDATE packages SET status='failed', "
                    "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                    "WHERE package_id=? AND status='uploading'",
                    (package_id,),
                )

    # Remove temp files abandoned more than an hour ago.
    cutoff = time.time() - 3600
    try:
        for tmp in config.TMP_DIR.glob(".*.tmp"):
            try:
                if tmp.stat().st_mtime < cutoff:
                    tmp.unlink(missing_ok=True)
            except OSError:
                pass
    except OSError:
        pass


def run_worker(stop_event: threading.Event) -> None:
    log.info("worker started (instance %s)", INSTANCE_ID)
    _recover_on_startup()
    while not stop_event.is_set():
        job = None
        try:
            job = claim_job()
            if job is None:
                stop_event.wait(config.WORKER_POLL_INTERVAL)
                continue
            log.info("claimed job %s for package %s (attempt %s)",
                     job["id"], job["package_id"], job["attempts"] + 1)
            _execute_job(job)
            log.info("job %s succeeded", job["id"])
        except Exception as exc:
            if job is not None:
                log.exception("job %s failed, will retry", job["id"])
                try:
                    _fail_job(job["id"], job["attempts"] + 1, exc)
                except Exception:
                    log.exception("could not record failure of job %s", job["id"])
            stop_event.wait(config.WORKER_POLL_INTERVAL)
    log.info("worker stopped (instance %s)", INSTANCE_ID)


def start_worker() -> tuple[threading.Thread, threading.Event]:
    db.init_db()
    stop_event = threading.Event()
    thread = threading.Thread(
        target=run_worker, args=(stop_event,), name="rewrite-worker", daemon=True
    )
    thread.start()
    return thread, stop_event


# Kept for tests / diagnostics.
def write_json_for_debug(path, rows) -> None:  # pragma: no cover
    with open(path, "w", encoding="utf-8") as fh:
        for r in rows:
            fh.write(json.dumps(r, ensure_ascii=False) + "\n")


if os.environ.get("CORPUS_LOG_LEVEL"):
    logging.basicConfig(level=os.environ["CORPUS_LOG_LEVEL"].upper())
