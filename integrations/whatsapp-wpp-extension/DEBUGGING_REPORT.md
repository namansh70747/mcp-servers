# WhatsApp WPP connector — debugging report & findings

How the `whatsapp` connector went from "every message errored / `wpp_ready` always false" to a working,
fully-background, autopilot-capable connector. Each issue below is **symptom → root cause → fix**.
Environment: macOS + Google Chrome, WhatsApp Web build **2.3000.104x** (Meta/Metro module system),
wppconnect **wa-js 4.3.1 / 4.3.2-alpha.0**.

## Architecture (how it actually talks to WhatsApp)

- A one-time MV3 extension (`integrations/whatsapp-wpp-extension/`) injects wa-js (`window.WPP`) into
  `web.whatsapp.com` in the page's **MAIN world**.
- The Python connector reaches the page only through AppleScript `execute javascript`
  (`shared/mcp_base/chrome.py`), which runs in an **ISOLATED world** — it cannot see `window.WPP`. So the
  extension and the connector talk through **shared-DOM attributes on `<html>`**:
  `data-wa-bridge/ready/build/patch` (state) and a `data-wa-cmd` → `data-wa-res` request/response relay
  (`chrome.relay_call`). bridge.js (MAIN world) runs the actual WPP calls and writes results back.

## Issues found & fixed

