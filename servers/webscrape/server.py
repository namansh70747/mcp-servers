"""webscrape — a powerful, free, general-purpose web scraper + entity extractor.

Static-first (httpx + BeautifulSoup + structured-data parsing) with an OPTIONAL headless-browser
render (python-playwright) that auto-engages when a page is a JS-empty shell — so it reads React/Next/
SPA sites too. Clean content via trafilatura + markdownify (each lazy, with a BeautifulSoup fallback).

Tools: fetch (clean content), extract (JSON-LD/OpenGraph/meta), links, tables, contacts (emails/
phones/socials), crawl (BFS), and profile (person/company site -> one merged data sheet). Every fetch
is SSRF-guarded (no localhost / private IPs), size-capped, and http(s)-only. Nothing ever raises.
"""
from __future__ import annotations

import concurrent.futures as cf
import ipaddress
import json
import re
import threading
from urllib.parse import urljoin, urlsplit

from mcp_base import err, fetch as _hfetch, http, make_server, ok, scrape  # noqa: F401

mcp = make_server(
    "webscrape",
    instructions=("Free general web scraper. fetch(url) -> clean markdown/text/html; extract(url) -> "
                  "structured JSON-LD/OpenGraph/meta; links/tables; contacts(url) -> emails/phones/"
                  "socials; crawl(url) -> BFS pages; profile(target) -> a person/company site (or a "
                  "name) distilled into one data sheet (name, bio, emails, socials, location). Set "
                  "render='always' for JS/SPA sites (needs the optional browser extra)."),
)

MAX_PAGE_BYTES = 2_000_000          # cap each page to bound memory
DEFAULT_TIMEOUT = 20.0
_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
# phone: optional +cc then 2-4 grouped runs of digits; validated to >=7 digits downstream
PHONE_RE = re.compile(r"(?:(?:\+|00)\d{1,3}[\s.\-]?)?(?:\(?\d{2,4}\)?[\s.\-]?){2,5}\d{2,4}")
_SOCIAL_HOSTS = {
    "github.com": "github", "linkedin.com": "linkedin", "twitter.com": "twitter", "x.com": "twitter",
    "instagram.com": "instagram", "youtube.com": "youtube", "facebook.com": "facebook",
    "t.me": "telegram", "medium.com": "medium", "dribbble.com": "dribbble", "behance.net": "behance",
    "mastodon.social": "mastodon", "bsky.app": "bluesky", "tiktok.com": "tiktok",
}
_CONTACT_PATHS = ["", "/about", "/about-us", "/contact", "/contact-us", "/team", "/people"]


# ---------- safety + fetching ----------
def _is_internal_host(host: str) -> bool:
    """True if host is localhost / a private/loopback/link-local IP (SSRF guard)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False  # a normal hostname


def _norm_url(url: str) -> str:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else f"https://{url}"


def _guard(url: str) -> tuple[str, str]:
    """Return (full_url, error). error is '' when the URL is safe to fetch."""
    full = _norm_url(url)
    parts = urlsplit(full)
    if parts.scheme not in ("http", "https"):
        return full, "only http(s) URLs are allowed"
    host = (parts.hostname or "").lower()
    if not host or _is_internal_host(host):
        return full, "refusing to fetch an internal/private host"
    return full, ""


def _fetch_static(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """SSRF-guarded static GET via the hardened shared fetch (retry+jitter, encoding detection,
    final-host re-guard). Size-capped."""
    r = _hfetch.fetch(url, timeout=timeout, max_bytes=MAX_PAGE_BYTES, ua=_BROWSER_UA)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error", "fetch failed")}
    return {"ok": True, "html": r.get("html", ""), "final_url": r.get("final_url", url),
            "status": r.get("status"), "content_type": r.get("content_type", ""), "rendered": False}


def _render(url: str, timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Headless-Chromium render via python-playwright. Runs in a worker thread because the sync
    Playwright API refuses to run inside the server's asyncio loop. Degrades to a hint, never raises."""
    full, e = _guard(url)
    if e:
        return {"ok": False, "error": e}
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return {"ok": False, "error": "playwright is not installed",
                "hint": "uv sync --group browser && uv run playwright install chromium"}

    result: dict = {}

    def work() -> None:
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                browser = p.chromium.launch(headless=True)
                try:
                    page = browser.new_page(user_agent=_BROWSER_UA)
                    page.goto(full, wait_until="networkidle", timeout=int(timeout * 1000))
                    result.update({"ok": True, "html": page.content()[:MAX_PAGE_BYTES],
                                   "final_url": page.url, "rendered": True})
                finally:
                    browser.close()
        except Exception as ex:  # noqa: BLE001
            msg = str(ex)
            out = {"ok": False, "error": msg}
            if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
                out["hint"] = "uv run playwright install chromium"
            result.update(out)

    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout + 15)
    if not result:
        return {"ok": False, "error": "render timed out"}
    if result.get("ok") and _is_internal_host((urlsplit(result.get("final_url", "")).hostname or "").lower()):
        return {"ok": False, "error": "redirected to an internal/private host"}
    return result


