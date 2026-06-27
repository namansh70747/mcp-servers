"""Chrome DevTools Protocol client — reach EVERY target, including extension side panels.

The AppleScript `chrome` connector runs JS in a page's *isolated world*; it cannot see Chrome's
native side panel (a separate `chrome-extension://` document). CDP can attach to every target, so
this module is what lets us drive the real Apollo/ContactOut/Lusha side-panel extensions fully in
the background.

Requires Chrome launched with `--remote-debugging-port` and (Chrome ≥136) a non-default
`--user-data-dir`. Use `launch_command()` for the exact one-time command.

Design notes (the performance spine):
  * Websocket connections are POOLED per target id (module-level `_SOCKETS`) and reused across calls
    — no per-call reconnect. A dead socket auto-reconnects once.
  * Every call is wrapped and returns (ok, value|reason); nothing raises.
  * No new dependencies — `requests` + `websockets` (sync client), both already present.

macOS-focused but the protocol is cross-platform; only `launch_command()` is mac-specific.
"""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import threading
import time
from typing import Any

import requests
from websockets.sync.client import connect as _ws_connect

from .config import get_env
from .log import get_logger

log = get_logger("cdp")

CDP_PORT = int(get_env("CDP_PORT", "9222") or "9222")
CDP_PROFILE = get_env("CDP_USER_DATA_DIR", "") or os.path.join(
    os.path.expanduser("~"), ".chrome-apollo-automation")
_CHROME_BIN = ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome")

# id -> live websocket connection (pooled, reused across calls)
_SOCKETS: dict[str, Any] = {}
_LOCK = threading.Lock()
_MSG_ID = 0

# Owned-tab registry: role → targetId, persisted across email-finder restarts while Chrome stays up.
# Only automation-created tabs are ever in this dict — user tabs are never touched.
_OWNED: dict[str, str] = {}
_OWNED_LOCK = threading.Lock()
_OWNED_PATH = os.path.join(CDP_PROFILE, "owned_tabs.json")


def _load_owned() -> None:
    try:
        if os.path.isfile(_OWNED_PATH):
            data = json.loads(open(_OWNED_PATH).read())
            if isinstance(data, dict):
                _OWNED.update({k: v for k, v in data.items() if isinstance(k, str) and isinstance(v, str)})
    except Exception:
        pass


def _save_owned() -> None:
    try:
        os.makedirs(CDP_PROFILE, exist_ok=True)
        with open(_OWNED_PATH, "w") as f:
            json.dump(dict(_OWNED), f)
    except Exception:
        pass


_load_owned()
_OWNED_ADOPTED = False   # adopt_owned() runs once lazily on first owned_tab() call


# Flags that keep a backgrounded/occluded window fully live (no throttling, never marked hidden) so
# reveals work with the window behind another app — and we never need Page.bringToFront.
_BG_FLAGS = [
    "--disable-backgrounding-occluded-windows",
    "--disable-renderer-backgrounding",
    "--disable-background-timer-throttling",
]


def launch_command(port: int = CDP_PORT, profile: str = CDP_PROFILE) -> str:
    """The exact command to launch the debug Chrome on the persistent profile (Default directly,
    skipping the multi-profile picker), with background-throttling disabled."""
    return (f'"{_CHROME_BIN}" '
            f'--remote-debugging-port={port} '
            f'--user-data-dir="{profile}" '
            f'--profile-directory=Default '
            f'--no-first-run --no-default-browser-check '
            + " ".join(_BG_FLAGS))


def default_chrome_dir() -> str:
    """The user's real Chrome user-data-dir (macOS)."""
    return os.path.join(os.path.expanduser("~"), "Library", "Application Support",
                        "Google", "Chrome")


