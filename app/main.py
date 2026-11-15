"""FastAPI application: corpus package upload/download and consent withdrawal."""
from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI, HTTPException, Path as PathParam, Request, UploadFile
from fastapi.responses import FileResponse, RedirectResponse

from . import config, db, schemas, storage, worker
from .schemas import JobOut, PackageOut, WithdrawalAck, WithdrawalOut
from .storage import InvalidId

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s %(message)s",
)
log = logging.getLogger("corpus.api")

# Populated by lifespan; used only by the test-only shutdown endpoint.
_SERVER_HOLDER: dict = {"server": None}


@asynccontextmanager
async def lifespan(app: FastAPI):
    db.init_db()
    thread, stop_event = worker.start_worker()
    log.info("API ready, data dir %s, instance %s", config.DATA_DIR, worker.INSTANCE_ID)
    try:
        yield
    finally:
        stop_event.set()
        thread.join(timeout=10)

app = FastAPI(
    title="语音转写语料包授权管理 API",
    description=(
        "按包号上传 JSONL 转写语料，按受访者号撤回授权。"
        "撤回后受影响包进入 rewriting 状态并重写，期间下载返回 409 处理中。"
    ),
    version="1.0.0",
    lifespan=lifespan,
)


def _check_id(value: str, kind: str) -> str:
    try:
        return storage.validate_id(value, kind)
    except InvalidId as exc:
        raise HTTPException(status_code=400, detail=str(exc)) from exc


@app.get("/", include_in_schema=False)
def root():
    return RedirectResponse(url="/docs")


@app.get("/health", tags=["system"])
def health():
    return {"status": "ok", "instance": worker.INSTANCE_ID}


@app.post("/test-shutdown", include_in_schema=False, tags=["system"])
async def test_shutdown():
    """Graceful self-termination for self-tests in environments where the
    kill() syscall is unavailable. Disabled unless ENABLE_TEST_SHUTDOWN=1."""
    import asyncio
    import os

    if os.environ.get("ENABLE_TEST_SHUTDOWN") != "1":
        raise HTTPException(status_code=404, detail={"error": "not_found"})

    server = _SERVER_HOLDER["server"]
    if server is None:
        raise HTTPException(status_code=500, detail={"error": "no_server_handle"})

    async def _stop():
        await asyncio.sleep(0.05)
        server.should_exit = True

    asyncio.create_task(_stop())
    return {"shutting_down": True}


# --------------------------------------------------------------------------- #
# Packages
# --------------------------------------------------------------------------- #
@app.post(
    "/packages/{package_id}",
    response_model=PackageOut,
    status_code=201,
    tags=["packages"],
    summary="上传一个语料包（最多 100 行 JSONL，包号不可复用）",
)
async def upload_package(
    request: Request,
    package_id: str = PathParam(..., pattern=config.ID_PATTERN),
    file: UploadFile | None = None,
):
    _check_id(package_id, "package_id")

    if file is not None:
        raw = await file.read()
    else:
        raw = await request.body()

    try:
        rows = schemas.parse_jsonl_payload(raw)
    except ValueError as exc:
        raise HTTPException(status_code=422, detail=str(exc)) from exc

    respondent_ids = sorted({r.respondent_id for r in rows})
    canonical = [r.model_dump() for r in rows]

    # Phase 1: reserve the package id and commit all records.
    try:
        with db.locked_tx() as conn:
            existing = conn.execute(
                "SELECT 1 FROM packages WHERE package_id=?", (package_id,)
            ).fetchone()
            if existing is not None:
                raise HTTPException(
                    status_code=409,
                    detail={
                        "error": "package_id_already_used",
                        "package_id": package_id,
                        "message": "包号不可复用",
                    },
                )

            placeholders = ",".join("?" for _ in respondent_ids)
            blocked = conn.execute(
                f"SELECT respondent_id FROM withdrawals "
                f"WHERE respondent_id IN ({placeholders})",
                respondent_ids,
            ).fetchall()
            if blocked:
                raise HTTPException(
                    status_code=403,
                    detail={
                        "error": "respondent_withdrawn",
                        "withdrawn_respondents": [r["respondent_id"] for r in blocked],
                        "message": "含已撤回授权受访者，整包拒收",
                    },
                )

            conn.execute(
                "INSERT INTO packages(package_id, status, line_count) "
                "VALUES (?, ?, 0)",
                (package_id, config.STATUS_UPLOADING),
            )
            conn.executemany(
                "INSERT INTO records(package_id, record_id, respondent_id, text) "
                "VALUES (?, ?, ?, ?)",
                [(package_id, r["record_id"], r["respondent_id"], r["text"])
                 for r in canonical],
            )
    except HTTPException:
        raise
    except Exception as exc:
        log.exception("upload phase 1 failed for %s", package_id)
        raise HTTPException(status_code=500, detail=f"database error: {exc}") from exc

    # Phase 2: materialise the deliverable atomically on the shared volume.
    try:
        storage.atomic_write_jsonl(storage.package_file(package_id), canonical)
    except Exception as exc:
        log.exception("file write failed for %s", package_id)
        with db.locked_tx() as conn:
            conn.execute(
                "UPDATE packages SET status='failed', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE package_id=?",
                (package_id,),
            )
        raise HTTPException(
            status_code=500,
            detail={"error": "file_write_failed", "package_id": package_id},
        ) from exc

    # Phase 3: publish. A withdrawal may have landed between phase 1 and the
    # file write; if a rewrite job now exists, stay in 'rewriting'.
    with db.locked_tx() as conn:
        active_job = conn.execute(
            "SELECT 1 FROM rewrite_jobs WHERE package_id=? "
            "AND status IN ('pending','running','failed') LIMIT 1",
            (package_id,),
        ).fetchone()
        new_status = (
            config.STATUS_REWRITING if active_job else config.STATUS_READY
        )
        conn.execute(
            "UPDATE packages SET status=?, line_count=?, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE package_id=?",
            (new_status, len(canonical), package_id),
        )
        pkg = conn.execute(
            "SELECT * FROM packages WHERE package_id=?", (package_id,)
        ).fetchone()

    return PackageOut(**dict(pkg))


