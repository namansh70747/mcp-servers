# WhatsApp WPP Bridge (one-time setup)

This tiny Chrome extension injects **WPPConnect [`wa-js`](https://github.com/wppconnect-team/wa-js)**
into WhatsApp Web so your local `whatsapp` MCP connector can drive WhatsApp through its stable
`window.WPP` engine (instead of fragile DOM clicking). It uses your **existing logged-in** WhatsApp Web
session — no QR, no separate profile.

## Install (once)
1. Open **chrome://extensions** in your Chrome.
2. Toggle **Developer mode** (top-right) on.
3. Click **Load unpacked** and select this folder:
   `/Users/namansharma/mcp-servers/integrations/whatsapp-wpp-extension`
4. Open/reload **https://web.whatsapp.com** (be logged in).
5. Back in the agent, run `whatsapp.diagnose()` — it should show `wpp_ready: true`.

That's it. The extension only runs on `web.whatsapp.com` and only injects `wa-js`.

## Update wa-js (if WhatsApp changes break it)
```bash
bash fetch_wajs.sh           # latest
bash fetch_wajs.sh 4.3.1     # a specific version
```
then reload the extension at chrome://extensions and reload web.whatsapp.com.

## Files
- `manifest.json` — MV3; injects the bundle in the page's MAIN world at `document_start` (so it hooks
  WhatsApp before the app loads, and bypasses the page CSP — extension-injected scripts aren't CSP-gated).
- `wppconnect-wa.js` — the vendored wa-js bundle (sets `window.WPP`). Version in `WAJS_VERSION.txt`.
- `bridge.js` — sets a small `window.__WA_BRIDGE__.ready` marker the connector can poll.

## Notes
- Personal/local use. Driving WhatsApp's engine is against WhatsApp's ToS and bypasses UI rate limits —
  keep volume modest to avoid a number ban.
- wa-js tracks WhatsApp's internals; if something stops working after a WhatsApp update, re-run
  `fetch_wajs.sh` to pull a newer build.
