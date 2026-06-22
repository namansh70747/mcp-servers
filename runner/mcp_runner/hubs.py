"""Hub grouping for Qwen Desktop (and any client with a small MCP-entry cap).

Each hub is one FastMCP process that re-exposes many sub-servers' tools under namespaced
names (e.g. `notes_search`, `spotify_play_song`). Custom Python servers are mounted in-process
(cheap — same venv); ready-made stdio servers are proxied (the hub spawns them as subprocesses).

`ready_spec(name)` resolves the canonical specs from mcp/servers.base.json to absolute local
paths — the hub is a normal Python process, free of Qwen's bare-`npx`/`uvx` restriction, so it
can use the github binary and the absolute uvx path directly for reliability.
"""
from __future__ import annotations

import os
import shutil
from pathlib import Path

from mcp_base import repo_root

# uvx caches this runner package in an isolated dir, so __file__-based paths break. Prefer an
# explicit MCP_SUITE_ROOT (set in the Qwen config env); fall back to repo_root() (editable
# mcp-base -> <repo>/shared, which resolves to the real repo).
REPO_ROOT = Path(os.environ.get("MCP_SUITE_ROOT") or repo_root())


def _uvx() -> str:
    """Absolute path to uvx (falls back to bare 'uvx' on PATH)."""
    local = Path.home() / ".local" / "bin" / "uvx"
    if local.exists():
        return str(local)
    return shutil.which("uvx") or "uvx"


def _github_bin() -> str:
    return str(REPO_ROOT / "bin" / "github-mcp-server")


def _env(*keys: str) -> dict:
    """Collect named env vars that are actually set (loaded from .env by mcp_base)."""
    return {k: os.environ[k] for k in keys if os.environ.get(k)}


# Ready-made stdio servers, resolved to absolute local commands.
def ready_spec(name: str) -> dict | None:
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
                   "scaffold", "api-tester", "devlog", "readme-changelog", "github-profile"],
        "ready": ["filesystem", "git", "github"],
    },
    "outreach": {
        "custom": ["reachout", "mailbox", "mailmerge", "campaign", "contacts", "funding-radar",
                   "news-radar", "email-finder", "emailcheck", "apollo", "pitchbuilder",
                   "meeting-prep", "webscrape", "browser", "webengine"],
        "ready": ["google_workspace"],
    },
    "career": {
        "custom": ["resume-forge", "deckforge", "portfolio-site", "linkedin-optimizer",
                   "blog-drafter", "interview-prep", "jobtrack", "learn-tracker", "videoforge"],
        "ready": [],
    },
    "prod": {
        "custom": ["task-manager", "time-tracker", "habit-tracker", "notes", "bookmark-vault",
                   "expense-tracker", "rss-reader", "daily-digest", "recipes", "project-memory"],
        "ready": ["memory", "fetch", "duckduckgo"],
    },
    "system": {
        # Order matters: some clients (Qwen Desktop) truncate a hub's tool list at a cap, so the
        # servers the user reaches for most — whatsapp/chrome/background messaging — MUST mount first
        # or their tools fall off the end and look "unavailable". Heavy/rare servers go last.
        "custom": ["whatsapp", "chrome", "background", "deskpilot", "mac-control", "homebrew", "spotify"],
        "ready": ["messages", "applescript", "shortcuts", "apple-events", "apple-notes",
                  "screenshot", "spotlight", "playwright"],
    },
}
