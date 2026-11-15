"""Tiny urllib-based HTTP client so self-tests need only the stdlib."""
from __future__ import annotations

import json
import urllib.error
import urllib.request


class Response:
    def __init__(self, status: int, body: bytes, headers):
        self.status = status
        self.body = body
        self.headers = headers

    def json(self):
        return json.loads(self.body.decode("utf-8"))

    @property
    def text(self) -> str:
        return self.body.decode("utf-8")


def request(method: str, url: str, data: bytes | str | None = None,
            content_type: str = "application/x-ndjson",
            headers: dict | None = None) -> Response:
    if isinstance(data, str):
        data = data.encode("utf-8")
    req = urllib.request.Request(url, data=data, method=method)
    if data is not None:
        req.add_header("Content-Type", content_type)
    for k, v in (headers or {}).items():
        req.add_header(k, v)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            return Response(resp.status, resp.read(), resp.headers)
    except urllib.error.HTTPError as exc:
        return Response(exc.code, exc.read(), exc.headers)


def jsonl(rows: list[dict]) -> str:
    return "".join(json.dumps(r, ensure_ascii=False) + "\n" for r in rows)


def wait_for(url: str, timeout: float = 20.0) -> None:
    import time
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        try:
            r = request("GET", url)
            if r.status == 200:
                return
        except Exception as exc:  # pragma: no cover - startup race
            last = exc
        time.sleep(0.2)
    raise RuntimeError(f"service at {url} did not become ready: {last}")
