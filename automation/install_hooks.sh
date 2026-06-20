#!/usr/bin/env bash
# Install a git post-commit hook that auto-reindexes a repo with codeindex and refreshes its
# CLAUDE.md / AGENTS.md so every coding agent starts each session already primed.
# Usage:  automation/install_hooks.sh [repo_path]   (defaults to the current repo)
set -euo pipefail

SUITE="/Users/namansharma/mcp-servers"
TARGET="${1:-$PWD}"
REPO="$(cd "$TARGET" 2>/dev/null && git rev-parse --show-toplevel 2>/dev/null || true)"
[ -z "$REPO" ] && { echo "Not a git repo: $TARGET"; exit 1; }

HOOK="$REPO/.git/hooks/post-commit"
cat > "$HOOK" <<EOF
#!/bin/sh
# codeindex auto-prime (installed by mcp-suite). Refreshes the index + CLAUDE.md/AGENTS.md.
"$SUITE/.venv/bin/python" "$SUITE/automation/reindex_hook.py" "$REPO" >/dev/null 2>&1 || true
EOF
chmod +x "$HOOK"
echo "✓ post-commit reindex hook installed in $REPO"

echo "Running initial index…"
"$SUITE/.venv/bin/python" "$SUITE/automation/reindex_hook.py" "$REPO"
echo "✓ $REPO is primed — CLAUDE.md + AGENTS.md written; agents start familiar with it."
