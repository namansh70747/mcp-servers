#!/usr/bin/env bash
# Vendor the WPPConnect wa-js bundle (window.WPP) used by the WhatsApp WPP Bridge extension.
# Re-run to update when WhatsApp changes its internals and wa-js ships a fix.
set -euo pipefail
HERE="$(cd "$(dirname "$0")" && pwd)"
VERSION="${1:-latest}"   # e.g. ./fetch_wajs.sh 4.3.1   (default: latest)
URL="https://cdn.jsdelivr.net/npm/@wppconnect/wa-js@${VERSION}/dist/wppconnect-wa.js"

echo "Fetching wa-js@${VERSION} -> wppconnect-wa.js"
curl -fsSL "$URL" -o "$HERE/wppconnect-wa.js"
BYTES=$(wc -c < "$HERE/wppconnect-wa.js" | tr -d ' ')
echo "Saved $BYTES bytes."
# Record what was pinned (best-effort: grab the version string from the bundle).
grep -oE "WPPConnect/WA-JS[^\"']*|wa-js@[0-9.]+|VERSION[\"' :=]+[0-9.]+" "$HERE/wppconnect-wa.js" | head -1 > "$HERE/WAJS_VERSION.txt" 2>/dev/null || true
echo "Pinned: $(cat "$HERE/WAJS_VERSION.txt" 2>/dev/null || echo "$VERSION")"
