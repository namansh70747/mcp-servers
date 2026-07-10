"""Hub grouping for Qwen Desktop (and any client with a small MCP-entry cap).

Each hub is one FastMCP process that re-exposes many sub-servers' tools under namespaced
names (e.g. `notes_search`, `spotify_play_song`). Custom Python servers are mounted in-process
(cheap — same venv); ready-made stdio servers are proxied (the hub spawns them as subprocesses).
"""
from __future__ import annotations

import os
import shutil
import sys
from pathlib import Path

from mcp_base import repo_root

REPO_ROOT = Path(os.environ.get("MCP_SUITE_ROOT") or repo_root())
IS_DARWIN = sys.platform == "darwin"

_MACOS_READY = {
    "applescript", "shortcuts", "messages", "apple-events", "apple-notes", "screenshot", "spotlight",
}
_MACOS_CUSTOM = {"mac-control", "homebrew", "spotify", "webengine"}


def _platform_ok(name: str, *, custom: bool) -> bool:
    if IS_DARWIN:
        return True
    if custom:
        return name not in _MACOS_CUSTOM
    return name not in _MACOS_READY


def _uvx() -> str:
    local = Path.home() / ".local" / "bin" / ("uvx.exe" if sys.platform == "win32" else "uvx")
    if local.exists():
        return str(local)
    return shutil.which("uvx") or "uvx"


def _github_bin() -> str:
    base = REPO_ROOT / "bin" / "github-mcp-server"
    if sys.platform == "win32":
        exe = base.with_suffix(".exe")
        return str(exe if exe.exists() else base)
    return str(base)


def _env(*keys: str) -> dict:
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


def ready_spec(name: str) -> dict | None:
    if not _platform_ok(name, custom=False):
        return None
    uvx = _uvx()
    root = str(REPO_ROOT)
    specs: dict[str, dict] = {
        "filesystem": {"command": "npx",
                       "args": ["-y", "@modelcontextprotocol/server-filesystem", root]},
        "git": {"command": uvx, "args": ["mcp-server-git", "--repository", root]},
        "github": {"command": _github_bin(), "args": ["stdio"],
                   "env": _env("GITHUB_PERSONAL_ACCESS_TOKEN")},
        "google_workspace": {"command": uvx, "args": ["workspace-mcp", "--tool-tier", "core"],
                             "env": {**_env("GOOGLE_OAUTH_CLIENT_ID", "GOOGLE_OAUTH_CLIENT_SECRET"),
                                     "OAUTHLIB_INSECURE_TRANSPORT": "1"}},
        "memory": {"command": "npx", "args": ["-y", "@modelcontextprotocol/server-memory"]},
        "fetch": {"command": uvx, "args": ["mcp-server-fetch"]},
        "duckduckgo": {"command": uvx, "args": ["duckduckgo-mcp-server"]},
        "applescript": {"command": "npx", "args": ["-y", "@peakmojo/applescript-mcp"]},
        "shortcuts": {"command": "npx", "args": ["-y", "mcp-server-apple-shortcuts"]},
        "messages": {"command": uvx, "args": ["mac-messages-mcp"]},
        "apple-events": {"command": "npx", "args": ["-y", "mcp-server-apple-events"]},
        "apple-notes": {"command": "npx", "args": ["-y", "@griches/apple-notes-mcp"]},
        "screenshot": {"command": "npx", "args": ["-y", "@kazuph/mcp-screenshot"]},
        "spotlight": {"command": uvx, "args": ["mcp-server-everything-search"]},
        "playwright": {"command": "npx", "args": ["-y", "@playwright/mcp@latest"]},
    }
    return specs.get(name)


# Hub -> {custom: [server dir names], ready: [ready-made names]}.
HUBS: dict[str, dict] = {
    "dev": {
        "custom": ["codeindex", "codeedit", "gitflow", "repo-health", "snippet-vault",
                   "scaffold", "api-tester", "devlog", "readme-changelog", "github-profile",
                   "project-memory", "recipes"],
        "ready": ["filesystem", "git", "github", "fetch", "duckduckgo"],
    },
    "outreach": {
        "custom": ["reachout", "mailbox", "mailmerge", "campaign", "contacts", "funding-radar",
                   "news-radar", "email-finder", "emailcheck", "apollo", "pitchbuilder",
                   "meeting-prep", "webscrape", "browser", "webengine"],
        "ready": ["google_workspace"],
    },
    "career": {
        "custom": ["resume-forge", "deckforge", "portfolio-site", "linkedin-optimizer",
                   "blog-drafter", "interview-prep", "jobtrack", "learn-tracker"],
        "ready": [],
    },
    "prod": {
        "custom": ["task-manager", "time-tracker", "habit-tracker", "notes", "bookmark-vault",
                   "expense-tracker", "rss-reader", "daily-digest"],
        "ready": ["memory", "fetch", "duckduckgo"],
    },
    "system": {
        "custom": ["mac-control", "homebrew", "spotify"],
        "ready": ["applescript", "shortcuts", "messages", "apple-events", "apple-notes",
                  "screenshot", "spotlight", "playwright"],
    },
}


def hub_servers(hub_id: str) -> dict:
    """Return filtered custom/ready lists for a hub on this platform."""
    spec = HUBS[hub_id]
    return {
        "custom": [s for s in spec.get("custom", []) if _platform_ok(s, custom=True)],
        "ready": [s for s in spec.get("ready", []) if _platform_ok(s, custom=False)],
    }
