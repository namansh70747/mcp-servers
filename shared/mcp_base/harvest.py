"""Deep email harvesting: sitemap + contact pages + JS render + deep_fallback.

Functions:
  discover_contact_pages(domain) → list[str]   — contact/about/team URLs for a domain
  harvest_emails(domain, name=None) → dict      — {email: weight} from all contact pages
  deep_fallback(name, domain, company) → dict   — last-resort: web search + BFS crawl + scrape
"""
from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from urllib.parse import urljoin, urlsplit

import json as _json_hc
import sqlite3 as _sqlite3
import threading as _thr_hc

from .email_extract import extract_emails, filter_emails
from .fetch import RateLimiter, fetch

# ── Harvest page cache (conditional-GET / 304 skip) ───────────────────────────────────────────
# Avoids re-fetching unchanged contact pages on repeat calls.  Stored as a sidecar SQLite in
# data_dir("email-finder") so it persists across restarts but doesn't couple harvest.py to the
# email-finder server module (no circular import).

class _HarvestCache:
    _lock = _thr_hc.Lock()
    _conn: "_sqlite3.Connection | None" = None
    _path: "str | None" = None

    def _db(self) -> "_sqlite3.Connection":
        if self._conn is not None:
            return self._conn
        with self._lock:
            if self._conn is not None:
                return self._conn
            try:
                from .config import data_dir
                p = str(data_dir("email-finder")) + "/harvest_cache.db"
            except Exception:
                p = "/tmp/harvest_cache.db"
            self._path = p
            c = _sqlite3.connect(p, check_same_thread=False)
            c.execute(
                "CREATE TABLE IF NOT EXISTS harvest_cache("
                "  url TEXT PRIMARY KEY, etag TEXT, modified TEXT,"
                "  emails_json TEXT, fetched_at TEXT)"
            )
            c.commit()
            self._conn = c
            return c

    def get(self, url: str) -> "tuple[str|None,str|None,dict]":
        """Return (etag, modified, emails_dict) from cache, or (None,None,{}) on miss."""
        try:
            row = self._db().execute(
                "SELECT etag, modified, emails_json FROM harvest_cache WHERE url=?", (url,)
            ).fetchone()
            if row:
                return row[0], row[1], _json_hc.loads(row[2] or "{}")
        except Exception:
            pass
        return None, None, {}

    def put(self, url: str, etag: "str|None", modified: "str|None", emails: dict) -> None:
        """Upsert a URL's extracted emails into the cache."""
        try:
            from datetime import datetime, timezone
            now = datetime.now(timezone.utc).isoformat()
            self._db().execute(
                "INSERT OR REPLACE INTO harvest_cache(url,etag,modified,emails_json,fetched_at)"
                " VALUES(?,?,?,?,?)",
                (url, etag, modified, _json_hc.dumps(emails), now)
            )
            self._db().commit()
        except Exception:
            pass

_HARVEST_CACHE = _HarvestCache()


_DEFAULT_CONTACT_PATHS = [
    "/", "/about", "/about-us", "/contact", "/contact-us", "/team", "/people",
    "/leadership", "/staff", "/founders", "/our-team", "/who-we-are", "/meet-the-team",
]
_CONTACT_RE = re.compile(
    r"/(about|about-us|contact|contact-us|team|people|leadership|staff|founders|"
    r"our-team|who-we-are|meet-the-team|press|media|management|board|executives)",
    re.IGNORECASE,
)
_RATE = RateLimiter(min_interval=0.5)


# ---------------------------------------------------------------------------
# Contact page discovery
# ---------------------------------------------------------------------------

def discover_contact_pages(domain: str) -> list[str]:
    """Return probable contact/team/about page URLs for a domain.

    Sources: default path list + sitemap.xml + llms.txt
    """
    base = f"https://{domain.strip().lower().lstrip('https://').lstrip('http://').split('/')[0]}"
    urls = [base + p for p in _DEFAULT_CONTACT_PATHS]

    # Sitemap
    try:
        sitemap_urls = _parse_sitemap(base + "/sitemap.xml")
        for u in sitemap_urls:
            if _CONTACT_RE.search(urlsplit(u).path):
                if u not in urls:
                    urls.append(u)
    except Exception:
        pass

    # llms.txt
    try:
        r = fetch(base + "/llms.txt", timeout=8.0)
        if r.get("ok") and r.get("html"):
            for line in r["html"].splitlines():
                line = line.strip()
                if line.startswith("http") and _CONTACT_RE.search(line):
                    if line not in urls:
                        urls.append(line)
    except Exception:
        pass

    return list(dict.fromkeys(urls))  # dedup, preserve order


