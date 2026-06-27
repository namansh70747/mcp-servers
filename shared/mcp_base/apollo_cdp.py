"""Background email reveal by driving the real Apollo (or ContactOut/Lusha) side panel over CDP.

The free reveal extensions render their "Access email" button + the revealed address in Chrome's
native side panel — a separate `chrome-extension://` document that AppleScript cannot touch. CDP
*can* attach to it, so this module replicates the user's manual move fully in the background:

    open the LinkedIn profile  →  (panel auto-updates)  →  click "Access email"  →  read the email

Stays on the extension's FREE monthly credits (no paid API key). Rotates across the installed pool
(Apollo → ContactOut → Lusha → …) when one is out of credits. Nothing is fabricated — the caller
still runs every returned address through verify().

Entry point:
    reveal(name, company="", domain="", linkedin_url="") -> {email, emails, extension, source,
                                                              ms, credit_used, degraded}
"""
from __future__ import annotations

import json
import re
import threading
import time
from typing import Any

from . import cdp
from .config import data_dir, get_env
from .email_extract import is_role
from .extensions import _BY_NAME, _POOL_MEMBERS
from .log import get_logger
from .quota import QUOTA

log = get_logger("apollo_cdp")

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
_REVEAL_POLL_S = 0.15      # poll cadence (returns the instant a terminal state is detected)
_REVEAL_CAP_S = 3.0        # max wait for the email to appear after clicking Access
_PANEL_LOAD_S = 5.0        # max wait for the panel to reach a terminal state (email/button/unavailable)
_PANEL_SYNC_S = float(get_env("APOLLO_PANEL_SYNC_S", "11") or 11)  # max wait for panel to sync the person
_NAV_SETTLE_S = 0.5        # brief settle after navigating the LinkedIn tab
_OPENER_WAIT_S = 3.0       # max wait for Apollo to inject its in-page opener

# Side-panel document title hint per extension (the panel's <title> / target title).
_PANEL_TITLE = {
    "apollo": "apollo", "contactout": "contactout", "lusha": "lusha",
    "getprospect": "getprospect", "skrapp_ext": "skrapp", "snov_ext": "snov",
    "wiza": "wiza", "kaspr": "kaspr",
}


def _clean_emails(blob: str, domain: str = "") -> list[str]:
    """Pull real (non-role, non-asset) emails from a DOM blob, on-domain first."""
    dom = (domain or "").lower().lstrip("@")
    out: list[str] = []
    for m in _EMAIL_RE.findall(blob or ""):
        e = m.lower()
        if e in out or is_role(e):
            continue
        if "noreply" in e or "example.com" in e or e.endswith((".png", ".jpg", ".svg", ".gif")):
            continue
        out.append(e)
    out.sort(key=lambda e: (0 if dom and e.endswith(dom) else 1))
    return out


_APOLLO_EXT_ID = "alhgpfoeiimagjlnfekdhkjlkiomcapa"
# The panel UI renders inside an assets.apollo.io iframe; the side-panel.html is just the container.
_PANEL_URL_HINTS = {"apollo": ("assets.apollo.io", _APOLLO_EXT_ID, "side-panel")}
_PANEL_MARKER = "contact information|access email|add to sequence|save contact|compose email"
# Apollo's own infra emails that are never the target.
_NOISE = ("apollo.io", "sentry", "@sentry", "intercom", "googleapis", "cloudfront")


def _is_panel(tgt: dict) -> bool:
    """True if this target's DOM looks like the open Apollo person panel."""
    ok, val = cdp.eval_js(
        tgt, f"/{_PANEL_MARKER}/i.test((document.body&&document.body.innerText||'').slice(0,4000))",
        timeout=4.0)
    return bool(ok and val)


def _li_slug(linkedin_url: str) -> str:
    m = re.search(r"/in/([a-z0-9\-]+)", linkedin_url or "", re.I)
    return m.group(1).lower() if m else ""


def _profile_name_from_tab(li_target: dict | None) -> str:
    """Read the person's real name from the open LinkedIn profile tab (document.title is reliably
    '<First Last> | LinkedIn'). Works for single-token slugs where _name_from_slug can't split."""
    if not li_target:
        return ""
    ok, title = cdp.eval_js(li_target, "document.title", timeout=4.0)
    if not ok or not title:
        return ""
    t = re.split(r"\s[|\-–]\s", str(title))[0].strip()
    # strip a trailing "(123) " unread-count prefix LinkedIn sometimes adds
    t = re.sub(r"^\(\d+\)\s*", "", t)
    return t if 2 <= len(t.split()) <= 4 else ""


def _name_from_slug(linkedin_url: str) -> str:
    """Best-effort person name from a /in/<slug> URL (drops trailing id tokens). '' if unsplittable —
    e.g. /in/atai-barkai → 'Atai Barkai'; /in/damiengarros (single token) → '' (no reliable split)."""
    slug = _li_slug(linkedin_url)
    if not slug:
        return ""
    toks = [t for t in slug.split("-") if t and not any(c.isdigit() for c in t) and len(t) > 1]
    return " ".join(t.capitalize() for t in toks[:3]) if len(toks) >= 2 else ""


# Known installed-extension ids (service-worker/panel targets carry these). Apollo is the primary.
_EXT_IDS = {"apollo": _APOLLO_EXT_ID}


def _present_members() -> set[str]:
    """Which pool extensions are actually INSTALLED right now — detected by a live CDP target
    (service worker or panel) carrying the extension's id or panel-title hint. Avoids wasting time
    panel-syncing extensions that aren't installed."""
    present: set[str] = set()
    blob = " ".join(((t.get("url") or "") + " " + (t.get("title") or "")).lower()
                     for t in cdp.targets())
    for name in (m for m, _ in _POOL_MEMBERS):
        ext_id = _EXT_IDS.get(name, "")
        hint = _PANEL_TITLE.get(name, name)
        if (ext_id and ext_id in blob) or (hint and hint in blob):
            present.add(name)
    return present


def _last_name(name: str) -> str:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if len(p) > 1]
    return parts[-1].lower() if parts else ""


# Last person we successfully read off the shared panel (last name). Used so a sequential bulk
# reveal never accepts/settles on a panel still showing the PREVIOUS person before Apollo swaps it.
_LAST_PANEL_PERSON = ""

# Reentrant lock serializing all Apollo CDP work (reveal + people-search) on the SINGLE hidden tab.
# Concurrent find() calls (parallel bulk discovery, or two requests) would otherwise collide on the
# shared tab / `_LAST_PANEL_PERSON` global and cross-contaminate results. RLock so a reveal that
# internally calls another locked helper doesn't deadlock.
_CDP_LOCK = threading.RLock()

# ── Persistent cache helpers (survive process restarts) ───────────────────────────────────────
# Both people-search and discovery caches are now persisted to JSON sidecars in data_dir("email-finder").
# Cache entries use time.time() (wall-clock) so timestamps survive restarts — contrast with the
# per-operation deadline loops elsewhere in this file which correctly stay on time.monotonic().

import os as _os
import pathlib as _pathlib

def _cache_dir() -> _pathlib.Path:
    try:
        return _pathlib.Path(data_dir("email-finder"))
    except Exception:
        return _pathlib.Path("/tmp")

def _load_sidecar(fname: str, ttl: float) -> "dict[str, tuple[float, dict]]":
    """Load a cache sidecar JSON, dropping entries older than ttl seconds."""
    path = _cache_dir() / fname
    try:
        with open(path) as _f:
            raw: dict = json.load(_f)
        now = time.time()
        return {k: (ts, v) for k, (ts, v) in (
            (k, (float(row[0]), row[1])) for k, row in raw.items()
        ) if (now - ts) < ttl}
    except Exception:
        return {}

def _save_sidecar(fname: str, cache: "dict[str, tuple[float, dict]]") -> None:
    """Atomically write cache to JSON sidecar (temp file + rename)."""
    path = _cache_dir() / fname
    tmp = path.with_suffix(".tmp")
    try:
        with open(tmp, "w") as _f:
            json.dump({k: [ts, v] for k, (ts, v) in cache.items()}, _f)
        _os.replace(tmp, path)
    except Exception:
        try:
            _os.unlink(tmp)
        except Exception:
            pass


# Cache for apollo_people_search keyed by (norm-company | domain, role). Company→people is stable
# within a session, so repeats and bulk batches return instantly with no re-query. TTL ~1h.
_PEOPLE_TTL = 3600.0
_PEOPLE_CACHE: dict[str, tuple[float, dict]] = _load_sidecar("apollo_people_cache.json", _PEOPLE_TTL)


def _find_panel(ext_name: str = "apollo", want_slug: str = "", want_name: str = "",
                avoid_last: str | None = None) -> dict | None:
    """Locate the OPEN reveal panel as a CDP target. To guarantee we read the RIGHT person after a
    navigation, a candidate must look like a person panel AND match the requested person — by the
    profile slug in its URL OR (more reliable, since Apollo updates content without always changing
    the iframe URL) the person's LAST NAME appearing in the panel text. When a person is requested
    but none matches yet, returns None so the caller keeps waiting (never reads a stale prior person).

    `avoid_last` (defaults to the last person read on the shared panel) makes a panel that STILL
    shows the previous person — and not yet the target — count as stale → keep waiting. This is what
    makes sequential bulk reveals reliable while Apollo lags swapping the panel A→B."""
    hints = _PANEL_URL_HINTS.get(ext_name, (ext_name,))
    last = _last_name(want_name)
    avoid = (avoid_last if avoid_last is not None else _LAST_PANEL_PERSON) or ""
    best = None
    for tgt in cdp.targets():
        url = (tgt.get("url") or "").lower()
        if not tgt.get("webSocketDebuggerUrl") or not any(h in url for h in hints):
            continue
        ok, txt = cdp.eval_js(
            tgt, "(document.body&&document.body.innerText||'').slice(0,5000)", timeout=4.0)
        low = (str(txt) if ok else "").lower()
        if not re.search(_PANEL_MARKER, low):
            continue
        if (last and last in low) or (want_slug and f"/in/{want_slug}" in url):
            return tgt   # definitively this person
        # Stale: panel still shows the PREVIOUS person and not yet the target → skip (keep waiting).
        if avoid and avoid != last and avoid in low and not (last and last in low):
            continue
        best = best or tgt
    if want_slug or last:
        return None      # person requested but not synced yet → keep waiting
    return best