# Cache/transient dirs we skip when cloning — keeps the copy lean (the real profile is ~GBs).
_CLONE_EXCLUDES = [
    "Cache", "Code Cache", "GPUCache", "DawnCache", "DawnGraphiteCache", "DawnWebGPUCache",
    "GrShaderCache", "ShaderCache", "Service Worker/CacheStorage", "Service Worker/ScriptCache",
    "component_crx_cache", "extensions_crx_cache", "Crashpad", "BrowserMetrics",
    # Session/tab state — prevent the automation profile from restoring the user's open tabs on launch
    "Sessions", "Session Storage", "Current Session", "Current Tabs",
    "Last Session", "Last Tabs", "Tabs",
]

# The same set used to wipe before each launch (automation profile only — never the real Chrome).
_SESSION_FILES = [
    "Sessions", "Session Storage", "Current Session", "Current Tabs",
    "Last Session", "Last Tabs", "Tabs",
]


def _wipe_session_state(profile: str = CDP_PROFILE) -> None:
    """Remove Chrome session/tab state from the automation profile ONLY so Chrome starts clean
    (no 113-tab restore). Scoped strictly to `profile` — never touches the user's real Chrome."""
    default = os.path.join(profile, "Default")
    if not os.path.isdir(default):
        return
    for name in _SESSION_FILES:
        p = os.path.join(default, name)
        try:
            if os.path.isdir(p):
                shutil.rmtree(p, ignore_errors=True)
            elif os.path.isfile(p):
                os.remove(p)
        except Exception:
            pass
    # Patch Preferences so Chrome sees a clean exit (no restore bubble, open new-tab page)
    prefs_path = os.path.join(default, "Preferences")
    if os.path.isfile(prefs_path):
        try:
            with open(prefs_path) as f:
                prefs = json.loads(f.read())
            prefs.setdefault("profile", {})["exit_type"] = "Normal"
            prefs.setdefault("sessions", {})["restore_on_startup"] = 5  # NTP
            with open(prefs_path, "w") as f:
                json.dump(prefs, f)
        except Exception:
            pass


def is_set_up(profile: str = CDP_PROFILE) -> bool:
    """True once the persistent debug profile exists (cloned at least once)."""
    return os.path.isdir(os.path.join(profile, "Default"))


def clone_profile(src: str = "Default", dest: str = CDP_PROFILE,
                  force: bool = False) -> tuple[bool, Any]:
    """One-time lean copy of the real Chrome `src` profile into the persistent debug `dest`
    (as its Default profile) + the top-level Local State. Excludes caches. Idempotent: skips if the
    debug profile already exists unless force=True. Returns (ok, info). Never raises."""
    if is_set_up(dest) and not force:
        return True, "already set up (skipped clone)"
    src_dir = os.path.join(default_chrome_dir(), src)
    if not os.path.isdir(src_dir):
        return False, f"source profile not found: {src_dir}"
    dest_default = os.path.join(dest, "Default")
    try:
        os.makedirs(dest_default, exist_ok=True)
        if shutil.which("rsync"):
            cmd = ["rsync", "-a", "--delete"]
            for ex in _CLONE_EXCLUDES:
                cmd += ["--exclude", ex]
            cmd += [src_dir + "/", dest_default + "/"]
            p = subprocess.run(cmd, capture_output=True, text=True, timeout=900)
            if p.returncode not in (0, 24):  # 24 = some files vanished during copy (harmless)
                return False, (p.stderr or "rsync failed").strip()[:300]
        else:
            shutil.copytree(src_dir, dest_default, dirs_exist_ok=True,
                            ignore=shutil.ignore_patterns(*_CLONE_EXCLUDES))
        # top-level Local State (holds profile + encryption metadata)
        ls = os.path.join(default_chrome_dir(), "Local State")
        if os.path.isfile(ls):
            shutil.copy2(ls, os.path.join(dest, "Local State"))
        size = subprocess.run(["du", "-sh", dest], capture_output=True, text=True)
        return True, (size.stdout.split("\t")[0].strip() if size.returncode == 0 else "cloned")
    except Exception as e:  # noqa: BLE001
        return False, str(e)[:300]