def _parse_sitemap(sitemap_url: str, depth: int = 0) -> list[str]:
    """Parse a sitemap.xml (and sitemapindex) → flat list of URLs."""
    if depth > 2:
        return []
    r = fetch(sitemap_url, timeout=10.0)
    if not r.get("ok") or not r.get("html"):
        return []
    content = r["html"]
    urls = []
    try:
        root = ET.fromstring(content)
        ns = root.tag.split("}")[0].lstrip("{") if "}" in root.tag else ""
        ns_prefix = f"{{{ns}}}" if ns else ""
        # sitemapindex → recurse
        for sitemap in root.findall(f"{ns_prefix}sitemap"):
            loc = sitemap.find(f"{ns_prefix}loc")
            if loc is not None and loc.text:
                urls.extend(_parse_sitemap(loc.text.strip(), depth + 1))
        # urlset
        for url_el in root.findall(f"{ns_prefix}url"):
            loc = url_el.find(f"{ns_prefix}loc")
            if loc is not None and loc.text:
                urls.append(loc.text.strip())
    except Exception:
        # Regex fallback for malformed XML
        urls.extend(re.findall(r"<loc>\s*(https?://[^<]+)\s*</loc>", content))
    return urls


# ---------------------------------------------------------------------------
# Email harvesting
# ---------------------------------------------------------------------------

def harvest_emails(domain: str, name: str | None = None,
                   render_js: bool = False) -> dict[str, int]:
    """Fetch all contact pages and extract emails.

    Returns {email: weight} (see email_extract.extract_emails for weight semantics).
    Fetches up to 6 pages IN PARALLEL (was serial — saves ~4× wall-clock time).
    Optionally filters to only emails matching `name` pattern.
    """
    import concurrent.futures as _cf_h
    import threading
    pages = discover_contact_pages(domain)
    combined: dict[str, int] = {}
    _lock = threading.Lock()

    def _fetch_and_extract(url: str) -> dict[str, int]:
        # Conditional-GET: if we have a cached ETag/Last-Modified, send it and skip re-extraction on 304.
        _RATE.wait(url)
        cached_etag, cached_mod, cached_emails = _HARVEST_CACHE.get(url)
        r_raw = fetch(url, timeout=15.0, etag=cached_etag, modified=cached_mod)
        if r_raw.get("not_modified") and cached_emails:
            return cached_emails  # 304 — page unchanged, reuse
        html = r_raw.get("html", "") if r_raw.get("ok") else ""
        # JS render fallback for thin SPA shells
        if not html or (len(html) < 2000 and html.count(" ") < 100):
            try:
                js_html = _playwright_render(url)
                if js_html and len(js_html) > len(html):
                    html = js_html
            except Exception:
                pass
        if not html:
            return {}
        found = extract_emails(html, domain_filter=None)
        # F1 OCR: if the page renders emails as images, OCR them. No-op unless pytesseract installed.
        try:
            from .frontier.ocr import available as _ocr_ok, harvest_image_emails
            if _ocr_ok():
                for e in harvest_image_emails(html, url):
                    found[e] = max(found.get(e, 0), 2)
        except Exception:
            pass
        # Cache the result so the next call gets a 304-skip
        _HARVEST_CACHE.put(url, r_raw.get("etag"), r_raw.get("modified"), found)
        return found

    # Parallel fetch of contact pages (capped at 6 to avoid runaway).
    with _cf_h.ThreadPoolExecutor(max_workers=6) as _pool:
        for future in _cf_h.as_completed(
            [_pool.submit(_fetch_and_extract, url) for url in pages[:6]],
            timeout=90.0,
        ):
            try:
                for email, weight in (future.result() or {}).items():
                    with _lock:
                        combined[email] = max(combined.get(email, 0), weight)
            except Exception:
                continue

    # Also check for PDF links on the home page.
    try:
        home_r = fetch(f"https://{domain}/", timeout=10.0)
        if home_r.get("ok") and home_r.get("html"):
            pdf_emails = _harvest_pdfs(home_r["html"], f"https://{domain}/")
            for e, w in pdf_emails.items():
                combined[e] = max(combined.get(e, 0), w)
    except Exception:
        pass

    return combined