def _panel_target_exists(ext_name: str = "apollo") -> bool:
    """True if a side-panel CDP target for this extension is currently open (regardless of which
    person it shows). CDP can't OPEN the native side panel — this tells us whether one already is."""
    hints = _PANEL_URL_HINTS.get(ext_name, (ext_name,))
    for t in cdp.targets():
        url = (t.get("url") or "").lower()
        if t.get("webSocketDebuggerUrl") and any(h in url for h in hints):
            return True
    return False


def panel_is_open(ext_name: str = "apollo") -> bool:
    """Public: is the reveal extension's side panel open right now? (readiness for reveals)."""
    try:
        return _panel_target_exists(ext_name)
    except Exception:  # noqa: BLE001
        return False


def _open_panel(li_target: dict) -> bool:
    """Open the Apollo side panel by TRUSTED-clicking the in-page opener icon (a real user gesture —
    required for chrome.sidePanel.open). Returns True once a panel target appears."""
    ok, rect = cdp.eval_js(li_target, """(function(){
      var e=document.querySelector('.apollo-opener-icon, input.apollo-opener-icon, [class*=apollo-opener]');
      if(!e) return ''; var r=e.getBoundingClientRect();
      return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2});})()""", timeout=6.0)
    if not ok or not rect:
        return False
    try:
        import json as _json
        d = _json.loads(rect)
    except Exception:  # noqa: BLE001
        return False
    cdp.click_at(li_target, d["x"], d["y"])
    deadline = time.monotonic() + 5.0
    while time.monotonic() < deadline:
        if _find_panel("apollo"):
            return True
        time.sleep(0.25)
    return False


def _read_panel_emails(panel: dict, domain: str) -> list[str]:
    ok, blob = cdp.eval_js(panel, "document.body.innerText || ''", timeout=6.0)
    if not ok or not blob:
        return []
    emails = _clean_emails(str(blob), domain)
    return [e for e in emails if not any(n in e for n in _NOISE)]


# Synthetic click on the PRECISE reveal button (the reveal is an in-app XHR — no trusted gesture
# needed, unlike opening the side panel). Prefers the email-only button over "Access email & phone".
_CLICK_ACCESS_JS = """(function(){
  function clk(e){try{e.click();return true;}catch(_){return false;}}
  var btns=[].slice.call(document.querySelectorAll('button,[role=button]'));
  for (var i=0;i<btns.length;i++){var t=(btns[i].innerText||'').trim().toLowerCase();
    if(t==='access email') return clk(btns[i])?'email':'';}
  for (var j=0;j<btns.length;j++){var u=(btns[j].innerText||'').trim().toLowerCase();
    if(u.indexOf('access email')===0) return clk(btns[j])?'email+phone':'';}
  var ds=[].slice.call(document.querySelectorAll('[class*=zp_]'));
  for (var k=0;k<ds.length;k++){var w=(ds[k].innerText||'').trim().toLowerCase();
    if(w==='access email' && ds[k].onclick) return clk(ds[k])?'div':'';}
  return '';})()"""

# Bounding-box center of the precise email-only "Access email" BUTTON (for a trusted-click fallback).
_ACCESS_BOX_JS = """(function(){
  var btns=[].slice.call(document.querySelectorAll('button,[role=button]'));
  var hit=null;
  for (var i=0;i<btns.length;i++){var t=(btns[i].innerText||'').trim().toLowerCase();
    if(t==='access email'){hit=btns[i];break;}}
  if(!hit){for (var j=0;j<btns.length;j++){var u=(btns[j].innerText||'').trim().toLowerCase();
    if(u.indexOf('access email')===0){hit=btns[j];break;}}}
  if(!hit) return '';
  var r=hit.getBoundingClientRect();
  return JSON.stringify({x:r.x+r.width/2, y:r.y+r.height/2});})()"""


def _click_access_email(panel: dict) -> str:
    """Synthetically click Apollo's precise 'Access email' button. Returns which control was clicked
    ('email' | 'email+phone' | 'div' | '')."""
    ok, val = cdp.eval_js(panel, _CLICK_ACCESS_JS, timeout=6.0)
    return str(val) if (ok and val) else ""


def _trusted_click_access(panel: dict) -> bool:
    """Fallback: trusted CDP click at the BUTTON's own center (not the row container's)."""
    ok, val = cdp.eval_js(panel, _ACCESS_BOX_JS, timeout=4.0)
    if not ok or not val:
        return False
    try:
        import json as _json
        d = _json.loads(val)
    except Exception:  # noqa: BLE001
        return False
    cdp.click_at(panel, d["x"], d["y"])
    return True


def _poll_read(panel: dict, domain: str, secs: float) -> list[str]:
    """Poll the panel DOM for an email up to `secs` — returns the instant one appears."""
    deadline = time.monotonic() + secs
    while True:
        emails = _read_panel_emails(panel, domain)
        if emails:
            return emails
        if time.monotonic() >= deadline:
            return []
        time.sleep(_REVEAL_POLL_S)


# ---------------------------------------------------------------------------
# Sync strategy ladder — force Apollo to swap the (hidden) panel to THIS person, fast-first.
# Each strategy is a window-hidden-only nudge; we re-check the panel (stale-aware, early-exit) after
# each. A calibrated order (persisted per machine) tries the proven-winning strategy FIRST.
# ---------------------------------------------------------------------------

_CALIB_PATH = None  # lazily resolved to data_dir("email-finder")/apollo_calibration.json
_DEFAULT_STRATEGY_ORDER = ["emulate", "spoof", "activity", "renav", "reopen", "lifecycle"]
_STRATEGY_BUDGET_S = float(get_env("APOLLO_STRATEGY_BUDGET_S", "2.6") or 2.6)
_SYNC_ROUNDS = int(get_env("APOLLO_SYNC_ROUNDS", "2") or 2)


def _calib_file():
    global _CALIB_PATH
    if _CALIB_PATH is None:
        try:
            from .config import data_dir
            _CALIB_PATH = str(data_dir("email-finder") / "apollo_calibration.json")
        except Exception:  # noqa: BLE001
            _CALIB_PATH = ""
    return _CALIB_PATH


def load_strategy_order() -> list[str]:
    """Calibrated strategy order for this machine (winning strategy first), else the default."""
    try:
        import json as _json
        with open(_calib_file()) as fh:
            order = _json.load(fh).get("order") or []
        # keep only known strategies, append any missing so all are still tried
        order = [s for s in order if s in _DEFAULT_STRATEGY_ORDER]
        return order + [s for s in _DEFAULT_STRATEGY_ORDER if s not in order] if order \
            else list(_DEFAULT_STRATEGY_ORDER)
    except Exception:  # noqa: BLE001
        return list(_DEFAULT_STRATEGY_ORDER)


def save_strategy_order(order: list[str]) -> bool:
    try:
        import json as _json
        with open(_calib_file(), "w") as fh:
            _json.dump({"order": order}, fh)
        return True
    except Exception:  # noqa: BLE001
        return False


def _apply_strategy(name: str, li_target: dict, slug: str) -> None:
    """Apply one window-hidden-only sync nudge to the LinkedIn tab (+ any open panel)."""
    if name == "emulate":
        cdp.force_active(li_target)
        p = _find_panel("apollo", avoid_last="")  # any panel, just to un-throttle it
        if p:
            cdp.force_active(p)
    elif name == "spoof":
        cdp.spoof_visible(li_target)
    elif name == "activity":
        cdp.eval_js(li_target, "window.scrollBy(0, 240); window.scrollBy(0, -120); true", timeout=3.0)
        cdp._send(li_target, "Input.dispatchMouseEvent",
                  {"type": "mouseMoved", "x": 240, "y": 320})
    elif name == "renav":
        if slug:
            cdp.navigate(li_target, f"https://www.linkedin.com/in/{slug}")
            time.sleep(_NAV_SETTLE_S + 0.6)
            cdp.force_active(li_target)
            cdp.spoof_visible(li_target)
    elif name == "reopen":
        _open_panel(li_target)
    elif name == "lifecycle":
        cdp.lifecycle_bounce(li_target)


def _sync_panel_to_person(li_target: dict | None, ext_name: str, want_slug: str,
                          want_name: str, trace: list | None = None,
                          avoid_last: str | None = None) -> dict | None:
    """Return a panel synced to THIS person, escalating through the strategy ladder (calibrated
    order first) over a few rounds, re-checking stale-aware after each strategy with early-exit.
    Records each attempt in `trace`. Window-hidden the whole time."""
    def _check():
        return _find_panel(ext_name, want_slug=want_slug, want_name=want_name, avoid_last=avoid_last)

    def _rec(strategy, round_i, panel):
        if trace is not None:
            li_url = (li_target or {}).get("url", "")
            trace.append({"strategy": strategy, "round": round_i,
                          "panel_present": bool(panel), "li_title_slug": want_slug,
                          "t": round(time.monotonic(), 3)})

    # Already synced? (warm panel — instant, no nudges)
    panel = _check()
    if panel:
        _rec("warm", 0, panel)
        return panel
    if li_target is None:
        return None

    order = load_strategy_order()
    for round_i in range(max(1, _SYNC_ROUNDS)):
        for strat in order:
            _apply_strategy(strat, li_target, want_slug)
            deadline = time.monotonic() + _STRATEGY_BUDGET_S
            while time.monotonic() < deadline:
                panel = _check()
                if panel:
                    _rec(strat, round_i, panel)
                    return panel
                time.sleep(0.15)
            _rec(strat, round_i, None)
    return None