def launch(port: int = CDP_PORT, profile: str = CDP_PROFILE) -> tuple[bool, str]:
    """Launch the debug Chrome on the persistent profile, detached. Returns (ok, msg).
    Wipes session/tab state first so Chrome opens a single blank tab (no restore bubble)."""
    if not os.path.exists(_CHROME_BIN):
        return False, f"Chrome not found at {_CHROME_BIN}"
    _wipe_session_state(profile)   # clean start — no 113-tab restore
    try:
        subprocess.Popen(
            [_CHROME_BIN, f"--remote-debugging-port={port}", f"--user-data-dir={profile}",
             "--profile-directory=Default", "--no-first-run", "--no-default-browser-check",
             "--hide-crash-restore-bubble",
             *_BG_FLAGS],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            start_new_session=True)
        return True, "launched"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def chrome_running() -> bool:
    """True if the user's normal Chrome is running (any instance)."""
    try:
        p = subprocess.run(["pgrep", "-x", "Google Chrome"], capture_output=True, timeout=5)
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def ensure_running(port: int = CDP_PORT, profile: str = CDP_PROFILE,
                   wait_s: float = 6.0) -> tuple[bool, str]:
    """Make the debug Chrome reachable with NO repeated setup: if already up, no-op; elif the
    persistent profile exists, auto-launch it (no clone, no login); else report not-set-up."""
    ok, _ = reachable(port)
    if ok:
        return True, "reachable"
    if not is_set_up(profile):
        return False, "not set up — run cdp_setup(do_clone=True) once"
    ok, msg = launch(port, profile)
    if not ok:
        return False, msg
    import time as _t
    deadline = _t.monotonic() + wait_s
    while _t.monotonic() < deadline:
        if reachable(port)[0]:
            hide_app(force=True)   # freshly-launched window must be hidden
            return True, "auto-started from persistent profile"
        _t.sleep(0.3)
    return False, "launched but not reachable yet — retry shortly"


def _base(port: int = CDP_PORT) -> str:
    return f"http://localhost:{port}"


def reachable(port: int = CDP_PORT, timeout: float = 2.0) -> tuple[bool, str]:
    """True if a debug Chrome is listening on the port."""
    try:
        r = requests.get(f"{_base(port)}/json/version", timeout=timeout)
        if r.ok:
            return True, (r.json() or {}).get("Browser", "chrome")
        return False, f"http {r.status_code}"
    except requests.exceptions.ConnectionError:
        return False, "connection refused — debug Chrome not running on this port"
    except Exception as e:  # noqa: BLE001
        return False, str(e)


def targets(port: int = CDP_PORT, timeout: float = 2.0) -> list[dict]:
    """All open CDP targets ({id, type, title, url, webSocketDebuggerUrl}). [] if unreachable."""
    try:
        r = requests.get(f"{_base(port)}/json", timeout=timeout)
        if r.ok and isinstance(r.json(), list):
            return r.json()
    except Exception as e:  # noqa: BLE001
        log.debug("cdp targets failed: %s", e)
    return []


def find_target(url_substr: str | None = None, title_substr: str | None = None,
                type_: str | None = None, port: int = CDP_PORT) -> dict | None:
    """First target matching the given filters (case-insensitive substring), else None."""
    us = (url_substr or "").lower()
    ts = (title_substr or "").lower()
    for t in targets(port):
        if type_ and t.get("type") != type_:
            continue
        if us and us not in (t.get("url") or "").lower():
            continue
        if ts and ts not in (t.get("title") or "").lower():
            continue
        if t.get("webSocketDebuggerUrl"):
            return t
    return None


def _socket(target: dict):
    """Get or open a pooled websocket for a target; reconnect once if the cached one is dead."""
    ws_url = target.get("webSocketDebuggerUrl")
    tid = target.get("id") or ws_url
    if not ws_url:
        return None, "target has no webSocketDebuggerUrl"
    with _LOCK:
        sock = _SOCKETS.get(tid)
        if sock is not None:
            return sock, ""
        try:
            # ping_interval=None: CDP sockets don't answer WS keepalive pings — leaving it on makes
            # the keepalive thread time out and kill the pooled connection mid-reveal.
            sock = _ws_connect(ws_url, open_timeout=5, max_size=None, ping_interval=None)
            _SOCKETS[tid] = sock
            return sock, ""
        except Exception as e:  # noqa: BLE001
            return None, f"connect failed: {e}"


