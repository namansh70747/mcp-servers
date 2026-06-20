#!/usr/bin/env bash
# Scheduled weekly outreach. Triggered by launchd (see com.naman.mcp.outreach.plist) or cron.
# Drives Claude Code headless through the pipeline using the independent MCP servers.
# DRAFTS-FIRST by default (nothing auto-sends) — review drafts in Gmail, then send.
#
# Requires: `claude` on PATH, the suite configured (node mcp/generate.mjs run), and your
# Gmail/credentials set up for reachout.
set -euo pipefail

ROOT="$(cd "$(dirname "$0")/.." && pwd)"
LOG="${MCP_DATA_DIR:-$HOME/.mcp-suite}/outreach.log"
mkdir -p "$(dirname "$LOG")"

read -r -d '' PROMPT <<'EOF' || true
Run my weekly funded-company outreach pipeline. Steps, using the MCP servers:
1. campaign.next_run_due(7) — if not due, stop.
2. funding-radar.scan then funding-radar.as_targets — get recent funded companies.
3. campaign.filter_uncontacted(targets) — keep only FRESH companies (never re-contact).
4. For each fresh company (respect reachout's daily cap):
   - resolve the CTO/CEO + a few team members (apollo.find_people / email-finder.find).
   - contacts.add_contact for each.
   - write a tailored project idea, pitchbuilder.save_pitch, then reachout.render_template.
   - reachout.create_draft (DO NOT auto-send) with the one-pager attached.
   - campaign.record_outreach(company, domain, contacts).
5. campaign.record_run, then summarize what was drafted and who to review.
Be conservative; if anything is ambiguous, draft fewer rather than more.
EOF

echo "=== $(date) outreach run ===" >> "$LOG"
cd "$ROOT"
claude -p "$PROMPT" 2>&1 | tee -a "$LOG"
