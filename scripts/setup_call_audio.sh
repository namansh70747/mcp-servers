#!/usr/bin/env bash
# Post-reboot voice call audio setup.
# Run ONCE after installing BlackHole 2ch and rebooting.
# Creates: Multi-Output Device (Speakers + BlackHole) + Aggregate Device (Mic + BlackHole)
# Then sets Chrome/WhatsApp mic and verifies voice.diagnose() passes.

set -e
GREEN='\033[0;32m'; YELLOW='\033[1;33m'; RED='\033[0;31m'; NC='\033[0m'

ok()   { echo -e "${GREEN}✓${NC} $1"; }
warn() { echo -e "${YELLOW}⚠${NC}  $1"; }
err()  { echo -e "${RED}✗${NC} $1"; }

echo ""
echo "═══════════════════════════════════════════"
echo "  Voice Call Audio Setup — post-reboot"
echo "═══════════════════════════════════════════"

# 1. Verify BlackHole is loaded
if system_profiler SPAudioDataType 2>/dev/null | grep -qi "BlackHole"; then
    ok "BlackHole 2ch driver detected"
elif ls /Library/Audio/Plug-Ins/HAL/ 2>/dev/null | grep -qi "BlackHole"; then
    ok "BlackHole 2ch plug-in found"
else
    err "BlackHole not found — did you reboot after 'brew install blackhole-2ch'?"
    exit 1
fi

# 2. Open Audio MIDI Setup for device creation
echo ""
echo "Opening Audio MIDI Setup..."
open -a "Audio MIDI Setup"
sleep 2

# 3. AppleScript: create Multi-Output Device (Speakers + BlackHole)
# and Aggregate Device (Mic + BlackHole)
osascript <<'APPLESCRIPT'
tell application "Audio MIDI Setup"
    activate
end tell

delay 1

-- Guide user via notification
display notification "Audio MIDI Setup is open. Claude will now guide you through the setup." with title "Voice Call Setup" sound name "Glass"
APPLESCRIPT

echo ""
echo "═══════════════════════════════════════════"
echo "  Manual steps needed in Audio MIDI Setup:"
echo "═══════════════════════════════════════════"
echo ""
echo "  A) CREATE Multi-Output Device (so you hear the call + BlackHole loopback):"
echo "     1. Click ＋ at bottom-left → 'Create Multi-Output Device'"
echo "     2. Check: 'Built-in Output' (your speakers/headphones)"
echo "     3. Check: 'BlackHole 2ch'"
echo "     4. Double-click the name → rename to 'Voice Multi-Output'"
echo ""
echo "  B) CREATE Aggregate Device (so STT hears call partner via loopback):"
echo "     1. Click ＋ at bottom-left → 'Create Aggregate Device'"
echo "     2. Check: 'Built-in Microphone'"
echo "     3. Check: 'BlackHole 2ch'"
echo "     4. Double-click the name → rename to 'Voice Call Input'"
echo ""

read -p "Press ENTER when both devices are created..."

# 4. Verify devices exist
echo ""
if SwitchAudioSource -a -t output 2>/dev/null | grep -q "Voice Multi-Output"; then
    ok "Multi-Output device 'Voice Multi-Output' found"
else
    warn "Could not find 'Voice Multi-Output' — check Audio MIDI Setup"
fi

if SwitchAudioSource -a -t input 2>/dev/null | grep -q "Voice Call Input"; then
    ok "Aggregate device 'Voice Call Input' found"
else
    warn "Could not find 'Voice Call Input' — check Audio MIDI Setup"
fi

# 5. Set Chrome/WhatsApp mic to BlackHole
echo ""
echo "Opening Chrome to set WhatsApp mic to BlackHole..."
osascript <<'CHROME_SCRIPT'
tell application "Google Chrome"
    activate
    open location "https://web.whatsapp.com"
end tell
delay 2
display notification "In WhatsApp Web:\n1. Start any call\n2. Click mic icon → Settings → Microphone → BlackHole 2ch" with title "Set WhatsApp Mic" sound name "Glass"
CHROME_SCRIPT

echo ""
echo "  C) SET WhatsApp Web microphone:"
echo "     1. Go to WhatsApp Web (opening now)"
echo "     2. Click ⋮ (menu) → Settings → Notifications (or during a call → mic settings)"
echo "     3. Set Microphone → 'BlackHole 2ch'"
echo "     4. This makes the agent's voice enter the call"
echo ""

read -p "Press ENTER when WhatsApp mic is set to BlackHole 2ch..."

# 6. Quick voice test
echo ""
echo "Running voice.diagnose() to verify readiness..."
cd "$(dirname "$0")/.."
.venv/bin/python - <<'PYCHECK'
import asyncio, importlib.util, sys
from pathlib import Path
ROOT = Path(".").resolve()
spec = importlib.util.spec_from_file_location("vs", ROOT / "servers/voice/server.py")
mod = importlib.util.module_from_spec(spec); sys.modules[spec.name] = mod; spec.loader.exec_module(mod)
from fastmcp import Client

async def main():
    async with Client(mod.mcp) as c:
        r = (await c.call_tool("diagnose", {})).data
        print(f"\ncall_ready: {r.get('whatsapp_call_ready')}")
        for ch in r.get("checks", []):
            sym = "✓" if ch["ok"] else "✗"
            print(f"  {sym} {ch['check']}")
        if r.get("whatsapp_call_ready"):
            print("\n✓ READY — run: voice.call_autopilot('7696074751', voice='me', language='hi')")
        else:
            print(f"\n  Next action: {r.get('next_action')}")
asyncio.run(main())
PYCHECK

echo ""
ok "Setup complete. Reconnect Claude Code to reload the .env, then:"
echo "   voice.call_autopilot('7696074751', voice='me', language='hi')"
echo ""