def _drop_socket(target: dict) -> None:
    tid = target.get("id") or target.get("webSocketDebuggerUrl")
    with _LOCK:
        sock = _SOCKETS.pop(tid, None)
    if sock is not None:
        try:
            sock.close()
        except Exception:  # noqa: BLE001
            pass


# CDP methods that would raise/focus the OS window — BANNED so the reveal stays fully invisible.
# Enforced in _send(): a call to one of these is refused (never sent), keeping the no-raise invariant
# a code guarantee rather than a convention. `apollo_visibility_report()` surfaces this set.
_BANNED_METHODS = frozenset({"Page.bringToFront", "Target.activateTarget"})


def _send(target: dict, method: str, params: dict | None = None,
          timeout: float = 10.0) -> tuple[bool, Any]:
    """Send one CDP command on the pooled socket; auto-reconnect once on failure.

    Refuses window-raising methods (`_BANNED_METHODS`) so nothing can ever surface the window."""
    if method in _BANNED_METHODS:
        return False, f"refused: {method} would raise the window (no-raise invariant)"
    global _MSG_ID
    for attempt in (1, 2):
        sock, why = _socket(target)
        if sock is None:
            if attempt == 2:
                return False, why
            continue
        with _LOCK:
            _MSG_ID += 1
            mid = _MSG_ID
        payload = json.dumps({"id": mid, "method": method, "params": params or {}})
        try:
            sock.send(payload)
            # read until we see our id (ignore async events interleaved on the socket)
            while True:
                raw = sock.recv(timeout=timeout)
                msg = json.loads(raw)
                if msg.get("id") == mid:
                    if "error" in msg:
                        return False, msg["error"].get("message", "cdp error")
                    return True, msg.get("result", {})
        except Exception as e:  # noqa: BLE001
            _drop_socket(target)
            if attempt == 2:
                return False, str(e)
    return False, "unreachable"


def eval_js(target: dict, expression: str, timeout: float = 10.0,
            await_promise: bool = True) -> tuple[bool, Any]:
    """Runtime.evaluate `expression` in `target`, returning its value (returnByValue)."""
    ok, result = _send(target, "Runtime.evaluate", {
        "expression": expression,
        "returnByValue": True,
        "awaitPromise": await_promise,
    }, timeout=timeout)
    if not ok:
        return False, result
    res = (result or {}).get("result", {})
    if (result or {}).get("exceptionDetails"):
        return False, str(result["exceptionDetails"].get("text", "js exception"))
    return True, res.get("value")


def navigate(target: dict, url: str, timeout: float = 10.0) -> tuple[bool, Any]:
    """Navigate a page target to `url` (Page domain)."""
    return _send(target, "Page.navigate", {"url": url}, timeout=timeout)


def new_tab(url: str, port: int = CDP_PORT) -> dict | None:
    """Open a NEW background tab at `url` and return its target dict (with webSocketDebuggerUrl).
    Used when the debug Chrome has no page tab at all (e.g. just relaunched). Never raises."""
    # Preferred: browser-level Target.createTarget (works headless + backgrounded, no window raise).
    try:
        ver = requests.get(f"{_base(port)}/json/version", timeout=3).json()
        bws = ver.get("webSocketDebuggerUrl")
        if bws:
            with _ws_connect(bws, open_timeout=5, max_size=None, ping_interval=None) as sock:
                import json as _json
                sock.send(_json.dumps({"id": 1, "method": "Target.createTarget",
                                       "params": {"url": url, "background": True}}))
                while True:
                    msg = _json.loads(sock.recv(timeout=8))
                    if msg.get("id") == 1:
                        tid = (msg.get("result") or {}).get("targetId")
                        break
            if tid:
                for t in targets(port):
                    if t.get("id") == tid:
                        hide_app(force=True)   # a new window may surface Chrome — re-hide it now
                        return t
    except Exception as e:  # noqa: BLE001
        log.debug("createTarget failed: %s", e)
    # Fallback: HTTP PUT /json/new (older/newer Chrome variants).
    for call in (lambda: requests.put(f"{_base(port)}/json/new?{url}", timeout=5),
                 lambda: requests.get(f"{_base(port)}/json/new?{url}", timeout=5)):
        try:
            r = call()
            if r.ok and isinstance(r.json(), dict) and r.json().get("webSocketDebuggerUrl"):
                hide_app(force=True)
                return r.json()
        except Exception:  # noqa: BLE001
            continue
    return None