def _reveal_with(ext_name: str, domain: str, li_target: dict | None,
                 want_slug: str = "", want_name: str = "", trace: list | None = None,
                 avoid_last: str | None = None) -> tuple[list[str], list[str], bool, bool]:
    """Locate (or open) the panel SYNCED TO THIS PERSON (matched by slug URL or last name in the
    panel text), read an already-revealed email, else TRUSTED-click "Access email" and poll. The
    person match guarantees we never read a stale prior person's email in bulk."""
    degraded: list[str] = []
    # FAST PRE-CHECK: CDP cannot OPEN Chrome's native side panel (needs a real user gesture). If no
    # panel target exists at all, fail immediately with an actionable message instead of grinding the
    # full sync ladder for ~50s. The "keep the panel open once" model relies on this being instant.
    if not _panel_target_exists(ext_name):
        if trace is not None:
            trace.append({"strategy": "precheck", "panel_present": False, "note": "no panel target"})
        return [], [f"{ext_name}:panel-not-open — open the {ext_name.capitalize()} side panel ONCE "
                    "in the debug Chrome (click its toolbar icon) and keep it open; the background "
                    "reveal then reuses it and syncs it to each profile automatically"], False, False

    # Panel IS open → force it to sync to THIS person via the strategy ladder (fast-first, early-exit).
    panel = _sync_panel_to_person(li_target, ext_name, want_slug, want_name, trace, avoid_last)
    if not panel:
        # Last resort: any open panel — but only if no person was specified, to avoid stale reads.
        if not (want_slug or _last_name(want_name)):
            panel = _find_panel(ext_name)
    if not panel:
        return [], [f"{ext_name}:panel-not-synced — panel is open but didn't sync to this person in "
                    "time (Apollo may have no card for them)"], False, False

    # Keep the side-panel iframe un-throttled while backgrounded (focus emulation only, no raise).
    cdp.make_visible(panel)

    btn = (_BY_NAME.get(ext_name, {}) or {}).get("button", "access email")

    # 1) The panel loads asynchronously. Poll up to _PANEL_LOAD_S for ONE of three terminal states:
    #    (a) an email shown directly (already revealed) → return, no credit;
    #    (b) an "Access email" button → break out to click it (costs a credit);
    #    (c) "Unavailable" / "(No Name)" → Apollo has no email → return the honest reason.
    deadline = time.monotonic() + _PANEL_LOAD_S
    has_access = False
    while time.monotonic() < deadline:
        emails = _read_panel_emails(panel, domain)
        if emails:
            return emails, degraded, False, True
        ok, txt = cdp.eval_js(panel, "(document.body.innerText||'')", timeout=4.0)
        low = (str(txt) if ok else "").lower()
        # is there a clickable reveal control? (text-matched, like cdp.click_element)
        ok2, hit = cdp.eval_js(
            panel,
            "(function(){var re=/access email/i;var es=[].slice.call("
            "document.querySelectorAll('button,a,[role=button],div,span'));"
            "for(var i=0;i<es.length;i++){var t=(es[i].innerText||'').trim();"
            "if(t&&t.length<40&&re.test(t))return true;}return false;})()", timeout=4.0)
        if ok2 and hit:
            has_access = True
            break
        if "unavailable" in low:
            return [], [f"{ext_name}:email-unavailable — Apollo has a card but NO email on record"], False, True
        if "(no name)" in low or "no linkedin url" in low:
            return [], [f"{ext_name}:no-card — Apollo couldn't match this LinkedIn profile"], False, True
        time.sleep(_REVEAL_POLL_S)

    if not has_access:
        # one last read, then give up honestly
        emails = _read_panel_emails(panel, domain)
        if emails:
            return emails, degraded, False, True
        return [], [f"{ext_name}:no-email — Apollo has no revealable email for this person"], False, True

    # 2) Click the PRECISE "Access email" button. Synthetic .click() first (the reveal is an in-app
    #    XHR, no trusted gesture needed); then poll for the revealed email.
    which = _click_access_email(panel)
    if which:
        emails = _poll_read(panel, domain, _REVEAL_CAP_S)
        if emails:
            return emails, degraded, True, True

    # 3) Fallback: trusted CDP click at the BUTTON's true center (covers apps that ignore synthetic
    #    clicks, and the case where the button matched but the handler needs a real pointer event).
    if _trusted_click_access(panel):
        emails = _poll_read(panel, domain, _REVEAL_CAP_S)
        if emails:
            return emails, degraded, True, True

    if not which:
        return [], [f"{ext_name}:access-button-not-found"], False, True
    return [], [f"{ext_name}:no-email-after-click — out of credits or no email on record"], True, True


# ---------------------------------------------------------------------------
# Company/role → LinkedIn profile URL discovery (the manual "search → top link" move, automated)
# ---------------------------------------------------------------------------

_LI_PROFILE_RE = re.compile(r"https?://([a-z]{2,3}\.)?linkedin\.com/in/[a-zA-Z0-9_%\-]+", re.I)
_DISCOVERY_BUDGET_S = float(get_env("LI_DISCOVERY_BUDGET_S", "9") or 9)
_SEARCH_SETTLE = {"bing": 1.3, "duck": 1.3, "linkedin": 2.6}
_DISCOVERY_TTL = 3600.0
_DISCOVERY_CACHE: dict[str, tuple[float, dict]] = _load_sidecar("apollo_discovery_cache.json", _DISCOVERY_TTL)


def _scratch_tab(seed: str) -> dict | None:
    """Return the automation-owned scratch tab for searches. NEVER repurposes a user tab."""
    return cdp.owned_tab("scratch", seed)


def _await_commit(target: dict, expect: str, max_s: float = 3.0) -> None:
    """Poll until the tab's URL reflects the just-issued navigation, so we never scan the PREVIOUS
    query's page when a scratch tab is reused. Best-effort; returns when committed or timed out."""
    want = (expect or "").lower()
    deadline = time.monotonic() + max_s
    while time.monotonic() < deadline:
        ok, href = cdp.eval_js(target, "location.href", timeout=2.0)
        if ok and want in str(href).lower():
            return
        time.sleep(0.2)


def _decode_bing_redirect(href: str) -> str:
    """Bing wraps result links as bing.com/ck/a?...&u=a1<base64url>. Decode the `u` param to the
    real destination URL so a linkedin.com/in/ link can be extracted. Returns href unchanged if it
    isn't a Bing redirect, '' on decode failure."""
    if "bing.com/ck/a" not in href and "/ck/a?" not in href:
        return href
    try:
        from urllib.parse import urlsplit, parse_qs
        import base64
        u = (parse_qs(urlsplit(href).query).get("u") or [""])[0]
        if not u:
            return ""
        if u.startswith("a1"):
            u = u[2:]
        return base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode("utf-8", "ignore")
    except Exception:
        return ""


def _name_from_result_title(text: str) -> str:
    """Extract a person name from a search-result title like 'Damien Garros - CEO - OpsMill | LinkedIn'
    or 'Damien Garros | LinkedIn'. Returns '' if it doesn't look like a 'First Last' name."""
    t = re.split(r"\s[|\-–·]\s", (text or "").strip())[0].strip()
    t = re.sub(r"\s*\(.*?\)\s*$", "", t)  # drop trailing "(2)" etc.
    toks = t.split()
    if 2 <= len(toks) <= 4 and all(re.match(r"[A-Za-z][A-Za-z'’.\-]+$", w) for w in toks):
        return t
    return ""


def _collect_li_profiles(target: dict, settle: float, expect: str = "",
                         names_out: dict | None = None) -> list[str]:
    """Wait for the navigation to commit (URL reflects `expect`), then `settle` for render, then scan
    result anchors for linkedin.com/in/<slug> URLs IN DOCUMENT ORDER (top result first). When
    `names_out` is provided, also map slug→display-name parsed from each result's anchor/title text
    (so single-token slugs like 'damiengarros' still yield 'Damien Garros')."""
    if expect:
        _await_commit(target, expect)
    time.sleep(settle)
    # Capture EVERY result anchor's full href (NOT split) + its visible text. Bing wraps links in
    # /ck/a redirects, so we keep the raw href and decode it in Python below.
    ok, val = cdp.eval_js(target, r"""(function(){
      var out=[];
      var as=[].slice.call(document.querySelectorAll('a[href]'));
      for(var i=0;i<as.length;i++){
        var h=as[i].getAttribute('href')||as[i].href||'';
        if(!h) continue;
        var t=(as[i].innerText||as[i].textContent||'').trim();
        if(!t){ var p=as[i].closest('li,div'); t=p?((p.querySelector('h2,h3,h4')||{}).innerText||'').trim():''; }
        out.push({h:h, t:t.slice(0,140)});
      }
      // also include the raw HTML so a regex fallback can find encoded linkedin URLs
      var html=document.documentElement?document.documentElement.outerHTML:'';
      return JSON.stringify({anchors: out.slice(0,80), html: html.slice(0,120000)});
    })()""", timeout=8.0)
    if not ok:
        return []
    try:
        import json as _json
        data = _json.loads(val) if isinstance(val, str) else (val or {})
    except Exception:
        data = {}
    anchors = data.get("anchors", []) if isinstance(data, dict) else []

    seen, clean = set(), []

    def _take(url: str, text: str = ""):
        slug = _li_slug(url or "")
        if not slug or slug in seen:
            return
        seen.add(slug)
        u = f"https://www.linkedin.com/in/{slug}"
        clean.append(u)
        if names_out is not None and text:
            nm = _name_from_result_title(text)
            if nm:
                names_out[u] = nm

    # 1) decode each anchor (direct linkedin link OR a Bing /ck/a redirect) + pair with its text
    for a in anchors:
        href = a.get("h", "")
        text = a.get("t", "")
        real = href if "linkedin.com/in/" in href else _decode_bing_redirect(href)
        if real and "linkedin.com/in/" in real:
            _take(real.split("?")[0], text)
    # 2) regex fallback over raw HTML (catches encoded URLs missed above; no name)
    if not clean:
        import re as _re
        html = data.get("html", "") if isinstance(data, dict) else ""
        for m in _re.finditer(r"linkedin\.com/in/[a-zA-Z0-9_%\-]+", html, _re.I):
            _take("https://www." + m.group(0))
    return clean


