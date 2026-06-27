#!/usr/bin/env bash
# warm-servers.sh — pre-load the curated lite-profile servers into the hub
# module cache so the first hub.run() call for each is instant.
#
# Usage:
#   ./warm-servers.sh              # warm LITE_SERVERS (default)
#   ./warm-servers.sh email-finder whatsapp   # warm specific servers
#
# This runs a one-shot Python snippet through the project venv.  Pair with
# your Hermes/Qwen/Claude session start to eliminate cold-start latency.

set -euo pipefail

REPO="$(cd "$(dirname "$0")" && pwd)"

# Default list matches LITE_SERVERS in servers/hub/server.py
DEFAULT_SERVERS=(
  email-finder emailcheck whatsapp voice browser
  contacts webscrape task-manager mailbox reachout
  notes devlog codeindex
)

if [[ $# -gt 0 ]]; then
  TARGETS=("$@")
else
  TARGETS=("${DEFAULT_SERVERS[@]}")
fi

# Build a Python list literal
PY_LIST="[$(printf '"%s",' "${TARGETS[@]}" | sed 's/,$//')]"

echo "warming: ${TARGETS[*]}"

cd "$REPO"
uv run python - <<PYEOF
import sys, json
sys.path.insert(0, "servers/hub")
# Import the hub module directly (not via MCP transport) to access _load()
import importlib.util, pathlib

spec = importlib.util.spec_from_file_location(
    "hub_warm", pathlib.Path("servers/hub/server.py")
)
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)

targets = ${PY_LIST}
results = mod.warm(targets)
ok  = [k for k, v in results["results"].items() if v == "loaded"]
err = [k for k, v in results["results"].items() if v.startswith("error")]
skp = [k for k, v in results["results"].items() if v == "unknown"]

print(f"warmed {results['warmed']} servers  |  errors: {results['errors']}  |  unknown: {len(skp)}")
for s in ok:
    print(f"  ✓  {s}")
for s in err:
    print(f"  ✗  {s}: {results['results'][s]}")
for s in skp:
    print(f"  ?  {s}  (server dir not found)")
PYEOF
