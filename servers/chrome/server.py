"""chrome — act inside YOUR real Google Chrome (where you're already logged in), via AppleScript + JS.

Open a tab on any site you're already signed into and drive it with JavaScript — no separate profile,
no re-login. Generalizes the whatsapp trick to ANY web app (Slack, Gmail-web, X, Notion, …).

One-time setup (health() checks): Chrome → View → Developer → "Allow JavaScript from Apple Events",
and approve the Automation prompt to control Chrome. macOS + Google Chrome only.
"""
from __future__ import annotations

from mcp_base import err, make_server, ok
from mcp_base.chrome import (CHROME_APP, automation_hint, chrome_running, close_tab as _close,
                             find_tab, js_enabled, list_tabs as _list, navigate, open_tab,
                             run_js as _run_js)

mcp = make_server(
    "chrome",
    instructions=("Act in YOUR real Chrome (already logged in): open(url) opens a background tab; "
                  "run_js(url, js) executes JavaScript in the first tab matching url and returns its "
                  "value (return a JSON string for structured data); read_tab(url) returns the page "
                  "text; list_tabs(); close_tab(url). Needs Chrome's 'Allow JavaScript from Apple "
                  "Events' on + an Automation grant (see health()). macOS + Chrome only."),
)

MAX_JS = 200000

try:
    mcp.local_provider.remove_tool("health")
except Exception:  # noqa: BLE001
    try:
        mcp.remove_tool("health")
    except Exception:  # noqa: BLE001
        pass


@mcp.tool
def health() -> dict:
    """Is Chrome running and able to run automation JavaScript (the two one-time setup steps)."""
    running = chrome_running()
    en, hint = (js_enabled() if running else (False, f"{CHROME_APP} isn't running — open it"))
    steps = []
    if not running:
        steps.append(f"open {CHROME_APP}")
    elif not en:
        steps.append(hint or "enable Chrome → View → Developer → 'Allow JavaScript from Apple Events'")
        steps.append(automation_hint())
    return ok(server="chrome", chrome_app=CHROME_APP, chrome_running=running,
              js_from_apple_events=en, ready=bool(running and en), setup_steps=steps or None)


@mcp.tool
def open(url: str, reuse: bool = True) -> dict:
    """Open `url` in a background tab in your Chrome (does not steal focus). reuse=True navigates an
    existing tab on the same site instead of opening a duplicate."""
    if not (url or "").strip():
        return err("url is required")
    if not chrome_running():
        return err(f"{CHROME_APP} isn't running — open it first")
    host = url.split("://")[-1].split("/")[0]
    if reuse and host and find_tab(host):
        return ok(opened=url, reused=True) if navigate(host, url) else err("could not navigate existing tab")
    okt, e = open_tab(url)
    return ok(opened=url, reused=False) if okt else err(f"could not open tab: {e}")


@mcp.tool
def run_js(url: str, js: str, timeout: int = 30) -> dict:
    """Run JavaScript in the first Chrome tab whose URL contains `url`, returning its value (parsed if
    the JS returns a JSON string). Open the tab first with open(url)."""
    if not (url or "").strip() or not (js or "").strip():
        return err("url and js are required")
    if len(js) > MAX_JS:
        return err(f"js too long (>{MAX_JS} chars)")
    okj, val = _run(url, js, timeout)
    return ok(value=val) if okj else err(str(val))


def _run(url, js, timeout=30):
    return _run_js(url, js, timeout=max(2, min(120, int(timeout))))


@mcp.tool
def read_tab(url: str, max_chars: int = 8000) -> dict:
    """Return the visible text of the first Chrome tab matching `url` (document.body.innerText)."""
    if not (url or "").strip():
        return err("url is required")
    n = max(200, min(50000, int(max_chars)))
    okj, val = _run(url, f"(document.body?document.body.innerText:'').slice(0,{n})", 20)
    return ok(url=url, text=val if isinstance(val, str) else str(val)) if okj else err(str(val))


@mcp.tool
def list_tabs() -> dict:
    """List all open Chrome tabs ({window, tab, url, title})."""
    okj, val = _list()
    return ok(tabs=val, count=len(val) if isinstance(val, list) else 0) if okj else err(str(val))


@mcp.tool
def close_tab(url: str) -> dict:
    """Close the first Chrome tab whose URL contains `url`."""
    if not (url or "").strip():
        return err("url is required")
    return ok(closed=url) if _close(url) else err(f"no open tab matching '{url}'")


if __name__ == "__main__":
    mcp.run()
