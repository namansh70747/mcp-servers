"""Resilient parallel multi-engine web search with Chrome JA3/HTTP2 impersonation.

Anti-blocking ladder for each engine request:
  1. curl_cffi with impersonate="chrome" (Chrome JA3/HTTP2 fingerprint — most reliable in 2026)
  2. httpx via shared http.get_text (rotating realistic UA)
  3. Playwright render (if installed)

Agreement ranking: score += 1/(1+rank), sort by (-engines, -score).
Any engine throwing/returning empty is swallowed — degraded[], never raises.

Usage:
    from mcp_base.websearch import search_all, search_links

    results = search_all("John Smith Acme Corp email", n=10)
    # → [{"url": ..., "score": ..., "engines": ...}, ...]
"""
from __future__ import annotations

import concurrent.futures as cf
import re
from typing import Any
from urllib.parse import parse_qs, unquote, urlsplit

_BROWSER_UA = (
    "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
)

# Engine registry: (name, url, css_selectors_or_None, extra_params)
_ENGINES: list[tuple[str, str, list[str] | None, dict]] = [
    ("ddg",       "https://html.duckduckgo.com/html/",  None, {}),
    ("bing",      "https://www.bing.com/search",
     ["li.b_algo h2 a", "h2 a"], {}),
    ("mojeek",    "https://www.mojeek.com/search",
     ["a.ob", "ul.results-standard li a", "h2 a"], {}),
    ("startpage", "https://www.startpage.com/sp/search",
     ["a.result-link", "a.w-gl__result-title", "h3 a"], {}),
    ("brave",     "https://search.brave.com/search",
     ["a[href].snippet-url", "a.result-header", "h3 a"], {}),
    ("marginalia","https://search.marginalia.nu/search",
     ["a.url", "li a", "h2 a"], {}),
    # Public SearX/SearXNG instances (JSON API then HTML fallback)
    ("searx1",    "https://searx.be/search",
     None, {"format": "json"}),
    ("searx2",    "https://search.disroot.org/search",
     None, {"format": "json"}),
]

_EXCLUDED_HOSTS = frozenset({
    "bing.com", "mojeek.com", "microsoft.com", "msn.com",
    "duckduckgo.com", "startpage.com", "brave.com",
    "google.com", "googleadservices.com",
    "marginalia.nu",
})


# ---------------------------------------------------------------------------
# Transport helpers
# ---------------------------------------------------------------------------

def _curl_cffi_get(url: str, params: dict | None = None, timeout: float = 15.0) -> str:
    """GET via curl_cffi Chrome impersonation. Raises ImportError if absent."""
    from curl_cffi.requests import get as cffi_get
    r = cffi_get(url, params=params, impersonate="chrome",
                 timeout=timeout, allow_redirects=True)
    r.raise_for_status()
    return r.text


def _httpx_get(url: str, params: dict | None = None, timeout: float = 15.0) -> str:
    from .http import get_text
    return get_text(url, params=params,
                    headers={"User-Agent": _BROWSER_UA}, timeout=timeout, cache_ttl=900.0)


def _playwright_get(url: str, params: dict | None = None, timeout: float = 20.0) -> str:
    from urllib.parse import urlencode
    full = url + ("?" + urlencode(params) if params else "")
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(user_agent=_BROWSER_UA)
            page.goto(full, wait_until="networkidle", timeout=int(timeout * 1000))
            return page.content()
        finally:
            browser.close()


def _fetch_html(url: str, params: dict | None = None, timeout: float = 15.0) -> str:
    """Try curl_cffi → httpx → Playwright in order. Returns '' on total failure."""
    # 1. curl_cffi (best TLS fingerprint)
    try:
        return _curl_cffi_get(url, params=params, timeout=timeout)
    except ImportError:
        pass
    except Exception:
        pass

    # 2. httpx
    try:
        text = _httpx_get(url, params=params, timeout=timeout)
        if text:
            return text
    except Exception:
        pass

    # 3. Playwright
    try:
        return _playwright_get(url, params=params, timeout=timeout)
    except Exception:
        pass

    return ""


# ---------------------------------------------------------------------------
# Per-engine parsers
# ---------------------------------------------------------------------------

def _parse_ddg(html_text: str, n: int) -> list[str]:
    if not html_text:
        return []
    try:
        anchors = [a.get("href", "") for a in _soup(html_text).find_all("a", href=True)]
    except Exception:
        anchors = re.findall(r'href="([^"]+)"', html_text)
    out: list[str] = []
    for href in anchors:
        if href.startswith("//"):
            href = "https:" + href
        target = ""
        try:
            parts = urlsplit(href)
            if parts.path.startswith("/l/") or "uddg=" in (parts.query or ""):
                target = unquote((parse_qs(parts.query).get("uddg") or [""])[0])
            elif parts.scheme in ("http", "https") and "duckduckgo.com" not in (parts.hostname or ""):
                target = href
        except Exception:
            continue
        if target and target not in out and not _excluded(target):
            out.append(target)
        if len(out) >= n:
            break
    return out


def _parse_searx_json(html_or_json: str, n: int) -> list[str]:
    """Parse SearXNG JSON response."""
    try:
        import json
        data = json.loads(html_or_json)
        results = data.get("results", [])
        return [r["url"] for r in results[:n] if r.get("url") and not _excluded(r["url"])]
    except Exception:
        return []


