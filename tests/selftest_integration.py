"""End-to-end self-tests against two real uvicorn instances sharing one volume.

Run:  /tmp/venv/bin/python tests/selftest_integration.py
The script starts both instances itself and tears them down afterwards.
"""
from __future__ import annotations

import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from http_client import jsonl, request, wait_for  # noqa: E402

ROOT = Path(__file__).resolve().parent.parent
PASS, FAIL = 0, 0
RESULTS: list[tuple[str, bool, str]] = []


def check(name: str, cond: bool, detail: str = "") -> None:
    global PASS, FAIL
    RESULTS.append((name, bool(cond), detail))
    if cond:
        PASS += 1
        print(f"  PASS  {name}")
    else:
        FAIL += 1
        print(f"  FAIL  {name}  {detail}")


def free_port() -> int:
    s = socket.socket()
    s.bind(("127.0.0.1", 0))
    port = s.getsockname()[1]
    s.close()
    return port


def wait_status(base: str, package_id: str, wanted: str, timeout: float = 15.0) -> dict:
    deadline = time.time() + timeout
    last = {}
    while time.time() < deadline:
        r = request("GET", f"{base}/packages/{package_id}")
        last = r.json()
        if last["status"] == wanted:
            return last
        time.sleep(0.2)
    raise AssertionError(f"{package_id} never reached {wanted}, last={last}")


def start_instance(data_dir: str, port: int) -> subprocess.Popen:
    env = os.environ.copy()
    env["CORPUS_DATA_DIR"] = data_dir
    env["PYTHONPATH"] = str(ROOT)
    env["ENABLE_TEST_SHUTDOWN"] = "1"
    env["TEST_UVICORN_PORT"] = str(port)
    proc = subprocess.Popen(
        [sys.executable, str(Path(__file__).resolve().parent / "_test_server.py")],
        cwd=str(ROOT), env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )
    wait_for(f"http://127.0.0.1:{port}/health")
    return proc


def shutdown_instance(base: str, proc: subprocess.Popen) -> None:
    try:
        request("POST", f"{base}/test-shutdown")
        proc.wait(timeout=15)
        return
    except Exception:
        pass
    # Fallback for environments that do allow signals.
    try:
        proc.send_signal(signal.SIGTERM)
        proc.wait(timeout=10)
    except Exception:
        try:
            proc.kill()
        except Exception:
            pass


