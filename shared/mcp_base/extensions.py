"""Free browser-extension reveal pool with rotation — the user's manual move, automated.

Replicates "company → LinkedIn → person → click the extension → read the revealed email" using the
user's REAL logged-in Chrome (via `mcp_base.chrome`), where free people-data extensions are already
installed. Rotates across the free pool like API keys so the combined monthly free credits
(~370+/mo) never run out — each extension's usage is tracked in the shared `QUOTA` credit pool.

⚠️ ToS / account-safety: automating logged-in LinkedIn at scale violates ToS and risks restriction.
So this layer is OPT-IN (`use_extensions=True`), human-in-the-loop, rate-limited, small-batch — the
free keyless backbone (layers 1–14) is the default and needs none of this. Every revealed address
is still passed through verify; nothing is fabricated.

Main entry:
    reveal_email(linkedin_url, name="", domain="") -> {email, emails, extension, note, degraded}
    pool_status() -> per-extension remaining free credit this month

⚠️ SUPERSEDED for the reveal flow: modern Apollo/ContactOut/Lusha render the "Access email" button
and the revealed address in Chrome's NATIVE SIDE PANEL (a separate chrome-extension:// document).
AppleScript `execute javascript` runs in the page's isolated world and cannot see/click the side
panel, so `reveal_email()` here returns no email against current extension UIs. The working
background path is `mcp_base.apollo_cdp` (Chrome DevTools Protocol), which CAN reach the side panel.
This module is kept for `pool_status()` (the credit pool) and the legacy in-page DOM path.
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import chrome
from .email_extract import is_role
from .quota import QUOTA

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# The free reveal pool: (name, monthly_free_credit, reveal_button_text, overlay_email_selector_hint).
# Ordered cheapest-credit-first is not meaningful here; we rotate by remaining credit. Apollo's free
# corporate-domain tier is largest, so it leads. Selectors are best-effort DOM hints — extensions
# inject their own overlays, so we also fall back to scraping any email in the injected DOM.
_POOL: list[dict[str, Any]] = [
    {"name": "apollo",     "cap": 10000, "button": "access email|apollo",     "hint": "[class*=apollo] , [id*=apollo]"},
    {"name": "contactout", "cap": 100,   "button": "contactout|show email",   "hint": "[class*=contactout] , [id*=contactout]"},
    {"name": "lusha",      "cap": 70,    "button": "lusha|show",              "hint": "[class*=lusha] , [id*=lusha]"},
    {"name": "getprospect","cap": 50,    "button": "getprospect|find email",  "hint": "[class*=getprospect]"},
    {"name": "skrapp_ext", "cap": 50,    "button": "skrapp|find email",       "hint": "[class*=skrapp]"},
    {"name": "snov_ext",   "cap": 50,    "button": "snov|find email",         "hint": "[class*=snov]"},
    {"name": "wiza",       "cap": 25,    "button": "wiza|reveal",             "hint": "[class*=wiza]"},
    {"name": "kaspr",      "cap": 25,    "button": "kaspr|show",              "hint": "[class*=kaspr]"},
    {"name": "rocketreach","cap": 5,     "button": "rocketreach|get contact|reveal", "hint": "[class*=rocketreach] , [id*=rocketreach]"},
    {"name": "signalhire", "cap": 10,    "button": "signalhire|reveal contacts|show contacts", "hint": "[class*=signalhire]"},
    {"name": "clearbit",   "cap": 100,   "button": "clearbit|connect|find email", "hint": "[class*=clearbit]"},
]
_POOL_MEMBERS = [(e["name"], e["cap"]) for e in _POOL]
_BY_NAME = {e["name"]: e for e in _POOL}

_TOS_NOTE = ("Used your real logged-in Chrome + a free people-data extension (human-in-the-loop, "
             "small-batch). Automating logged-in LinkedIn at scale violates ToS — use sparingly. "
             "Revealed address was still verified, not fabricated.")
_SETTLE = 3.5


def _available() -> tuple[bool, str]:
    if not chrome.chrome_running():
        return False, "chrome:not-running"
    ok, _ = chrome.js_enabled()
    if not ok:
        return False, "chrome:js-from-apple-events-off"
    return True, ""


def pool_status() -> dict:
    """Per-extension remaining free reveal credit this month (from the QUOTA credit pool)."""
    out: dict[str, Any] = {}
    try:
        state = QUOTA.state()
    except Exception:
        state = {}
    for e in _POOL:
        used = 0
        prov = state.get(e["name"], {})
        if isinstance(prov, dict):
            for k in prov.get("keys", []) or []:
                used += int(k.get("calls", 0) or 0)
        out[e["name"]] = {"cap": e["cap"], "used": used, "remaining": max(0, e["cap"] - used)}
    return out


def _click_extension_button(url_substr: str, button_re: str) -> bool:
    """Click the extension's reveal control (matched by visible text), best-effort."""
    js = (
        "(function(){var re=new RegExp('(" + button_re + ")','i');"
        "var els=[].slice.call(document.querySelectorAll('button,a,[role=button],span,div,img'));"
        "for(var i=0;i<els.length;i++){var e=els[i];"
        "var tx=((e.innerText||e.textContent||'')+' '+(e.title||'')+' '+(e.alt||'')).trim();"
        "if(tx && re.test(tx)){try{e.click();return true;}catch(_){}}}"
        "return false;})()"
    )
    ok, val = chrome.run_js(url_substr, js)
    return bool(ok and val in (True, "true", 1))


