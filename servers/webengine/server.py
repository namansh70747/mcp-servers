"""webengine — ONE connector: read a whole organization's docs, pass authentication, store credentials
(macOS Keychain), and crawl logged-in content. The "read docs like Claude" engine.

Four capabilities in one self-contained server:
  • Credential vault — secrets in the macOS Keychain (via `security`), permission-gated (confirm=True);
    only domain->username is kept in SQLite (never the password).
  • Built-in browser — its own async Playwright persistent context for login / signup / sessions.
  • Massive docs engine — discovery cascade (llms-full.txt -> llms.txt -> sitemap.xml -> nav-crawl),
    bounded-concurrent fetch, a persistent corpus with FTS5 search (read_docs / search_docs / ask).
  • Authenticated crawl — after login, reuse the live session's cookies for the bulk crawl.

Needs the browser extra:  uv sync --group browser && uv run playwright install chromium
Polite by default: respects robots.txt, ~8 workers, ~200 pages (hard cap 2000). Never raises — every
tool returns {ok: False, error, hint?} on failure.
"""
from __future__ import annotations

import asyncio
import concurrent.futures as cf
import re
import subprocess
import time
import xml.etree.ElementTree as ET
from datetime import datetime, timezone
from urllib.parse import urljoin, urlsplit
from urllib.robotparser import RobotFileParser

from mcp_base import (BaseStore, data_dir, db_path, embed, err, fetch, http, make_server, ok,
                      repo_root, scrape)

try:
    import json as _json
except Exception:  # pragma: no cover
    _json = None

mcp = make_server(
    "webengine",
    instructions=("One connector to read a whole org's docs + pass auth. read_docs(url) ingests an "
                  "ENTIRE doc set (tries llms-full.txt/llms.txt/sitemap.xml, then crawls); search_docs/"
                  "ask query the corpus; map_site lists URLs. Auth: login(url,user[,password]) uses the "
                  "Keychain vault (add_credential confirm=True; it asks permission first); read_docs(..., "
                  "authed=True) crawls logged-in pages. signup_autofill fills forms from profile.json. "
                  "Visible browser for CAPTCHA/2FA. Polite: respects robots.txt."),
)

# ---------- paths / constants ----------
PROFILE_DIR = data_dir("webengine") / "profile"
SESSIONS_DIR = data_dir("webengine") / "sessions"
EXPORTS_DIR = data_dir("webengine") / "exports"
KC_SERVICE = "mcp-webengine"           # Keychain service prefix: mcp-webengine:<domain>
MAX_PAGE_BYTES = 2_000_000
HARD_PAGE_CAP = 2000
DEFAULT_WORKERS = 8
DEFAULT_TIMEOUT = 30000  # ms (browser)
_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")

_USER_SELECTORS = ("input[type=email]", "input[name*=email i]", "input[id*=email i]",
                   "input[name*=user i]", "input[id*=user i]", "input[autocomplete=username]",
                   "input[name*=login i]", "input[type=text]")
_PASS_SELECTORS = ("input[type=password]", "input[name*=pass i]", "input[id*=pass i]")
_SUBMIT_SELECTORS = ("button[type=submit]", "input[type=submit]",
                     "button:has-text('Log in')", "button:has-text('Sign in')",
                     "button:has-text('Continue')", "button:has-text('Sign up')",
                     "button:has-text('Log In')", "button:has-text('Login')")