def adopt_owned(port: int = CDP_PORT) -> dict[str, str]:
    """Scan running page targets for tabs we previously owned (window.name == 'mcp-ef-<role>').
    Re-adopts them into the _OWNED registry without creating new tabs.
    Called lazily on the first owned_tab() call when the registry is empty (crash recovery)."""
    adopted: dict[str, str] = {}
    for t in targets(port):
        if t.get("type") != "page" or not t.get("webSocketDebuggerUrl"):
            continue
        ok, name = eval_js(t, "window.name", timeout=3.0, await_promise=False)
        if ok and isinstance(name, str) and name.startswith("mcp-ef-"):
            role = name[len("mcp-ef-"):]
            with _OWNED_LOCK:
                _OWNED[role] = t["id"]
            adopted[role] = t["id"]
    if adopted:
        _save_owned()
    return adopted


def owned_tab(role: str, url: str = "about:blank", port: int = CDP_PORT) -> dict | None:
    """Get or create the automation-owned tab for `role` ('li', 'scratch', etc.).

    Tries to reuse the persisted targetId; creates a fresh background tab if it's absent or gone.
    The tab is tagged with window.name='mcp-ef-<role>' for crash recovery via adopt_owned().
    NEVER repurposes an existing user tab — only automation-created tabs live in this registry."""
    global _OWNED_ADOPTED
    # Lazy one-time recovery: if registry is empty, try to re-adopt from running tabs
    if not _OWNED_ADOPTED and not _OWNED:
        adopt_owned(port)
        _OWNED_ADOPTED = True
    # Reuse the stored tab if it's still live
    with _OWNED_LOCK:
        tid = _OWNED.get(role)
    if tid:
        for t in targets(port):
            if t.get("id") == tid and t.get("webSocketDebuggerUrl"):
                return t
        # Dead target — clear it and fall through to create
        with _OWNED_LOCK:
            _OWNED.pop(role, None)
    # Create a new background tab owned by the automation
    t = new_tab(url, port)
    if t is None:
        return None
    # Tag it for crash recovery (best-effort)
    eval_js(t, f"window.name = 'mcp-ef-{role}'", timeout=5.0, await_promise=False)
    with _OWNED_LOCK:
        _OWNED[role] = t["id"]
        _save_owned()
    return t


_APP_HIDDEN = False   # memo: once hidden, skip the osascript shell-out until a new window surfaces


def hide_app(app: str = "Google Chrome", force: bool = False) -> None:
    """Hide the debug Chrome app (like Cmd+H) so its windows never show or switch visibly — without
    bringing another app forward. CDP keeps working on a hidden app (renderer stays live with the
    anti-throttle flags). Best-effort; silent on failure (e.g. no Accessibility grant).

    Memoized: the osascript shell-out (~50-200ms) runs once and is skipped on subsequent calls until
    a new window/tab surfaces (callers that open one pass force=True). Saves real time across a bulk
    run where hide_app would otherwise fire on every reveal/people-search."""
    global _APP_HIDDEN
    if _APP_HIDDEN and not force:
        return
    try:
        subprocess.run(
            ["osascript", "-e",
             f'tell application "System Events" to set visible of process "{app}" to false'],
            capture_output=True, timeout=5)
        _APP_HIDDEN = True
    except Exception:  # noqa: BLE001
        pass


