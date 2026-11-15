"""Test-only launcher that installs the crash-injection hook before uvicorn.

Usage: python tests/_crash_server.py
Env: CORPUS_CRASH_PACKAGE, TEST_UVICORN_PORT, CORPUS_DATA_DIR, ...
"""
from __future__ import annotations

import runpy
import sys
from pathlib import Path

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE))

# Install monkeypatch explicitly (independent of sitecustomize semantics).
import _crash_hook.sitecustomize as hook  # noqa: E402,F401

runpy.run_path(str(HERE / "_test_server.py"), run_name="__main__")