def _looks_js_empty(html: str, text: str) -> bool:
    """Heuristic: a JS app shell with little server-rendered text."""
    if len((text or "").strip()) >= 200:
        return False
    markers = ('id="root"', "id='root'", 'id="__next"', "ng-app", "<app-root", "window.__NUXT",
               "data-reactroot", "__NEXT_DATA__")
    return any(m in (html or "") for m in markers) or len((text or "").strip()) < 40


def _fetch_raw(url: str, render: str = "auto", timeout: float = DEFAULT_TIMEOUT) -> dict:
    """Unified fetch. render: 'auto' (static, browser only if JS-empty) | 'always' | 'never'."""
    render = (render or "auto").lower()
    if render == "always":
        r = _render(url, timeout)
        if r.get("ok"):
            return r
        s = _fetch_static(url, timeout)         # fall back to static if the browser is unavailable
        if s.get("ok"):
            s["render_error"] = r.get("error")
            return s
        return r
    s = _fetch_static(url, timeout)
    if render == "never" or not s.get("ok"):
        return s
    if _looks_js_empty(s["html"], _bs4_text(s["html"])[:400]):
        r = _render(url, timeout)
        if r.get("ok"):
            return r
    return s


# ---------- HTML parsing (delegated to the shared mcp_base.scrape module) ----------
_soup = scrape.soup
_bs4_text = lambda html: scrape.bs4_main_and_text(html)[1]  # noqa: E731
_content = scrape.main_content
_title = scrape.title
_structured = scrape.structured
_emails_from_html = scrape.emails_from_html
_socials_from_html = scrape.socials_from_html
_phones = scrape.phones
_contacts = scrape.contacts
_links = scrape.links
_tables = scrape.tables


def _walk_jsonld(node, want: set, acc: list) -> None:
    """Collect dicts whose @type intersects `want` from arbitrarily nested JSON-LD (@graph, lists)."""
    if isinstance(node, list):
        for x in node:
            _walk_jsonld(x, want, acc)
    elif isinstance(node, dict):
        t = node.get("@type")
        types = {t} if isinstance(t, str) else set(t) if isinstance(t, list) else set()
        if types & want:
            acc.append(node)
        for v in node.values():
            if isinstance(v, (list, dict)):
                _walk_jsonld(v, want, acc)


def _entity_from_jsonld(jsonld: list) -> dict:
    """Pull a Person/Organization entity from JSON-LD into flat fields."""
    acc: list = []
    for block in jsonld:
        _walk_jsonld(block, {"Person", "Organization", "Corporation", "LocalBusiness"}, acc)
    out: dict = {}
    for e in acc:
        def g(*keys):
            for k in keys:
                if e.get(k):
                    return e[k]
            return None
        out.setdefault("name", g("name", "legalName"))
        out.setdefault("title", g("jobTitle"))
        out.setdefault("bio", g("description"))
        out.setdefault("email", (g("email") or "").replace("mailto:", "") or None)
        out.setdefault("phone", g("telephone"))
        img = g("image", "logo")
        if isinstance(img, dict):
            img = img.get("url")
        out.setdefault("image", img)
        same = e.get("sameAs")
        if same:
            out.setdefault("sameAs", same if isinstance(same, list) else [same])
        addr = e.get("address")
        if isinstance(addr, dict):
            parts = [addr.get(k) for k in ("streetAddress", "addressLocality", "addressRegion",
                                           "postalCode", "addressCountry")]
            out.setdefault("location", ", ".join(p for p in parts if isinstance(p, str)))
        elif isinstance(addr, str):
            out.setdefault("location", addr)
    return {k: v for k, v in out.items() if v}


# ---------- tools ----------
@mcp.tool
def fetch(url: str, format: str = "markdown", render: str = "auto") -> dict:
    """Fetch a page and return its CLEAN main content. format: markdown|text|html.
    render: auto (browser only if the page looks JS-empty) | always | never."""
    r = _fetch_raw(url, render)
    if not r.get("ok"):
        return err(r.get("error", "fetch failed"),
                   **{k: r[k] for k in ("hint", "status", "render_error") if r.get(k)})
    content = _content(r["html"], r["final_url"], format)
    return ok(url=r["final_url"], title=_title(r["html"]), format=format,
              rendered=r.get("rendered", False), chars=len(content or ""), content=content)