def _parse_html_selectors(html_text: str, selectors: list[str], n: int) -> list[str]:
    if not html_text:
        return []
    try:
        soup = _soup(html_text)
        anchors: list = []
        for sel in selectors:
            anchors = soup.select(sel)
            if anchors:
                break
        hrefs = [a.get("href", "") for a in (anchors or soup.find_all("a", href=True))]
    except Exception:
        hrefs = re.findall(r'href="([^"]+)"', html_text)
    out: list[str] = []
    for href in hrefs:
        if not href.startswith("http"):
            continue
        if not _excluded(href):
            out.append(href)
        if len(out) >= n:
            break
    return out


# ---------------------------------------------------------------------------
# Engine runner
# ---------------------------------------------------------------------------

def _run_engine(name: str, url: str, selectors: list[str] | None,
                extra_params: dict, query: str, n: int) -> list[str]:
    params = {"q": query, **extra_params}
    html_text = _fetch_html(url, params=params, timeout=15.0)
    if not html_text:
        return []

    if selectors is None and name.startswith("searx"):
        # Try JSON parse first
        results = _parse_searx_json(html_text, n)
        if results:
            return results
        # Fall back to HTML parse
        return _parse_html_selectors(html_text, ["h3 a", "a.result_title", "li a"], n)

    if name == "ddg":
        return _parse_ddg(html_text, n)

    return _parse_html_selectors(html_text, selectors or ["h2 a", "h3 a"], n)


# ---------------------------------------------------------------------------
# Public API
# ---------------------------------------------------------------------------

def search_all(query: str, n: int = 10,
               engines: list[str] | None = None) -> tuple[list[dict], list[str]]:
    """Query all (or selected) engines in parallel; merge by canonical URL, rank by agreement.

    Returns:
        (results, degraded)
        results: [{"url": str, "score": float, "engines": int}, ...]
        degraded: [str, ...]  — names of engines that failed
    """
    from .fetch import canonicalize, is_internal_host

    selected = [(name, url, sels, xp) for name, url, sels, xp in _ENGINES
                if engines is None or name in engines]

    lists: list[tuple[str, list[str]]] = []
    degraded: list[str] = []

    # F5: prefer a self-hosted SearXNG (+ optional Tor) instance when configured — it fans out to
    # 70+ engines and is far more block-resistant. Treated as one high-weight "engine" in the merge.
    try:
        from .frontier import searxng as _sx
        if _sx.available():
            sx_links = _sx.search_links(query, n=n * 2)
            if sx_links:
                lists.append(("searxng", sx_links))
    except Exception:
        degraded.append("engine:searxng:failed")

    with cf.ThreadPoolExecutor(max_workers=min(len(selected), 8)) as ex:
        futs = {ex.submit(_run_engine, name, url, sels, xp, query, n * 2): name
                for name, url, sels, xp in selected}
        for fut in cf.as_completed(futs):
            name = futs[fut]
            try:
                result = fut.result() or []
                lists.append((name, result))
            except Exception:
                degraded.append(f"engine:{name}:failed")
                lists.append((name, []))

    # Agreement ranking
    agg: dict[str, dict] = {}
    for engine_name, lst in lists:
        for rank, raw_url in enumerate(lst):
            try:
                canon = canonicalize(raw_url)
                if not canon or is_internal_host((urlsplit(canon).hostname or "")):
                    continue
                entry = agg.setdefault(canon, {"url": raw_url, "score": 0.0, "engines": 0})
                entry["score"] += 1.0 / (1 + rank)
                entry["engines"] += 1
            except Exception:
                continue

    ranked = sorted(agg.values(), key=lambda a: (-a["engines"], -a["score"]))[:n]
    return ranked, degraded


def search_links(query: str, n: int = 5) -> list[str]:
    """Convenience wrapper: return just the top-N URLs from search_all."""
    results, _ = search_all(query, n=n)
    return [r["url"] for r in results]


def search(query: str, n: int = 8, engines: list[str] | None = None) -> dict:
    """Structured search result for MCP tool exposure.

    Returns {ok, query, results: [{url, score, engines}], degraded}.
    """
    results, degraded = search_all(query, n=n, engines=engines)
    return {
        "ok": True,
        "query": query,
        "count": len(results),
        "results": results,
        "degraded": degraded,
    }


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _excluded(url: str) -> bool:
    try:
        host = (urlsplit(url).hostname or "").lower()
        return bool(host and any(h in host for h in _EXCLUDED_HOSTS))
    except Exception:
        return False


def _soup(html_text: str) -> Any:
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html_text, "html.parser")
    except ImportError:
        raise


def health_check() -> dict:
    """Ping each engine with a quick test query; return per-engine status."""
    statuses: dict[str, str] = {}
    for name, url, _, _ in _ENGINES:
        try:
            text = _fetch_html(url, params={"q": "test"}, timeout=8.0)
            statuses[name] = "ok" if text else "empty"
        except Exception as e:
            statuses[name] = f"error: {str(e)[:40]}"

    # Check curl_cffi availability
    try:
        import curl_cffi  # noqa: F401
        statuses["curl_cffi"] = "available"
    except ImportError:
        statuses["curl_cffi"] = "not-installed (install for best anti-blocking)"

    return statuses