# Resource types Apollo doesn't need (it reads the URL + DOM) — blocking them loads LinkedIn faster.
_HEAVY_BLOCK = ["*.jpg", "*.jpeg", "*.png", "*.gif", "*.webp", "*.svg", "*.mp4", "*.webm",
                "*.woff", "*.woff2", "*.ttf", "*.otf", "*.ico",
                "*media*", "*/video/*", "*licdn.com/dms/image*"]


def block_heavy_resources(target: dict) -> None:
    """Block images/media/fonts on a page target so it becomes 'ready' for content scripts faster.
    Best-effort; never raises."""
    _send(target, "Network.enable")
    _send(target, "Network.setBlockedURLs", {"urls": _HEAVY_BLOCK})


def make_visible(target: dict) -> None:
    """Make a backgrounded tab report focused+visible (so content scripts like Apollo inject and
    don't throttle) WITHOUT raising the OS window. Pure CDP emulation — NO Page.bringToFront, which
    would pop the Chrome window to the foreground. Combined with the anti-throttle launch flags the
    page stays fully live while its window sits behind another app."""
    _send(target, "Emulation.setFocusEmulationEnabled", {"enabled": True})
    _send(target, "Page.setWebLifecycleState", {"state": "active"})


# JS that makes the page believe it is foreground-visible + active and re-fires the events content
# scripts (like Apollo) gate their work on — WITHOUT touching the OS window. Idempotent per page.
_SPOOF_VISIBLE_JS = r"""(function(){try{
  var d=document;
  try{Object.defineProperty(d,'visibilityState',{configurable:true,get:function(){return 'visible';}});}catch(e){}
  try{Object.defineProperty(d,'hidden',{configurable:true,get:function(){return false;}});}catch(e){}
  try{Object.defineProperty(d,'webkitVisibilityState',{configurable:true,get:function(){return 'visible';}});}catch(e){}
  try{Object.defineProperty(d,'hasFocus',{configurable:true,value:function(){return true;}});}catch(e){}
  d.dispatchEvent(new Event('visibilitychange'));
  try{d.dispatchEvent(new Event('webkitvisibilitychange'));}catch(e){}
  window.dispatchEvent(new Event('focus'));
  window.dispatchEvent(new Event('pageshow'));
  try{window.dispatchEvent(new PointerEvent('pointermove',{bubbles:true,clientX:200,clientY:200}));}catch(e){
    window.dispatchEvent(new MouseEvent('mousemove',{bubbles:true,clientX:200,clientY:200}));}
  return true;
}catch(e){return false;}})()"""


def force_active(target: dict) -> None:
    """Strongest no-window emulation: focus + active lifecycle + no-idle override. Makes a hidden
    tab behave like the foreground tab for content scripts, without raising the OS window."""
    _send(target, "Emulation.setFocusEmulationEnabled", {"enabled": True})
    _send(target, "Page.setWebLifecycleState", {"state": "active"})
    # Report the user as active (not idle) so idle-gated extension logic keeps running.
    _send(target, "Emulation.setIdleOverride", {"isUserActive": True, "isScreenUnlocked": True})


def clear_idle_override(target: dict) -> None:
    _send(target, "Emulation.clearIdleOverride")


def spoof_visible(target: dict) -> bool:
    """Inject the in-page visibility spoof + re-dispatch visibility/focus/pointer events so a content
    script re-scans the current page. Returns True if the script ran. No OS window change."""
    ok, val = eval_js(target, _SPOOF_VISIBLE_JS, timeout=5.0)
    return bool(ok and val)


def lifecycle_bounce(target: dict) -> None:
    """Kick a re-render by toggling the page lifecycle frozen→active (no window raise)."""
    _send(target, "Page.setWebLifecycleState", {"state": "frozen"})
    _send(target, "Page.setWebLifecycleState", {"state": "active"})


