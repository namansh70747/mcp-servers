#!/usr/bin/env bash
# Start a voice call to a contact using the full agent pipeline.
# Usage: ./scripts/start_call.sh [phone_or_name] [language]
# Example: ./scripts/start_call.sh 7696074751 hi
#          ./scripts/start_call.sh "Mamma" hi

set -e
CONTACT="${1:-7696074751}"
LANG="${2:-hi}"
VOICE="me"

cd "$(dirname "$0")/.."

echo "Starting voice call to '$CONTACT' in '$LANG'..."
.venv/bin/python - <<PYEOF
import asyncio, importlib.util, sys
from pathlib import Path
ROOT = Path(".").resolve()
spec = importlib.util.spec_from_file_location("vs", ROOT / "servers/voice/server.py")
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
from fastmcp import Client

async def main():
    async with Client(mod.mcp) as c:
        # First check if we're ready
        d = (await c.call_tool("diagnose", {})).data
        if not d.get("whatsapp_call_ready"):
            print(f"NOT READY: {d.get('next_action')}")
            print("Run: scripts/setup_call_audio.sh")
            return
        # Launch
        r = (await c.call_tool("call_autopilot", {
            "contact": "${CONTACT}",
            "language": "${LANG}",
            "voice": "${VOICE}",
            "directive": "Have a friendly spoken conversation. Reply naturally, stay concise.",
            "max_turns": 30,
        })).data
        if r.get("ok"):
            print(f"Call started — job_id: {r.get('job_id') or r.get('id')}")
        else:
            print(f"Failed: {r}")
asyncio.run(main())
PYEOF