def main() -> int:
    data_dir = tempfile.mkdtemp(prefix="corpus-selftest-")
    port1, port2 = free_port(), free_port()
    b1, b2 = f"http://127.0.0.1:{port1}", f"http://127.0.0.1:{port2}"
    procs = []
    try:
        print("== starting two uvicorn instances sharing", data_dir)
        procs = [start_instance(data_dir, port1), start_instance(data_dir, port2)]

        print("\n== 1. upload happy path (routed to instance 2) ==")
        rows = [
            {"record_id": "r1", "respondent_id": "s1", "text": "hello world"},
            {"record_id": "r2", "respondent_id": "s2", "text": "second line"},
            {"record_id": "r3", "respondent_id": "s1", "text": "third line"},
        ]
        r = request("POST", f"{b2}/packages/pkga", jsonl(rows))
        check("upload returns 201 + ready", r.status == 201 and r.json()["status"] == "ready",
              f"status={r.status} body={r.text[:200]}")

        r = request("GET", f"{b1}/packages/pkga/lines")  # other instance
        check("line count visible on other instance", r.status == 200 and r.json()["line_count"] == 3,
              r.text)

        r = request("GET", f"{b1}/packages/pkga/download")
        dl = [json.loads(l) for l in r.text.splitlines()]
        check("download from other instance gives 3 rows",
              r.status == 200 and len(dl) == 3 and dl[0]["record_id"] == "r1",
              f"status={r.status}")
        check("download content-type is ndjson",
              r.headers.get_content_type() == "application/x-ndjson", r.headers.get("Content-Type"))

        print("\n== 2. validation ==")
        r = request("POST", f"{b1}/packages/pkg-bad1",
                    jsonl([{"record_id": "a", "respondent_id": "s1", "text": "x"}])
                    + "{not json}\n")
        check("invalid JSON line -> 422", r.status == 422, r.text)

        dup = [{"record_id": "x", "respondent_id": "s1", "text": "a"},
               {"record_id": "x", "respondent_id": "s2", "text": "b"}]
        r = request("POST", f"{b1}/packages/pkg-bad2", jsonl(dup))
        check("duplicate record id -> 422", r.status == 422, r.text)

        big = [{"record_id": f"r{i}", "respondent_id": "s", "text": "t"} for i in range(101)]
        r = request("POST", f"{b1}/packages/pkg-bad3", jsonl(big))
        check("101 lines -> 422", r.status == 422 and "100" in r.text, r.text)

        r = request("POST", f"{b1}/packages/pkg-bad4", jsonl([
            {"record_id": "r", "respondent_id": "s"}
        ]))
        check("missing field -> 422", r.status == 422, r.text)

        r = request("POST", f"{b1}/packages/../evil", jsonl([
            {"record_id": "r", "respondent_id": "s", "text": "x"}
        ]))
        check("path traversal id rejected", r.status in (400, 404), f"status={r.status}")

        check("rejected packages left no package row",
              request("GET", f"{b1}/packages/pkg-bad1").status == 404
              and request("GET", f"{b1}/packages/pkg-bad2").status == 404)

        print("\n== 3. package id is not reusable ==")
        r = request("POST", f"{b2}/packages/pkga", jsonl([
            {"record_id": "new1", "respondent_id": "s9", "text": "new"}]))
        check("re-upload same id -> 409", r.status == 409, r.text)
        r = request("GET", f"{b1}/packages/pkga/download")
        check("original package content unchanged",
              r.status == 200 and len(r.text.splitlines()) == 3, r.text)

        print("\n== 4. withdrawal blocks new uploads (whole package rejected) ==")
        r = request("POST", f"{b1}/withdrawals/s9?reason=opt-out")
        check("withdrawal accepted 202", r.status == 202 and r.json()["respondent_id"] == "s9",
              r.text)
        r = request("POST", f"{b2}/packages/pkgg", jsonl([
            {"record_id": "g1", "respondent_id": "ok1", "text": "fine"},
            {"record_id": "g2", "respondent_id": "s9", "text": "withdrawn!"},
        ]))
        check("upload containing withdrawn respondent -> 403 whole-package reject",
              r.status == 403 and r.json()["detail"]["withdrawn_respondents"] == ["s9"], r.text)
        check("rejected package not created",
              request("GET", f"{b1}/packages/pkgg").status == 404)
        r = request("GET", f"{b2}/withdrawals")
        check("withdrawal list persisted",
              r.status == 200 and any(w["respondent_id"] == "s9" for w in r.json()), r.text)

        print("\n== 5. withdrawal rewrites affected package, other packages untouched ==")
        other_rows = [{"record_id": "o1", "respondent_id": "s3", "text": "keep me"}]
        r = request("POST", f"{b1}/packages/pkgother", jsonl(other_rows))
        check("unrelated package ready", r.status == 201 and r.json()["status"] == "ready")

        r = request("POST", f"{b2}/withdrawals/s1")
        ack = r.json()
        check("withdrawal of s1 affects pkga only",
              r.status == 202 and ack["affected_packages"] == ["pkga"], r.text)

        # Immediately after acceptance, download must not contain s1 any more.
        r = request("GET", f"{b1}/packages/pkga/download")
        check("download during/after withdrawal never serves old content",
              r.status == 409 and r.json()["detail"]["status"] == "rewriting", r.text)

        st = wait_status(b1, "pkga", "ready")
        check("pkga rewritten -> ready with 1 remaining row",
              st["line_count"] == 1, str(st))
        r = request("GET", f"{b2}/packages/pkga/download")
        dl = [json.loads(l) for l in r.text.splitlines()]
        check("rewritten file has only s2 rows",
              len(dl) == 1 and dl[0]["respondent_id"] == "s2" and dl[0]["record_id"] == "r2",
              str(dl))
        check("other records preserved (retry-safe semantics)",
              dl[0]["text"] == "second line")

        r = request("GET", f"{b1}/packages/pkgother/download")
        check("unaffected package still downloadable with its row",
              r.status == 200 and "s3" in r.text, r.text)

        r = request("GET", f"{b1}/jobs?package_id=pkga")
        check("rewrite job recorded as succeeded",
              r.status == 200 and all(j["status"] == "succeeded" for j in r.json()), r.text)

        print("\n== 6. idempotent repeated withdrawal ==")
        r = request("POST", f"{b2}/withdrawals/s1")
        check("repeat withdrawal idempotent (202, already_withdrawn)",
              r.status == 202 and r.json()["already_withdrawn"] is True, r.text)

        print("\n== 7. concurrent uploads: distinct ids all succeed ==")
        def upload(pid):
            return request("POST", f"{b1}/packages/{pid}", jsonl([
                {"record_id": "r", "respondent_id": f"u{pid}", "text": "x"}]))
        with ThreadPoolExecutor(max_workers=8) as pool:
            outs = list(pool.map(upload, [f"conc{i}" for i in range(10)]))
        check("10 parallel distinct uploads all 201",
              all(o.status == 201 for o in outs), str([o.status for o in outs]))

        print("\n== 8. concurrent uploads of same package id: exactly one wins ==")
        with ThreadPoolExecutor(max_workers=8) as pool:
            same = list(pool.map(
                lambda i: request("POST", f"{b2}/packages/sameid", jsonl([
                    {"record_id": f"r{i}", "respondent_id": f"u{i}", "text": "x"}
                ])), range(8)))
        codes = sorted(o.status for o in same)
        check("one 201 and seven 409 for same id",
              codes.count(201) == 1 and codes.count(409) == 7, str(codes))
        r = request("GET", f"{b1}/packages/sameid/lines")
        check("winner package has exactly 1 row (no interleaving)",
              r.json()["line_count"] == 1, r.text)

        print("\n== 9. concurrent withdrawals converge ==")
        setup = [{"record_id": f"r{i}", "respondent_id": f"w{i % 3}", "text": "t"}
                 for i in range(6)]
        request("POST", f"{b1}/packages/conv", jsonl(setup))
        with ThreadPoolExecutor(max_workers=6) as pool:
            wouts = list(pool.map(
                lambda sid: request("POST", f"{b2}/withdrawals/" + sid),
                ["w0", "w1", "w2", "w0", "w1", "w2"]))
        check("all concurrent withdrawal calls accepted",
              all(o.status == 202 for o in wouts), str([o.status for o in wouts]))
        st = wait_status(b1, "conv", "ready")
        check("conv package converged with zero rows",
              st["line_count"] == 0, str(st))
        r = request("GET", f"{b2}/packages/conv/download")
        check("emptied package downloads as empty file",
              r.status == 200 and r.body in (b"", b"\n"), repr(r.body[:20]))

        print("\n== 10. multipart upload via /docs-style form works ==")
        boundary = "----selftestboundary"
        body_rows = jsonl([{"record_id": "m1", "respondent_id": "mp1", "text": "form"}])
        multipart = (
            f"--{boundary}\r\n"
            'Content-Disposition: form-data; name="file"; filename="p.jsonl"\r\n'
            "Content-Type: application/json\r\n\r\n"
            f"{body_rows}\r\n--{boundary}--\r\n"
        ).encode()
        r = request("POST", f"{b1}/packages/multipkg", multipart,
                    content_type=f"multipart/form-data; boundary={boundary}")
        check("multipart upload 201 ready", r.status == 201 and r.json()["status"] == "ready",
              r.text[:200])

        print("\n== 11. failed job retry path ==")
        # Drop a shared-volume fault marker so the first rewrite attempt for
        # pkgother fails; the package must stay non-downloadable and the job
        # be retried automatically with backoff.
        marker = Path(data_dir) / "tmp" / "force-fail.pkgother"
        marker.write_text("1")
        request("POST", f"{b1}/withdrawals/s3")

        deadline = time.time() + 15
        failed_jobs = []
        while time.time() < deadline:
            r = request("GET", f"{b1}/jobs?package_id=pkgother")
            failed_jobs = [j for j in r.json() if j["status"] == "failed"]
            if failed_jobs and failed_jobs[0]["last_error"]:
                break
            time.sleep(0.3)
        check("injected failure recorded with last_error",
              bool(failed_jobs) and bool(failed_jobs[0]["last_error"]), r.text)
        r = request("GET", f"{b2}/packages/pkgother/download")
        check("package stays non-downloadable while failed (pending semantics)",
              r.status == 409, r.text)
        # Remove the marker and let automatic backoff/retry finish it.
        marker.unlink()
        st = wait_status(b1, "pkgother", "ready", timeout=40)
        check("automatic retry succeeds, s3 removed, 0 rows remain",
              st["line_count"] == 0, str(st))
        r = request("GET", f"{b2}/packages/pkgother/download")
        check("recovered package downloadable with empty content",
              r.status == 200 and r.body in (b"", b"\n"), repr(r.body[:20]))
        # Manual retry endpoint refuses a job that already succeeded.
        r = request("POST", f"{b1}/jobs/1/retry")
        check("retry on already-succeeded job -> 409", r.status == 409, r.text)

        print("\n== 12. listing and docs ==")
        r = request("GET", f"{b1}/packages")
        check("package listing works", r.status == 200 and len(r.json()) >= 5, r.text[:120])
        r = request("GET", f"{b1}/docs")
        check("swagger docs available on each instance", r.status == 200, r.text[:60])
        r = request("GET", f"{b1}/openapi.json")
        check("openapi schema served", r.status == 200 and "paths" in r.json())

    finally:
        print("\n== tearing down instances ==")
        for base, p in zip((b1, b2), procs):
            shutdown_instance(base, p)
        shutil.rmtree(data_dir, ignore_errors=True)

    print(f"\nRESULT: {PASS} passed, {FAIL} failed")
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