def commoncrawl_emails(domain: str, name: str | None = None, max_records: int = 12) -> dict[str, int]:
    """Harvest emails for a domain from the **Common Crawl** index — FREE, no key, ~unlimited (a
    100B+ page web archive). Queries the latest CC index for captured pages on the domain, fetches the
    archived HTML from the free S3 bucket via a ranged GET (WARC offset), and extracts emails. A big
    published-email source beyond the live site (recovers pages sitemaps/Wayback miss). Never raises."""
    import gzip
    import json as _json
    dom = (domain or "").strip().lower().lstrip("@")
    if not dom:
        return {}
    out: dict[str, int] = {}
    try:
        import httpx
        info = httpx.get("https://index.commoncrawl.org/collinfo.json", timeout=15,
                         follow_redirects=True).json()
        cdx_api = (info[0] or {}).get("cdx-api") if isinstance(info, list) and info else None
        if not cdx_api:
            return out
        r = httpx.get(cdx_api, params={"url": f"{dom}/*", "output": "json", "limit": 100,
                                       "filter": "status:200", "fl": "url,filename,offset,length,mime"},
                      timeout=25, follow_redirects=True)
        records = []
        for line in r.text.splitlines():
            try:
                j = _json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            if "html" in (j.get("mime") or "") and j.get("filename") and j.get("offset"):
                records.append(j)

        def _is_contactish(j) -> bool:
            u = (j.get("url") or "").lower()
            return any(k in u for k in ("contact", "about", "team", "people", "staff",
                                        "leadership", "author", "founder"))

        records.sort(key=lambda j: not _is_contactish(j))  # contact-ish pages first

        import concurrent.futures as _cf_cc
        import threading as _threading_cc
        _cc_lock = _threading_cc.Lock()
        _enough = _threading_cc.Event()  # set when we have ≥3 distinct on-domain emails

        def _fetch_cc_record(j: dict) -> dict[str, int]:
            if _enough.is_set():
                return {}
            try:
                off, ln = int(j["offset"]), int(j["length"])
                rr = httpx.get(f"https://data.commoncrawl.org/{j['filename']}",
                               headers={"Range": f"bytes={off}-{off + ln - 1}"},
                               timeout=20, follow_redirects=True)
                text = gzip.decompress(rr.content).decode("utf-8", "ignore")
                body = text.split("\r\n\r\n", 2)[-1]  # WARC hdrs → HTTP hdrs → body
                return extract_emails(body, domain_filter=None)
            except Exception:  # noqa: BLE001
                return {}

        with _cf_cc.ThreadPoolExecutor(max_workers=6) as _pool_cc:
            for _fut_cc in _cf_cc.as_completed(
                [_pool_cc.submit(_fetch_cc_record, j) for j in records[:max_records]],
                timeout=60.0,
            ):
                try:
                    for e, w in (_fut_cc.result() or {}).items():
                        with _cc_lock:
                            out[e] = max(out.get(e, 0), w)
                except Exception:
                    continue
                # Early-exit once we have enough distinct on-domain hits to avoid burning more budget
                with _cc_lock:
                    on_dom = sum(1 for e in out if dom in e)
                if on_dom >= 3:
                    _enough.set()
                    break
    except Exception:  # noqa: BLE001
        return out
    return out


def theharvester_emails(domain: str, timeout_s: float = 40.0) -> dict[str, int]:
    """Run theHarvester (OSINT email harvester) for a domain IF the `theHarvester` binary is installed
    — aggregates many free public sources. Returns {email: weight}; {} if not installed. Never raises."""
    import json as _json
    import shutil
    import subprocess
    import tempfile
    dom = (domain or "").strip().lower().lstrip("@")
    if not dom or not shutil.which("theHarvester"):
        return {}
    out: dict[str, int] = {}
    try:
        with tempfile.NamedTemporaryFile(suffix=".json", delete=True) as tf:
            # keyless sources only (no API keys needed); JSON output
            subprocess.run(["theHarvester", "-d", dom, "-b",
                            "duckduckgo,bing,crtsh,certspotter,anubis,hackertarget,rapiddns",
                            "-f", tf.name], capture_output=True, timeout=timeout_s)
            try:
                data = _json.load(open(tf.name))
            except Exception:  # noqa: BLE001
                data = {}
        for e in (data.get("emails") or []):
            e = (e or "").strip().lower()
            if "@" in e:
                out[e] = max(out.get(e, 0), 2)
    except Exception:  # noqa: BLE001
        return out
    return out


def _fetch_page(url: str, render_js: bool = False) -> str | None:
    """Fetch a page; try JS render if the static fetch returns a thin shell."""
    r = fetch(url, timeout=15.0)
    html = r.get("html", "") if r.get("ok") else ""

    # If it looks like a SPA shell (< 2 KB body or very few words), try JS render
    if render_js or (html and len(html) < 2000 and html.count(" ") < 100):
        try:
            js_html = _playwright_render(url)
            if js_html and len(js_html) > len(html):
                return js_html
        except Exception:
            pass

    return html or None


