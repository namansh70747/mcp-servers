"""Hardened HTTP fetch shared by the web servers: SSRF guard, retry+backoff+jitter, encoding
detection, conditional GET (ETag/Last-Modified), size cap, URL canonicalization, per-host rate limit.

All functions are dependency-light (lazy httpx) and never raise — failures come back as {ok: False}.
"""
from __future__ import annotations

import ipaddress
import threading
import time
from urllib.parse import parse_qsl, urlencode, urlsplit, urlunsplit

DEFAULT_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
MAX_PAGE_BYTES = 2_000_000
_TRACKING_PREFIXES = ("utm_", "mc_", "pk_")
_TRACKING_KEYS = {"fbclid", "gclid", "gclsrc", "dclid", "msclkid", "ref", "ref_src", "ref_url",
                  "igshid", "yclid", "_hsenc", "_hsmi", "mkt_tok", "spm"}
_DEFAULT_PORTS = {"http": "80", "https": "443"}


def is_internal_host(host: str) -> bool:
    """True if host is localhost / a private/loopback/link-local IP (SSRF guard)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False


def canonicalize(url: str) -> str:
    """Normalize a URL for dedup: drop fragment + tracking params, lowercase host, strip default port,
    and normalize a bare-root trailing slash. Best-effort; returns the input on parse failure."""
    try:
        p = urlsplit(url.strip())
        if p.scheme not in ("http", "https"):
            return url.strip()
        host = (p.hostname or "").lower()
        netloc = host
        if p.port and _DEFAULT_PORTS.get(p.scheme) != str(p.port):
            netloc = f"{host}:{p.port}"
        q = [(k, v) for k, v in parse_qsl(p.query, keep_blank_values=True)
             if not (k.lower() in _TRACKING_KEYS or k.lower().startswith(_TRACKING_PREFIXES))]
        path = p.path or "/"
        if path != "/" and path.endswith("/"):
            path = path.rstrip("/")
        return urlunsplit((p.scheme, netloc, path, urlencode(q), ""))
    except Exception:
        return url.strip()


def norm_url(url: str) -> str:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else f"https://{url}"


def fetch(url: str, *, timeout: float = 20.0, cookies: dict | None = None, etag: str | None = None,
          modified: str | None = None, ua: str = DEFAULT_UA, retries: int = 2,
          max_bytes: int = MAX_PAGE_BYTES) -> dict:
    """SSRF-guarded GET with retry+jitter, encoding detection, and conditional GET.

    Returns {ok, status, html, final_url, etag, modified, content_type, not_modified} or
    {ok: False, error}. A 304 → {ok: True, not_modified: True} (skip re-processing)."""
    full = norm_url(url)
    p = urlsplit(full)
    if p.scheme not in ("http", "https"):
        return {"ok": False, "error": "only http(s) URLs are allowed"}
    if is_internal_host((p.hostname or "").lower()):
        return {"ok": False, "error": "refusing to fetch an internal/private host"}

    headers = {"User-Agent": ua, "Accept-Encoding": "gzip, deflate"}
    if etag:
        headers["If-None-Match"] = etag
    if modified:
        headers["If-Modified-Since"] = modified

    last = ""
    for attempt in range(retries + 1):
        try:
            import httpx
            with httpx.Client(timeout=timeout, follow_redirects=True, headers=headers,
                              cookies=cookies or {}) as c:
                r = c.get(full)
            if r.status_code == 304:
                return {"ok": True, "not_modified": True, "status": 304, "final_url": full}
            if r.status_code in (408, 425, 429, 500, 502, 503, 504) and attempt < retries:
                time.sleep(_backoff(attempt))
                continue
            final = str(r.url)
            if is_internal_host((urlsplit(final).hostname or "").lower()):
                return {"ok": False, "error": "redirected to an internal/private host"}
            try:
                text = r.text  # httpx picks encoding from headers/charset
            except Exception:
                text = r.content.decode(r.encoding or "utf-8", "ignore")
            return {"ok": r.is_success, "status": r.status_code, "html": text[:max_bytes],
                    "final_url": final, "etag": r.headers.get("etag"),
                    "modified": r.headers.get("last-modified"),
                    "content_type": r.headers.get("content-type", "")}
        except Exception as e:  # noqa: BLE001
            last = str(e)
            if attempt < retries:
                time.sleep(_backoff(attempt))
    return {"ok": False, "error": last or "request failed", "status": None}


def _backoff(attempt: int) -> float:
    """Exponential backoff with jitter (deterministic-ish, no Math.random dependency)."""
    base = 0.6 * (2 ** attempt)
    jitter = (hash((attempt, time.monotonic_ns() % 997)) % 250) / 1000.0
    return min(base + jitter, 8.0)


class RateLimiter:
    """Thread-safe per-host minimum-interval throttle for polite concurrent crawling."""

    def __init__(self, min_interval: float = 0.0):
        self.min_interval = max(0.0, float(min_interval))
        self._last: dict[str, float] = {}
        self._lock = threading.Lock()

    def wait(self, url: str) -> None:
        if self.min_interval <= 0:
            return
        host = (urlsplit(url).hostname or "").lower()
        with self._lock:
            now = time.monotonic()
            prev = self._last.get(host, 0.0)
            delay = self.min_interval - (now - prev)
            self._last[host] = now + max(0.0, delay)
        if delay > 0:
            time.sleep(delay)
