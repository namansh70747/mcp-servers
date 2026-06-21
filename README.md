# mcp-servers — a free, portable MCP suite for every coding agent

A self-hosted suite of **Model Context Protocol (MCP) servers** that give *any* AI agent — Claude Code,
Claude Desktop, Cursor, Windsurf, VS Code, Gemini CLI, Qwen Code, Kimi CLI, Cline, Roo Code, Zed — a
shared set of superpowers: deep **code understanding & editing**, durable **project memory**, a
**funded-company outreach pipeline**, career tooling (résumé / deck / portfolio / GitHub / LinkedIn),
email, dev-workflow automation, learning & productivity trackers, and **hands-free macOS control**.

- **57 servers** (15 ready-made + 42 custom) · **672 tools** · wired into **10 clients** from one config.
- **100% free.** No paid APIs. Runs fully offline-capable; even the model can be free/local (Ollama).
- **Hardened.** A universal no-crash guard fuzzes every tool — a tool returns a clean error, it never throws.
- **These are MCP servers only — not an agent.** You bring the agent; the servers do the heavy lifting.

> Full machine-readable tool catalog: [`TOOLS.md`](TOOLS.md) (every server + tool + signature).

---

## Quick start

```bash
# 0. prerequisites: Python 3.11+, Node 18+, git, and uv (https://docs.astral.sh/uv/)
curl -LsSf https://astral.sh/uv/install.sh | sh

# 1. install all server dependencies into one shared venv
cd ~/mcp-servers
uv sync

# 2. (optional, free) local AI: multi-language symbols + local semantic search — no torch
uv sync --group code --group embed

# 3. secrets — copy and fill in (all free; see "Credentials" below). Everything works without them
#    too; credentialed tools just return a clear "set X in .env" hint until you do.
cp .env.example .env

# 4. the free, local GitHub server binary
mkdir -p bin
curl -sL https://github.com/github/github-mcp-server/releases/download/v1.4.0/github-mcp-server_Darwin_arm64.tar.gz \
  | tar -xz -C bin && chmod +x bin/github-mcp-server

# 5. generate every client's config (re-run after ANY change to servers or .env)
node mcp/generate.mjs

# health check
node mcp/generate.mjs --check
```

That's it — open any wired client and the servers appear.

---

## Connect your agents (one config → 10 clients)

`mcp/generate.mjs` reads one canonical source (`mcp/servers.base.json` + auto-discovered
`servers/*/server.py`) and writes each client's **native** config in its own format. It backs up any
existing config (`*.orig-mcp.bak`) and skips files it can't safely parse, so it never wipes your settings.

| Agent | Config it writes | How to verify |
|---|---|---|
| **Claude Code** | `<repo>/.mcp.json` (project scope) | `claude mcp list` / `/mcp` |
| **Claude Desktop** | `~/Library/Application Support/Claude/claude_desktop_config.json` | restart → Settings → Developer |
| **Cursor** | `~/.cursor/mcp.json` | Settings → MCP |
| **Windsurf** | `~/.codeium/windsurf/mcp_config.json` | Cascade → MCP |
| **VS Code** (Copilot) | `~/Library/Application Support/Code/User/mcp.json` | MCP: List Servers |
| **Gemini CLI** | `~/.gemini/settings.json` | `/mcp` |
| **Qwen Code** | `~/.qwen/settings.json` | `/mcp` |
| **Kimi CLI** | `~/.kimi/mcp.json` | `/mcp` |
| **Cline** | `…/globalStorage/saoudrizwan.claude-dev/settings/cline_mcp_settings.json` | MCP panel |
| **Roo Code** | `…/globalStorage/rooveterinaryinc.roo-cline/settings/mcp_settings.json` | MCP panel |
| **Zed** | `~/.config/zed/settings.json` | Agent panel |

