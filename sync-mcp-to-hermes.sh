#!/usr/bin/env bash
#
# sync-mcp-to-hermes: One-command sync of all MCP servers into Hermes.
# Run this after adding/removing/renaming any server in the mcp-servers repo.
#
# Usage: ./sync-mcp-to-hermes.sh [--minimal|--select "server1,server2"]
#

set -euo pipefail

cd "$(dirname "$0")"
echo "🔄 Syncing MCP servers to Hermes..."

# Check prerequisites
command -v python3 >/dev/null 2>&1 || { echo "❌ python3 required"; exit 1; }

# Run the Python sync
python3 mcp-to-hermes.py --install "$@"

echo
echo "✅ Sync complete!"
echo "🔄 Restart Hermes to pick up changes: run '/reset' in a Hermes session"
echo "📊 Verify with: python3 mcp-for-hermes.py"