@app.get("/packages", response_model=list[PackageOut], tags=["packages"],
         summary="列出语料包")
def list_packages(limit: int = 100, offset: int = 0):
    limit = max(1, min(limit, 500))
    offset = max(0, offset)
    rows = db.get_conn().execute(
        "SELECT * FROM packages ORDER BY package_id LIMIT ? OFFSET ?",
        (limit, offset),
    ).fetchall()
    return [PackageOut(**dict(r)) for r in rows]


@app.get("/packages/{package_id}", response_model=PackageOut, tags=["packages"],
         summary="查询语料包状态与行数")
def get_package(package_id: str = PathParam(..., pattern=config.ID_PATTERN)):
    row = db.get_conn().execute(
        "SELECT * FROM packages WHERE package_id=?", (package_id,)
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "package_not_found"})
    return PackageOut(**dict(row))


@app.get("/packages/{package_id}/lines", tags=["packages"],
         summary="查询语料包行数（ready 时为权威值）")
def get_lines(package_id: str = PathParam(..., pattern=config.ID_PATTERN)):
    row = db.get_conn().execute(
        "SELECT package_id, status, line_count FROM packages WHERE package_id=?",
        (package_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "package_not_found"})
    return {
        "package_id": row["package_id"],
        "status": row["status"],
        "line_count": row["line_count"] if row["status"] == config.STATUS_READY else None,
        "last_known_line_count": row["line_count"],
        "message": (
            "line_count authoritative only when status=ready"
            if row["status"] != config.STATUS_READY
            else "ok"
        ),
    }


@app.get("/packages/{package_id}/download", tags=["packages"],
         summary="下载语料包 JSONL（仅 ready 可下载，处理中返回 409）")
def download_package(package_id: str = PathParam(..., pattern=config.ID_PATTERN)):
    row = db.get_conn().execute(
        "SELECT status, line_count FROM packages WHERE package_id=?",
        (package_id,),
    ).fetchone()
    if row is None:
        raise HTTPException(status_code=404, detail={"error": "package_not_found"})

    if row["status"] != config.STATUS_READY:
        # Never deliver content that might still contain withdrawn records.
        raise HTTPException(
            status_code=409,
            detail={
                "error": "package_not_ready",
                "package_id": package_id,
                "status": row["status"],
                "retryable": True,
                "message": (
                    "包正在重写处理中，请稍后重试"
                    if row["status"] == config.STATUS_REWRITING
                    else "包暂不可下载（处理中或失败待重试）"
                ),
            },
        )

    path = storage.package_file(package_id)
    if not path.exists():
        # File vanished from the shared volume: force a retryable state rather
        # than serving nothing or stale data.
        with db.locked_tx() as conn:
            conn.execute(
                "UPDATE packages SET status='failed', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE package_id=? AND status='ready'",
                (package_id,),
            )
        raise HTTPException(
            status_code=409,
            detail={"error": "package_file_missing", "status": "failed",
                    "retryable": True},
        )

    return FileResponse(
        path,
        media_type="application/x-ndjson",
        filename=f"{package_id}.jsonl",
    )


