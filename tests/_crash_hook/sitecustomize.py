import os
import sys
import threading


def _install() -> None:
    crash_pkg = os.environ.get("CORPUS_CRASH_PACKAGE")
    if not crash_pkg:
        return
    try:
        from app import storage
    except Exception:
        return

    original = storage.atomic_write_jsonl

    def crashing_write(path, rows):
        in_worker = threading.current_thread().name == "rewrite-worker"
        if not in_worker or f"{crash_pkg}.jsonl" not in str(path):
            return original(path, rows)
        import json as _json
        import uuid as _uuid

        tmp = storage.config.TMP_DIR / f".{path.stem}.{_uuid.uuid4().hex}.tmp"
        payload = "".join(
            _json.dumps(row, ensure_ascii=False) + "\n" for row in rows
        )
        fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())
        sys.stderr.write(
            f"[crash-hook] simulated hard exit before replacing {path}\n"
        )
        sys.stderr.flush()
        os._exit(37)

    storage.atomic_write_jsonl = crashing_write


_install()
