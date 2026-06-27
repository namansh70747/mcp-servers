"""Shared HTTP helper: in-process TTL cache + retry/backoff + a default User-Agent.

Network servers (funding-radar, email-finder, github-profile, bookmark-vault, news-radar) can
swap their raw httpx calls for these to get caching + resilience for free. Lazy httpx import.
"""
from __future__ import annotations

import os
import threading
import time
from typing import Any

DEFAULT_UA = "mcp-suite/1.0 (+https://github.com/; personal use)"
_CACHE: dict[str, tuple[float, Any]] = {}
_RETRY_STATUS = {429, 500, 502, 503, 504}

_CLIENT = None
_CLIENT_LOCK = threading.Lock()


def _client():
    """Return (or lazily build) the shared long-lived httpx.Client for the whole process."""
    global _CLIENT
    if _CLIENT is None:
        with _CLIENT_LOCK:
            if _CLIENT is None:
                import httpx
                limits = httpx.Limits(
                    max_keepalive_connections=int(os.getenv("HTTP_POOL_MAX_KEEPALIVE", "20")),
                    max_connections=int(os.getenv("HTTP_POOL_MAX_CONNECTIONS", "100")),
                    keepalive_expiry=float(os.getenv("HTTP_POOL_KEEPALIVE_EXPIRY", "30")),
                )
                kw: dict = {}
                if os.getenv("HTTP_HTTP2", "0") == "1":
                    try:
                        import h2  # noqa: F401
                        kw["http2"] = True
                    except ImportError:
                        pass
                _CLIENT = httpx.Client(limits=limits, follow_redirects=True,
                                       timeout=httpx.Timeout(20.0), **kw)
    return _CLIENT


def _key(method: str, url: str, params: Any, headers: Any) -> str:
    return f"{method}\x1f{url}\x1f{sorted((params or {}).items())}\x1f{sorted((headers or {}).items())}"


def request(method: str, url: str, *, params: dict | None = None, headers: dict | None = None,
            json_body: Any = None, content: Any = None, timeout: float = 20.0,
            retries: int = 2, backoff: float = 0.8, cache_ttl: float = 0.0) -> dict:
    """Make an HTTP request with retry/backoff. Returns a dict envelope:
    {ok, status, headers, text, json?} or {ok: False, error}. GETs may be cached via cache_ttl (seconds)."""
    hdrs = {"User-Agent": DEFAULT_UA, **(headers or {})}
    ck = _key(method, url, params, hdrs) if cache_ttl and method.upper() == "GET" else None
    if ck and ck in _CACHE:
        ts, val = _CACHE[ck]
        if time.monotonic() - ts < cache_ttl:
            return {**val, "cached": True}

    last = ""
    for attempt in range(retries + 1):
        try:
            r = _client().request(method.upper(), url, params=params, headers=hdrs,
                                  json=json_body, content=content, timeout=timeout)
            if r.status_code in _RETRY_STATUS and attempt < retries:
                time.sleep(backoff * (2 ** attempt))
                continue
            out: dict = {"ok": r.is_success, "status": r.status_code,
                         "headers": dict(r.headers), "text": r.text}
            try:
                out["json"] = r.json()
            except Exception:
                pass
            if ck and r.is_success:
                _CACHE[ck] = (time.monotonic(), out)
            return out
        except Exception as e:  # noqa: BLE001 — network errors: retry then surface
            last = str(e)
            if attempt < retries:
                time.sleep(backoff * (2 ** attempt))
    return {"ok": False, "error": last or "request failed", "status": None}


def get_json(url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: float = 20.0, cache_ttl: float = 300.0) -> Any:
    """GET and return parsed JSON (cached 5 min by default), or {'error': ...}."""
    r = request("GET", url, params=params, headers=headers, timeout=timeout, cache_ttl=cache_ttl)
    if not r.get("ok"):
        return {"error": r.get("error", f"HTTP {r.get('status')}")}
    return r.get("json", {"error": "non-JSON response"})


def get_text(url: str, *, params: dict | None = None, headers: dict | None = None,
             timeout: float = 20.0, cache_ttl: float = 300.0) -> str:
    """GET and return body text (cached 5 min by default), or '' on failure."""
    r = request("GET", url, params=params, headers=headers, timeout=timeout, cache_ttl=cache_ttl)
    return r.get("text", "") if r.get("ok") else ""