def capture_screenshot(target: dict, path: str | None = None) -> tuple[bool, Any]:
    """Capture a PNG of the target via Page.captureScreenshot (works on hidden tabs/panels). If
    `path` is given, writes the PNG there and returns (ok, path); else returns (ok, base64_str)."""
    ok, result = _send(target, "Page.captureScreenshot", {"format": "png"}, timeout=15.0)
    if not ok:
        return False, result
    data = (result or {}).get("data")
    if not data:
        return False, "no-image-data"
    if path:
        try:
            import base64
            with open(path, "wb") as fh:
                fh.write(base64.b64decode(data))
            return True, path
        except Exception as e:  # noqa: BLE001
            return False, str(e)
    return True, data


def record_network(target: dict, duration_s: float = 25.0, url_filter: str = "apollo.io",
                   want_bodies: bool = True) -> list[dict]:
    """Record matching network requests/responses on a target for `duration_s` seconds.

    Opens a DEDICATED websocket (not the pooled one, which only does request/response and discards
    async events), enables the Network domain, and collects `Network.requestWillBeSent` +
    `responseReceived` (+ response bodies on `loadingFinished`) whose URL contains `url_filter`.
    Returns [{url, method, headers, postData, status, body}] in request order. Never raises.

    Run this in a background thread, then TRIGGER a reveal so the call is captured."""
    ws_url = target.get("webSocketDebuggerUrl")
    if not ws_url:
        return []
    records: dict[str, dict] = {}
    order: list[str] = []
    body_reqs: dict[int, str] = {}
    mid = [900000]

    try:
        sock = _ws_connect(ws_url, open_timeout=5, max_size=None, ping_interval=None)
    except Exception:  # noqa: BLE001
        return []

    def _cmd(method: str, params: dict | None = None) -> int:
        mid[0] += 1
        try:
            sock.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
        except Exception:  # noqa: BLE001
            pass
        return mid[0]

    _cmd("Network.enable")
    deadline = time.monotonic() + duration_s
    try:
        while time.monotonic() < deadline:
            try:
                raw = sock.recv(timeout=1.0)
            except Exception:  # noqa: BLE001 — recv timeout / transient
                continue
            try:
                msg = json.loads(raw)
            except Exception:  # noqa: BLE001
                continue
            method = msg.get("method")
            if method == "Network.requestWillBeSent":
                p = msg.get("params", {}); req = p.get("request", {}) or {}
                url = req.get("url", "")
                if url_filter in url:
                    rid = p.get("requestId")
                    records[rid] = {"url": url, "method": req.get("method"),
                                    "headers": req.get("headers", {}) or {},
                                    "postData": req.get("postData", ""),
                                    "status": None, "body": None}
                    order.append(rid)
            elif method == "Network.responseReceived":
                p = msg.get("params", {}); rid = p.get("requestId")
                if rid in records:
                    records[rid]["status"] = (p.get("response", {}) or {}).get("status")
            elif method == "Network.loadingFinished":
                rid = msg.get("params", {}).get("requestId")
                if want_bodies and rid in records and records[rid]["body"] is None:
                    body_reqs[_cmd("Network.getResponseBody", {"requestId": rid})] = rid
            elif msg.get("id") in body_reqs:
                rid = body_reqs.pop(msg["id"])
                body = (msg.get("result", {}) or {}).get("body", "")
                if rid in records:
                    records[rid]["body"] = (body or "")[:30000]
    finally:
        try:
            _cmd("Network.disable")
            sock.close()
        except Exception:  # noqa: BLE001
            pass
    return [records[r] for r in order if r in records]


