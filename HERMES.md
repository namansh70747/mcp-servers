# MCP Servers for Hermes — Comprehensive Integration

All **65 MCP servers** from my `mcp-servers` suite are now available inside **Hermes Agent** as first-class tools. This is at full parity with Claude Code, Claude Desktop, Cursor, VS Code, Cline, and every other MCP client.

## Profiles: Lite vs Full

**Problem:** 700+ tools in context floods the agent, slows reasoning, and increases cold-start time.

**Solution:** use the **lite profile** — `hub` router + 14 daily-driver servers (~80 tools). Everything else is still reachable via `hub.run('server','tool',{...})`.

### Install lite profile (recommended for daily use)

```bash
cd ~/mcp-servers
python mcp-to-hermes.py --install --profile lite
```

Then restart Hermes (`/reset`). Hermes now sees ~80 tools instead of 700+.

| Profile | Tools in context | Breadth |
|---|---|---|
| **lite** (recommended) | ~80 | Full — missing tools reachable via hub.run() |
| full | 700+ | Direct — every tool in context |

### Daily-driver servers in the lite profile

`hub`, `email-finder`, `emailcheck`, `whatsapp`, `voice`, `browser`, `contacts`,
`webscrape`, `task-manager`, `mailbox`, `reachout`, `notes`, `devlog`, `codeindex`

### Reaching a tool not in the lite set

```
# In Hermes, call hub tools:
hub.search_tools("videoforge compress")   # find it
hub.run("videoforge", "compress", {"input": "clip.mp4", "output": "out.mp4"})
```

### Warm-keep: eliminate cold-start latency

Servers cold-start their import chain on the first tool call. Pre-load the lite
servers to make all subsequent calls instant:

```bash
# One-shot pre-load (run at session start or from a startup script):
cd ~/mcp-servers
./warm-servers.sh

# Or warm specific servers only:
./warm-servers.sh email-finder whatsapp voice
```

You can also call `hub.warm()` (no args) from within a Hermes session to warm
the default curated set in-process.

## What Just Happened

- ✅ **65 MCP servers** configured in `~/.hermes/config.yaml`
- ✅ **672+ tools** will be discoverable on next Hermes restart
- ✅ Tools appear as `mcp_{server}_{tool}` natively
- ✅ No additional wrapper needed — Hermes native MCP support

## Quick Start

### 1. Verify Config

```bash
python3 /Users/namansharma/mcp-servers/mcp-for-hermes.py
```

Shows all 65 servers categorized (Ready-made, macOS, Dev, Career, Outreach, Productivity, System, Media, Communication).

### 2. Re-sync After Changes

After adding/removing servers in the mcp-servers repo:

```bash
cd ~/mcp-servers
./sync-mcp-to-hermes.sh           # Full sync
./sync-mcp-to-hermes.sh --minimal # Essential servers only
```

### 3. Tool Access

After restarting Hermes (`/reset`), tools are available as:

```
mcp_filesystem_read_file
mcp_github_list_issues
mcp_codeindex_relevant_context
mcp_mac_control_set_volume
mcp_recipes_plan_edits
mcp_funding_radar_scan
mcp_email_finder_find_email
mcp_reachout_send_gmail
mcp_task_manager_list_tasks
... and 670+ more
```

## Server Categories (65 total)

| Category | Servers | Example Tools |
|---|---|---|
| **Ready-made** (6) | filesystem, git, github, memory, google_workspace, playwright | `read_file`, `list_issues`, `search_people` |
| **macOS** (7) | applescript, shortcuts, messages, apple-events, apple-notes, screenshot, spotlight | `run_apouth`, `send_message`, `capture` |
| **Dev Tools** (10) | codeindex, project-memory, codeedit, gitflow, recipes, scaffold, devlog, readme-changelog, repo-health, api-tester | `relevant_context`, `patch_file`, `commit_push`, `plan_edits` |
| **Career** (7) | resume-forge, deckforge, portfolio-site, github-profile, linkedin-optimizer, blog-drafter, interview-prep | `generate_resume`, `build_deck`, `optimize_profile` |
| **Outreach** (9) | funding-radar, apollo, email-finder, emailcheck, contacts, pitchbuilder, campaign, reachout, mailbox, mailmerge | `scan_funding`, `find_email`, `send_email`, `track_campaign` |
| **Productivity** (11) | task-manager, notes, time-tracker, bookmark-vault, habit-tracker, expense-tracker, learn-tracker, snippet-vault, daily-digest, meeting-prep, news-radar, rss-reader | `add_task`, `track_time`, `add_expense`, `get_digest` |
| **System** (6) | mac-control, homebrew, chrome, browser, background, deskpilot | `set_volume`, `install_pkg`, `launch_browser` |
| **Media** (5) | voice, videoforge, webengine, webscrape, spotify | `text_to_speech`, `scrape_url`, `search_tracks` |
| **Communication** (2) | whatsapp, hub | `send_whatsapp`, `unified_query` |
| **Other** (1) | jobtrack | `track_application` |

## Required Environment Variables

Only 3 env vars are needed (all free, all optional):

```bash
# ~/.hermes/.env
GITHUB_PERSONAL_ACCESS_TOKEN=ghp_xxx
GOOGLE_OAUTH_CLIENT_ID=xxx
GOOGLE_OAUTH_CLIENT_SECRET=xxx
```

Servers work without them — they return a clear hint to set the missing variable.

## Management Commands

| Action | Command |
|---|---|
| Preview config | `python mcp-to-hermes.py --preview` |
| Install all | `python mcp-to-hermes.py --install` |
| Install minimal | `python mcp-to-hermes.py --install --minimal` |
| Check binaries | `python mcp-to-hermes.py --check` |
| Selective | `python mcp-to-hermes.py --install --servers fs,git,github` |
| Quick sync | `./sync-mcp-to-hermes.sh` |
| Status | `python mcp-for-hermes.py` |

## Comparison With Other Clients

| Client | Servers | How to Configure | Hermes Parity |
|---|---|---|---|
| Claude Code | `.mcp.json` | Project-level JSON | ✅ Same files, same servers |
| Claude Desktop | `claude_desktop_config.json` | Global JSON | ✅ Same files, same servers |
| Cursor | `.cursor/mcp.json` | Global JSON | ✅ Same files, same servers |
| VS Code Copilot | `mcp.json` | Global JSON | ✅ Same files, same servers |
| Qwen Code | `.qwen/settings.json` | Global JSON | ✅ Same files, same servers |
| **Hermes** | `config.yaml` | Global YAML | ✅ **Full parity — all 65 servers** |

## Architecture

```
mcp-servers/                My comprehensive MCP suite
├── .mcp.json              ← Canonical server list (what all clients read)
├── mcp-to-hermes.py       ← Installs all servers into Hermes
├── mcp-for-hermes.py      ← Status checker
├── sync-mcp-to-hermes.sh  ← One-command re-sync
└── servers/*/server.py     ← Individual Python servers

~/.hermes/config.yaml       ← Hermes configuration (mcp_servers block added)
```

The `mcp-to-hermes.py` script:
1. Reads `.mcp.json` for canonical server definitions
2. Converts each to Hermes `mcp_servers` YAML format
3. Merges into `~/.hermes/config.yaml` (with backup)
4. On Hermes restart, tools auto-discover and register as `mcp_{server}_{tool}`