def _login_walled(target: dict) -> bool:
    ok, val = cdp.eval_js(
        target,
        "/(sign in|join now|authwall|log in to linkedin)/i.test("
        "(document.body&&document.body.innerText||'').slice(0,1500))",
        timeout=4.0)
    return bool(ok and val)


def _search_web_for_li(query: str) -> tuple[list[str], str, dict]:
    """Run a plain '<query> linkedin' web search in the background Chrome (Bing → DuckDuckGo) and
    return (ordered /in/ profile URLs, source, {url: display-name})."""
    from urllib.parse import quote_plus
    q = quote_plus(f"{query} linkedin")
    for engine, url, settle_key in (
        ("bing", f"https://www.bing.com/search?q={q}", "bing"),
        ("duck", f"https://duckduckgo.com/?q={q}", "duck"),
    ):
        tab = _scratch_tab(f"https://www.bing.com/search?q={q}")
        if not tab:
            continue
        cdp.make_visible(tab)
        cdp.navigate(tab, url)
        token = q[:24]  # commit-token so we don't read a stale page
        names: dict = {}
        profiles = _collect_li_profiles(tab, _SEARCH_SETTLE[settle_key], expect=token, names_out=names)
        if profiles:
            return profiles, f"web:{engine}", names
    return [], "web:none", {}


def _search_linkedin_for_li(query: str) -> tuple[list[str], str, dict]:
    """Run LinkedIn's own authenticated people-search in the logged-in debug Chrome."""
    from urllib.parse import quote_plus
    url = f"https://www.linkedin.com/search/results/people/?keywords={quote_plus(query)}"
    tab = _scratch_tab(url)
    if not tab:
        return [], "linkedin:no-tab", {}
    cdp.make_visible(tab)
    cdp.navigate(tab, url)
    _await_commit(tab, "search/results/people")
    if _login_walled(tab):
        return [], "linkedin:not-logged-in", {}
    names: dict = {}
    return _collect_li_profiles(tab, _SEARCH_SETTLE["linkedin"], names_out=names), "linkedin:search", names


def find_linkedin_profile(query: str, company: str = "", role: str = "",
                          limit: int = 5) -> dict:
    """Resolve the top LinkedIn profile URL(s) for a "<company> <role>" style query — the automated
    version of the user's manual search. Races a background-Chrome web search against LinkedIn's
    authenticated people-search (whichever yields a real /in/ first wins), with a keyless fallback.

    Returns {profiles: [url,...], top: url|None, source: str, degraded: [str,...]}.
    """
    result: dict[str, Any] = {"profiles": [], "top": None, "source": None, "degraded": []}
    q = (query or "").strip()
    if not q:
        result["degraded"].append("discovery:empty-query")
        return result

    # In-process cache (the server is long-lived): a repeat company→profile lookup is instant.
    _ck = q.lower()
    _hit = _DISCOVERY_CACHE.get(_ck)
    if _hit and (time.time() - _hit[0]) < _DISCOVERY_TTL:
        return {**_hit[1], "cached": True}

    ok, why = cdp.ensure_running()
    if ok:
        from concurrent.futures import ThreadPoolExecutor, as_completed
        pool = ThreadPoolExecutor(max_workers=2)
        try:
            futs = {pool.submit(_search_web_for_li, q): "web",
                    pool.submit(_search_linkedin_for_li, q): "linkedin"}
            web_profiles: list[str] = []
            li_profiles: list[str] = []
            names: dict = {}
            deadline = time.monotonic() + _DISCOVERY_BUDGET_S
            for fut in as_completed(futs, timeout=_DISCOVERY_BUDGET_S + 2):
                try:
                    profiles, src, nm = fut.result(timeout=max(0.1, deadline - time.monotonic()))
                except Exception:
                    profiles, src, nm = [], f"{futs[fut]}:error", {}
                names.update(nm or {})
                if src.startswith("web") and profiles:
                    web_profiles = profiles
                elif src.startswith("linkedin") and profiles:
                    li_profiles = profiles
                elif "not-logged-in" in src:
                    result["degraded"].append(src)
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
        # Prefer the web-search order (matches the manual "top link" intuition), then LinkedIn's.
        merged, seen = [], set()
        for u in web_profiles + li_profiles:
            s = _li_slug(u)
            if s and s not in seen:
                seen.add(s)
                merged.append(u)
        if merged:
            result["profiles"] = merged[:limit]
            result["top"] = merged[0]
            result["names"] = names
            result["top_name"] = names.get(merged[0]) or _name_from_slug(merged[0])
            # Authoritative fallback: if neither the result text nor the slug yielded a name
            # (e.g. single-token slug 'damiengarros'), open the profile and read document.title
            # ('Damien Garros | LinkedIn'). Reliable for ALL slugs; one extra nav, only when needed.
            if not result["top_name"]:
                try:
                    tab = _scratch_tab(merged[0])
                    if tab:
                        cdp.make_visible(tab)
                        cdp.navigate(tab, merged[0])
                        _await_commit(tab, "/in/" + _li_slug(merged[0]), max_s=6.0)
                        nm = _profile_name_from_tab(tab)
                        if nm:
                            result["top_name"] = nm
                            names[merged[0]] = nm
                except Exception:
                    pass
            result["source"] = "web" if web_profiles else "linkedin"
            _DISCOVERY_CACHE[_ck] = (time.time(), result)
            with _CDP_LOCK:
                _save_sidecar("apollo_discovery_cache.json", _DISCOVERY_CACHE)
            return result
    else:
        result["degraded"].append(f"cdp:unreachable:{why}")

    # Keyless fallback (no Chrome): plain query, NO site: operator.
    try:
        from .websearch import search_links
        links = search_links(f'"{company or query}" {role} linkedin'.strip(), n=10)
        seen = set()
        for u in links:
            s = _li_slug(u)
            if s and s not in seen:
                seen.add(s)
                result["profiles"].append(f"https://www.linkedin.com/in/{s}")
        if result["profiles"]:
            result["profiles"] = result["profiles"][:limit]
            result["top"] = result["profiles"][0]
            result["source"] = "keyless"
            _DISCOVERY_CACHE[_ck] = (time.time(), result)
            with _CDP_LOCK:
                _save_sidecar("apollo_discovery_cache.json", _DISCOVERY_CACHE)
            return result
    except Exception as e:  # noqa: BLE001
        result["degraded"].append(f"keyless:error:{str(e)[:40]}")

    if not result["degraded"]:
        result["degraded"].append("discovery:no-profile-found")
    return result


def _ensure_li_tab(linkedin_url: str) -> dict | None:
    """Return the automation-owned LinkedIn tab navigated to `linkedin_url`.

    Uses cdp.owned_tab('li', ...) — NEVER repurposes any tab the user is viewing.
    The owned tab is reused across reveals; only navigated when the URL differs."""
    slug = _li_slug(linkedin_url)
    target_sub = f"/in/{slug}" if slug else "linkedin.com"
    seed = linkedin_url or "https://www.linkedin.com/feed/"
    pg = cdp.owned_tab("li", seed)
    if not pg:
        return None
    cdp.make_visible(pg)
    cdp.block_heavy_resources(pg)
    # Check if already on the right profile — skip navigation
    if slug:
        cur_ok, cur = cdp.eval_js(pg, "location.href", timeout=4.0)
        if cur_ok and f"/in/{slug}" in str(cur).lower():
            return pg
    # Navigate to the desired profile
    if linkedin_url:
        cur_ok, cur = cdp.eval_js(pg, "location.href", timeout=4.0)
        if not (cur_ok and linkedin_url.split("?")[0] in str(cur)):
            cdp.navigate(pg, linkedin_url)
    # Poll until navigation committed (URL reflects the profile slug)
    deadline = time.monotonic() + 8.0
    while time.monotonic() < deadline:
        ok, cur = cdp.eval_js(pg, "location.href", timeout=2.0)
        if ok and target_sub in str(cur).lower():
            cdp.make_visible(pg)
            cdp.block_heavy_resources(pg)
            return pg
        time.sleep(0.3)
    cdp.make_visible(pg)
    return pg


