"""AI agentic browsing for email discovery — observe → think → act over the REAL Chrome.

The user's "see the web, not normal search" layer. Instead of brittle scraping, this drives the
user's already-logged-in Chrome (via the `chrome` connector primitives in `mcp_base.chrome`):
open a contact/team/profile page, read the rendered DOM, click "show email"/"reveal"/expand
controls, paginate, and read the result — then hand every surfaced address to email_extract +
verify (never fabricated).

Design:
- Zero hard deps. If Chrome isn't running / JS-from-Apple-Events is off, it returns cleanly with
  a `degraded` note — never raises, never blocks the finder's free backbone.
- The inner "think" step is heuristic by default (find a reveal/expand control whose text matches
  known patterns, click it, re-observe). If a local Ollama is present it can optionally pick the
  next action, but the heuristic path needs no model.
- `deep_research()` is the never-give-up F6 fallback: shell out to a detached `claude -p` running
  the deep-research skill when every other layer is exhausted. Guarded; absent → skipped.

Main entry:
    browse_for_email(name, company="", domain="", role="") -> {emails, sources, note, degraded}
"""
from __future__ import annotations

import re
import time
from typing import Any

from . import chrome
from .email_extract import deobfuscate_text, extract_emails, is_role

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Visible-text patterns for controls that reveal/expand a hidden email.
_REVEAL_PATTERNS = (
    "show email", "show e-mail", "reveal email", "reveal e-mail", "view email",
    "get email", "see email", "show contact", "reveal contact", "view contact",
    "show more", "view profile", "contact", "email", "get contact",
)

_TOS_NOTE = ("Drove your real logged-in Chrome (human-in-the-loop). Public pages only; "
             "no bulk automation. Every address still passes extraction + verification.")

# How long to let a page settle before reading the DOM.
_SETTLE = 1.2
_MAX_PAGES = 6


def _available() -> tuple[bool, str]:
    """Is the real-Chrome observe→act path usable right now?"""
    if not chrome.chrome_running():
        return False, "chrome:not-running"
    ok, _ = chrome.js_enabled()
    if not ok:
        return False, "chrome:js-from-apple-events-off"
    return True, ""


def _read_dom(url_substr: str) -> str:
    """Observe: return the rendered page's visible text + a slice of HTML (for mailto/cfemail)."""
    js = (
        "(function(){try{"
        "var t=document.body?document.body.innerText:'';"
        "var h=document.documentElement?document.documentElement.outerHTML:'';"
        "return JSON.stringify({text:t.slice(0,40000), html:h.slice(0,200000)});"
        "}catch(e){return JSON.stringify({text:'',html:''});}})()"
    )
    ok, val = chrome.run_js(url_substr, js)
    if not ok:
        return ""
    try:
        import json as _json
        data = _json.loads(val) if isinstance(val, str) else (val or {})
        return f"{data.get('text','')}\n{data.get('html','')}"
    except Exception:
        return str(val or "")


def _click_reveal(url_substr: str) -> bool:
    """Act: find the first clickable control whose text matches a reveal pattern and click it.
    Returns True if something was clicked (so the caller re-observes)."""
    pats = "|".join(re.escape(p) for p in _REVEAL_PATTERNS)
    js = (
        "(function(){"
        "var re=new RegExp('(" + pats + ")','i');"
        "var els=[].slice.call(document.querySelectorAll('button,a,[role=button],span,div'));"
        "for(var i=0;i<els.length;i++){var e=els[i];var tx=(e.innerText||e.textContent||'').trim();"
        "if(tx && tx.length<40 && re.test(tx)){try{e.click();return true;}catch(_){}}}"
        "return false;})()"
    )
    ok, val = chrome.run_js(url_substr, js)
    return bool(ok and val in (True, "true", 1))


def _emails_from_blob(blob: str, domain: str, first: str, last: str) -> dict[str, int]:
    """Extract + deobfuscate emails from a DOM blob, keeping name/domain matches."""
    found: dict[str, int] = {}
    if not blob:
        return found
    for em, w in extract_emails(blob, domain_filter=None).items():
        found[em] = max(found.get(em, 0), w)
    for em in deobfuscate_text(blob):
        found[em.lower()] = max(found.get(em.lower(), 0), 1)
    out: dict[str, int] = {}
    dom = (domain or "").lower().lstrip("@")
    for em, w in found.items():
        local, _, edom = em.partition("@")
        on_domain = bool(dom) and edom.endswith(dom)
        name_match = (first and first in local) or (last and last in local)
        if on_domain or name_match or not dom:
            out[em] = w + (3 if on_domain else 0) + (2 if name_match else 0)
    return out