The generator handles each client's quirks automatically: the three config schemas (`mcpServers` vs VS
Code's `servers`+`inputs` vs Zed's `context_servers`), and three secret styles (`${VAR}` refs, `${env:VAR}`,
VS Code `inputs[]`, or inlined literals where a client supports nothing else). **In-repo files keep
`${ref}` placeholders only — secrets are never committed.**

> **First thing to tell any new agent:** call `recipes.start_here()` — it returns the canonical "how to
> drive this suite" briefing, and `recipes.capabilities()` lists every server and its tools.

ChatGPT & Perplexity are intentionally excluded (both paywall MCP and refuse local servers) — see
[`mcp/DEFERRED.md`](mcp/DEFERRED.md) to add them later via a tunnel.

---

## What you can do

### 1) Subscription-free coding loop — understand → plan → edit → validate → commit → PR
Drive it from a free agent (Claude Desktop free, or a **local Ollama** model). The servers make the
edits safe so even a weak model can't break your code:

- **`codeindex`** — indexes every file & line, AST/tree-sitter symbols, import & call graph, FTS +
  optional local-embedding semantic search. `relevant_context(task)` returns exactly the right files;
  `export_context_file()` writes `CLAUDE.md`/`AGENTS.md` so every session starts primed.
- **`project-memory`** — durable decisions, conventions, and a working thread that survives compaction
  (`checkpoint`/`resume`).
- **`codeedit`** — patches / line edits / atomic multi-file edits with **auto-backup + validate + auto-
  rollback** (a broken edit is reverted, never left on disk) and `undo`.
- **`gitflow`** — branch / stage / commit / push + `pr_body()` (a PR title & description from the diff).
- **`recipes`** — `plan_edits(task)`, and playbooks `make_change` / `review_and_pr` that chain the above.

### 2) Funded-company outreach pipeline (your weekly flow, automated & dedup-safe)
`funding-radar` (free: TechCrunch RSS + SEC EDGAR + Hacker News) → `apollo` / `email-finder` (free email
resolution via GitHub commits + patterns + free verifiers) → `contacts` → `pitchbuilder` (tailored idea
in your fixed template) → `reachout` (Gmail, **drafts-first**) → `campaign` (ledger that **never re-mails
a company**). `automation/run_outreach.sh` + a launchd plist run it on a schedule.

### 3) Career artifacts (from one `profile.json`)
`resume-forge` (tailored résumé + cover letter, ATS scoring), `deckforge` (`.pptx` decks),
`portfolio-site` (static site → GitHub Pages), `github-profile`, `linkedin-optimizer`, `blog-drafter`.

### 4) macOS automation (your MacBook, hands-free)
`mac-control` (volume, dark mode, battery, clipboard, notifications, screenshots, windows, wallpaper,
Spotlight, …) and `homebrew`, plus ready-made AppleScript / Shortcuts / Messages / Calendar servers.
See **macOS permissions** below.

### 5) Everything else
Email triage (`mailbox`, `mailmerge`), dev workflow (`scaffold`, `devlog`, `readme-changelog`,
`snippet-vault`, `jobtrack`, `api-tester`, `repo-health`), learning & productivity (`interview-prep`,
`learn-tracker`, `task-manager`, `notes`, `time-tracker`, `bookmark-vault`, `habit-tracker`,
`expense-tracker`), discovery (`news-radar`), and `daily-digest` (one "what's due today" across them all).

---

## Run the model for free too (no subscription)

Every server works regardless of which model drives the agent. To run with **zero subscription**:

- **Local (Ollama):** `ollama serve`, then point an OpenAI-compatible CLI at it:
  `OPENAI_BASE_URL=http://localhost:11434/v1`, `OPENAI_API_KEY=ollama`, e.g. model `qwen3-coder:30b`
  (32 GB Mac) or `qwen2.5-coder:7b` (16 GB). Works great for Qwen Code / Kimi CLI.
- **Free hosted tiers:** Cerebras (~1M tokens/day), Groq, or Gemini Flash — all OpenAI-compatible.

---

## Credentials (`.env`) — all free, all optional