def prewarm() -> tuple[bool, str]:
    """Get the pipeline warm ONCE before a bulk run: ensure the hidden debug Chrome is up, ensure a
    LinkedIn page tab exists, and open the Apollo side panel. After this every reveal in the batch is
    the warm path (navigate → sync → read), paying no per-item setup. Best-effort; never raises."""
    ok, why = cdp.ensure_running()
    if not ok:
        return False, why
    cdp.hide_app()
    # Use the owned "li" tab so we never repurpose a tab the user is viewing
    seed = "https://www.linkedin.com/feed/"
    li = cdp.owned_tab("li", seed)
    if li:
        cur_ok, cur = cdp.eval_js(li, "location.href", timeout=4.0)
        if not (cur_ok and "linkedin.com" in str(cur).lower()):
            cdp.make_visible(li); cdp.block_heavy_resources(li); cdp.navigate(li, seed)
            time.sleep(_NAV_SETTLE_S + 0.5)
    if li:
        cdp.make_visible(li)
        # open the panel once if it isn't already
        if not _find_panel("apollo"):
            deadline = time.monotonic() + _OPENER_WAIT_S
            while time.monotonic() < deadline:
                ok2, has = cdp.eval_js(
                    li, "!!document.querySelector('.apollo-opener-icon, input.apollo-opener-icon')",
                    timeout=4.0)
                if ok2 and has:
                    _open_panel(li)
                    break
                time.sleep(0.4)
    cdp.hide_app()
    return True, "warm"


# ===========================================================================
# Apollo BACKEND reveal — replicate the extension's authenticated call via an in-page fetch in the
# logged-in app.apollo.io tab. Fully background (no side panel, no DOM fragility). Free web-app
# credits. We auto-capture the exact request once, then replay it forever.
# ===========================================================================

_TEMPLATE_PATH = None
_APP = "https://app.apollo.io"


def _template_file() -> str:
    global _TEMPLATE_PATH
    if _TEMPLATE_PATH is None:
        try:
            _TEMPLATE_PATH = str(data_dir("email-finder") / "apollo_reveal_template.json")
        except Exception:  # noqa: BLE001
            _TEMPLATE_PATH = ""
    return _TEMPLATE_PATH


def _load_template() -> dict | None:
    try:
        with open(_template_file()) as fh:
            return json.load(fh)
    except Exception:  # noqa: BLE001
        return None


def _save_template(t: dict) -> bool:
    try:
        with open(_template_file(), "w") as fh:
            json.dump(t, fh, indent=2)
        return True
    except Exception:  # noqa: BLE001
        return False


def _apollo_tab_ready(tab: dict, timeout: float = 9.0) -> bool:
    """Poll until the app tab is genuinely ready for an AUTHENTICATED fetch: on app.apollo.io, logged
    in, and a CSRF token is actually present (meta tag OR cookie). A freshly-created/cold tab returns
    empty results until the SPA + session are loaded — this avoids the transient empty→wrong-fallback."""
    js = ("(function(){try{"
          "var host=/apollo\\.io/i.test(location.host);"
          "var li=!/(log in|sign in to apollo)/i.test((document.body&&document.body.innerText||'').slice(0,400));"
          "var m=document.querySelector('meta[name=csrf-token]');"
          "var hasCsrf=!!(m&&m.content)|| /X-CSRF-TOKEN=/i.test(document.cookie);"
          "return JSON.stringify({ready:host&&li&&hasCsrf});"
          "}catch(e){return JSON.stringify({ready:false});}})()")
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        ok, val = cdp.eval_js(tab, js, timeout=4.0)
        try:
            if (json.loads(val) if isinstance(val, str) else {}).get("ready"):
                return True
        except Exception:  # noqa: BLE001
            pass
        time.sleep(0.4)
    return False


def _apollo_app_tab(require_ready: bool = True) -> dict | None:
    """Return a READY logged-in app.apollo.io page target (reuse or create), forced active + hidden.
    Polls for session/CSRF readiness so a cold tab can't return a transient empty result."""
    t = cdp.find_target(url_substr="app.apollo.io", type_="page")
    if not t:
        t = cdp.new_tab(_APP + "/#/people")
        if t:
            time.sleep(_NAV_SETTLE_S + 1.0)
    if t:
        cdp.force_active(t)
        if require_ready and not _apollo_tab_ready(t):
            # cold/not-ready: nudge a navigation to the app and poll once more
            try:
                cdp.eval_js(t, f"if(!/apollo\\.io/i.test(location.host))location.href='{_APP}/#/people'",
                            timeout=4.0)
            except Exception:  # noqa: BLE001
                pass
            _apollo_tab_ready(t)
    return t


def _apollo_logged_in(tab: dict) -> bool:
    ok, val = cdp.eval_js(
        tab, "!/(log in|sign in to apollo)/i.test((document.body&&document.body.innerText||'')"
             ".slice(0,400)) && /apollo/i.test(location.host)", timeout=5.0)
    return bool(ok and val)


def capture_apollo_reveal(linkedin_url: str = "") -> dict:
    """Auto-capture Apollo's authenticated reveal request ONCE: record network on the app.apollo.io
    tab, trigger ONE reveal in-app (clicks an 'Access email' control), then identify + persist the
    reveal endpoint + payload + headers as a template. Spends one free credit. Window stays hidden.

    Returns {ok, endpoint, identifier_hint, degraded, raw_path}."""
    out: dict[str, Any] = {"ok": False, "degraded": []}
    okr, why = cdp.ensure_running()
    if not okr:
        out["degraded"].append(f"cdp:unreachable:{why}")
        return out
    cdp.hide_app()
    tab = _apollo_app_tab()
    if not tab:
        out["degraded"].append("apollo:no-tab")
        return out
    if not _apollo_logged_in(tab):
        out["degraded"].append("apollo:not-logged-in — log into app.apollo.io once in the debug Chrome")
        return out

    # record network while we trigger a reveal
    records: list = []

    def _rec():
        records.extend(cdp.record_network(tab, duration_s=30.0, url_filter="apollo.io"))

    th = threading.Thread(target=_rec, daemon=True)
    th.start()
    time.sleep(1.0)
    # Trigger ONE reveal. The People list masks emails until "Access email" is clicked; that click
    # fires the reveal endpoint we want to capture. Reload to (re)generate the list, then try a
    # broad set of reveal controls (button text, lock/email icons, masked-email cells).
    cdp._send(tab, "Page.reload", {"ignoreCache": False})
    time.sleep(6.0)
    cdp.force_active(tab)
    _click_js = r"""(function(){
      function clk(e){try{e.click();return true;}catch(_){return false;}}
      var re=/access email|access email & phone|get email|reveal email|unlock|show email/i;
      var els=[].slice.call(document.querySelectorAll('button,[role=button],a,span,div,i,svg'));
      // 1) explicit reveal controls by text/title/aria
      for(var i=0;i<els.length;i++){var e=els[i];
        var t=((e.innerText||e.textContent||'')+' '+(e.getAttribute&&(e.getAttribute('aria-label')||'')||'')
              +' '+(e.title||'')).trim();
        if(t&&t.length<40&&re.test(t)){ if(clk(e)) return 'ctrl:'+t.slice(0,24); }}
      // 2) buttons near a masked email (•••• / locked) in a person row
      var rows=[].slice.call(document.querySelectorAll('tr,[role=row],[class*=zp_]'));
      for(var r=0;r<rows.length;r++){var rt=rows[r].innerText||'';
        if(/access email|•|•|locked|\*\*\*/i.test(rt)){
          var b=rows[r].querySelector('button,[role=button]');
          if(b&&clk(b)) return 'row-btn';}}
      return '';})()"""
    clicked = cdp.eval_js(tab, _click_js, timeout=6.0)[1]
    # if nothing clicked, open the first person drawer then retry the reveal control
    if not clicked:
        cdp.eval_js(tab, """(function(){var a=document.querySelector(
          'a[href*="/people/"], [class*=zp_] a, tr a'); if(a){try{a.click();return 1;}catch(e){}}return 0;})()""",
          timeout=5.0)
        time.sleep(3.0)
        clicked = cdp.eval_js(tab, _click_js, timeout=6.0)[1]
    out["clicked"] = clicked or ""
    time.sleep(7.0)
    th.join(timeout=34)

    # persist the raw capture for inspection
    try:
        raw_path = str(data_dir("email-finder") / "apollo_capture_raw.json")
        with open(raw_path, "w") as fh:
            json.dump(records, fh, indent=2)
        out["raw_path"] = raw_path
    except Exception:  # noqa: BLE001
        pass

    # Identify the reveal request: a POST whose response body contains an email address.
    _EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    reveal_rec = None
    for r in records:
        body = r.get("body") or ""
        if r.get("method") in ("POST", "PUT") and _EMAIL.search(body) \
                and not any(n in body[:200] for n in ("\"errors\"", "captcha")):
            reveal_rec = r
            break
    if not reveal_rec:
        # fallback: any apollo api POST whose URL hints at reveal/match
        for r in records:
            if r.get("method") == "POST" and re.search(r"reveal|email|people/match|mixed_people",
                                                        r.get("url", ""), re.I):
                reveal_rec = r
                break
    if not reveal_rec:
        out["degraded"].append("capture:no-reveal-request-seen — open the People view / ensure an "
                               "unrevealed contact exists, then retry")
        out["captured_count"] = len(records)
        return out

    template = {
        "url": reveal_rec["url"],
        "method": reveal_rec.get("method", "POST"),
        "headers": {k: v for k, v in (reveal_rec.get("headers") or {}).items()
                    if k.lower() in ("content-type", "x-csrf-token", "x-requested-with", "accept")},
        "postData": reveal_rec.get("postData", ""),
        "captured_at": _now_iso(),
    }
    _save_template(template)
    out.update(ok=True, endpoint=template["url"], method=template["method"],
               has_payload=bool(template["postData"]),
               identifier_hint=_guess_identifier(template["postData"]))
    return out


