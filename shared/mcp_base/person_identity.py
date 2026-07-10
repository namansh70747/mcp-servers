"""Web-first person identity enrichment (no Apollo, no credits)."""
from __future__ import annotations

import importlib.util
import re
import threading
from functools import lru_cache
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from . import http
from .config import repo_root
from .llm import llm_resolve_identity

_TEAM_PATHS = ("/team", "/about", "/company", "/people", "/leadership", "")
_LINKEDIN_IN_RE = re.compile(r"https?://(?:[a-z]+\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?", re.I)
_BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SPA_MARKERS = ('id="root"', "id='root'", 'id="__next"', "ng-app", "<app-root",
                "window.__NUXT", "data-reactroot", "__NEXT_DATA__", "data-dpl-id")

# Known company -> domain overrides (slug heuristics are wrong for these)
_KNOWN_DOMAINS = {
    "liquid ai": "liquid.ai",
    "openai": "openai.com",
    "anthropic": "anthropic.com",
}


@lru_cache(maxsize=1)
def _email_finder():
    root = repo_root()
    path = root / "servers" / "email-finder" / "server.py"
    if not path.is_file():
        return None
    spec = importlib.util.spec_from_file_location("email_finder_identity", path)
    if not spec or not spec.loader:
        return None
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def guess_domain_from_company(company: str) -> str:
    """Best-effort domain from company name (e.g. Liquid AI -> liquid.ai)."""
    c = (company or "").strip()
    if not c:
        return ""
    key = c.lower()
    if key in _KNOWN_DOMAINS:
        return _KNOWN_DOMAINS[key]
    slug = re.sub(r"[^a-z0-9]+", "", key)
    if not slug:
        return ""
    # Try dotted form first for multi-word names: "Liquid AI" -> liquid.ai
    words = [w for w in re.split(r"[^a-z0-9]+", key) if w]
    if len(words) >= 2 and words[-1] in ("ai", "io", "co", "dev"):
        return f"{words[0]}.{words[-1]}"
    if slug.endswith("ai") and len(slug) > 2:
        return f"{slug}.ai"
    if slug.endswith("io") and len(slug) > 2:
        return f"{slug}.io"
    return f"{slug}.com"


def _name_parts(name: str) -> tuple[str, str]:
    parts = re.sub(r"[.\s]+", " ", (name or "").strip()).split()
    if not parts:
        return "", ""
    first = parts[0].lower()
    last = parts[-1].lower() if len(parts) > 1 else ""
    return first, last


def _linkedin_slug(url: str) -> str:
    m = re.search(r"linkedin\.com/in/([^/?#]+)", (url or "").lower())
    return (m.group(1).rstrip("/") if m else "")


def _looks_spa_shell(text: str) -> bool:
    t = (text or "").strip()
    if not t:
        return True
    if any(m in t for m in _SPA_MARKERS) and len(t) < 800:
        return True
    return t.startswith("<!DOCTYPE") and len(t) < 1200


def _html_to_text(html: str) -> str:
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html or "", "html.parser").get_text("\n", strip=True)
    except Exception:
        return re.sub(r"<[^>]+>", " ", html or "")


def _fetch_rendered_text(url: str, timeout: float = 25.0) -> str:
    """Headless render for JS SPA pages. Returns plain text or '' on failure."""
    result: dict = {}

    def work() -> None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent=_BROWSER_UA)
                    page.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
                    result["text"] = _html_to_text(page.content())
                finally:
                    browser.close()
        except Exception:
            result["text"] = ""

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout + 10)
    return (result.get("text") or "").strip()


def _fetch_page_text(url: str, name: str = "") -> str:
    """Plain HTTP first; render when SPA shell detected."""
    try:
        text = http.get_text(url, timeout=12, cache_ttl=600.0) or ""
    except Exception:
        text = ""
    first, last = _name_parts(name)
    name_missing = name and first not in text.lower() and last not in text.lower()
    if _looks_spa_shell(text) or name_missing:
        rendered = _fetch_rendered_text(url)
        if rendered:
            return rendered
    return text


