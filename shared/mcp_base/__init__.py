"""Shared foundation for every custom server in the suite.

Import surface kept tiny on purpose:
    from mcp_base import make_server, BaseStore, data_dir, db_path, get_env

Optional submodules (agent_browse, harvest, osint_engines, etc.) are lazily
imported on first access — cold-starting a server that doesn't use them pays
zero overhead at module load time.
"""
from __future__ import annotations

# ── Core: always imported ──────────────────────────────────────────────────
from . import chrome, embed, fetch, http, scrape, semantic
from .app import make_server
from .jobs import Jobs
from .config import (
    base_data_dir,
    data_dir,
    db_path,
    get_env,
    get_env_bool,
    get_env_int,
    load_repo_env,
    repo_root,
)
from .errors import err, not_found, ok
from .gmail import DEFAULT_GMAIL_SCOPES, get_gmail_service, gmail_auth_status
from .log import get_logger
from .store import BaseStore
from .deadline import Deadline

# ── Optional submodules: lazily imported on first attribute access ─────────
# Any server that does `from mcp_base import harvest` or `mcp_base.harvest.fn`
# will trigger the import on demand — not at server startup.  The loaded module
# is cached in globals() so subsequent accesses are free.
_LAZY_MODULES: frozenset[str] = frozenset({
    "agent_browse",
    "dns_resolve",
    "email_extract",
    "emailverify",
    "enumerate_accounts",
    "extensions",
    "harvest",
    "osint_engines",
    "people",
    "providers",
    "websearch",
})


def __getattr__(name: str):
    if name in _LAZY_MODULES:
        import importlib
        mod = importlib.import_module(f".{name}", __package__)
        globals()[name] = mod   # cache so next access is a plain dict lookup
        return mod
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")


__all__ = [
    # Core symbols
    "make_server", "Jobs", "BaseStore",
    "base_data_dir", "data_dir", "db_path",
    "get_env", "get_env_bool", "get_env_int", "load_repo_env", "repo_root",
    "get_gmail_service", "gmail_auth_status", "DEFAULT_GMAIL_SCOPES",
    "ok", "err", "not_found",
    "get_logger",
    "Deadline",
    # Core submodules (eagerly loaded)
    "http", "scrape", "fetch", "embed", "semantic", "chrome",
    # Optional submodules (lazily loaded via __getattr__)
    "agent_browse", "dns_resolve", "email_extract", "emailverify",
    "enumerate_accounts", "extensions", "harvest", "osint_engines",
    "people", "providers", "websearch",
]