def capture_reveal_manual(seconds: float = 70.0) -> dict:
    """Capture the FREE extension reveal call while the USER does one manual 'Access email' click on
    a LinkedIn profile in the debug Chrome. Records ALL Apollo-related targets concurrently — the
    extension's reveal goes through its SERVICE WORKER, not the page — then identifies + persists the
    reveal endpoint/payload as the replay template. Returns {ok, endpoint, identifier_hint, ...}."""
    out: dict[str, Any] = {"ok": False, "degraded": [], "instructions":
        "Open a LinkedIn profile in the debug Chrome and click Apollo's 'Access email' ONCE now."}
    okr, why = cdp.ensure_running()
    if not okr:
        out["degraded"].append(f"cdp:unreachable:{why}")
        return out

    # Record every Apollo-touching target: app.apollo.io page, side panel (assets.apollo.io),
    # and the extension service worker (where the reveal XHR actually originates).
    targets = [t for t in cdp.targets()
               if t.get("webSocketDebuggerUrl")
               and any(h in ((t.get("url") or "") + " " + (t.get("title") or "")).lower()
                       for h in ("apollo.io", _APOLLO_EXT_ID, "apollo"))]
    if not targets:
        out["degraded"].append("no-apollo-targets — is the extension installed / a tab open?")
        return out

    all_records: list = []
    lock = threading.Lock()

    def _rec(tg):
        recs = cdp.record_network(tg, duration_s=seconds, url_filter="apollo")
        with lock:
            all_records.extend(recs)

    threads = [threading.Thread(target=_rec, args=(t,), daemon=True) for t in targets]
    for th in threads:
        th.start()
    out["recording_targets"] = len(targets)
    for th in threads:
        th.join(timeout=seconds + 8)

    try:
        raw_path = str(data_dir("email-finder") / "apollo_capture_raw.json")
        with open(raw_path, "w") as fh:
            json.dump(all_records, fh, indent=2)
        out["raw_path"] = raw_path
    except Exception:  # noqa: BLE001
        pass

    template = _identify_reveal(all_records)
    out["captured_count"] = len(all_records)
    if not template:
        out["degraded"].append("no-reveal-request-seen — did the manual 'Access email' click fire? retry")
        out["seen_endpoints"] = sorted({(r.get("method") or "") + " " +
                                        (r.get("url") or "").split("apollo.io")[-1][:60]
                                        for r in all_records if r.get("method") in ("POST", "PUT")})[:20]
        return out
    _save_template(template)
    out.update(ok=True, endpoint=template["url"], method=template["method"],
               identifier_hint=_guess_identifier(template["postData"]))
    return out


def _identify_reveal(records: list) -> dict | None:
    """Pick the reveal request: a POST/PUT whose RESPONSE body contains a real (non-masked) email
    and whose URL looks like a people/reveal endpoint (not the user's own profile)."""
    _EMAIL = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    _SKIP = ("users/current", "paging_events", "experiments", "promotions", "assistant_threads",
             "amp-outbound", "team_plans", "increment_page_visit")
    cands = []
    for r in records:
        url = r.get("url", "")
        if r.get("method") not in ("POST", "PUT", "GET"):
            continue
        if any(s in url for s in _SKIP):
            continue
        body = r.get("body") or ""
        emails = [e for e in _EMAIL.findall(body)
                  if "not_unlocked" not in e and "email_not" not in e and "apollo.io" not in e]
        if emails and re.search(r"people|prospect|email|reveal|match|contact", url, re.I):
            cands.append((r, len(emails)))
    if not cands:
        return None
    # prefer the most email-rich response (the reveal payload)
    rec = max(cands, key=lambda c: c[1])[0]
    return {
        "url": rec["url"],
        "method": rec.get("method", "POST"),
        "headers": {k: v for k, v in (rec.get("headers") or {}).items()
                    if k.lower() in ("content-type", "x-csrf-token", "x-requested-with", "accept",
                                     "x-api-version")},
        "postData": rec.get("postData", ""),
        "captured_at": _now_iso(),
    }


def _now_iso() -> str:
    # avoid importing datetime at module top; stamp lightly
    import datetime as _dt
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


# The captured FREE reveal call (extension.apollo.io, authed by the extension's own session). We
# replay it from the extension SERVICE-WORKER context so its cookies/auth apply. The `url` field is
# just the target LinkedIn profile URL.
_REVEAL_ENDPOINT = "https://extension.apollo.io/api/v1/linkedin_chrome_extension/parse_profile_page"
_REVEAL_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _apollo_service_worker() -> dict | None:
    """The Apollo extension's service-worker target — its fetch() carries the extension.apollo.io
    session, so a reveal POST from here is authenticated exactly like the extension's own call."""
    for t in cdp.targets():
        u = (t.get("url") or "").lower()
        ttype = (t.get("type") or "").lower()
        if _APOLLO_EXT_ID in u and ("worker" in ttype or "service" in ttype or "background" in u
                                    or "serviceworker" in u):
            if t.get("webSocketDebuggerUrl"):
                return t
    # fallback: any target on the apollo ext id with a ws url
    for t in cdp.targets():
        if _APOLLO_EXT_ID in (t.get("url") or "").lower() and t.get("webSocketDebuggerUrl"):
            return t
    return None


def _reveal_via_apollo_backend(name: str = "", company: str = "", domain: str = "",
                               linkedin_url: str = "") -> dict:
    """PRIMARY reveal: replay the extension's authenticated reveal call in the BACKGROUND (no panel,
    no window) by running an in-context fetch in the Apollo service worker. Returns
    {email, emails, source, ms, credit_used, degraded}. Never raises, never fabricates.

    Serialized on `_CDP_LOCK`: the in-context fetch runs over the shared service-worker eval_js
    socket (global _MSG_ID), so two concurrent callers must not interleave on it."""
    with _CDP_LOCK:
        return _reveal_via_apollo_backend_impl(name=name, company=company, domain=domain,
                                               linkedin_url=linkedin_url)


def _reveal_via_apollo_backend_impl(name: str = "", company: str = "", domain: str = "",
                                    linkedin_url: str = "") -> dict:
    t0 = time.monotonic()
    res: dict[str, Any] = {"email": None, "emails": [], "source": "apollo-backend",
                           "ms": 0, "credit_used": False, "degraded": []}
    if not linkedin_url:
        res["degraded"].append("backend:no-linkedin-url")
        return res
    okr, why = cdp.ensure_running()
    if not okr:
        res["degraded"].append(f"cdp:unreachable:{why}")
        return res
    cdp.hide_app()
    sw = _apollo_service_worker()
    if not sw:
        res["degraded"].append("backend:no-service-worker — Apollo extension not loaded in debug Chrome")
        return res

    li = linkedin_url.split("?")[0]
    # authenticated, same-context fetch (credentials included → extension.apollo.io session applies)
    js = (
        "(async function(){try{"
        "var r=await fetch(" + json.dumps(_REVEAL_ENDPOINT) + ",{method:'POST',credentials:'include',"
        "headers:{'Content-Type':'application/json','X-Accept-Language':'en','client-origin':'linkedin'},"
        "body:JSON.stringify({html:'',language_code:'en',url:" + json.dumps(li) + ",url_parsing:true,"
        "cacheKey:Date.now()})});"
        "var t=await r.text();return JSON.stringify({status:r.status,body:t.slice(0,12000)});"
        "}catch(e){return JSON.stringify({status:0,error:String(e)});}})()"
    )
    ok, val = cdp.eval_js(sw, js, timeout=25.0, await_promise=True)
    res["ms"] = int((time.monotonic() - t0) * 1000)
    if not ok:
        res["degraded"].append(f"backend:fetch-failed:{str(val)[:40]}")
        return res
    try:
        payload = json.loads(val) if isinstance(val, str) else (val or {})
        body = payload.get("body", "") or ""
        status = payload.get("status")
    except Exception:  # noqa: BLE001
        res["degraded"].append("backend:bad-response")
        return res
    if status and status >= 400:
        res["degraded"].append(f"backend:http-{status}")
    # Pull the person's org + name from the contact JSON (authoritative — used to verify we got the
    # RIGHT person at the target company, disambiguating which discovered profile to trust).
    try:
        cobj = (json.loads(body) or {}).get("contact", {}) if body.strip().startswith("{") else {}
    except Exception:  # noqa: BLE001
        cobj = {}
    if isinstance(cobj, dict):
        res["org"] = cobj.get("organization_name") or ""
        res["person_name"] = (cobj.get("name")
                              or " ".join(x for x in [cobj.get("first_name"), cobj.get("last_name")] if x))
        res["title"] = cobj.get("title") or ""
    # extract emails from the contact JSON, drop apollo/noise + masked placeholders
    dom = (domain or "").lower().lstrip("@")
    found: list[str] = []
    for e in _REVEAL_EMAIL_RE.findall(body):
        el = e.lower()
        if el in found or is_role(el):
            continue
        if any(n in el for n in ("apollo.io", "sentry", "example.com", "not_unlocked", "email_not")):
            continue
        found.append(el)
    found.sort(key=lambda e: (0 if dom and e.endswith(dom) else 1))
    if found:
        res["emails"] = found
        res["email"] = found[0]
        res["credit_used"] = True
        QUOTA.record_pool_use("apollo", (_BY_NAME.get("apollo", {}) or {}).get("cap"))
    else:
        res["degraded"].append("backend:no-email-in-response")
    return res


def _norm(s: str) -> str:
    return re.sub(r"[^a-z0-9]+", "", (s or "").lower())


# role → Apollo person_titles filter (broad so we catch the actual title variant)
_ROLE_TITLES = {
    "ceo": ["ceo", "chief executive officer", "founder", "co-founder", "owner"],
    "cto": ["cto", "chief technology officer", "co-founder", "vp engineering", "head of engineering"],
    "cfo": ["cfo", "chief financial officer", "vp finance"],
    "coo": ["coo", "chief operating officer"],
    "cmo": ["cmo", "chief marketing officer", "vp marketing", "head of marketing"],
    "founder": ["founder", "co-founder", "ceo", "owner"],
    "head of sales": ["head of sales", "vp sales", "sales director", "chief revenue officer", "cro"],
}


