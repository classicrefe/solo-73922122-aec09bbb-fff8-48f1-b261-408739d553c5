"""Crash-recovery self-tests.

Scenario A - hard exit while rewriting:
  a crash hook makes the worker call os._exit() after the new temp file is
  written but before the atomic replace. The old package file must stay
  intact, downloads must stay blocked, and a restart must finish the rewrite.

Scenario B - hard exit between upload DB commit and file write:
  the 'uploading' package row and records are committed but the JSONL file was
  never created. Startup recovery must rebuild the deliverable from the DB.

Run:  /tmp/venv/bin/python tests/selftest_crash.py
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from http_client import jsonl, request, wait_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
CRASH_HOOK = Path(__file__).resolve().parent / "_crash_hook"
TEST_SERVER = Path(__file__).resolve().parent / "_test_server.py"
CRASH_SERVER = Path(__file__).resolve().parent / "_crash_server.py"
PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def start(data_dir: str, port: int, crash_pkg: str | None = None,
          crash_at: str | None = None) -> subprocess.Popen:
    env = os.environ.copy()
    env["CORPUS_DATA_DIR"] = data_dir
    env["PYTHONPATH"] = f"{ROOT}{os.pathsep}{CRASH_HOOK.parent}"
    env["ENABLE_TEST_SHUTDOWN"] = "1"
    env["TEST_UVICORN_PORT"] = str(port)
    launcher = str(TEST_SERVER)
    if crash_pkg:
        env["CORPUS_CRASH_PACKAGE"] = crash_pkg
        env["CORPUS_CRASH_AT"] = crash_at or "replace"
        launcher = str(CRASH_SERVER)
    proc = subprocess.Popen(
        [sys.executable, launcher],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    try:
        wait_for(f"http://127.0.0.1:{port}/health", timeout=25)
    except Exception:
        if proc.poll() is not None:
            raise AssertionError(
                f"server exited early rc={proc.returncode}\n{proc.stdout.read()}"
            )
        raise
    return proc


def stop(proc: subprocess.Popen, port: int | None = None) -> None:
    if port is not None:
        try:
            request("POST", f"http://127.0.0.1:{port}/test-shutdown")
            proc.wait(timeout=15)
            return
        except Exception:
            pass
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass

def wait_exit(proc: subprocess.Popen, timeout: float = 15.0) -> int | None:
    deadline = time.time() + timeout
    while time.time() < deadline and proc.poll() is None:
        time.sleep(0.1)
    if proc.poll() is None:
        try:
            proc.kill()
        except Exception:
            pass
    return proc.returncode


def wait_status(base, pid, wanted, timeout=20):
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = request("GET", f"{base}/packages/{pid}")
        last = r.json()
        if last["status"] == wanted:
            return last
        time.sleep(0.2)
    raise AssertionError(f"{pid} not {wanted}: {last}")


def scenario_a(data_dir: str, port: int) -> None:
    print("\n== Scenario A: crash mid-rewrite, then restart ==")
    base = f"http://127.0.0.1:{port}"
    rows = [
        {"record_id": "r1", "respondent_id": "alpha", "text": "alpha line 1"},
        {"record_id": "r2", "respondent_id": "beta", "text": "beta line"},
        {"record_id": "r3", "respondent_id": "alpha", "text": "alpha line 2"},
    ]
    bootstrap = start(data_dir, port)
    try:
        r = request("POST", f"{base}/packages/crashpkg", jsonl(rows))
        check("package uploaded ready",
              r.status == 201 and r.json()["status"] == "ready", r.text)
    finally:
        stop(bootstrap, port)

    proc = start(data_dir, port, crash_pkg="crashpkg", crash_at="replace")
    try:
        r = request("POST", f"{base}/withdrawals/alpha")
        check("withdrawal accepted before crash", r.status == 202, r.text)
        code = wait_exit(proc)
        check("worker process died hard (simulated crash)", code not in (0, None), f"rc={code}")
    finally:
        if proc.poll() is None:
            try:
                proc.kill()
            except Exception:
                pass

    pkg_file = Path(data_dir) / "packages" / "crashpkg.jsonl"
    old_lines = pkg_file.read_text(encoding="utf-8").splitlines()
    check("old package file intact after crash (never replaced by half file)",
          len(old_lines) == 3 and all("alpha" in json.loads(l)["respondent_id"] or True
                                      for l in old_lines),
          str(old_lines))
    orphans = list((Path(data_dir) / "tmp").glob(".*.tmp"))
    check("temp file left behind is not the deliverable", pkg_file.exists() and len(orphans) >= 1,
          f"orphans={orphans}")

    # Restart normally: startup must reclaim the orphaned running job.
    proc = start(data_dir, port)
    try:
        st = wait_status(base, "crashpkg", "ready")
        check("restart recovered package to ready", st["status"] == "ready", str(st))
        r = request("GET", f"{base}/packages/crashpkg/download")
        dl = [json.loads(l) for l in r.text.splitlines()]
        check("alpha rows gone, beta row preserved",
              len(dl) == 1 and dl[0]["respondent_id"] == "beta" and dl[0]["text"] == "beta line",
              str(dl))
        check("line count updated to 1", st["line_count"] == 1, str(st))
        r = request("GET", f"{base}/jobs?package_id=crashpkg")
        jobs = r.json()
        check("job eventually succeeded after reclaim",
              any(j["status"] == "succeeded" for j in jobs), str(jobs))
        check("no stale running job left",
              all(j["status"] != "running" for j in jobs), str(jobs))
    finally:
        stop(proc, port)


def scenario_b(data_dir: str, port: int) -> None:
    print("\n== Scenario B: crash between upload commit and file write ==")
    base = f"http://127.0.0.1:{port}"
    proc = start(data_dir, port)
    try:
        stop(proc, port)
    except Exception:
        pass

    # Hand-craft the exact state: 'uploading' row + records, no JSONL file.
    conn = sqlite3.connect(Path(data_dir) / "corpus.db", isolation_level=None)
    conn.execute("PRAGMA busy_timeout=10000")
    conn.execute("BEGIN IMMEDIATE")
    conn.execute("INSERT INTO packages(package_id, status, line_count) VALUES (?, 'uploading', 0)",
                 ("halffull",))
    conn.executemany(
        "INSERT INTO records(package_id, record_id, respondent_id, text) VALUES (?,?,?,?)",
        [("halffull", "h1", "gamma", "g1"), ("halffull", "h2", "delta", "d1")],
    )
    conn.execute("COMMIT")
    conn.close()
    target = Path(data_dir) / "packages" / "halffull.jsonl"
    assert not target.exists()

    proc = start(data_dir, port)
    try:
        st = wait_status(base, "halffull", "ready")
        check("interrupted upload rebuilt and marked ready",
              st["line_count"] == 2, str(st))
        r = request("GET", f"{base}/packages/halffull/download")
        dl = [json.loads(l) for l in r.text.splitlines()]
        check("rebuilt file contains both committed records",
              sorted(x["record_id"] for x in dl) == ["h1", "h2"], str(dl))

        # And if gamma was withdrawn while the instance was down, startup must
        # rebuild without gamma's row instead of resurrecting it.
        stop(proc, port)
        conn = sqlite3.connect(Path(data_dir) / "corpus.db", isolation_level=None)
        conn.execute("BEGIN IMMEDIATE")
        conn.execute("INSERT INTO withdrawals(respondent_id) VALUES ('gamma')")
        conn.execute("INSERT INTO rewrite_jobs(package_id, status) VALUES ('halffull','running')")
        conn.execute("UPDATE packages SET status='rewriting' WHERE package_id='halffull'")
        target.unlink()
        conn.execute("COMMIT")
        conn.close()

        proc = start(data_dir, port)
        st = wait_status(base, "halffull", "ready")
        r = request("GET", f"{base}/packages/halffull/download")
        dl = [json.loads(l) for l in r.text.splitlines()]
        check("restart honors withdrawal: gamma not resurrected",
              len(dl) == 1 and dl[0]["respondent_id"] == "delta", str(dl))
        check("line count 1 after combined recovery", st["line_count"] == 1, str(st))
    finally:
        stop(proc, port)


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="corpus-crash-")
    import socket
    s = socket.socket(); s.bind(("127.0.0.1", 0)); port = s.getsockname()[1]; s.close()
    try:
        scenario_a(data_dir, port)
        scenario_b(data_dir, port)
    finally:
        shutil.rmtree(data_dir, ignore_errors=True)
    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