| Variable | Used by | How to get it (free) |
|---|---|---|
| `GITHUB_PERSONAL_ACCESS_TOKEN` | github, github-profile, email-finder | GitHub → Settings → Developer settings → fine-grained PAT |
| `GOOGLE_OAUTH_CLIENT_ID` / `_SECRET` | google_workspace, reachout, mailbox | Google Cloud → enable Calendar/Drive/Docs/Gmail → Desktop OAuth client |
| `SEC_USER_AGENT` | funding-radar | any `Name email` string (SEC requires it) |
| `REOON_API_KEY` / `HUNTER_API_KEY` | email-finder | free signups (email verification) |
| `APOLLO_API_KEY` | apollo | optional; free people-search |

For Gmail, drop your OAuth `credentials.json` in `~/.mcp-suite/reachout/` — the first send opens a browser
to authorize, then caches the token. Nothing sends without your review (drafts-first).

---

## Architecture & safety

- **Shared foundation** `shared/mcp_base/` — a FastMCP app factory, a SQLite `BaseStore`, config/`.env`
  auto-load, a cached/retry HTTP helper, a shared Gmail helper, and consistent `ok()/err()/not_found()`
  results. Every custom server is a thin layer on top.
- **Independence** — each server is its own stdio process; the agent composes them. Cross-server state
  (contacts, the outreach ledger, the code index) lives in **shared SQLite files** under `~/.mcp-suite/`,
  so every server still works standalone.
- **Safety** — destructive/system-mutating tools (Homebrew installs, wallpaper, sends, bulk email) are
  **`confirm=True`-gated** or **drafts-first**; file edits **auto-rollback** on failure; the
  `tests/test_no_crash.py` guard fuzzes 400+ tools so none ever throw on bad input.

---

## Server catalog (57 total)

**Ready-made (15):** filesystem, git, github, fetch, duckduckgo, google_workspace, memory, applescript,
macos-use, shortcuts, messages, apple-events, apple-notes, screenshot, spotlight.

**Custom (42):** codeindex, project-memory · funding-radar, apollo, email-finder, emailcheck, news-radar,
contacts, pitchbuilder, campaign, reachout, mailbox, mailmerge · deckforge, resume-forge, portfolio-site,
github-profile, blog-drafter, linkedin-optimizer · scaffold, devlog, readme-changelog, snippet-vault,
jobtrack, api-tester, repo-health, codeedit, gitflow · interview-prep, learn-tracker, task-manager, notes,
time-tracker, bookmark-vault, habit-tracker, expense-tracker · mac-control, homebrew · recipes,
meeting-prep, daily-digest.

Full tool-by-tool reference: [`TOOLS.md`](TOOLS.md).

---

## Development

```bash
uv run python tests/verify_all.py     # every server imports
uv run python tests/test_no_crash.py  # universal no-crash guard
for f in tests/test_*.py; do uv run python "$f"; done   # full suite

# add a server: drop servers/<name>/server.py (use make_server from mcp_base), then:
node mcp/generate.mjs   # auto-discovered and wired into all 10 clients
```

A git `post-commit` hook (install with `automation/install_hooks.sh`) keeps the code index +
`CLAUDE.md`/`AGENTS.md` fresh after every commit, so agents are always primed.

---

## Troubleshooting

- **Server not showing in a client?** Re-run `node mcp/generate.mjs`, restart the client. `--check` shows
  per-client counts and whether the github binary / secrets are present.
- **Gmail tool returns an auth hint?** Add `credentials.json` (above); the first call authorizes.
- **macOS tool returns a permission error?** Grant the *host app* (the CLI/IDE that launched the server)
  **Automation / Accessibility / Full Disk Access / Screen Recording** in System Settings → Privacy.
- **Semantic search says "FTS only"?** `uv sync --group embed` to enable local embeddings.

## License

MIT.

## Tool catalog

See [`TOOLS.md`](TOOLS.md) for the full catalog of every server and its tools (43 servers, 685 tools), auto-generated by `tests/gen_docs.py`.