def _role_titles(role: str) -> list[str]:
    r = (role or "ceo").strip().lower()
    if r in _ROLE_TITLES:
        return _ROLE_TITLES[r]
    # generic: the role itself + common C-suite/founder anchors
    return list(dict.fromkeys([r, "founder", "co-founder", "ceo"]))


def _org_matches(company: str, org: str) -> bool:
    """True if Apollo's returned org name plausibly matches the requested company — a guard on the
    fuzzy-name fallback so a loose match can't bind to an unrelated org."""
    a, b = _norm(company), _norm(org)
    if not a or not b:
        return False
    return a in b or b in a


def _apollo_people_query(tab: dict, body: dict) -> tuple[bool, Any]:
    """Run one authenticated mixed_people/search from the app.apollo.io tab; return (ok, parsed)."""
    js = (
        "(async function(){try{"
        "var csrf='';var m=document.querySelector('meta[name=csrf-token]');if(m)csrf=m.content;"
        "if(!csrf){var c=document.cookie.match(/X-CSRF-TOKEN=([^;]+)/i);if(c)csrf=decodeURIComponent(c[1]);}"
        "var r=await fetch('/api/v1/mixed_people/search',{method:'POST',credentials:'include',"
        "headers:{'Content-Type':'application/json','X-CSRF-TOKEN':csrf},body:" + json.dumps(json.dumps(body)) + "});"
        "var j=await r.json();"
        "var out=(j.contacts||j.people||[]).map(function(c){return {name:c.name,title:c.title,"
        "linkedin_url:c.linkedin_url,email:c.email,email_status:c.email_status,"
        "org:c.organization_name,org_domain:(c.organization||{}).primary_domain};});"
        "return JSON.stringify({status:r.status,people:out});"
        "}catch(e){return JSON.stringify({status:0,error:String(e)});}})()"
    )
    ok, val = cdp.eval_js(tab, js, timeout=20.0, await_promise=True)
    if not ok:
        return False, str(val)[:60]
    try:
        return True, (json.loads(val) if isinstance(val, str) else (val or {}))
    except Exception:  # noqa: BLE001
        return False, "bad-json"


def _clean_people(raw: list) -> list:
    """Keep named rows; null out masked/placeholder emails so callers know to reveal."""
    people = [p for p in (raw or []) if p.get("name")]
    for p in people:
        e = (p.get("email") or "")
        if "not_unlocked" in e or "email_not" in e or "@" not in e:
            p["email"] = None
    return people


_CREDIT_CACHE: dict = {"ts": 0.0, "data": None}


def apollo_credit_status(force: bool = False) -> dict:
    """Read the team's remaining Apollo reveal credits — FREE, spends nothing (the same
    credit_usage_summary call the app makes). Reads team_id from the page's localStorage, then fetches
    `/api/v1/teams/{id}/credit_usage_summary`. Cached ~60s. Returns
    {ok, total, used, remaining, pct_used, exhausted, team_id, degraded}.

    Used to WARN before credits run out and to route around the credit-spending reveal — the FREE
    apollo_people_search identity + the finder/verify backbone keep working at 0 credits."""
    if not force and _CREDIT_CACHE["data"] and (time.monotonic() - _CREDIT_CACHE["ts"]) < 60:
        return _CREDIT_CACHE["data"]
    out: dict[str, Any] = {"ok": False, "total": None, "used": None, "remaining": None,
                           "pct_used": None, "exhausted": None, "team_id": None, "degraded": []}
    okr, why = cdp.ensure_running()
    if not okr:
        out["degraded"].append(f"cdp:unreachable:{why}")
        return out
    cdp.hide_app()
    tab = _apollo_app_tab()
    if not tab or not _apollo_logged_in(tab):
        out["degraded"].append("apollo:not-logged-in")
        return out
    js = (
        "(async function(){try{"
        "var tid=localStorage.getItem('team_id');"
        "if(!tid){for(var i=0;i<localStorage.length;i++){var k=localStorage.key(i);"
        "if(/team_id/i.test(k)){tid=localStorage.getItem(k);break;}}}"
        "tid=(tid||'').replace(/[^0-9a-f]/gi,'');"   # localStorage stores it JSON-quoted -> strip
        "if(!tid)return JSON.stringify({error:'no-team-id'});"
        "var r=await fetch('/api/v1/teams/'+tid+'/credit_usage_summary',"
        "{credentials:'include',headers:{'Accept':'application/json'}});"
        "var j=await r.json();var t=j.team||{};"
        "return JSON.stringify({status:r.status,team_id:tid,"
        "total:t.effective_num_lead_credits,used:t.total_unified_credits_used});"
        "}catch(e){return JSON.stringify({error:String(e)});}})()"
    )
    with _CDP_LOCK:
        ok, val = cdp.eval_js(tab, js, timeout=15.0, await_promise=True)
    if not ok:
        out["degraded"].append(f"credit:eval-failed:{str(val)[:40]}")
        return out
    try:
        d = json.loads(val) if isinstance(val, str) else (val or {})
    except Exception:  # noqa: BLE001
        out["degraded"].append("credit:bad-json")
        return out
    if d.get("error"):
        out["degraded"].append(f"credit:{str(d['error'])[:50]}")
        return out
    total, used = d.get("total"), d.get("used")
    if isinstance(total, (int, float)) and isinstance(used, (int, float)):
        rem = max(0, int(total) - int(used))
        out.update(ok=True, total=int(total), used=int(used), remaining=rem,
                   pct_used=round(100.0 * used / total, 1) if total else None,
                   exhausted=(rem <= 0), team_id=d.get("team_id"))
        _CREDIT_CACHE.update(ts=time.monotonic(), data=out)
    else:
        out["degraded"].append("credit:unparsed-summary")
    return out


def apollo_reveal_credits_left() -> int | None:
    """Best-effort remaining reveal credits (None when unknown). Cheap (cached) — used to skip a
    doomed reveal so the pipeline falls straight to the free finder/verify backbone."""
    try:
        s = apollo_credit_status()
        return s.get("remaining") if s.get("ok") else None
    except Exception:  # noqa: BLE001
        return None


def apollo_people_search(domain: str, role: str = "CEO", company: str = "",
                         per_page: int = 5) -> dict:
    """Authoritative discovery via Apollo's OWN people DB. Cascade: (1) filter by company `domain`
    + role titles; (2) if empty and a distinctive `company` name is given, retry by Apollo's fuzzy
    company name — self-corrects a wrong domain guess and yields the org's canonical primary_domain.
    Each person: {name,title,linkedin_url,email,email_status,org,org_domain}. Authenticated
    same-origin fetch in the logged-in app.apollo.io tab. No Bing.

    Cached per (company|domain, role) for ~1h and serialized on the shared CDP tab via `_CDP_LOCK`,
    so repeats/bulk are instant and concurrent callers never collide.

    Returns {ok, people:[...], matched_by, canonical_domain, degraded}."""
    ck = f"{_norm(company)}|{(domain or '').strip().lower().lstrip('@')}|{(role or '').strip().lower()}"
    hit = _PEOPLE_CACHE.get(ck)
    if hit and (time.time() - hit[0]) < _PEOPLE_TTL:
        return {**hit[1], "cached": True}
    with _CDP_LOCK:
        # re-check inside the lock — a concurrent caller may have just populated it
        hit = _PEOPLE_CACHE.get(ck)
        if hit and (time.time() - hit[0]) < _PEOPLE_TTL:
            return {**hit[1], "cached": True}
        res = _apollo_people_search_impl(domain, role=role, company=company, per_page=per_page)
        if res.get("ok"):  # cache only positive results; misses stay cheap to retry
            _PEOPLE_CACHE[ck] = (time.time(), res)
            _save_sidecar("apollo_people_cache.json", _PEOPLE_CACHE)
        return res


def _apollo_people_search_impl(domain: str, role: str = "CEO", company: str = "",
                               per_page: int = 5) -> dict:
    """Uncached body of apollo_people_search (see wrapper). Runs the domain→fuzzy cascade."""
    out: dict[str, Any] = {"ok": False, "people": [], "degraded": [],
                           "matched_by": None, "canonical_domain": None}
    dom = (domain or "").strip().lower().lstrip("@")
    okr, why = cdp.ensure_running()
    if not okr:
        out["degraded"].append(f"cdp:unreachable:{why}")
        return out
    cdp.hide_app()
    tab = _apollo_app_tab()
    if not tab or not _apollo_logged_in(tab):
        out["degraded"].append("apollo:not-logged-in")
        return out
    titles = _role_titles(role)
    pp = max(1, min(int(per_page), 10))
    base = {"page": 1, "per_page": pp, "person_titles": titles,
            "display_mode": "explorer_mode", "finder_version": 2}

    cname = (company or "").strip()

    # (1) by domain(s) — query the given domain PLUS the company's TLD variants in ONE request, so a
    # wrong `.com` guess still matches the real `.ai`/`.io`. Apollo accepts a domain LIST. When a
    # company name is given, org-filter the results so a candidate domain owned by an unrelated org
    # can't bind. Far more reliable than depending on Apollo's flaky fuzzy-name search.
    dom_list: list[str] = [dom] if dom else []
    if cname:
        try:
            from .dns_resolve import _domain_candidates
            dom_list += _domain_candidates(cname)
        except Exception:  # noqa: BLE001
            pass
    dom_list = list(dict.fromkeys([d for d in dom_list if d]))[:10]
    if dom_list:
        ok, data = _apollo_people_query(tab, {**base, "q_organization_domains_list": dom_list})
        if not ok:
            out["degraded"].append(f"search:{data}")
        elif isinstance(data, dict) and data.get("error"):
            out["degraded"].append(f"search:{str(data['error'])[:50]}")
        else:
            people = _clean_people(data.get("people"))
            if cname:  # drop any org that doesn't resemble the requested company
                people = [p for p in people if _org_matches(cname, p.get("org"))]
            if people:
                cd = next((p.get("org_domain") for p in people if p.get("org_domain")), None)
                out.update(ok=True, people=people, matched_by="domain",
                           canonical_domain=(cd if cd and cd != dom else None))
                return out

    # (2) fuzzy company name — last resort for names whose domain we couldn't guess (e.g. "Jupid
    # Tax" → jupid.com). Org-filtered to avoid binding to an unrelated fuzzy match.
    if cname:
        ok, data = _apollo_people_query(tab, {**base, "q_organization_fuzzy_name": cname})
        if ok and isinstance(data, dict) and not data.get("error"):
            people = [p for p in _clean_people(data.get("people"))
                      if _org_matches(cname, p.get("org"))]
            if people:
                out.update(ok=True, people=people, matched_by="fuzzy_name",
                           canonical_domain=(people[0].get("org_domain") or None))
                return out

    if not dom and not cname:
        out["degraded"].append("no-domain-or-company")
    elif not out["degraded"]:
        out["degraded"].append("search:no-people")
    return out