@mcp.tool
def extract(url: str, render: str = "auto") -> dict:
    """Extract STRUCTURED data: title, description, canonical, lang, OpenGraph, Twitter cards, meta,
    JSON-LD (schema.org), and an h1/h2 outline. The richest machine-readable layer a site exposes."""
    r = _fetch_raw(url, render)
    if not r.get("ok"):
        return err(r.get("error", "fetch failed"),
                   **{k: r[k] for k in ("hint", "status") if r.get(k)})
    data = _structured(r["html"], r["final_url"])
    return ok(url=r["final_url"], rendered=r.get("rendered", False), **data)


@mcp.tool
def links(url: str, scope: str = "all", render: str = "auto") -> dict:
    """All hyperlinks on the page, absolute-resolved and split into internal vs external.
    scope: all|internal|external."""
    r = _fetch_raw(url, render)
    if not r.get("ok"):
        return err(r.get("error", "fetch failed"))
    got = _links(r["html"], r["final_url"])
    scope = (scope or "all").lower()
    if scope == "internal":
        return ok(url=r["final_url"], internal=got["internal"], count=len(got["internal"]))
    if scope == "external":
        return ok(url=r["final_url"], external=got["external"], count=len(got["external"]))
    return ok(url=r["final_url"], internal=got["internal"], external=got["external"],
              count=len(got["internal"]) + len(got["external"]))


@mcp.tool
def tables(url: str, render: str = "auto") -> dict:
    """Extract every HTML <table> as JSON (header-keyed records when headers are present)."""
    r = _fetch_raw(url, render)
    if not r.get("ok"):
        return err(r.get("error", "fetch failed"))
    found = _tables(r["html"])
    return ok(url=r["final_url"], table_count=len(found), tables=found)


@mcp.tool
def contacts(url: str, max_pages: int = 4, render: str = "auto") -> dict:
    """Harvest contact data across a site's key pages (home + about/contact/team): emails, phones,
    and social profiles. Free, no key — often the fastest path to a person's details."""
    full, e = _guard(url)
    if e:
        return err(e)
    try:
        max_pages = max(1, min(int(max_pages), len(_CONTACT_PATHS)))
    except (TypeError, ValueError):
        max_pages = 4
    base = full.rstrip("/")
    base_host = (urlsplit(full).hostname or "").lower()
    emails: dict[str, int] = {}
    phones: list[str] = []
    socials: dict[str, str] = {}
    pages_hit = 0
    for path in _CONTACT_PATHS[:max_pages]:
        r = _fetch_raw(base + path, render)
        if not r.get("ok"):
            continue
        pages_hit += 1
        c = _contacts(r["html"])
        for em in c["emails"]:
            emails[em] = emails.get(em, 0) + 1
        for ph in c["phones"]:
            if ph not in phones:
                phones.append(ph)
        for k, v in c["socials"].items():
            socials.setdefault(k, v)
    on_domain = [e for e in emails if e.split("@")[-1].lower().endswith(base_host)]
    ranked = sorted(emails, key=lambda e: (-(e in on_domain), -emails[e]))
    return ok(url=full, pages_scanned=pages_hit, emails=ranked, phones=phones[:10], socials=socials)


@mcp.tool
def crawl(url: str, max_pages: int = 10, same_domain: bool = True,
          format: str = "markdown", render: str = "auto") -> dict:
    """Breadth-first crawl from a starting URL, returning clean content per page. Stays on-domain by
    default; capped at 25 pages. format: markdown|text|html."""
    full, e = _guard(url)
    if e:
        return err(e)
    try:
        max_pages = max(1, min(int(max_pages), 25))
    except (TypeError, ValueError):
        max_pages = 10
    base_host = (urlsplit(full).hostname or "").lower()
    queue = [full]
    seen = {full}
    pages = []
    while queue and len(pages) < max_pages:
        cur = queue.pop(0)
        r = _fetch_raw(cur, render)
        if not r.get("ok"):
            continue
        pages.append({"url": r["final_url"], "title": _title(r["html"]),
                      "content": _content(r["html"], r["final_url"], format)})
        got = _links(r["html"], r["final_url"])
        candidates = got["internal"] if same_domain else got["internal"] + got["external"]
        for ln in candidates:
            if ln not in seen and (not same_domain or (urlsplit(ln).hostname or "").lower() == base_host):
                seen.add(ln)
                queue.append(ln)
    return ok(start=full, pages_crawled=len(pages), pages=pages)


