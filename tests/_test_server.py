"""Test-only uvicorn launcher.

Runs uvicorn programmatically with a Server subclass that registers itself on
the application, enabling the /test-shutdown endpoint in sandboxes where the
kill() syscall is denied. The production Docker image never imports this.
"""
from __future__ import annotations

import os
import sys

import uvicorn

from app.main import app, _SERVER_HOLDER


class SelfStoppingServer(uvicorn.Server):
    async def startup(self, sockets=None) -> None:  # type: ignore[override]
        await super().startup(sockets=sockets)
        _SERVER_HOLDER["server"] = self


def main() -> None:
    port = int(os.environ.get("TEST_UVICORN_PORT", "8000"))
    config = uvicorn.Config(
        app, host="127.0.0.1", port=port,
        log_level=os.environ.get("CORPUS_LOG_LEVEL", "warning").lower()
    )
    server = SelfStoppingServer(config)
    server.run()
    sys.exit(0)


if __name__ == "__main__":
    main()
