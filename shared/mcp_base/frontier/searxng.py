"""F5 — self-hosted SearXNG (+ optional Tor) metasearch. Active only when SEARXNG_URL is set.

A local SearXNG instance fans out to 70+ engines behind one JSON API and is the most
block-resistant search backend. Optionally route through Tor (SEARXNG_TOR_PROXY) for IP rotation.
When SEARXNG_URL is unset this module reports unavailable and websearch uses its direct engines.
"""
from __future__ import annotations

import os

_SEARXNG_URL = os.environ.get("SEARXNG_URL", "").strip()
_TOR_PROXY = os.environ.get("SEARXNG_TOR_PROXY", "").strip()  # e.g. socks5://127.0.0.1:9050


def available() -> bool:
    return bool(_SEARXNG_URL)


def search(query: str, n: int = 10) -> list[dict]:
    """Query the local SearXNG JSON API → [{title, url, engine}]. [] if unavailable/errors."""
    if not _SEARXNG_URL or not query:
        return []
    base = _SEARXNG_URL.rstrip("/")
    params = {"q": query, "format": "json", "safesearch": 0}
    try:
        if _TOR_PROXY:
            # use httpx directly so we can attach a SOCKS proxy for IP rotation
            import httpx
            with httpx.Client(proxies=_TOR_PROXY, timeout=20, follow_redirects=True) as c:
                r = c.get(f"{base}/search", params=params)
                data = r.json() if r.is_success else {}
        else:
            from ..http import get_json
            data = get_json(f"{base}/search", params=params, timeout=20, cache_ttl=300) or {}
    except Exception:
        return []
    out: list[dict] = []
    for item in (data.get("results") or [])[:n]:
        url = item.get("url")
        if url:
            out.append({"title": item.get("title", ""), "url": url,
                        "engine": item.get("engine", "searxng")})
    return out


def search_links(query: str, n: int = 10) -> list[str]:
    return [r["url"] for r in search(query, n)]
