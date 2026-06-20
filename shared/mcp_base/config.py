"""Paths, data dirs, and env access shared by every server."""
from __future__ import annotations

import os
from pathlib import Path


def repo_root() -> Path:
    """Best-effort path to the repo root (parent of shared/)."""
    return Path(__file__).resolve().parents[2]


def load_repo_env() -> Path | None:
    """Load the repo .env into os.environ so get_env() works everywhere.

    Idempotent and safe to call repeatedly. Existing env vars win over the file
    (override=False) so a process/launcher can always take precedence. Searches
    the repo root first, then walks up from the cwd. Silent no-op if python-dotenv
    is unavailable or no .env exists. Returns the file loaded, or None.
    """
    try:
        from dotenv import find_dotenv, load_dotenv
    except Exception:
        return None

    candidates: list[Path] = []
    root_env = repo_root() / ".env"
    if root_env.exists():
        candidates.append(root_env)
    found = find_dotenv(usecwd=True)
    if found:
        candidates.append(Path(found))

    loaded: Path | None = None
    for env_file in candidates:
        try:
            if load_dotenv(env_file, override=False):
                loaded = loaded or env_file
        except Exception:
            continue
    return loaded


# Auto-load the repo .env exactly once when mcp_base is imported, so every server
# gets get_env() working without boilerplate. Opt out with MCP_NO_DOTENV=1.
if os.environ.get("MCP_NO_DOTENV", "").strip() not in ("1", "true", "True"):
    try:
        load_repo_env()
    except Exception:
        pass


def base_data_dir() -> Path:
    """Root directory for all suite runtime data (SQLite DBs, caches, outputs).

    Override with MCP_DATA_DIR; defaults to ~/.mcp-suite. Created on demand.
    """
    raw = os.environ.get("MCP_DATA_DIR", "").strip()
    base = Path(raw).expanduser() if raw else Path.home() / ".mcp-suite"
    base.mkdir(parents=True, exist_ok=True)
    return base


def data_dir(server_name: str) -> Path:
    """Per-server data directory (created on demand)."""
    d = base_data_dir() / server_name
    d.mkdir(parents=True, exist_ok=True)
    return d


def db_path(server_name: str, filename: str = "store.db") -> Path:
    """Path to a server's SQLite database file."""
    return data_dir(server_name) / filename


def get_env(key: str, default: str | None = None, *, required: bool = False) -> str | None:
    """Read an environment variable, optionally enforcing presence."""
    val = os.environ.get(key, default)
    if required and not val:
        raise RuntimeError(
            f"Missing required environment variable: {key}. "
            f"Add it to your .env (see .env.example)."
        )
    return val


def get_env_bool(key: str, default: bool = False) -> bool:
    """Read a boolean env var. True for 1/true/yes/on (case-insensitive)."""
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def get_env_int(key: str, default: int | None = None) -> int | None:
    """Read an integer env var, falling back to default on missing/invalid."""
    raw = os.environ.get(key)
    if raw is None or raw.strip() == "":
        return default
    try:
        return int(raw.strip())
    except ValueError:
        return default
