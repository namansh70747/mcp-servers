"""Drive the user's REAL Google Chrome via AppleScript (macOS) — open tabs and run JavaScript in the
pages they're ALREADY logged into. No Playwright, no separate profile, no QR.

Two one-time setup steps are required (probed by `js_enabled()` / surfaced in health):
  1. Chrome → View → Developer → "Allow JavaScript from Apple Events"
  2. Approve the Automation prompt to control "Google Chrome" (System Settings → Privacy → Automation)

Everything degrades to (ok=False, message) — nothing raises. macOS + Chrome only.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import tempfile
import time

from .config import get_env
from .log import get_logger

log = get_logger("chrome")

CHROME_APP = get_env("CHROME_APP", "Google Chrome") or "Google Chrome"
_JS_OFF = "javascript through applescript is turned off"  # Chrome's error when the setting is disabled


def _osa(script: str, timeout: int = 30) -> tuple[bool, str]:
    """Run an AppleScript (written to a temp file → no shell escaping). Returns (ok, stdout|error)."""
    path = ""
    try:
        with tempfile.NamedTemporaryFile("w", suffix=".applescript", delete=False) as f:
            f.write(script)
            path = f.name
        p = subprocess.run(["osascript", path], capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return False, (p.stderr or p.stdout or "osascript failed").strip()
        return True, (p.stdout or "").strip()
    except subprocess.TimeoutExpired:
        return False, f"osascript timed out after {timeout}s"
    except FileNotFoundError:
        return False, "osascript not found (this feature is macOS-only)"
    except Exception as e:  # noqa: BLE001
        return False, str(e)
    finally:
        if path:
            try:
                os.unlink(path)
            except Exception:  # noqa: BLE001
                pass


def _qs(s: str) -> str:
    """Escape a Python string for an AppleScript double-quoted literal."""
    return (s or "").replace("\\", "\\\\").replace('"', '\\"')


def chrome_running() -> bool:
    """True if Chrome is already open (does NOT launch it)."""
    try:
        p = subprocess.run(["pgrep", "-x", CHROME_APP], capture_output=True, text=True, timeout=5)
        return p.returncode == 0
    except Exception:  # noqa: BLE001
        return False


def automation_hint() -> str:
    return (f"approve the Automation prompt to control \"{CHROME_APP}\" "
            "(System Settings → Privacy & Security → Automation)")


def js_enabled() -> tuple[bool, str]:
    """Probe whether 'Allow JavaScript from Apple Events' is on. Returns (enabled, hint_if_not)."""
    if not chrome_running():
        return False, f"{CHROME_APP} is not running — open it first"
    ok_, out = _osa(f'tell application "{CHROME_APP}"\n'
                    'if (count of windows) = 0 then return "nowin"\n'
                    'set t to active tab of front window\n'
                    'return (execute t javascript "1+1")\n'
                    'end tell')
    if ok_:
        return True, ""
    low = out.lower()
    if _JS_OFF in low or "turned off" in low:
        return False, "enable Chrome → View → Developer → 'Allow JavaScript from Apple Events'"
    if "not authorized" in low or "not allowed" in low or "-1743" in out:
        return False, automation_hint()
    return False, out


def open_tab(url: str) -> tuple[bool, str]:
    """Open `url` in a NEW background tab in the front Chrome window (does not `activate` Chrome)."""
    if not chrome_running():
        return False, f"{CHROME_APP} is not running — open it first"
    return _osa(f'tell application "{CHROME_APP}"\n'
                'if (count of windows) = 0 then make new window\n'
                f'make new tab at end of tabs of front window with properties {{URL:"{_qs(url)}"}}\n'
                'return "ok"\n'
                'end tell')


def find_tab(url_substr: str) -> str | None:
    """Return "winIndex,tabIndex" (1-based) of the first tab whose URL contains url_substr, else None."""
    ok_, out = _osa(f'tell application "{CHROME_APP}"\n'
                    'set wi to 0\n'
                    'repeat with w in windows\n'
                    '  set wi to wi + 1\n'
                    '  set ti to 0\n'
                    '  repeat with t in tabs of w\n'
                    '    set ti to ti + 1\n'
                    f'    if (URL of t) contains "{_qs(url_substr)}" then return ((wi as text) & "," & (ti as text))\n'
                    '  end repeat\n'
                    'end repeat\n'
                    'return "none"\n'
                    'end tell')
    if ok_ and out and out != "none":
        return out
    return None


def run_js(url_substr: str, js: str, timeout: int = 30) -> tuple[bool, object]:
    """Execute `js` in the first tab whose URL contains url_substr. Returns (ok, value). If the JS
    returns a JSON string it is parsed; otherwise the raw text is returned."""
    ref = find_tab(url_substr)
    if not ref:
        return False, f"no open Chrome tab with URL containing '{url_substr}'"
    wi, ti = ref.split(",")
    ok_, out = _osa(f'tell application "{CHROME_APP}"\n'
                    f'set t to tab {ti} of window {wi}\n'
                    f'return (execute t javascript "{_qs(js)}")\n'
                    'end tell', timeout=timeout)
    if not ok_:
        if _JS_OFF in out.lower() or "turned off" in out.lower():
            return False, "Chrome 'Allow JavaScript from Apple Events' is off — enable it in View → Developer"
        return False, out
    try:
        return True, json.loads(out)
    except Exception:  # noqa: BLE001
        return True, out


def run_async_js(url_substr: str, promise_expr: str, timeout: int = 30) -> tuple[bool, object]:
    """Run an ASYNC JS expression (one that evaluates to a Promise — e.g. `window.WPP.chat.send(...)`)
    in the tab matching url_substr, and return its resolved value. AppleScript `execute javascript`
    can't await, so we kick the promise off (stash the result on window.__cj[id]) and poll. Returns
    (ok, resolved_value) or (False, error)."""
    rid = "cj_" + secrets.token_hex(6)
    # Store a JSON-SAFE value (WPP returns can be large/circular → guard so the poll's JSON.stringify
    # can't throw and hang the bridge).
    kickoff = (
        "(function(){window.__cj=window.__cj||{};var id=" + json.dumps(rid) + ";"
        "function safe(v){try{JSON.stringify(v);return v;}catch(e){try{return String(v);}catch(_){return null;}}}"
        "try{Promise.resolve((function(){return (" + promise_expr + ");})())"
        ".then(function(v){window.__cj[id]={done:true,ok:true,v:safe(v)}})"
        ".catch(function(e){window.__cj[id]={done:true,ok:false,e:String((e&&e.message)||e)}});}"
        "catch(e){window.__cj[id]={done:true,ok:false,e:String((e&&e.message)||e)}}"
        "return 'started';})()"
    )
    ok_, out = run_js(url_substr, kickoff, timeout=15)
    if not ok_:
        return False, out
    poll = ("(function(){var r=(window.__cj||{})[" + json.dumps(rid) + "];"
            "if(!r||!r.done)return '';try{delete window.__cj[" + json.dumps(rid) + "];}catch(e){}"
            "return JSON.stringify(r);})()")
    deadline = time.time() + max(2, int(timeout))
    while time.time() < deadline:
        ok2, val = run_js(url_substr, poll, timeout=15)
        if ok2 and isinstance(val, dict) and val.get("done"):
            return (True, val.get("v")) if val.get("ok") else (False, val.get("e") or "async error")
        time.sleep(0.3)
    return False, f"async JS timed out after {timeout}s"


def get_dom_attr(url_substr: str, name: str) -> tuple[bool, object]:
    """Read a <html> attribute (shared DOM, readable across JS worlds). Returns (ok, value|None)."""
    js = ("(function(){var v=document.documentElement.getAttribute(" + json.dumps(name) + ");"
          "return v===null?'':v;})()")
    return run_js(url_substr, js)


def relay_call(url_substr: str, expr: str, timeout: int = 30, poll: float = 0.35) -> tuple[bool, object]:
    """Execute an async JS `expr` (evaluates to a value or Promise) in the page's MAIN world via the
    extension's shared-DOM relay, and return its resolved value.

    AppleScript `execute javascript` runs in an ISOLATED world that cannot see MAIN-world globals
    (window.WPP, webpack modules), but the extension's bridge.js (MAIN world) watches the <html>
    `data-wa-cmd` attribute, evals the expr, and writes the JSON result to `data-wa-res` — both on the
    shared DOM. We write a unique-id command, then poll the result attribute. Returns (ok, value)."""
    rid = "cj_" + secrets.token_hex(6)
    payload = json.dumps({"id": rid, "expr": expr}, ensure_ascii=False)
    setjs = ("(function(){var r=document.documentElement;r.setAttribute('data-wa-res','');"
             "r.setAttribute('data-wa-cmd'," + json.dumps(payload, ensure_ascii=False) + ");return 'set';})()")
    ok_, out = run_js(url_substr, setjs, timeout=15)
    if not ok_:
        return False, out
    readjs = "(function(){var v=document.documentElement.getAttribute('data-wa-res');return v||'';})()"
    deadline = time.time() + max(2, int(timeout))
    while time.time() < deadline:
        ok2, val = run_js(url_substr, readjs, timeout=15)
        if ok2 and val:
            res = val if isinstance(val, dict) else None
            if res is None:
                try:
                    res = json.loads(val)
                except Exception:  # noqa: BLE001
                    res = None
            if isinstance(res, dict) and res.get("id") == rid:
                return (True, res.get("v")) if res.get("ok") else (False, res.get("e") or "relay error")
        time.sleep(poll)
    return False, f"WPP relay timed out after {timeout}s (is web.whatsapp.com loaded + the bridge extension on?)"


def navigate(url_substr: str, new_url: str) -> bool:
    """Point an existing tab (first match of url_substr) at new_url. False if no match."""
    ref = find_tab(url_substr)
    if not ref:
        return False
    wi, ti = ref.split(",")
    ok_, _ = _osa(f'tell application "{CHROME_APP}"\n'
                  f'set URL of tab {ti} of window {wi} to "{_qs(new_url)}"\n'
                  'return "ok"\n'
                  'end tell')
    return ok_


def close_tab(url_substr: str) -> bool:
    ref = find_tab(url_substr)
    if not ref:
        return False
    wi, ti = ref.split(",")
    ok_, _ = _osa(f'tell application "{CHROME_APP}"\n'
                  f'close tab {ti} of window {wi}\n'
                  'return "ok"\n'
                  'end tell')
    return ok_


def list_tabs() -> tuple[bool, object]:
    """Return [{window, tab, title, url}] for all open Chrome tabs (tab-delimited from AppleScript)."""
    if not chrome_running():
        return False, f"{CHROME_APP} is not running"
    ok_, out = _osa(f'tell application "{CHROME_APP}"\n'
                    'set AppleScript\'s text item delimiters to ""\n'
                    'set outp to ""\n'
                    'set wi to 0\n'
                    'repeat with w in windows\n'
                    '  set wi to wi + 1\n'
                    '  set ti to 0\n'
                    '  repeat with t in tabs of w\n'
                    '    set ti to ti + 1\n'
                    '    set outp to outp & wi & tab & ti & tab & (URL of t) & tab & (title of t) & linefeed\n'
                    '  end repeat\n'
                    'end repeat\n'
                    'return outp\n'
                    'end tell')
    if not ok_:
        return False, out
    items = []
    for line in (out or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 4:
            items.append({"window": _int(parts[0]), "tab": _int(parts[1]),
                          "url": parts[2], "title": parts[3]})
    return True, items


def _int(s):
    try:
        return int(s)
    except (TypeError, ValueError):
        return s