def _read_revealed(url_substr: str, domain: str) -> list[str]:
    """Read any email surfaced in the page/overlay DOM after a reveal, preferring the target domain."""
    js = (
        "(function(){try{var h=document.documentElement.outerHTML;"
        "return h.slice(0,200000);}catch(e){return '';}})()"
    )
    ok, html = chrome.run_js(url_substr, js)
    if not ok or not html:
        return []
    dom = (domain or "").lower().lstrip("@")
    found: list[str] = []
    for m in _EMAIL_RE.findall(str(html)):
        e = m.lower()
        if e in found or is_role(e):
            continue
        if "noreply" in e or "example.com" in e or e.endswith(".png") or e.endswith(".jpg"):
            continue
        found.append(e)
    # prefer on-domain hits first
    found.sort(key=lambda e: (0 if dom and e.endswith(dom) else 1))
    return found


def reveal_email(linkedin_url: str, name: str = "", domain: str = "") -> dict:
    """Open a LinkedIn (or company) profile in the real Chrome and read the email a free extension
    reveals. Rotates across the pool by remaining monthly credit. OPT-IN, human-in-the-loop.

    Returns {email, emails, extension, note, degraded}. Safe: degrades cleanly if Chrome or the
    extension pool is unavailable / exhausted.
    """
    result: dict[str, Any] = {"email": None, "emails": [], "extension": None,
                              "note": _TOS_NOTE, "degraded": []}
    ok, why = _available()
    if not ok:
        result["degraded"].append(why)
        return result
    if not linkedin_url:
        result["degraded"].append("no-url")
        return result

    member = QUOTA.pool_pick(_POOL_MEMBERS)
    if not member:
        result["degraded"].append("pool:all-credits-exhausted")
        return result
    ext = _BY_NAME[member]
    result["extension"] = member

    try:
        chrome.open_tab(linkedin_url)
        time.sleep(_SETTLE)
        # think→act: click the extension's reveal control, then observe the overlay
        clicked = _click_extension_button(linkedin_url, ext["button"])
        time.sleep(_SETTLE if clicked else 0.4)
        emails = _read_revealed(linkedin_url, domain)
        result["emails"] = emails
        if emails:
            result["email"] = emails[0]
            QUOTA.record_pool_use(member, ext["cap"])   # consumed a free credit
        elif clicked:
            result["degraded"].append(f"{member}:no-email-revealed")
        else:
            result["degraded"].append(
                f"{member}:button-not-found — extension may not be installed; "
                "install Apollo/ContactOut/Lusha from the Chrome Web Store"
            )
    except Exception as e:  # noqa: BLE001
        result["degraded"].append(f"{member}:error:{str(e)[:40]}")
    finally:
        try:
            chrome.close_tab(linkedin_url)
        except Exception:
            pass
    return result
