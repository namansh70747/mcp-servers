"""Canonical project/repo identity shared across servers."""
from __future__ import annotations

import os
import subprocess
from pathlib import Path

from .errors import err


def resolve_project(path: str | None = None) -> str:
    """Return absolute git root for `path`, or error dict on failure."""
    raw = (path or "").strip() or os.environ.get("MCP_PROJECT_DIR", "").strip() or os.getcwd()
    p = Path(raw).expanduser().resolve()
    if not p.exists():
        return err(f"no such path: {raw}", hint="pass an existing directory")
    try:
        r = subprocess.run(
            ["git", "-C", str(p), "rev-parse", "--show-toplevel"],
            capture_output=True,
            text=True,
            timeout=15,
        )
        if r.returncode == 0 and r.stdout.strip():
            return str(Path(r.stdout.strip()).resolve())
    except Exception:
        pass
    if p.is_dir():
        return str(p)
    return err(f"not a directory: {raw}")


def default_repo() -> str:
    """Best-effort default repo path for tool defaults."""
    result = resolve_project()
    if isinstance(result, dict) and not result.get("ok", True):
        return os.getcwd()
    return result