@mcp.tool
def profile(target: str, render: str = "auto") -> dict:
    """Person/company intelligence: given a URL (or a NAME, which is searched for first), crawl the
    site's key pages and merge JSON-LD Person/Organization + bio + emails/phones/socials + og:image
    into ONE data sheet: {name, title, bio, emails, phones, socials, location, image, links}."""
    target = (target or "").strip()
    if not target:
        return err("target (a URL or a name) is required")

    url = target
    searched_from = None
    if not target.lower().startswith(("http://", "https://")) and "." not in target.split("/")[0]:
        # looks like a name, not a domain/url -> find a site
        hits = _search_links(target, 1)
        if not hits:
            return err(f"could not find a site for {target!r}", hint="pass the URL directly")
        url, searched_from = hits[0], target

    full, e = _guard(url)
    if e:
        return err(e)
    base = full.rstrip("/")
    base_host = (urlsplit(full).hostname or "").lower()

    bio = ""
    emails: dict[str, int] = {}
    phones: list[str] = []
    socials: dict[str, str] = {}
    entity: dict = {}
    image = None
    source_pages = []
    for path in _CONTACT_PATHS[:5]:
        r = _fetch_raw(base + path, render)
        if not r.get("ok"):
            continue
        source_pages.append(r["final_url"])
        st = _structured(r["html"], r["final_url"])
        ent = _entity_from_jsonld(st.get("jsonld", []))
        for k, v in ent.items():
            entity.setdefault(k, v)
        image = image or st.get("opengraph", {}).get("image")
        if not bio:
            bio = (st.get("description") or "")[:1000]
        c = _contacts(r["html"])
        for em in c["emails"]:
            emails[em] = emails.get(em, 0) + 1
        for ph in c["phones"]:
            if ph not in phones:
                phones.append(ph)
        for k, v in c["socials"].items():
            socials.setdefault(k, v)
        if path == "":  # homepage main content as a bio fallback
            if not bio or len(bio) < 80:
                bio = (_content(r["html"], r["final_url"], "text") or bio)[:1000]

    # merge JSON-LD sameAs links into socials
    for s in entity.get("sameAs", []) or []:
        host = (urlsplit(s).hostname or "").lower().removeprefix("www.")
        key = _SOCIAL_HOSTS.get(host)
        if key:
            socials.setdefault(key, s)
    if entity.get("email"):
        emails.setdefault(entity["email"].lower(), 5)

    on_domain = [e for e in emails if e.split("@")[-1].lower().endswith(base_host)]
    ranked = sorted(emails, key=lambda e: (-(e in on_domain), -emails[e]))
    out = {
        "url": full,
        "name": entity.get("name") or _title(_fetch_raw(base, "never").get("html", "")) or None,
        "title": entity.get("title"),
        "bio": (entity.get("bio") or bio or "").strip()[:1000] or None,
        "emails": ranked,
        "phones": phones[:10],
        "socials": socials,
        "location": entity.get("location"),
        "image": entity.get("image") or image,
        "source_pages": source_pages,
    }
    if searched_from:
        out["found_via_search"] = searched_from
    return ok(**out)


@mcp.tool
def search(query: str, max_results: int = 8, fetch: bool = False) -> dict:
    """Keyless web search across MULTIPLE engines in parallel (DuckDuckGo, Bing, Mojeek, Startpage),
    merged + deduped + ranked by cross-engine agreement. With fetch=True, pulls a clean snippet from
    each result page (concurrently). Free, no API key."""
    query = (query or "").strip()
    if not query:
        return err("query is required")
    try:
        max_results = max(1, min(int(max_results), 20))
    except (TypeError, ValueError):
        max_results = 8
    ranked = _search_all(query, max_results)
    if not ranked:
        return err("no results (search engines may be rate-limiting)", query=query, results=[])
    results = [{"url": a["url"], "host": (urlsplit(a["url"]).hostname or ""), "engines": a["engines"]}
               for a in ranked[:max_results]]
    if fetch:
        def grab(item):
            r = _fetch_static(item["url"])
            if r.get("ok"):
                item["title"] = _title(r["html"])
                item["snippet"] = (_content(r["html"], item["url"], "text") or "")[:500]
            return item
        with cf.ThreadPoolExecutor(max_workers=6) as ex:
            results = list(ex.map(grab, results))
    return ok(query=query, count=len(results), results=results)