# --------------------------------------------------------------------------- #
# Withdrawals
# --------------------------------------------------------------------------- #
@app.post(
    "/withdrawals/{respondent_id}",
    response_model=WithdrawalAck,
    status_code=202,
    tags=["withdrawals"],
    summary="按受访者号撤回授权（持久化，受影响包排队重写）",
)
def withdraw(
    respondent_id: str = PathParam(..., pattern=config.ID_PATTERN),
    reason: str | None = None,
):
    _check_id(respondent_id, "respondent_id")

    with db.locked_tx() as conn:
        already = db.is_withdrawn(conn, respondent_id)
        conn.execute(
            "INSERT INTO withdrawals(respondent_id, reason) VALUES (?, ?) "
            "ON CONFLICT(respondent_id) DO UPDATE SET "
            "reason=COALESCE(excluded.reason, withdrawals.reason)",
            (respondent_id, reason),
        )

        affected = [
            r["package_id"]
            for r in conn.execute(
                "SELECT DISTINCT package_id FROM records "
                "WHERE respondent_id=? ORDER BY package_id",
                (respondent_id,),
            ).fetchall()
        ]

        for package_id in affected:
            active = conn.execute(
                "SELECT 1 FROM rewrite_jobs WHERE package_id=? "
                "AND status IN ('pending','running','failed') LIMIT 1",
                (package_id,),
            ).fetchone()
            if active is None:
                conn.execute(
                    "INSERT INTO rewrite_jobs(package_id, status) VALUES (?, 'pending')",
                    (package_id,),
                )
            # Block downloads immediately; the worker finishes the rewrite.
            conn.execute(
                "UPDATE packages SET status='rewriting', "
                "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') "
                "WHERE package_id=? AND status='ready'",
                (package_id,),
            )

    log.info("withdrawal accepted for %s (already=%s, affected=%s)",
             respondent_id, already, affected)
    return WithdrawalAck(
        respondent_id=respondent_id,
        already_withdrawn=already,
        affected_packages=affected,
        pending_jobs=len(affected),
    )


@app.get("/withdrawals", response_model=list[WithdrawalOut], tags=["withdrawals"],
         summary="撤回名单（持久化）")
def list_withdrawals():
    rows = db.get_conn().execute(
        "SELECT * FROM withdrawals ORDER BY created_at"
    ).fetchall()
    return [WithdrawalOut(**dict(r)) for r in rows]


# --------------------------------------------------------------------------- #
# Rewrite jobs (observability + manual retry)
# --------------------------------------------------------------------------- #
@app.get("/jobs", response_model=list[JobOut], tags=["jobs"],
         summary="查看重写任务状态")
def list_jobs(package_id: str | None = None):
    if package_id:
        rows = db.get_conn().execute(
            "SELECT * FROM rewrite_jobs WHERE package_id=? ORDER BY id",
            (package_id,),
        ).fetchall()
    else:
        rows = db.get_conn().execute(
            "SELECT * FROM rewrite_jobs ORDER BY id DESC LIMIT 200"
        ).fetchall()
    return [JobOut(**dict(r)) for r in rows]


@app.post("/jobs/{job_id}/retry", response_model=JobOut, tags=["jobs"],
          summary="手动重试失败的重写任务（保留其他记录）")
def retry_job(job_id: int):
    with db.locked_tx() as conn:
        row = conn.execute(
            "SELECT * FROM rewrite_jobs WHERE id=?", (job_id,)
        ).fetchone()
        if row is None:
            raise HTTPException(status_code=404, detail={"error": "job_not_found"})
        if row["status"] not in ("failed", "pending"):
            raise HTTPException(
                status_code=409,
                detail={"error": "job_not_retryable", "status": row["status"]},
            )
        conn.execute(
            "UPDATE rewrite_jobs SET status='pending', "
            "run_after=strftime('%Y-%m-%dT%H:%M:%fZ','now'), "
            "last_error=NULL, locked_by=NULL, locked_at=NULL, "
            "updated_at=strftime('%Y-%m-%dT%H:%M:%fZ','now') WHERE id=?",
            (job_id,),
        )
        conn.execute(
            "UPDATE packages SET status='rewriting' WHERE package_id=? "
            "AND status='failed'",
            (row["package_id"],),
        )
        row = conn.execute(
            "SELECT * FROM rewrite_jobs WHERE id=?", (job_id,)
        ).fetchone()
    return JobOut(**dict(row))