def reveal_best_of(candidates: list[str], name: str = "", company: str = "",
                   domain: str = "") -> dict:
    """Background-reveal up to ~5 candidate LinkedIn profiles and return the BEST-matching person —
    the one whose revealed email domain matches the company OR whose Apollo `organization` matches
    the company name. This self-corrects when discovery's top link is the wrong person. Each reveal
    is the ~0.8s authenticated backend call; stops early on a strong (org+domain) match.

    Returns {email, emails, org, person_name, linkedin_url, source, match, tried, degraded}."""
    out: dict[str, Any] = {"email": None, "emails": [], "org": None, "person_name": None,
                           "linkedin_url": None, "source": "apollo-backend", "match": None,
                           "tried": [], "degraded": []}
    dom = (domain or "").lower().lstrip("@")
    comp = _norm(company)
    best = None  # (score, result, url)
    for cu in [c for c in (candidates or []) if c][:5]:
        r = _reveal_via_apollo_backend(name=name, company=company, domain=domain, linkedin_url=cu)
        em = (r.get("email") or "")
        org = _norm(r.get("org") or "")
        out["tried"].append({"linkedin_url": cu, "email": em or None, "org": r.get("org")})
        if not em:
            out["degraded"].extend(r.get("degraded", []))
            continue
        edom = em.split("@", 1)[-1].lower()
        dom_match = bool(dom) and (edom == dom or edom.split(".")[0] == dom.split(".")[0])
        org_match = bool(comp) and bool(org) and (comp in org or org in comp)
        score = (2 if dom_match else 0) + (2 if org_match else 0) + 1  # +1: has an email at all
        if best is None or score > best[0]:
            best = (score, r, cu)
        if dom_match and org_match:        # strong, unambiguous → stop early
            break
    if best:
        score, r, cu = best
        out.update(email=r.get("email"), emails=r.get("emails", []), org=r.get("org"),
                   person_name=r.get("person_name"), title=r.get("title"), linkedin_url=cu,
                   credit_used=r.get("credit_used", False),
                   match=("org+domain" if score >= 5 else "domain" if score >= 3 else
                          "org" if (_norm(r.get("org") or "") and comp and
                                    (comp in _norm(r.get("org") or ""))) else "weak"))
    return out


def _guess_identifier(post_data: str) -> str:
    """Heuristic: which payload field carries the person identifier (linkedin url / id / name)."""
    low = (post_data or "").lower()
    for k in ("linkedin_url", "linkedin", "person_id", "id", "name", "first_name", "domain"):
        if f'"{k}"' in low or f"{k}=" in low:
            return k
    return "unknown"


def reveal(name: str = "", company: str = "", domain: str = "",
           linkedin_url: str = "", debug: bool = False,
           avoid_last: str | None = None) -> dict:
    """Reveal an email in the background by driving the live reveal-extension side panel over CDP.

    Returns {email, emails, extension, source, ms, credit_used, degraded, trace}. Degrades cleanly
    to a quoted reason; never raises and never fabricates. `debug=True` keeps the full step trace;
    `avoid_last` is the previous person's last name (per-window stale avoidance in parallel runs).

    Serialized on `_CDP_LOCK`: the side-panel path mutates the shared tab + `_LAST_PANEL_PERSON`,
    so concurrent callers must not interleave.
    """
    with _CDP_LOCK:
        return _reveal_impl(name=name, company=company, domain=domain, linkedin_url=linkedin_url,
                            debug=debug, avoid_last=avoid_last)


def _reveal_impl(name: str = "", company: str = "", domain: str = "",
                 linkedin_url: str = "", debug: bool = False,
                 avoid_last: str | None = None) -> dict:
    t0 = time.monotonic()
    trace: list = []
    result: dict[str, Any] = {"email": None, "emails": [], "extension": None,
                              "source": "apollo-cdp", "ms": 0, "credit_used": False,
                              "degraded": [], "trace": trace}

    # Auto-start the persistent debug profile if it's set up but not running (no clone, no login).
    ok, why = cdp.ensure_running()
    if not ok:
        result["degraded"].append(f"cdp:unreachable:{why}")
        result["setup"] = cdp.launch_command()
        result["ms"] = int((time.monotonic() - t0) * 1000)
        return result
    cdp.hide_app()   # keep the debug Chrome invisible — no visible tab switching, ever

    # PRIMARY PATH (reliable, fully background, ~1s): replay the extension's own authenticated reveal
    # call in its service-worker context. No side panel, no DOM, no window — works every time.
    if linkedin_url:
        be = _reveal_via_apollo_backend(name=name, company=company, domain=domain,
                                        linkedin_url=linkedin_url)
        if be.get("email"):
            result.update(email=be["email"], emails=be["emails"], extension="apollo",
                          source="apollo-backend", credit_used=be.get("credit_used", False),
                          ms=int((time.monotonic() - t0) * 1000))
            if not debug:
                result.pop("trace", None)
            return result
        result["degraded"].extend(be.get("degraded", []))  # fall through to side panel / keyless

    # FALLBACK PATH — the side-panel reveal (only works if the panel is open). Get a top-level
    # LinkedIn page tab on THIS profile (reuse/repurpose/create + poll until navigation commits).
    li = _ensure_li_tab(linkedin_url)
    if li:
        # If a panel is already open it auto-updates to the new profile — skip the opener wait
        # (warm path, near-instant for bulk). Only wait for the opener when no panel exists yet.
        if not _find_panel("apollo"):
            deadline = time.monotonic() + _OPENER_WAIT_S
            while time.monotonic() < deadline:
                ok, has = cdp.eval_js(
                    li, "!!document.querySelector('.apollo-opener-icon, input.apollo-opener-icon')",
                    timeout=4.0)
                if ok and has:
                    break
                time.sleep(0.4)
    else:
        result["degraded"].append("cdp:no-linkedin-tab — open a LinkedIn profile in the debug Chrome")

    # Rotate across the installed pool by remaining free credit (Apollo first).
    member = QUOTA.pool_pick(_POOL_MEMBERS)
    if not member:
        result["degraded"].append("pool:all-credits-exhausted")
        result["ms"] = int((time.monotonic() - t0) * 1000)
        return result

    tried: list[str] = []
    emails: list[str] = []
    # Only probe extensions that are actually INSTALLED (a service-worker/panel target exists for
    # them) — otherwise we'd waste ~12s each panel-syncing 7 uninstalled extensions. Keeps reveal fast.
    present = _present_members()
    order = [member] + [m for m, _ in _POOL_MEMBERS if m != member]
    order = [m for m in order if m in present] or ([member] if member in present else [])
    if not order:
        result["degraded"].append(
            "extensions:none-installed — install Apollo (or ContactOut/Lusha) in the debug Chrome")
        result["ms"] = int((time.monotonic() - t0) * 1000)
        return result
    # Match the panel to this person: caller's name → the profile tab's real name (handles
    # single-token slugs) → a name derived from the slug. The real name is the strongest signal.
    eff_name = name or _profile_name_from_tab(li) or _name_from_slug(linkedin_url)
    for ext_name in order:
        cap = (_BY_NAME.get(ext_name, {}) or {}).get("cap")
        if QUOTA.pool_pick([(ext_name, cap or 0)]) != ext_name:
            # this source is out of free credits → rotate to the next installed extension
            result["degraded"].append(f"{ext_name}:no-credit-rotated")
            continue
        tried.append(ext_name)
        emails, degraded, clicked, panel_found = _reveal_with(
            ext_name, domain, li, want_slug=_li_slug(linkedin_url), want_name=eff_name,
            trace=trace, avoid_last=avoid_last)
        result["degraded"].extend(degraded)
        if emails:
            result["extension"] = ext_name
            # Remember who we just read so the NEXT sequential reveal waits past this person
            # (avoids reading a stale panel before Apollo swaps to the new profile).
            global _LAST_PANEL_PERSON
            _LAST_PANEL_PERSON = _last_name(eff_name)
            # Count a credit only on a fresh reveal (we clicked Access), not a cached read.
            if clicked:
                QUOTA.record_pool_use(ext_name, cap)
                result["credit_used"] = True
            break
        # The installed extension's panel was handled but had no email → don't waste time probing
        # other (uninstalled) extensions. Only keep rotating if this one isn't installed/open.
        if panel_found:
            break

    result["emails"] = emails
    result["email"] = emails[0] if emails else None
    result["tried"] = tried
    result["ms"] = int((time.monotonic() - t0) * 1000)
    if emails:
        result["synced_via"] = trace[-1]["strategy"] if trace else None
    if not debug:
        result.pop("trace", None)  # keep the result lean unless debugging
    return result