@mcp.tool
def research(query: str, max_sources: int = 5) -> dict:
    """Deep research: search the web, fetch the top sources CONCURRENTLY, extract clean passages from
    each, and return ranked {url, title, passages} ready to read/synthesize. A one-call research surface."""
    query = (query or "").strip()
    if not query:
        return err("query is required")
    try:
        max_sources = max(1, min(int(max_sources), 10))
    except (TypeError, ValueError):
        max_sources = 5
    ranked = _search_all(query, max_sources * 2)
    if not ranked:
        return err("no results (search engines may be rate-limiting)", query=query)
    urls = [a["url"] for a in ranked[:max_sources]]

    def dig(u):
        r = _fetch_static(u)
        if not r.get("ok"):
            return None
        ch = scrape.chunks(_content(r["html"], u, "markdown"), max_chars=900)
        return {"url": r["final_url"], "title": _title(r["html"]),
                "passages": [{"heading": h, "text": t} for h, t in ch[:3]]}
    sources = []
    with cf.ThreadPoolExecutor(max_workers=6) as ex:
        for res in ex.map(dig, urls):
            if res and res["passages"]:
                sources.append(res)
    if not sources:
        return err("found results but couldn't extract content", query=query, urls=urls)
    return ok(query=query, sources=[s["url"] for s in sources], count=len(sources), results=sources)


# ---------- keyless multi-engine search (parallel + agreement-ranked) ----------
_ENGINES = (
    ("ddg", "https://html.duckduckgo.com/html/", None),
    ("bing", "https://www.bing.com/search", ["li.b_algo h2 a", "h2 a"]),
    ("mojeek", "https://www.mojeek.com/search", ["a.ob", "ul.results-standard li a", "h2 a"]),
    ("startpage", "https://www.startpage.com/sp/search",
     ["a.result-link", "a.w-gl__result-title", "h3 a"]),
)


def _run_engine(engine: tuple, query: str, n: int) -> list:
    name, url, sels = engine
    try:
        html_text = http.get_text(url, params={"q": query}, headers={"User-Agent": _BROWSER_UA},
                                   timeout=15, cache_ttl=900.0)
        return _ddg_targets(html_text, n) if name == "ddg" else _engine_targets(html_text, sels, n)
    except Exception:
        return []


def _search_all(query: str, n: int) -> list:
    """Query all engines in parallel; merge by canonical URL, rank by cross-engine agreement then rank."""
    lists: list = []
    with cf.ThreadPoolExecutor(max_workers=len(_ENGINES)) as ex:
        futs = [ex.submit(_run_engine, e, query, n * 2) for e in _ENGINES]
        for f in cf.as_completed(futs):
            try:
                lists.append(f.result() or [])
            except Exception:
                lists.append([])
    agg: dict = {}
    for lst in lists:
        for rank, u in enumerate(lst):
            canon = _hfetch.canonicalize(u)
            a = agg.setdefault(canon, {"url": u, "score": 0.0, "engines": 0})
            a["score"] += 1.0 / (1 + rank)
            a["engines"] += 1
    return sorted(agg.values(), key=lambda a: (-a["engines"], -a["score"]))


def _search_links(query: str, max_links: int = 5) -> list:
    """Back-compat helper (used by profile-by-name): canonical, agreement-ranked URLs."""
    return [a["url"] for a in _search_all(query, max_links)][:max_links]


def _ddg_targets(html_text: str, max_links: int) -> list:
    from urllib.parse import parse_qs, unquote
    if not html_text:
        return []
    try:
        anchors = [a.get("href", "") for a in _soup(html_text).find_all("a", href=True)]
    except Exception:
        anchors = re.findall(r'href="([^"]+)"', html_text)
    out: list = []
    for href in anchors:
        if href.startswith("//"):
            href = "https:" + href
        try:
            parts = urlsplit(href)
        except Exception:
            continue
        target = ""
        if parts.path.startswith("/l/") or "uddg=" in (parts.query or ""):
            target = unquote((parse_qs(parts.query).get("uddg") or [""])[0])
        elif parts.scheme in ("http", "https") and "duckduckgo.com" not in (parts.hostname or ""):
            target = href
        if target and target not in out and not _is_internal_host((urlsplit(target).hostname or "")):
            out.append(target)
        if len(out) >= max_links:
            break
    return out


def _engine_targets(html_text: str, selectors: list, max_links: int) -> list:
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
    out: list = []
    for href in hrefs:
        if not href.startswith("http"):
            continue
        host = (urlsplit(href).hostname or "").lower()
        if (host and not _is_internal_host(host)
                and not any(s in host for s in ("bing.com", "mojeek.com", "microsoft.com", "msn.com"))
                and href not in out):
            out.append(href)
        if len(out) >= max_links:
            break
    return out


if __name__ == "__main__":
    mcp.run()