def _split_name(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", (name or "").strip()) if p]
    if not parts:
        return "", ""
    first = re.sub(r"[^a-z]", "", parts[0].lower())
    last = re.sub(r"[^a-z]", "", parts[-1].lower()) if len(parts) > 1 else ""
    return first, last


def browse_for_email(name: str, company: str = "", domain: str = "",
                     role: str = "", max_pages: int = _MAX_PAGES) -> dict:
    """Observe→think→act over the real Chrome to surface a person's email.

    Returns {emails: [str,...], sources: [url,...], note: str, degraded: [str,...]}.
    Always safe: returns empty emails + a degraded note if Chrome/automation is unavailable.
    """
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    result: dict[str, Any] = {"emails": [], "sources": [], "note": _TOS_NOTE, "degraded": []}

    ok, why = _available()
    if not ok:
        result["degraded"].append(why)
        return result

    # Build the page work-list: company contact/team pages + a few SERP hits for the person.
    urls: list[str] = []
    try:
        from .harvest import discover_contact_pages
        if dom:
            urls.extend(discover_contact_pages(dom)[:4])
    except Exception:
        pass
    try:
        from .websearch import search_links
        q = f'"{name}" {company or dom} email contact'.strip()
        urls.extend(search_links(q, n=4))
    except Exception as e:  # noqa: BLE001
        result["degraded"].append(f"search:{str(e)[:30]}")

    seen_urls: list[str] = []
    for u in urls:
        if u and u not in seen_urls:
            seen_urls.append(u)

    collected: dict[str, int] = {}
    for url in seen_urls[:max_pages]:
        try:
            chrome.open_tab(url)
            time.sleep(_SETTLE)
            # observe
            blob = _read_dom(url)
            for em, w in _emails_from_blob(blob, dom, first, last).items():
                collected[em] = max(collected.get(em, 0), w)
            # think→act: try one reveal click, then re-observe
            if _click_reveal(url):
                time.sleep(_SETTLE)
                blob2 = _read_dom(url)
                for em, w in _emails_from_blob(blob2, dom, first, last).items():
                    collected[em] = max(collected.get(em, 0), w + 1)
            result["sources"].append(url)
        except Exception as e:  # noqa: BLE001
            result["degraded"].append(f"page:{str(e)[:30]}")
        finally:
            try:
                chrome.close_tab(url)
            except Exception:
                pass

    ranked = sorted(collected.items(), key=lambda kv: -kv[1])
    result["emails"] = [em for em, _ in ranked if not is_role(em)]
    return result


# ---------------------------------------------------------------------------
# F6 — never-give-up deep research via a detached `claude -p` agent
# ---------------------------------------------------------------------------

def deep_research(name: str, company: str = "", domain: str = "",
                  timeout: float = 240.0) -> dict:
    """Last-resort OSINT: run a detached `claude -p` deep-research pass that creatively hunts the
    address across the open web and prints any emails it finds. Returns {emails, raw, degraded}.
    Guarded: if the `claude` CLI is absent or errors, returns empty + a degraded note."""
    import shutil
    import subprocess

    out: dict[str, Any] = {"emails": [], "raw": "", "degraded": []}
    if not shutil.which("claude"):
        out["degraded"].append("claude-cli:absent")
        return out

    target = f"{name}" + (f" at {company}" if company else "") + (f" ({domain})" if domain else "")
    prompt = (
        f"Find the work email address of {target}. Use web search and public pages only. "
        "Do NOT guess or fabricate — only report addresses you actually saw published. "
        "Output ONLY the email addresses you found, one per line, nothing else."
    )
    try:
        proc = subprocess.run(
            ["claude", "-p", prompt],
            capture_output=True, text=True, timeout=timeout,
        )
        raw = (proc.stdout or "").strip()
        out["raw"] = raw[:2000]
        seen: list[str] = []
        for m in _EMAIL_RE.findall(raw):
            e = m.lower()
            if e not in seen and not is_role(e):
                seen.append(e)
        out["emails"] = seen
    except subprocess.TimeoutExpired:
        out["degraded"].append("claude-cli:timeout")
    except Exception as e:  # noqa: BLE001
        out["degraded"].append(f"claude-cli:{str(e)[:40]}")
    return out