def _score_linkedin_hit(hit: dict, name: str, company: str, title: str) -> int:
    first, last = _name_parts(name)
    slug = _linkedin_slug(hit.get("linkedin_url") or "")
    snippet = str(hit.get("snippet") or "")
    query = str(hit.get("query") or "")
    blob = f"{slug} {snippet} {query}".lower()

    score = 40 if hit.get("linkedin_url") else 0
    if first and first in slug:
        score += 20
    if last and last in slug:
        score += 20
    if first and first in blob:
        score += 20
    if last and last in blob:
        score += 20
    if company and company.lower() in blob:
        score += 25
    if title and any(w in blob for w in title.lower().split() if len(w) > 3):
        score += 10
    return score


def _fetch_team_evidence(domain: str, name: str) -> list[dict]:
    if not domain:
        return []
    first, last = _name_parts(name)
    evidence: list[dict] = []
    for path in _TEAM_PATHS:
        url = f"https://{domain.lstrip('@')}{path}"
        text = _fetch_page_text(url, name)
        if not text:
            continue
        snippet = ""
        for line in text.splitlines():
            ln = line.strip()
            if not ln:
                continue
            if (first and first in ln.lower()) or (last and last in ln.lower()):
                snippet = ln[:240]
                break
        if not snippet and first not in text.lower() and last not in text.lower():
            continue
        li_urls = _LINKEDIN_IN_RE.findall(text)
        li = li_urls[0].rstrip("/") if li_urls else None
        title_hint = None
        if snippet:
            m = re.search(r"(CEO|CTO|CFO|COO|CSO|Founder|Co-founder[^,\n]*)", snippet, re.I)
            if m:
                title_hint = m.group(0).strip()
        evidence.append({
            "url": url,
            "snippet": snippet or text[:240],
            "linkedin_url": li,
            "title_hint": title_hint,
            "score": 30 if snippet else 15,
        })
    return evidence