def _playwright_render(url: str) -> str:
    """Headless Playwright render — optional dep, degrades if absent.

    F9 stealth: if playwright-stealth is installed, apply its fingerprint patches so bot-walls
    (Cloudflare/PerimeterX) are bypassed rather than fatal. Plain render otherwise."""
    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        try:
            page = browser.new_page(
                user_agent="Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                           "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
            )
            try:  # optional stealth fingerprint patches
                from playwright_stealth import stealth_sync
                stealth_sync(page)
            except Exception:
                pass
            page.goto(url, wait_until="networkidle", timeout=20000)
            return page.content()
        finally:
            browser.close()


def _harvest_pdfs(html: str, base_url: str) -> dict[str, int]:
    """Find PDF links on a page and extract emails from them."""
    combined: dict[str, int] = {}
    pdf_links = re.findall(r'href=["\']([^"\']*\.pdf)["\']', html, re.IGNORECASE)
    for href in pdf_links[:5]:  # cap
        try:
            url = urljoin(base_url, href)
            r = fetch(url, timeout=15.0)
            if r.get("ok") and r.get("html"):
                from .email_extract import extract_from_pdf
                # Note: html content will be bytes for PDF; fetch returns text; try raw
                import httpx
                resp = httpx.get(url, timeout=10, follow_redirects=True)
                if resp.is_success:
                    emails = extract_from_pdf(resp.content)
                    for e in emails:
                        combined[e] = max(combined.get(e, 0), 2)
        except Exception:
            continue
    return combined


# ---------------------------------------------------------------------------
# Deep fallback — "pure web scraper + web engine" last resort
# ---------------------------------------------------------------------------

def deep_fallback(name: str, domain: str | None, company: str | None,
                  extra_queries: list[str] | None = None) -> dict:
    """Last-resort: web search + BFS crawl + site map + web engine.

    Returns {candidates: [{email, score, source}], degraded: [str]}.
    """
    from .websearch import search_links

    candidates: dict[str, dict] = {}
    degraded: list[str] = []

    # Build queries
    queries = []
    if name and company:
        queries += [
            f'"{name}" "{company}" email',
            f'"{name}" @{domain}' if domain else f'"{name}" "{company}" contact',
            f'"{name}" contact email site:{domain}' if domain else f'"{name}" contact',
        ]
    elif name:
        queries += [f'"{name}" email', f'"{name}" contact']
    if extra_queries:
        queries.extend(extra_queries)

    # Web search queries
    for q in queries[:5]:
        try:
            links = search_links(q, n=5)
            for url in links:
                _RATE.wait(url)
                html = _fetch_page(url, render_js=False) or ""
                domain_filter = domain if domain else None
                found = extract_emails(html, domain_filter=domain_filter)
                for email, weight in found.items():
                    if email not in candidates:
                        candidates[email] = {"email": email, "score": weight * 5, "source": "search:" + q[:40]}
                    else:
                        candidates[email]["score"] += weight * 3
        except Exception as e:
            degraded.append(f"search_query:error:{str(e)[:40]}")

    # Direct site BFS crawl
    if domain:
        try:
            bfs_emails = _bfs_crawl(domain)
            for email, weight in bfs_emails.items():
                if email not in candidates:
                    candidates[email] = {"email": email, "score": weight * 4, "source": "site_crawl"}
                else:
                    candidates[email]["score"] += weight * 2
        except Exception as e:
            degraded.append(f"bfs_crawl:error:{str(e)[:40]}")

    # Rank by score
    ranked = sorted(candidates.values(), key=lambda c: -c["score"])
    return {"candidates": ranked[:20], "degraded": degraded}


def _bfs_crawl(domain: str, max_pages: int = 20) -> dict[str, int]:
    """BFS crawl of a domain's contact pages to harvest emails."""
    base = f"https://{domain}"
    queue = list(dict.fromkeys(
        [base + p for p in _DEFAULT_CONTACT_PATHS[:8]]
    ))
    visited: set[str] = set()
    combined: dict[str, int] = {}
    page_count = 0

    while queue and page_count < max_pages:
        url = queue.pop(0)
        if url in visited:
            continue
        visited.add(url)
        page_count += 1
        _RATE.wait(url)
        r = fetch(url, timeout=12.0)
        if not r.get("ok") or not r.get("html"):
            continue
        html = r["html"]
        found = extract_emails(html, domain_filter=domain)
        for e, w in found.items():
            combined[e] = max(combined.get(e, 0), w)
        # Enqueue contact-path links from this page
        if page_count <= 5:
            for href in re.findall(r'href=["\']([^"\']+)["\']', html):
                full = urljoin(url, href)
                if (urlsplit(full).hostname or "").endswith(domain) and full not in visited:
                    if _CONTACT_RE.search(urlsplit(full).path):
                        queue.append(full)

    return combined