### 1. `WPP.isReady` never becomes true; `Module … was not found`
**Symptom:** console spam — `Module ChatStore / isAuthenticated / getIsMyContact / getMentionName /
getNotifyName was not found`; `WPP.isReady` stays false; `sendTextMessage` throws
`Cannot read properties of undefined (reading 'get')` (that's `ChatStore.get`).
**Root cause:** upstream **wppconnect-team/wa-js#3419**. WhatsApp Web ≥ 2.3000 reshuffled to a Metro-style
module graph (`self.__d`/`self.require`) that registers modules **progressively, ~12–30s after page
load**. wa-js runs its module-finders **once, at injection (`document_start`)**, before those modules
exist, and never retries. The finder *conditions* are still correct (e.g. `isAuthenticated` already maps
to `isLoggedIn`) — it's purely a timing problem.
**Fix — late-boot:** `wa-loader.js` wraps the wa-js bundle as `window.__bootWA()` (does **not** auto-run);
`wpp-patch.js` waits until the app has loaded (`#pane-side` present) and then calls `__bootWA()` once, so
the finders see the now-registered modules. Result: `WPP.isReady === true`, bindings populate.

### 2. Deeper root cause: negative module lookups cached permanently
**Symptom:** even after modules load, `WPP.whatsapp.ChatStore` etc. stay `undefined`; an ad-hoc
`WPP.loader.search(m=>m.ChatCollection)` (a *fresh* condition) finds them, but the internal bindings
never recover.
**Root cause:** `loader/index.ts` `searchId()` caches `null` against the condition function **forever**,
and `whatsapp/exportModule.ts` then pins the getter to `()=>undefined` on the first miss. A finder that
runs too early can never recover.
**Fix:** patched wa-js so negatives are recoverable (re-scan once new modules register; don't pin the
getter). Built from a fork and vendored as the extension's bundle; contributed upstream as
**wa-js PR #3476** (referencing #3419). This also fixed issue #4 below.

### 3. AppleScript JS can't see `window.WPP` (isolated world)
**Symptom:** `chrome.run_js("window.WPP…")` and `window.webpackChunkwhatsapp_web_client` both `undefined`,
even though wa-js clearly ran.
**Root cause:** AppleScript `execute javascript` runs in an **isolated world**; MAIN-world globals are
invisible. Only the DOM is shared.
**Fix:** the **shared-DOM relay** — connector writes `data-wa-cmd = {id, expr}`; bridge.js (MAIN world)
evals it and writes `data-wa-res = {id, ok, v|e}`; connector polls. Implemented as `chrome.relay_call`.

### 4. Sending to a real contact threw `… reading 'isBot'`
**Symptom:** `sendTextMessage` to a contact (not self) failed with
`Cannot read properties of undefined (reading 'isBot')`.
**Root cause:** the send path needs contact-helper modules that were unbound (same negative-cache bug).
**Fix:** the patched bundle (issue #2) — sends to any contact now succeed.

### 5. `WPP.chat.getMessages(...)` hangs
**Symptom:** reads via `getMessages` never resolve (the relay times out); lingering hung promises then
corrupt later calls (see #7).
**Root cause:** `getMessages` triggers a **server history fetch** that stalls on this build/session.
**Fix:** read **synchronously** from `WPP.whatsapp.ChatStore.get(<id>).msgs.getModelsArray()`. The store
holds recent messages and **updates live over the socket**, so once a chat is active (e.g. right after we
send to it) reads are instant and reliable. Connector helpers: `_msgs_js` / `wpp_chat_messages` /
`wpp_get_my_messages`, and `wait_for_reply` polls this.

### 6. Chats are `@lid`-keyed; `fromMe` direction quirks
**Symptom:** `ChatStore.get('916280852252@c.us')` → "no-chat"; in the "message-yourself" chat the user's
own typing showed as `fromMe:true` while connector sends showed `fromMe:false` (inverted).
**Root cause:** WhatsApp now keys chats by **LID** (e.g. self-chat `…@lid`, contact "Me (7696074751)"
`134265475977373@lid`, Aastha `47395676311@lid`), distinct from the `@c.us` phone id. The self-chat is
PN↔LID of the same account, which inverts `fromMe`.
**Fix:** (a) `send()` parses the real chat id out of the returned message id
(`<dir>_<chatId>_<hash>`, `chat_id_from_msg_id`) and returns it, so callers read/wait on the exact chat;
(b) a robust `_FIND_CHAT` JS finder matches by serialized id / phone / lid; (c) `wait_for_reply` takes a
`from_me` flag (default False = the peer's incoming message; True for the lid self-chat quirk).

### 7. Relay intermittently dead — clobbered by lingering promises
**Symptom:** `relay_call` works in bursts then times out for ~30s; `data-wa-res` shows a *stale* id.
**Root cause:** a previously-hung promise (e.g. a `getMessages` from #5) eventually settles and
**overwrites `data-wa-res`** with its stale-id result, racing the current command.
**Fix:** stop issuing hanging calls (use sync ChatStore, #5); a fresh page clears pending promises.

### 8. Multiple WhatsApp tabs confuse the transport
**Symptom:** `isFullReady` succeeds on one call and times out on the next.
**Root cause:** `chrome.find_tab` returns the **first** matching tab; with two `web.whatsapp.com` tabs,
consecutive calls can hit different tabs (one without the live bridge).
**Fix:** keep exactly one WhatsApp tab. (Operational note, not code.)

### 9. The big one for background use: background-tab timer throttling
**Symptom:** the relay stalls whenever the WhatsApp tab isn't the focused/foreground tab. Worked around
(badly) by force-focusing the tab via `osascript activate` every ~12s — which hijacked the user's screen.
**Root cause:** Chrome **throttles `setInterval`/`setTimeout` in background tabs** (to ~1/min after 5
min). bridge.js watched `data-wa-cmd` with a `setInterval`, so in the background it processed commands
far too slowly.
**Fix (current):** replace the `setInterval` command-watcher with a **`MutationObserver`** on the
`data-wa-cmd` attribute. The connector's `execute javascript` sets that attribute via a forced
main-thread task (runs even when backgrounded), and the observer callback is delivered as a **microtask**
— and microtasks are **not** throttled in background tabs. So the relay is fully responsive with the tab
in the background, and **no window focus is ever needed**. The `osascript activate` hack was removed.

## Net result

- Full WPP surface works: send / read / react / media / groups / status + per-contact tone + memory.
- **Fully background** — messaging never steals focus (MutationObserver relay).
- **`wait_for_reply`** turns a back-and-forth into one tool call each (live ChatStore, no hang).
- **`autopilot`** mode (in-session + detached via the `background` server) carries a whole conversation
  without per-message approval, with stop-word / idle / max-turn limits.

## Re-deriving if WhatsApp updates again

wa-js vs WhatsApp internals is cat-and-mouse. If finders break after a WhatsApp update: open WhatsApp
Web, let it fully load, then via the relay run `WPP.loader.search(<fresh condition>)` for the missing
module — if a fresh condition finds it, it's the same negative-cache/timing class (already handled by the
late-boot + patched bundle). Re-pin wa-js with `fetch_wajs.sh` and rebuild if needed.