def enrich_person_identity(
    name: str,
    company: str = "",
    domain: str = "",
    title: str = "",
    linkedin_url: str = "",
) -> dict:
    """Build an IdentityCard from web search + optional LLM resolver. Never raises."""
    name = (name or "").strip()
    company = (company or "").strip()
    title = (title or "").strip()
    domain = (domain or "").strip().lower().lstrip("@") or guess_domain_from_company(company)

    if linkedin_url:
        card = {
            "name": name,
            "company": company or None,
            "title": title or None,
            "linkedin_url": linkedin_url.rstrip("/"),
            "location": None,
            "domain": domain or None,
            "confidence": "high",
            "source": "user",
            "evidence": [{"url": linkedin_url, "snippet": "user provided", "linkedin_url": linkedin_url}],
            "llm_provider": None,
            "llm_stage": None,
        }
        team_ev = _fetch_team_evidence(domain, name)
        if team_ev:
            card["evidence"] = team_ev + card["evidence"]
            for ev in team_ev:
                if ev.get("title_hint") and not title:
                    card["title"] = ev["title_hint"]
                sn = (ev.get("snippet") or "").lower()
                if "mit" in sn or "csail" in sn:
                    card["title"] = f"{card.get('title') or title} | Researcher @ MIT".strip(" |")
        return card

    evidence: list[dict] = []
    ef = _email_finder()
    linkedin_hits: list[dict] = []
    if ef and hasattr(ef, "_discover_linkedin_urls"):
        try:
            linkedin_hits = ef._discover_linkedin_urls(name, company, title)  # noqa: SLF001
        except Exception:
            linkedin_hits = []

    for hit in linkedin_hits:
        evidence.append({
            "url": hit.get("source_url") or hit.get("linkedin_url"),
            "snippet": hit.get("snippet") or hit.get("query") or "",
            "linkedin_url": hit.get("linkedin_url"),
            "title_hint": title or None,
            "score": _score_linkedin_hit(hit, name, company, title),
        })

    evidence.extend(_fetch_team_evidence(domain, name))

    ranked = sorted(evidence, key=lambda e: -(e.get("score") or 0))
    best_li = None
    best_score = 0
    for ev in ranked:
        sc = ev.get("score") or 0
        if ev.get("linkedin_url") and sc >= best_score:
            best_score = sc
            best_li = ev["linkedin_url"]

    confidence = "low"
    source = "web_rule"
    llm_provider = None
    resolved_title = title or None
    resolved_company = company or None
    location = None

    if best_score >= 55:
        confidence = "high"
    elif best_score >= 35:
        confidence = "medium"

    plausible = [e for e in ranked if e.get("linkedin_url") and (e.get("score") or 0) >= 25]
    llm_evidence = plausible[:8] if plausible else ranked[:8]
    need_llm = (
        len(plausible) > 1
        or (plausible and confidence == "low")
        or (not best_li and ranked)
        or (best_li and best_score < 55)
    )
    if need_llm and llm_evidence:
        target = {"name": name, "company": company, "title": title, "domain": domain}
        llm_out = llm_resolve_identity(target, llm_evidence)
        if llm_out:
            if llm_out.get("linkedin_url"):
                best_li = llm_out["linkedin_url"].rstrip("/")
                best_score = max(best_score, 55)
            resolved_title = llm_out.get("title") or resolved_title
            resolved_company = llm_out.get("company") or resolved_company
            location = llm_out.get("location")
            confidence = llm_out.get("confidence") or confidence
            source = "web_llm"
            llm_provider = llm_out.get("llm_provider")
    elif best_li:
        source = "web_rule"

    return {
        "name": name,
        "company": resolved_company,
        "title": resolved_title,
        "linkedin_url": best_li,
        "location": location,
        "domain": domain or None,
        "confidence": confidence,
        "source": source,
        "evidence": ranked[:10],
        "llm_provider": llm_provider,
        "llm_stage": "identity" if llm_provider else None,
    }


def confirm_identity_web(name: str, company: str = "") -> dict:
    """One web query for LinkedIn URL — extension fallback path only. Never raises."""
    name = (name or "").strip()
    company = (company or "").strip()
    if not name:
        return {"name": name, "company": company or None, "linkedin_url": None,
                "title": None, "confidence": "none", "source": "web_confirm"}
    ef = _email_finder()
    if not ef or not hasattr(ef, "_search_snippets_merged"):
        return {"name": name, "company": company or None, "linkedin_url": None,
                "title": None, "confidence": "none", "source": "web_confirm"}
    query = f'"{name}" "{company}" linkedin' if company else f'"{name}" linkedin'
    try:
        pack = ef._search_snippets_merged(query, max_links=8)  # noqa: SLF001
    except Exception:
        pack = {}
    urls: list[str] = list(pack.get("linkedin_urls") or [])
    for u in pack.get("urls") or []:
        if "linkedin.com/in/" not in (u or "").lower():
            continue
        norm = u.split("?")[0].rstrip("/")
        if norm not in urls:
            urls.append(norm)
    snippet_blob = " ".join(pack.get("snippets") or [])[:400]
    best: str | None = None
    best_score = 0
    title_hint: str | None = None
    for u in urls:
        hit = {"linkedin_url": u, "snippet": snippet_blob, "query": query}
        sc = _score_linkedin_hit(hit, name, company, "")
        if sc > best_score:
            best_score = sc
            best = u.rstrip("/")
            m = re.search(
                r"(CEO|CTO|CFO|COO|Founder|Co-founder[^,\n|]*)", snippet_blob, re.I,
            )
            if m:
                title_hint = m.group(0).strip()
    confidence = "none"
    if best:
        confidence = "high" if best_score >= 55 else ("medium" if best_score >= 35 else "low")
    return {
        "name": name,
        "company": company or None,
        "linkedin_url": best,
        "title": title_hint,
        "confidence": confidence,
        "source": "web_confirm",
    }