SCHEMA = """
CREATE TABLE IF NOT EXISTS pages(
  id INTEGER PRIMARY KEY, site TEXT, url TEXT UNIQUE, title TEXT DEFAULT '',
  section TEXT DEFAULT '', markdown TEXT DEFAULT '', tokens INTEGER DEFAULT 0, fetched_at TEXT,
  etag TEXT DEFAULT '', modified TEXT DEFAULT ''
);
CREATE INDEX IF NOT EXISTS pages_site ON pages(site);
CREATE VIRTUAL TABLE IF NOT EXISTS docs_fts USING fts5(
  content, title, url UNINDEXED, site UNINDEXED, tokenize='porter'
);
CREATE TABLE IF NOT EXISTS chunks(
  id INTEGER PRIMARY KEY, page_id INTEGER, site TEXT, url TEXT, heading TEXT DEFAULT '',
  text TEXT, embedding BLOB
);
CREATE INDEX IF NOT EXISTS chunks_site ON chunks(site);
CREATE INDEX IF NOT EXISTS chunks_page ON chunks(page_id);
CREATE VIRTUAL TABLE IF NOT EXISTS chunks_fts USING fts5(
  text, heading, url UNINDEXED, site UNINDEXED, tokenize='porter'
);
CREATE TABLE IF NOT EXISTS sites(
  site TEXT PRIMARY KEY, root_url TEXT, pages INTEGER DEFAULT 0, source TEXT, ingested_at TEXT
);
CREATE TABLE IF NOT EXISTS creds(
  domain TEXT PRIMARY KEY, username TEXT, notes TEXT DEFAULT '', created_at TEXT, updated_at TEXT
);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""
store = BaseStore(db_path("webengine"), schema=SCHEMA)
# Backfill columns if an older DB exists (idempotent).
for _col in ("etag TEXT DEFAULT ''", "modified TEXT DEFAULT ''"):
    try:
        store.execute(f"ALTER TABLE pages ADD COLUMN {_col}")
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _host(url: str) -> str:
    return (urlsplit(url if "://" in url else "https://" + url).hostname or "").lower()


def _norm(url: str) -> str:
    url = (url or "").strip()
    return url if url.lower().startswith(("http://", "https://")) else f"https://{url}"


def _is_internal_host(host: str) -> bool:
    import ipaddress
    h = (host or "").strip().lower().rstrip(".")
    if not h or h == "localhost" or h.endswith((".localhost", ".local", ".internal")):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False


# ================================================================= credential vault (Keychain)
def _run(args: list[str], inp: str | None = None, timeout: int = 15) -> dict:
    try:
        p = subprocess.run(args, capture_output=True, text=True, input=inp, timeout=timeout)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": p.stdout.strip(), "err": p.stderr.strip()}
    except FileNotFoundError:
        return {"ok": False, "err": f"command not found: {args[0]}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)}


def _kc_set(domain: str, username: str, password: str) -> dict:
    return _run(["security", "add-generic-password", "-U", "-s", f"{KC_SERVICE}:{domain}",
                 "-a", username, "-w", password])


def _kc_get(domain: str) -> str | None:
    r = _run(["security", "find-generic-password", "-s", f"{KC_SERVICE}:{domain}", "-w"])
    return r["out"] if r.get("ok") and r.get("out") else None


def _kc_del(domain: str) -> dict:
    return _run(["security", "delete-generic-password", "-s", f"{KC_SERVICE}:{domain}"])


@mcp.tool
def request_add(domain: str, username: str) -> dict:
    """Ask permission to store a login. Returns the exact follow-up call to make once you approve."""
    domain = _host(domain) or domain
    return ok(permission_required=True, domain=domain, username=username,
              message=(f"To store a credential for {domain} ({username}) in your macOS Keychain, approve "
                       f"and then call add_credential with confirm=True."),
              call=f"add_credential(domain='{domain}', username='{username}', password='…', confirm=True)")


@mcp.tool
def add_credential(domain: str, username: str, password: str, confirm: bool = False,
                   notes: str = "") -> dict:
    """Store a website login in the macOS Keychain. Requires confirm=True (your explicit allow). The
    password goes ONLY into the Keychain; SQLite keeps just domain+username."""
    domain = _host(domain) or domain
    if not domain or not username or not password:
        return err("domain, username and password are required")
    if not confirm:
        return err("confirm required", blocked=True,
                   hint=f"call again with confirm=True to store the credential for {domain}")
    r = _kc_set(domain, username, password)
    if not r.get("ok"):
        return err(f"keychain write failed: {r.get('err')}",
                   hint="macOS Keychain (the `security` CLI) is required for the vault")
    store.execute("INSERT INTO creds(domain,username,notes,created_at,updated_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(domain) DO UPDATE SET username=excluded.username, notes=excluded.notes, "
                  "updated_at=excluded.updated_at",
                  (domain, username, notes, _now(), _now()))
    return ok(stored=domain, username=username, backend="macos-keychain")


@mcp.tool
def has_credential(domain: str) -> dict:
    """Is a credential stored for this domain? Returns username (no secret)."""
    domain = _host(domain) or domain
    row = store.query_one("SELECT username FROM creds WHERE domain=?", (domain,))
    return ok(exists=bool(row), domain=domain, username=row["username"] if row else None)


@mcp.tool
def get_credential(domain: str, reveal: bool = False) -> dict:
    """Get the stored username for a domain. With reveal=True also returns the password from the
    Keychain (sensitive — used internally by login)."""
    domain = _host(domain) or domain
    row = store.query_one("SELECT username,notes FROM creds WHERE domain=?", (domain,))
    if not row:
        return err(f"no stored credential for {domain}", exists=False)
    out = {"domain": domain, "username": row["username"], "notes": row["notes"]}
    if reveal:
        pw = _kc_get(domain)
        if pw is None:
            return err("index has the domain but the Keychain secret is missing", **out)
        out["password"] = pw
    return ok(**out)


@mcp.tool
def list_credentials() -> dict:
    """List stored credentials (domain + username only — never secrets)."""
    rows = store.query("SELECT domain,username,updated_at FROM creds ORDER BY domain")
    return ok(count=len(rows), credentials=rows)


@mcp.tool
def remove_credential(domain: str, confirm: bool = False) -> dict:
    """Delete a stored credential from the Keychain + index. Requires confirm=True."""
    domain = _host(domain) or domain
    if not confirm:
        return err("confirm required", blocked=True, hint=f"call again with confirm=True to delete {domain}")
    _kc_del(domain)
    store.execute("DELETE FROM creds WHERE domain=?", (domain,))
    return ok(removed=domain)


@mcp.tool
def auth_status() -> dict:
    """Vault status: backend availability + how many credentials are stored."""
    sec = _run(["security", "help"]).get("ok") or _run(["which", "security"]).get("ok")
    n = (store.query_one("SELECT COUNT(*) c FROM creds") or {}).get("c", 0)
    return ok(backend="macos-keychain", keychain_available=bool(sec), stored=n)


# ================================================================= built-in browser / auth
_pw = None
_ctx = None
_page = None
_lock = asyncio.Lock()


def _need_playwright() -> dict:
    return err("playwright is not installed",
               hint="uv sync --group browser && uv run playwright install chromium")


async def _ensure(headless: bool = False) -> dict:
    global _pw, _ctx, _page
    if _ctx is not None and _page is not None:
        return {}
    try:
        from playwright.async_api import async_playwright
    except Exception:
        return _need_playwright()
    try:
        PROFILE_DIR.mkdir(parents=True, exist_ok=True)
        _pw = await async_playwright().start()
        _ctx = await _pw.chromium.launch_persistent_context(
            user_data_dir=str(PROFILE_DIR), headless=headless, accept_downloads=True,
            viewport={"width": 1366, "height": 900}, user_agent=_UA)
        _ctx.set_default_timeout(DEFAULT_TIMEOUT)
        _page = _ctx.pages[0] if _ctx.pages else await _ctx.new_page()
        return {}
    except Exception as e:  # noqa: BLE001
        msg = str(e)
        out = {"ok": False, "error": msg}
        if "Executable doesn't exist" in msg or "playwright install" in msg.lower():
            out["hint"] = "uv run playwright install chromium"
        await _teardown()
        return out


async def _teardown() -> None:
    global _pw, _ctx, _page
    for fn in ((_ctx.close if _ctx else None), (_pw.stop if _pw else None)):
        try:
            if fn:
                await fn()
        except Exception:
            pass
    _pw = _ctx = _page = None


async def _state() -> dict:
    try:
        return {"url": _page.url, "title": (await _page.title()) or ""}
    except Exception:
        return {"url": None, "title": ""}


async def _find(selectors) -> str | None:
    for sel in selectors:
        try:
            loc = _page.locator(sel).first
            if await loc.count() and await loc.is_visible():
                return sel
        except Exception:
            continue
    return None


@mcp.tool
async def open(url: str = "", headless: bool = False) -> dict:
    """Start the built-in browser (persistent profile -> stays logged in) and optionally navigate."""
    async with _lock:
        e = await _ensure(headless)
        if e:
            return e
        if url:
            try:
                await _page.goto(_norm(url), wait_until="domcontentloaded")
            except Exception as ex:  # noqa: BLE001
                return err(f"opened but navigation failed: {ex}", **await _state())
        return ok(**await _state())


@mcp.tool
async def status() -> dict:
    """Is the browser running? Current url + title."""
    if _page is None:
        return ok(running=False)
    return ok(running=True, **await _state())


@mcp.tool
async def close() -> dict:
    """Close the browser (persistent profile keeps you logged in next time)."""
    async with _lock:
        await _teardown()
        return ok(closed=True)


@mcp.tool
async def login(url: str, username: str = "", password: str = "", user_selector: str = "",
                pass_selector: str = "", submit_selector: str = "") -> dict:
    """Log in to a site. If `password` is omitted, it's read from the Keychain vault for the domain; if
    no credential is stored, returns needs_permission=True telling you to approve add_credential first.
    Auto-detects fields. Finish any CAPTCHA/2FA in the visible window. Saves the session for authed crawls."""
    domain = _host(url)
    if not username or not password:
        row = store.query_one("SELECT username FROM creds WHERE domain=?", (domain,))
        if not row:
            return err(f"no stored credential for {domain}", needs_permission=True,
                       hint=(f"approve add_credential(domain='{domain}', username='you', password='…', "
                             f"confirm=True), then call login again"))
        username = username or row["username"]
        password = password or (_kc_get(domain) or "")
        if not password:
            return err(f"index has {domain} but no Keychain secret", needs_permission=True,
                       hint="re-add with add_credential(..., confirm=True)")
    async with _lock:
        e = await _ensure(headless=False)
        if e:
            return e
        try:
            await _page.goto(_norm(url), wait_until="domcontentloaded")
            usel = user_selector or await _find(_USER_SELECTORS)
            if not usel:
                return err("could not find a username/email field", hint="pass user_selector", **await _state())
            await _page.locator(usel).first.fill(username)
            psel = pass_selector or await _find(_PASS_SELECTORS)
            if not psel:  # two-step flow
                nxt = await _find(_SUBMIT_SELECTORS)
                if nxt:
                    await _page.locator(nxt).first.click()
                    await _page.wait_for_load_state("domcontentloaded")
                    psel = await _find(_PASS_SELECTORS)
            if not psel:
                return err("found username but no password field", hint="pass pass_selector", **await _state())
            await _page.locator(psel).first.fill(password)
            ssel = submit_selector or await _find(_SUBMIT_SELECTORS)
            await (_page.locator(ssel).first.click() if ssel else _page.keyboard.press("Enter"))
            try:
                await _page.wait_for_load_state("networkidle", timeout=15000)
            except Exception:
                pass
            logged_in = await _find(_PASS_SELECTORS) is None
            if logged_in and domain:
                try:
                    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
                    await _ctx.storage_state(path=str(SESSIONS_DIR / f"{_safe(domain)}.json"))
                except Exception:
                    pass
            return ok(logged_in=logged_in, session=_safe(domain),
                      note="if a CAPTCHA/2FA is showing, complete it in the window; then call save_session",
                      **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def signup_autofill(url: str = "", submit: bool = False, overrides: dict | None = None) -> dict:
    """Fill a signup/contact form from profile.json (name/email/phone/location); `overrides` win per
    field (e.g. {'password': '...'}). Never invents a password. Optionally submit."""
    async with _lock:
        e = await _ensure(headless=False)
        if e:
            return e
        try:
            if url:
                await _page.goto(_norm(url), wait_until="domcontentloaded")
            prof = {}
            try:
                prof = _json.loads((repo_root() / "profile.json").read_text())
            except Exception:
                pass
            ov = overrides or {}
            full = ov.get("name") or prof.get("name") or ""
            vals = {
                "email": ov.get("email") or prof.get("email") or "",
                "first": ov.get("first_name") or (full.split()[0] if full else ""),
                "last": ov.get("last_name") or (full.split()[-1] if len(full.split()) > 1 else ""),
                "name": full, "phone": ov.get("phone") or prof.get("phone") or "",
                "username": ov.get("username") or "",
                "location": ov.get("location") or prof.get("location") or "",
                "password": ov.get("password") or "",
            }
            sels = {
                "email": ["input[type=email]", "input[name*=email i]"],
                "first": ["input[name*=first i]", "input[autocomplete=given-name]"],
                "last": ["input[name*=last i]", "input[autocomplete=family-name]"],
                "name": ["input[name*=name i]", "input[autocomplete=name]"],
                "phone": ["input[type=tel]", "input[name*=phone i]"],
                "username": ["input[name*=user i]"], "location": ["input[name*=city i]", "input[name*=address i]"],
                "password": ["input[type=password]"],
            }
            filled = {}
            for f, cands in sels.items():
                if not vals.get(f):
                    continue
                s = await _find(cands)
                if s:
                    try:
                        await _page.locator(s).first.fill(str(vals[f]))
                        filled[f] = s
                    except Exception:
                        pass
            if submit:
                ssel = await _find(_SUBMIT_SELECTORS)
                if ssel:
                    await _page.locator(ssel).first.click()
                    await _page.wait_for_load_state("domcontentloaded")
            note = None
            if not vals["password"] and await _find(_PASS_SELECTORS):
                note = "a password field exists but none given — pass overrides={'password': ...}"
            return ok(filled=list(filled), submitted=submit, note=note, **await _state())
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def save_session(name: str = "default") -> dict:
    """Snapshot cookies/localStorage to a named session (used by authed crawls)."""
    async with _lock:
        if _ctx is None:
            return err("browser not open")
        try:
            SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
            path = SESSIONS_DIR / f"{_safe(name, 'default')}.json"
            await _ctx.storage_state(path=str(path))
            return ok(saved=str(path), session=_safe(name, "default"))
        except Exception as ex:  # noqa: BLE001
            return err(str(ex))


@mcp.tool
async def list_sessions() -> dict:
    """List saved sessions."""
    SESSIONS_DIR.mkdir(parents=True, exist_ok=True)
    return ok(sessions=sorted(p.stem for p in SESSIONS_DIR.glob("*.json")))


async def _live_cookies() -> list:
    """Cookies from the live browser context as httpx-ready {name,value,domain} dicts."""
    if _ctx is None:
        return []
    try:
        return [{"name": c["name"], "value": c["value"], "domain": c.get("domain", "")}
                for c in await _ctx.cookies()]
    except Exception:
        return []


def _safe(name: str, default: str = "out") -> str:
    return re.sub(r"[^a-zA-Z0-9_.-]", "_", name) if name else default


# ================================================================= docs engine
def _robots(base: str, obey: bool):
    """Return a RobotFileParser only if a REAL robots.txt exists. Many sites serve a 404 HTML page at
    /robots.txt; treating that as rules wrongly blocks everything — so we fetch it ourselves and only
    honor a plausible text robots file, otherwise allow all (return None)."""
    if not obey:
        return None
    txt = _get_text(urljoin(base, "/robots.txt"))
    head = txt.lstrip()[:600].lower()
    if not txt or head.startswith("<") or "<html" in head or "<!doctype" in head:
        return None  # missing / HTML 404 page -> no usable rules -> allow all
    try:
        rp = RobotFileParser()
        rp.parse(txt.splitlines())
        return rp
    except Exception:
        return None


def _allowed(rp, url: str) -> bool:
    if rp is None:
        return True
    try:
        return rp.can_fetch(_UA, url)
    except Exception:
        return True


# Polite per-host throttle shared across the concurrent crawl.
_RATE = fetch.RateLimiter(min_interval=0.25)


def _get_text(url: str, timeout: float = 20.0) -> str:
    """Plain GET returning text via the hardened shared fetch (retry/encoding/SSRF). '' on failure."""
    r = fetch.fetch(url, timeout=timeout, max_bytes=8_000_000)
    return r.get("html", "") if r.get("ok") and not r.get("not_modified") else ""


def _sitemaps_from_robots(base: str) -> list[str]:
    out = []
    txt = _get_text(urljoin(base, "/robots.txt"))
    for line in txt.splitlines():
        if line.lower().startswith("sitemap:"):
            out.append(line.split(":", 1)[1].strip())
    return out


def _parse_sitemap(url: str, depth: int = 0) -> list[str]:
    """Return all <loc> URLs; recurses into sitemap indexes (depth-capped)."""
    if depth > 3:
        return []
    txt = _get_text(url)
    if not txt:
        return []
    urls: list[str] = []
    try:
        root = ET.fromstring(txt.encode("utf-8", "ignore"))
        tag = root.tag.lower()
        locs = [e.text.strip() for e in root.iter() if e.tag.lower().endswith("loc") and e.text]
        if tag.endswith("sitemapindex"):
            for sm in locs[:50]:
                urls.extend(_parse_sitemap(sm, depth + 1))
        else:
            urls.extend(locs)
    except Exception:
        pass
    return urls


def _links_from_markdown(md: str, base: str) -> list[str]:
    out = []
    for m in re.findall(r"\]\((https?://[^)\s]+|/[^)\s]+)\)", md):
        out.append(urljoin(base, m))
    return out


def _discover(base_url: str, obey_robots: bool, max_pages: int) -> tuple[str, list[str], str | None]:
    """Return (source, urls, llms_full_text). Cascade: llms-full -> llms -> sitemap -> nav BFS."""
    base = _norm(base_url)
    origin = f"{urlsplit(base).scheme}://{urlsplit(base).netloc}"
    prefix = base.rstrip("/")

    # 1. llms-full.txt — the entire docs as one file
    for cand in (urljoin(origin + "/", "llms-full.txt"), prefix + "/llms-full.txt"):
        full = _get_text(cand)
        if full and len(full) > 500:
            return "llms-full", [cand], full

    # 2. llms.txt — a curated link index
    for cand in (urljoin(origin + "/", "llms.txt"), prefix + "/llms.txt"):
        txt = _get_text(cand)
        if txt and len(txt) > 50:
            urls = [u for u in _links_from_markdown(txt, origin) if _host(u) == _host(base)]
            if urls:
                return "llms", _dedupe(urls)[:max_pages], None

    # 3. sitemap.xml (+ robots Sitemap:)
    sm_urls: list[str] = []
    for sm in (_sitemaps_from_robots(origin) or []) + [urljoin(origin + "/", "sitemap.xml")]:
        sm_urls.extend(_parse_sitemap(sm))
    if sm_urls:
        same = [u for u in sm_urls if _host(u) == _host(base)]
        under = [u for u in same if u.rstrip("/").startswith(prefix)] or same
        if under:
            return "sitemap", _dedupe(under)[:max_pages], None

    # 4. nav/sidebar BFS within the path prefix
    rp = _robots(origin, obey_robots)
    seen, queue, found = {base}, [base], []
    while queue and len(found) < max_pages:
        cur = queue.pop(0)
        if not _allowed(rp, cur):
            continue
        html = _get_text(cur)
        if not html:
            continue
        found.append(cur)
        for ln in scrape.links(html, cur)["internal"]:
            ln = ln.split("#")[0]
            if ln not in seen and ln.rstrip("/").startswith(prefix) and _host(ln) == _host(base):
                seen.add(ln)
                queue.append(ln)
    return "nav", found[:max_pages], None


def _dedupe(urls: list[str]) -> list[str]:
    """Dedup by canonical key (drops #frag + tracking params + trailing slash)."""
    seen, out = set(), []
    for u in urls:
        k = fetch.canonicalize(u)
        if k not in seen:
            seen.add(k)
            out.append(k)
    return out


def _render_html(url: str, cookies: list, timeout: float = 30.0) -> str:
    """Render a JS page with headless Chromium in a worker thread (sync Playwright can't run in the
    server loop). '' if Playwright is unavailable or it fails."""
    try:
        from playwright.sync_api import sync_playwright  # noqa: F401
    except Exception:
        return ""
    result = {"html": ""}

    def work():
        try:
            from playwright.sync_api import sync_playwright
            with sync_playwright() as p:
                b = p.chromium.launch(headless=True)
                try:
                    ctx = b.new_context(user_agent=_UA)
                    if cookies:
                        try:
                            ctx.add_cookies([{"name": c["name"], "value": c["value"],
                                              "domain": c.get("domain") or _host(url), "path": "/"}
                                             for c in cookies if c.get("name")])
                        except Exception:
                            pass
                    pg = ctx.new_page()
                    pg.goto(url, wait_until="networkidle", timeout=int(timeout * 1000))
                    result["html"] = pg.content()[:MAX_PAGE_BYTES]
                finally:
                    b.close()
        except Exception:
            pass

    import threading
    t = threading.Thread(target=work, daemon=True)
    t.start()
    t.join(timeout + 15)
    return result["html"]


def _fetch_page(url: str, cookies: list, rp, render: str = "auto",
                etag: str = "", modified: str = "") -> dict | None:
    """Hardened single-page fetch: rate-limited, conditional GET, JS-render fallback. Returns a page
    dict, or {'not_modified': True} when the server says 304, or None on failure/disallow."""
    if not _allowed(rp, url) or _is_internal_host(_host(url)):
        return None
    _RATE.wait(url)
    jar = {c["name"]: c["value"] for c in (cookies or []) if c.get("name")}
    r = fetch.fetch(url, cookies=jar or None, etag=etag or None, modified=modified or None)
    if not r.get("ok"):
        return None
    if r.get("not_modified"):
        return {"not_modified": True, "url": url}
    html = r.get("html", "")
    md = scrape.main_content(html, r.get("final_url", url), "markdown")
    if (not md or len(md) < 200) and render != "never":  # JS shell -> render
        rh = _render_html(r.get("final_url", url), cookies)
        if rh:
            md2 = scrape.main_content(rh, r.get("final_url", url), "markdown")
            if md2 and len(md2) > len(md or ""):
                html, md = rh, md2
    if not md:
        return None
    return {"url": r.get("final_url", url), "title": scrape.title(html), "markdown": md,
            "etag": r.get("etag") or "", "modified": r.get("modified") or ""}


def _crawl_blocking(urls: list[str], cookies: list, base: str, obey_robots: bool,
                    workers: int, render: str = "auto", known: dict | None = None) -> list[dict]:
    rp = _robots(base, obey_robots)
    known = known or {}
    out: list[dict] = []
    with cf.ThreadPoolExecutor(max_workers=workers) as ex:
        futs = {ex.submit(_fetch_page, u, cookies, rp, render,
                          known.get(u, ("", ""))[0], known.get(u, ("", ""))[1]): u for u in urls}
        for fut in cf.as_completed(futs):
            try:
                r = fut.result()
            except Exception:
                r = None
            if r:
                out.append(r)
    return out


def _embed_chunks_for(pid: int, site: str, url: str, md: str) -> int:
    """Split a page into chunks, store them + FTS, and embed when a model is available. Returns chunks."""
    store.execute("DELETE FROM chunks WHERE page_id=?", (pid,))
    chs = scrape.chunks(md, max_chars=1200)
    if not chs:
        return 0
    texts = [t for _, t in chs]
    vecs = embed.encode(texts) if embed.available() else []
    for i, (heading, text) in enumerate(chs):
        blob = embed.pack(vecs[i]) if i < len(vecs) else None
        cid = store.execute(
            "INSERT INTO chunks(page_id,site,url,heading,text,embedding) VALUES(?,?,?,?,?,?)",
            (pid, site, url, heading, text, blob))
        store.execute("INSERT INTO chunks_fts(rowid,text,heading,url,site) VALUES(?,?,?,?,?)",
                      (cid, text, heading, url, site))
    if vecs:
        store.execute("INSERT INTO meta(key,value) VALUES('embed',?) "
                      "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
                      (_json.dumps({"model": embed.model_name(), "dim": len(vecs[0])}),))
    return len(chs)


def _index_page(pid: int, site: str, url: str, title: str, md: str) -> None:
    store.execute("DELETE FROM docs_fts WHERE rowid=?", (pid,))
    store.execute("INSERT INTO docs_fts(rowid,content,title,url,site) VALUES(?,?,?,?,?)",
                  (pid, md, title, url, site))
    _embed_chunks_for(pid, site, url, md)


def _store_page(site: str, url: str, title: str, section: str, md: str,
                etag: str = "", modified: str = "") -> int:
    tokens = max(1, len(md) // 4)
    pid = store.execute(
        "INSERT INTO pages(site,url,title,section,markdown,tokens,fetched_at,etag,modified) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET title=excluded.title, section=excluded.section, "
        "markdown=excluded.markdown, tokens=excluded.tokens, fetched_at=excluded.fetched_at, "
        "etag=excluded.etag, modified=excluded.modified",
        (site, url, title, section, md, tokens, _now(), etag, modified))
    row = store.query_one("SELECT id FROM pages WHERE url=?", (url,))
    pid = row["id"] if row else pid
    _index_page(pid, site, url, title, md)
    return pid


def _split_llms_full(full: str) -> list[tuple[str, str]]:
    """Split a llms-full.txt into (section_title, markdown) chunks by top-level headings."""
    parts = re.split(r"(?m)^#\s+", full)
    out = []
    for p in parts:
        p = p.strip()
        if not p:
            continue
        title = p.splitlines()[0][:200]
        out.append((title, "# " + p))
    return out or [("", full)]


@mcp.tool
async def read_docs(url: str, max_pages: int = 200, render: str = "auto", authed: bool = False,
                    obey_robots: bool = True, refresh: bool = False) -> dict:
    """Read an ENTIRE doc set into a searchable corpus. Tries llms-full.txt / llms.txt / sitemap.xml,
    then crawls the docs section. Returns {site, pages_ingested, source, tokens, sample_urls}. Idempotent
    (skips already-stored URLs unless refresh=True). authed=True reuses the live login session's cookies."""
    base = _norm(url)
    site = _host(base)
    if not site or _is_internal_host(site):
        return err("only public http(s) hosts are allowed")
    try:
        max_pages = max(1, min(int(max_pages), HARD_PAGE_CAP))
    except (TypeError, ValueError):
        max_pages = 200

    cookies = await _live_cookies() if authed else []
    t0 = time.monotonic()
    source, urls, full = await asyncio.to_thread(_discover, base, obey_robots, max_pages)

    ingested = 0
    if source == "llms-full" and full:
        for title, md in _split_llms_full(full)[:max_pages]:
            _store_page(site, f"{base}#{_safe(title)[:40]}" if title else base, title, "llms-full", md)
            ingested += 1
        store.execute("INSERT INTO sites(site,root_url,pages,source,ingested_at) VALUES(?,?,?,?,?) "
                      "ON CONFLICT(site) DO UPDATE SET pages=excluded.pages, source=excluded.source, "
                      "ingested_at=excluded.ingested_at", (site, base, ingested, source, _now()))
        return ok(site=site, source=source, pages_ingested=ingested,
                  tokens=sum(len(m) // 4 for _, m in _split_llms_full(full)),
                  seconds=round(time.monotonic() - t0, 1),
                  note="ingested the whole docs from llms-full.txt")

    rows = store.query("SELECT url,etag,modified FROM pages WHERE site=?", (site,))
    known = {r["url"]: (r["etag"] or "", r["modified"] or "") for r in rows}
    if not refresh:
        urls = [u for u in urls if u not in known] or urls
    # on refresh, send conditional-GET validators so unchanged pages return 304 (skipped)
    cond = known if refresh else {}
    pages = await asyncio.to_thread(_crawl_blocking, urls[:max_pages], cookies, base,
                                    obey_robots, DEFAULT_WORKERS, render, cond)
    tokens = 0
    unchanged = 0
    for p in pages:
        if p.get("not_modified"):
            unchanged += 1
            continue
        _store_page(site, p["url"], p["title"], "", p["markdown"], p.get("etag", ""), p.get("modified", ""))
        tokens += len(p["markdown"]) // 4
        ingested += 1
    total = (store.query_one("SELECT COUNT(*) c FROM pages WHERE site=?", (site,)) or {}).get("c", ingested)
    store.execute("INSERT INTO sites(site,root_url,pages,source,ingested_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(site) DO UPDATE SET pages=excluded.pages, source=excluded.source, "
                  "ingested_at=excluded.ingested_at", (site, base, total, source, _now()))
    return ok(site=site, source=source, pages_ingested=ingested, unchanged=unchanged,
              discovered=len(urls), tokens=tokens, embedded=embed.available(),
              seconds=round(time.monotonic() - t0, 1), sample_urls=[p["url"] for p in pages[:10]])


@mcp.tool
async def map_site(url: str, obey_robots: bool = True, max_pages: int = 1000) -> dict:
    """Discover a site's doc URLs WITHOUT fetching bodies. Reports which source was used."""
    base = _norm(url)
    if _is_internal_host(_host(base)):
        return err("only public http(s) hosts are allowed")
    try:
        max_pages = max(1, min(int(max_pages), HARD_PAGE_CAP))
    except (TypeError, ValueError):
        max_pages = 1000
    source, urls, full = await asyncio.to_thread(_discover, base, obey_robots, max_pages)
    return ok(site=_host(base), source=source, count=(1 if source == "llms-full" else len(urls)),
              urls=([base] if source == "llms-full" else urls[:500]),
              note="llms-full.txt found — the whole docs is in one file" if source == "llms-full" else None)


@mcp.tool
async def crawl_site(url: str, max_pages: int = 200, authed: bool = False, obey_robots: bool = True,
                     render: str = "auto") -> dict:
    """Crawl an entire site (not restricted to a docs path) into the corpus via same-host BFS."""
    base = _norm(url)
    site = _host(base)
    if not site or _is_internal_host(site):
        return err("only public http(s) hosts are allowed")
    try:
        max_pages = max(1, min(int(max_pages), HARD_PAGE_CAP))
    except (TypeError, ValueError):
        max_pages = 200
    cookies = await _live_cookies() if authed else []

    def bfs():
        rp = _robots(base, obey_robots)
        seen, queue, urls = {base}, [base], []
        while queue and len(urls) < max_pages:
            cur = queue.pop(0)
            if not _allowed(rp, cur):
                continue
            html = _get_text(cur)
            if not html:
                continue
            urls.append(cur)
            for ln in scrape.links(html, cur)["internal"]:
                ln = ln.split("#")[0]
                if ln not in seen and _host(ln) == site:
                    seen.add(ln)
                    queue.append(ln)
        return urls

    urls = await asyncio.to_thread(bfs)
    pages = await asyncio.to_thread(_crawl_blocking, urls, cookies, base, obey_robots,
                                    DEFAULT_WORKERS, render)
    n = 0
    for p in pages:
        if p.get("not_modified"):
            continue
        _store_page(site, p["url"], p["title"], "", p["markdown"], p.get("etag", ""), p.get("modified", ""))
        n += 1
    store.execute("INSERT INTO sites(site,root_url,pages,source,ingested_at) VALUES(?,?,?,?,?) "
                  "ON CONFLICT(site) DO UPDATE SET pages=excluded.pages, source=excluded.source, "
                  "ingested_at=excluded.ingested_at", (site, base, n, "crawl", _now()))
    return ok(site=site, pages_ingested=n, embedded=embed.available(),
              sample_urls=[p["url"] for p in pages[:10]])


@mcp.tool
async def read_page(url: str, render: str = "auto", authed: bool = False) -> dict:
    """Ingest a single page into the corpus and return its clean markdown."""
    base = _norm(url)
    if _is_internal_host(_host(base)):
        return err("only public http(s) hosts are allowed")
    cookies = await _live_cookies() if authed else []
    p = await asyncio.to_thread(_fetch_page, base, cookies, None, render)
    if not p or p.get("not_modified"):
        return err("could not fetch page")
    _store_page(_host(base), p["url"], p["title"], "", p["markdown"], p.get("etag", ""), p.get("modified", ""))
    return ok(url=p["url"], title=p["title"], chars=len(p["markdown"]), markdown=p["markdown"])


def _fts_query(q: str) -> str:
    # keep alnum tokens, OR them so partial queries still match
    toks = re.findall(r"[A-Za-z0-9_]+", q or "")
    return " OR ".join(toks) if toks else (q or "")


def _chunks_fts(query: str, site: str, n: int) -> list[dict]:
    """BM25 search over chunk passages -> ranked rows (chunk id, url, heading, snippet)."""
    try:
        sql = ("SELECT c.id, c.url, c.heading, snippet(chunks_fts,0,'«','»',' … ',14) AS snippet "
               "FROM chunks_fts f JOIN chunks c ON c.id=f.rowid WHERE chunks_fts MATCH ?")
        params: list = [_fts_query(query)]
        if site:
            sql += " AND c.site=?"
            params.append(site)
        sql += " ORDER BY rank LIMIT ?"
        params.append(n)
        return store.query(sql, tuple(params))
    except Exception:
        like = f"%{query}%"
        return store.query(
            "SELECT id,url,heading,substr(text,1,260) AS snippet FROM chunks "
            "WHERE text LIKE ?" + (" AND site=?" if site else "") + " LIMIT ?",
            ((like, site, n) if site else (like, n)))


def _vec_rank(query: str, site: str, n: int) -> list[dict]:
    """Cosine-rank chunk embeddings against the query. [] if no model/embeddings."""
    qv = embed.encode_one(query) if embed.available() else None
    if not qv:
        return []
    rows = store.query(
        "SELECT id,url,heading,text,embedding FROM chunks WHERE embedding IS NOT NULL"
        + (" AND site=?" if site else "") + " LIMIT 6000", ((site,) if site else ()))
    scored = []
    for r in rows:
        v = embed.unpack(r["embedding"])
        if len(v) == len(qv):
            scored.append((embed.cosine(qv, v), r))
    scored.sort(key=lambda x: -x[0])
    return [{"id": r["id"], "url": r["url"], "heading": r["heading"],
             "snippet": (r["text"] or "")[:260], "cos": round(s, 4)} for s, r in scored[:n]]


def _hybrid(query: str, site: str, limit: int) -> tuple[list[dict], str]:
    """Fuse BM25 (chunks_fts) + vector cosine via reciprocal-rank fusion. Returns (results, mode)."""
    fts = _chunks_fts(query, site, 50)
    fts_rank = {r["id"]: i for i, r in enumerate(fts)}
    meta = {r["id"]: {"id": r["id"], "url": r["url"], "heading": r["heading"],
                      "snippet": r["snippet"]} for r in fts}
    vec = _vec_rank(query, site, 50)
    vec_rank = {r["id"]: i for i, r in enumerate(vec)}
    for r in vec:
        meta.setdefault(r["id"], {"id": r["id"], "url": r["url"], "heading": r["heading"],
                                  "snippet": r["snippet"]})
    fused = []
    for cid in set(fts_rank) | set(vec_rank):
        score = 0.0
        if cid in fts_rank:
            score += 1.0 / (60 + fts_rank[cid])
        if cid in vec_rank:
            score += 1.0 / (60 + vec_rank[cid])
        fused.append((score, meta[cid]))
    fused.sort(key=lambda x: -x[0])
    mode = "hybrid" if vec else "keyword"
    return [{**m, "score": round(s, 5)} for s, m in fused[:limit]], mode


@mcp.tool
def search_docs(query: str, site: str = "", limit: int = 20) -> dict:
    """Hybrid search over the corpus: BM25 keyword + semantic vector similarity (reciprocal-rank
    fusion), returning ranked PASSAGES {url, heading, snippet, score}. Falls back to keyword-only when
    no embedding model is installed. Optionally scope to a site."""
    query = (query or "").strip()
    if not query:
        return err("query is required")
    try:
        limit = max(1, min(int(limit), 100))
    except (TypeError, ValueError):
        limit = 20
    results, mode = _hybrid(query, _host(site) if site else "", limit)
    return ok(query=query, mode=mode, count=len(results), results=results)


@mcp.tool
def ask(query: str, site: str = "", k: int = 6) -> dict:
    """Answer-surface: return the top-k most relevant PASSAGES (not whole pages) with their heading,
    source URL, and score — precise context to read and answer from. Hybrid-ranked. 'read the docs and
    tell me' in one call."""
    try:
        k = max(1, min(int(k), 20))
    except (TypeError, ValueError):
        k = 6
    results, mode = _hybrid((query or "").strip(), _host(site) if site else "", k)
    if not results:
        return err("no matching docs — read_docs(url) first?", query=query)
    passages = []
    for r in results:
        row = store.query_one("SELECT text FROM chunks WHERE id=?", (r["id"],))
        passages.append({"heading": r["heading"], "url": r["url"], "score": r["score"],
                         "text": (row["text"] if row else r["snippet"])[:4000]})
    return ok(query=query, mode=mode, sources=[p["url"] for p in passages], passages=passages)


@mcp.tool
def embed_site(site: str = "", refresh: bool = False) -> dict:
    """(Re)build semantic embeddings for stored pages so search/ask use vector ranking. Needs a local
    model (uv sync --group embed). With no model, reports unavailable. refresh=True re-embeds all."""
    if not embed.available():
        return err("no embedding model installed", engine="unavailable",
                   hint="uv sync --group embed (model2vec, free, ~30MB) then call again")
    site = _host(site) if site else ""
    where = "WHERE site=?" if site else ""
    pages = store.query(f"SELECT id,site,url,markdown FROM pages {where}", ((site,) if site else ()))
    done = chunks_n = 0
    for p in pages:
        if not refresh:
            has = store.query_one("SELECT COUNT(*) c FROM chunks WHERE page_id=? AND embedding IS NOT NULL",
                                  (p["id"],))
            if has and has["c"]:
                continue
        chunks_n += _embed_chunks_for(p["id"], p["site"], p["url"], p["markdown"])
        done += 1
    return ok(engine=embed.model_name(), pages_embedded=done, chunks=chunks_n,
              total_pages=len(pages))


@mcp.tool
def get_doc(url: str = "", id: int = 0) -> dict:
    """Return the full stored markdown of a page by url or id."""
    row = (store.query_one("SELECT * FROM pages WHERE id=?", (id,)) if id
           else store.query_one("SELECT * FROM pages WHERE url=?", (_norm(url),)))
    if not row:
        return err("not in corpus", hint="read_docs / read_page it first")
    return ok(url=row["url"], title=row["title"], site=row["site"],
              chars=len(row["markdown"]), markdown=row["markdown"])


@mcp.tool
def list_sites() -> dict:
    """List ingested sites with page counts + the discovery source used."""
    return ok(sites=store.query("SELECT site,root_url,pages,source,ingested_at FROM sites ORDER BY ingested_at DESC"))


@mcp.tool
def site_index(site: str, limit: int = 500) -> dict:
    """List the pages ingested for a site (its table of contents)."""
    site = _host(site) or site
    try:
        limit = max(1, min(int(limit), 2000))
    except (TypeError, ValueError):
        limit = 500
    rows = store.query("SELECT url,title,tokens FROM pages WHERE site=? ORDER BY url LIMIT ?", (site, limit))
    return ok(site=site, count=len(rows), pages=rows)


@mcp.tool
def export_corpus(site: str, path: str = "") -> dict:
    """Write a site's whole corpus to one markdown bundle on disk; returns the path."""
    site = _host(site) or site
    rows = store.query("SELECT url,title,markdown FROM pages WHERE site=? ORDER BY url", (site,))
    if not rows:
        return err(f"no pages stored for {site}")
    body = "\n\n---\n\n".join(f"# {r['title'] or r['url']}\n<{r['url']}>\n\n{r['markdown']}" for r in rows)
    EXPORTS_DIR.mkdir(parents=True, exist_ok=True)
    target = (data_dir("webengine") / path) if path else (EXPORTS_DIR / f"{_safe(site)}.md")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(body, encoding="utf-8")
    return ok(path=str(target), pages=len(rows), chars=len(body))


@mcp.tool
def forget_site(site: str, confirm: bool = False) -> dict:
    """Delete a site's pages from the corpus. Requires confirm=True."""
    site = _host(site) or site
    if not confirm:
        return err("confirm required", blocked=True, hint=f"call again with confirm=True to forget {site}")
    ids = [r["id"] for r in store.query("SELECT id FROM pages WHERE site=?", (site,))]
    for pid in ids:
        store.execute("DELETE FROM docs_fts WHERE rowid=?", (pid,))
    for cid in [r["id"] for r in store.query("SELECT id FROM chunks WHERE site=?", (site,))]:
        store.execute("DELETE FROM chunks_fts WHERE rowid=?", (cid,))
    store.execute("DELETE FROM chunks WHERE site=?", (site,))
    store.execute("DELETE FROM pages WHERE site=?", (site,))
    store.execute("DELETE FROM sites WHERE site=?", (site,))
    return ok(forgot=site, pages_removed=len(ids))


if __name__ == "__main__":
    mcp.run()