def create_window(url: str = "about:blank", port: int = CDP_PORT) -> dict | None:
    """Open a NEW Chrome window (its own side panel) via Target.createTarget(newWindow=True), then
    return its page target dict. Used to run reveals in parallel across windows. The window stays
    hidden (hide_app keeps the app hidden); we never bringToFront it."""
    global _MSG_ID
    # Use the browser-level endpoint to create a target in a new window.
    try:
        tlist = requests.get(f"{_base(port)}/json/version", timeout=3).json()
        browser_ws = tlist.get("webSocketDebuggerUrl")
    except Exception:  # noqa: BLE001
        browser_ws = None
    if browser_ws:
        try:
            with _ws_connect(browser_ws, open_timeout=5, close_timeout=2, max_size=None,
                             ping_interval=None) as bsock:
                with _LOCK:
                    _MSG_ID += 1
                    mid = _MSG_ID
                bsock.send(json.dumps({"id": mid, "method": "Target.createTarget",
                                       "params": {"url": url, "newWindow": True}}))
                while True:
                    msg = json.loads(bsock.recv(timeout=8))
                    if msg.get("id") == mid:
                        tid = (msg.get("result") or {}).get("targetId")
                        break
            if tid:
                for t in targets(port):
                    if t.get("id") == tid:
                        hide_app(force=True)   # a new window surfaces Chrome — re-hide immediately
                        return t
        except Exception as e:  # noqa: BLE001
            log.warning("create_window failed: %s", e)
    # Fallback: a plain new tab (still usable; just shares a window's panel).
    return new_tab(url, port)


def click_at(target: dict, x: float, y: float) -> tuple[bool, Any]:
    """Dispatch a TRUSTED mouse click at (x, y) via CDP Input — counts as a real user gesture
    (required to open extension side panels, which synthetic .click() cannot do)."""
    _send(target, "Input.dispatchMouseEvent",
          {"type": "mouseMoved", "x": x, "y": y})
    ok1, _ = _send(target, "Input.dispatchMouseEvent",
                   {"type": "mousePressed", "x": x, "y": y, "button": "left", "clickCount": 1})
    ok2, r = _send(target, "Input.dispatchMouseEvent",
                   {"type": "mouseReleased", "x": x, "y": y, "button": "left", "clickCount": 1})
    return (ok1 and ok2), r


def click_element(target: dict, selector_re: str, timeout: float = 8.0) -> tuple[bool, Any]:
    """Find an element whose visible text matches `selector_re` (case-insensitive), then TRUSTED-click
    its center. Returns (clicked, info)."""
    # Prefer the SMALLEST matching clickable (a real button/link/[role=button]) over a large wrapper
    # container — clicking a wrapper's center can miss a left-aligned button inside it.
    js = (
        "(function(){var re=new RegExp('(" + selector_re + ")','i');"
        "var best=null,bestA=Infinity;"
        "var sel=['button','a','[role=button]','div','span'];"
        "for(var s=0;s<sel.length;s++){"
        "var els=[].slice.call(document.querySelectorAll(sel[s]));"
        "for(var i=0;i<els.length;i++){var e=els[i];var t=(e.innerText||e.textContent||'').trim();"
        "if(!(t&&t.length<60&&re.test(t)))continue;"
        "var r=e.getBoundingClientRect();if(r.width<=0||r.height<=0)continue;"
        "var clickable=(e.tagName==='BUTTON'||e.tagName==='A'||e.getAttribute('role')==='button'||!!e.onclick);"
        "var area=r.width*r.height;var score=area-(clickable?1e7:0);"
        "if(score<bestA){bestA=score;best={x:r.x+r.width/2,y:r.y+r.height/2,t:t};}}}"
        "return best?JSON.stringify(best):'';})()"
    )
    ok, val = eval_js(target, js, timeout=timeout)
    if not ok or not val:
        return False, "not-found"
    try:
        d = json.loads(val)
    except Exception:  # noqa: BLE001
        return False, "parse-error"
    cok, _ = click_at(target, d["x"], d["y"])
    return cok, d.get("t", "")


def close_all() -> None:
    """Close every pooled socket (call on shutdown / when the debug Chrome restarts)."""
    with _LOCK:
        socks = list(_SOCKETS.values())
        _SOCKETS.clear()
    for s in socks:
        try:
            s.close()
        except Exception:  # noqa: BLE001
            pass
