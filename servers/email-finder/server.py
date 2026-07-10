"""email-finder — resolve & verify a person's work email for FREE (no paid reveal).

Strategy, cheapest-first: published emails on the company about/team page -> GitHub commit emails
(great for technical founders) -> name+domain pattern generation -> local syntax/MX gate -> catch-all
detection + best-effort self-hosted SMTP probe -> free-tier verifier APIs (Reoon 600/mo, Hunter 50/mo,
Tomba). Results are cached in SQLite. Returns a confidence score, not a binary — realistic coverage is
~50-70%, honest about Gmail/M365/catch-all walls.
"""
from __future__ import annotations

import ipaddress
import random
import re
import smtplib
import string
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

import httpx
from mcp_base import BaseStore, data_dir, db_path, get_env, http, make_server

MAX_PAGE_BYTES = 2_000_000  # cap each scraped page to avoid memory blowups
GITHUB_USER_RE = re.compile(r"^[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})$")


def _is_internal_host(host: str) -> bool:
    """True if host is localhost / a private/loopback/link-local IP (SSRF guard)."""
    h = (host or "").strip().lower().rstrip(".")
    if not h or h in ("localhost",) or h.endswith(".localhost") or h.endswith(".local"):
        return True
    if h.endswith(".internal"):
        return True
    try:
        ip = ipaddress.ip_address(h)
        return (ip.is_private or ip.is_loopback or ip.is_link_local
                or ip.is_reserved or ip.is_multicast or ip.is_unspecified)
    except ValueError:
        return False  # a normal hostname; DNS pinning is out of scope here

mcp = make_server(
    "email-finder",
    instructions=("Free email resolution. find(name, company, domain) orchestrates everything "
                  "(Hunter pattern + site scrape + GitHub + free web search + patterns, verifies all, "
                  "returns best + ranked candidates[]); guess() for patterns, verify() to check "
                  "deliverability, from_github() for devs, scrape_site() and search_web() to harvest "
                  "published emails, bulk_find() for batches."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_cache(
  email TEXT PRIMARY KEY, deliverable INTEGER, confidence TEXT, checks TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS found_emails(
  id INTEGER PRIMARY KEY, name TEXT, domain TEXT, email TEXT, source TEXT, confidence TEXT,
  found_at TEXT, UNIQUE(email)
);
CREATE TABLE IF NOT EXISTS domain_patterns(
  domain TEXT PRIMARY KEY, pattern TEXT, hits INTEGER DEFAULT 1, updated_at TEXT
);
"""
store = BaseStore(db_path("email-finder"), schema=SCHEMA)

CACHE_TTL_DAYS = 14
BIG_HOSTS = ("google", "gmail", "outlook", "microsoft", "office365", "protonmail", "zoho", "yahoo")
ROLE_LOCALS = {"info", "sales", "support", "hello", "contact", "admin", "team", "help",
               "office", "press", "media", "jobs", "careers", "hr", "billing", "no-reply",
               "noreply", "marketing", "enquiries", "inquiries"}
DISPOSABLE_DOMAINS = {"mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com",
                      "throwaway.email", "trashmail.com", "yopmail.com", "getnada.com",
                      "temp-mail.org", "fakeinbox.com", "sharklasers.com"}
EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _split_name(name: str) -> tuple[str, str]:
    parts = [p for p in re.split(r"\s+", name.strip()) if p]
    if not parts:
        return "", ""
    first = re.sub(r"[^a-z]", "", parts[0].lower())
    last = re.sub(r"[^a-z]", "", parts[-1].lower()) if len(parts) > 1 else ""
    return first, last


def _patterns(name: str, domain: str) -> list[str]:
    first, last = _split_name(name)
    domain = domain.lower().lstrip("@")
    if not first or not domain:
        return []
    fi, li = first[:1], (last[:1] if last else "")
    raw = [
        f"{first}.{last}", f"{first}", f"{fi}{last}", f"{first}{last}",
        f"{first}_{last}", f"{fi}.{last}", f"{first}{li}", f"{first}-{last}",
        f"{last}.{first}", f"{last}{first}", f"{last}{fi}", f"{li}{first}", f"{last}",
    ]
    seen, out = set(), []
    for local in raw:
        local = local.strip(".-_").replace("..", ".")
        if local and local not in seen:
            seen.add(local)
            out.append(f"{local}@{domain}")
    return out


@mcp.tool
def guess(name: str, domain: str) -> dict:
    """Generate ranked likely email patterns for a person at a domain (most common first)."""
    cands = _patterns(name, domain)
    return {"name": name, "domain": domain, "candidates": cands}


@mcp.tool
def mx(domain: str) -> dict:
    """Look up a domain's MX records (tells you the mail host / whether email is even possible)."""
    try:
        import dns.resolver
        recs = sorted(dns.resolver.resolve(domain, "MX"), key=lambda r: r.preference)
        hosts = [str(r.exchange).rstrip(".") for r in recs]
        host_str = " ".join(hosts).lower()
        return {"domain": domain, "mx": hosts,
                "big_host": any(b in host_str for b in BIG_HOSTS)}
    except Exception as e:
        return {"domain": domain, "mx": [], "error": str(e)}


def _is_role(email: str) -> bool:
    return email.split("@", 1)[0].lower() in ROLE_LOCALS


def _is_disposable(domain: str) -> bool:
    return domain.lower() in DISPOSABLE_DOMAINS


def _smtp_rcpt(host: str, email: str, ports: tuple = (25, 587, 465)) -> int | None:
    """RCPT-probe a mailbox across common SMTP ports (25/587 plaintext, 465 SSL). First definitive
    code wins; None if every port is blocked/unreachable."""
    for port in ports:
        try:
            cls = smtplib.SMTP_SSL if port == 465 else smtplib.SMTP
            with cls(host, port, timeout=10) as s:
                s.helo("example.com")
                s.mail("verify@example.com")
                code, _ = s.rcpt(email)
                if code:
                    return code
        except Exception:
            continue
    return None


def _smtp_probe(mx_hosts: list, email: str) -> int | None:
    """Try the first few MX hosts in preference order; return the first definitive RCPT code."""
    for host in (mx_hosts or [])[:3]:
        code = _smtp_rcpt(host, email)
        if code is not None:
            return code
    return None


def _catch_all(mx_hosts: list, domain: str) -> bool | None:
    """Catch-all only if TWO distinct random local-parts are both accepted (kills greylisting and
    one-off false positives). None if any probe is inconclusive."""
    accepts = 0
    for _ in range(2):
        rnd = "".join(random.choices(string.ascii_lowercase, k=16))
        code = _smtp_probe(mx_hosts, f"{rnd}@{domain}")
        if code is None:
            return None
        if code in (250, 251):
            accepts += 1
    return accepts == 2


def _cache_get(email: str) -> dict | None:
    row = store.query_one("SELECT * FROM verify_cache WHERE email=?", (email,))
    if not row:
        return None
    try:
        checked = datetime.fromisoformat(row["checked_at"])
        if datetime.now(timezone.utc) - checked > timedelta(days=CACHE_TTL_DAYS):
            return None
    except Exception:
        return None
    import json
    deliverable = row["deliverable"]
    return {"email": email, "cached": True, "confidence": row["confidence"],
            "deliverable": None if deliverable is None else bool(deliverable),
            "checks": json.loads(row["checks"] or "{}")}


def _cache_put(result: dict) -> None:
    import json
    d = result.get("deliverable")
    store.execute(
        "INSERT OR REPLACE INTO verify_cache(email,deliverable,confidence,checks,checked_at) "
        "VALUES(?,?,?,?,?)",
        (result["email"], None if d is None else int(d), result.get("confidence", "low"),
         json.dumps(result.get("checks", {})), _now()),
    )


@mcp.tool
def verify(email: str, check_smtp: bool = True, use_cache: bool = True) -> dict:
    """Verify an email: syntax + MX, role/disposable flags, catch-all + self-hosted SMTP probe, and
    Reoon/Hunter/Tomba if keys set. Returns deliverability with a confidence score. Honest: big
    hosts/catch-all are 'inconclusive'. Cached in SQLite (TTL 14d)."""
    email = (email or "").strip()
    if not email or "@" not in email:
        return {"email": email, "deliverable": False, "confidence": "high",
                "checks": {"syntax": "bad: not an email"}}
    if use_cache:
        cached = _cache_get(email)
        if cached:
            return cached

    result: dict = {"email": email, "confidence": "low", "checks": {}}
    # syntax
    try:
        from email_validator import validate_email
        validate_email(email, check_deliverability=False)
        result["checks"]["syntax"] = "ok"
    except Exception as e:  # noqa: BLE001
        result["checks"]["syntax"] = f"bad: {e}"
        result["deliverable"] = False
        result["confidence"] = "high"
        return result

    domain = email.split("@", 1)[1]
    result["checks"]["role_account"] = _is_role(email)
    if _is_disposable(domain):
        result.update(deliverable=False, confidence="high")
        result["checks"]["disposable"] = True
        _cache_put(result)
        return result

    mxinfo = mx(domain)
    result["checks"]["mx"] = "ok" if mxinfo.get("mx") else "none"
    if not mxinfo.get("mx"):
        result.update(deliverable=False, confidence="high")
        _cache_put(result)
        return result
    big = mxinfo.get("big_host")

    # free-tier verifier APIs (best signal if configured)
    reoon = get_env("REOON_API_KEY")
    if reoon:
        try:
            data = http.get_json("https://emailverifier.reoon.com/api/v1/verify",
                                 params={"email": email, "key": reoon, "mode": "power"},
                                 timeout=20, cache_ttl=0.0)
            if not isinstance(data, dict):
                data = {}
            result["checks"]["reoon"] = data.get("status")
            if data.get("status") == "valid":
                result.update(deliverable=True, confidence="high")
                _cache_put(result)
                return result
            if data.get("status") in ("invalid", "disabled"):
                result.update(deliverable=False, confidence="high")
                _cache_put(result)
                return result
        except Exception as e:  # noqa: BLE001
            result["checks"]["reoon"] = f"error: {e}"

    hunter = get_env("HUNTER_API_KEY")
    if hunter:
        try:
            payload = http.get_json("https://api.hunter.io/v2/email-verifier",
                                    params={"email": email, "api_key": hunter},
                                    timeout=20, cache_ttl=0.0)
            data = (payload or {}).get("data", {}) if isinstance(payload, dict) else {}
            status = data.get("status")
            result["checks"]["hunter"] = status
            if status in ("valid",):
                result.update(deliverable=True, confidence="high")
                _cache_put(result)
                return result
            if status in ("invalid",):
                result.update(deliverable=False, confidence="high")
                _cache_put(result)
                return result
        except Exception as e:  # noqa: BLE001
            result["checks"]["hunter"] = f"error: {e}"

    tomba = get_env("TOMBA_API_KEY")
    tomba_secret = get_env("TOMBA_SECRET")
    if tomba and tomba_secret:
        try:
            payload = http.get_json(f"https://api.tomba.io/v1/email-verifier/{email}",
                                    headers={"X-Tomba-Key": tomba, "X-Tomba-Secret": tomba_secret},
                                    timeout=20, cache_ttl=0.0)
            data = (payload or {}).get("data", {}) if isinstance(payload, dict) else {}
            res = (data.get("email") or {}).get("result") or data.get("result")
            result["checks"]["tomba"] = res
            if res == "deliverable":
                result.update(deliverable=True, confidence="high")
                _cache_put(result)
                return result
            if res == "undeliverable":
                result.update(deliverable=False, confidence="high")
                _cache_put(result)
                return result
        except Exception as e:  # noqa: BLE001
            result["checks"]["tomba"] = f"error: {e}"

    # self-hosted SMTP RCPT probe + catch-all detection (best-effort; unreliable on big hosts)
    if check_smtp and not big:
        mx_hosts = mxinfo["mx"][:3]
        is_catch = _catch_all(mx_hosts, domain)
        if is_catch is not None:
            result["checks"]["catch_all"] = is_catch
        if is_catch:
            result.update(deliverable=None, confidence="low",
                          note="catch-all domain: any address accepted, cannot confirm mailbox")
            _cache_put(result)
            return result
        code = _smtp_probe(mx_hosts, email)
        if code is not None:
            result["checks"]["smtp"] = code
            if code in (250, 251):
                result.update(deliverable=True, confidence="medium")
                _cache_put(result)
                return result
            if code in (550, 551, 553):
                result.update(deliverable=False, confidence="medium")
                _cache_put(result)
                return result
        else:
            result["checks"]["smtp"] = "inconclusive"

    # couldn't confirm: pattern is plausible (MX exists) but unverifiable
    result.update(deliverable=None, confidence="low" if big else "medium",
                  note="MX exists but mailbox unverifiable (big host/catch-all/blocked SMTP)")
    _cache_put(result)
    return result


@mcp.tool
def confidence_breakdown(email: str) -> dict:
    """Explain the verification signals for an email without short-circuiting: returns each check
    (syntax, role, disposable, mx, host type) so you can see why a score is what it is."""
    out: dict = {"email": email, "signals": {}}
    try:
        from email_validator import validate_email
        validate_email(email, check_deliverability=False)
        out["signals"]["syntax"] = "ok"
    except Exception as e:  # noqa: BLE001
        out["signals"]["syntax"] = f"bad: {e}"
        return out
    domain = email.split("@", 1)[1]
    out["signals"]["role_account"] = _is_role(email)
    out["signals"]["disposable"] = _is_disposable(domain)
    mxinfo = mx(domain)
    out["signals"]["mx_count"] = len(mxinfo.get("mx", []))
    out["signals"]["big_host"] = mxinfo.get("big_host", False)
    return out


@mcp.tool
def from_github(username: str, max_events: int = 30) -> dict:
    """Find a developer's email from their public GitHub commit history (events API + commit
    authors). Strong for technical founders/CTOs. Uses your free PAT for higher rate limits."""
    username = (username or "").strip()
    if not GITHUB_USER_RE.match(username):
        return {"username": username, "emails": [], "error": "invalid github username"}
    try:
        max_events = max(1, min(int(max_events), 100))
    except (TypeError, ValueError):
        max_events = 30
    token = get_env("GITHUB_PERSONAL_ACCESS_TOKEN")
    headers = {"Accept": "application/vnd.github+json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    emails: dict[str, int] = {}
    rate_limited = False

    def _add(em: str, w: int = 1) -> None:
        em = (em or "").strip().lower()
        if em and "@" in em and "noreply.github.com" not in em and "users.noreply" not in em:
            emails[em] = emails.get(em, 0) + w

    def _get(url: str, params: dict | None = None):
        nonlocal rate_limited
        r = http.request("GET", url, headers=headers, params=params, timeout=20)
        if r.get("status") in (403, 429):
            rate_limited = True
        return r.get("json") if r.get("status") == 200 else None

    # 1) public push events (recent commit authors)
    try:
        events = _get(f"https://api.github.com/users/{username}/events/public",
                      {"per_page": max_events})
        for ev in events or []:
            for commit in (ev.get("payload", {}) or {}).get("commits", []) or []:
                _add((commit.get("author", {}) or {}).get("email", ""))
    except Exception:
        pass

    # 2) public profile email (set by some users)
    try:
        prof = _get(f"https://api.github.com/users/{username}")
        if isinstance(prof, dict):
            _add(prof.get("email") or "", 3)
    except Exception:
        pass

    # 3) commits the user authored in their recently-pushed repos
    try:
        repos = _get(f"https://api.github.com/users/{username}/repos",
                     {"sort": "pushed", "per_page": 5}) or []
        for repo in repos[:5]:
            full = repo.get("full_name")
            if not full:
                continue
            commits = _get(f"https://api.github.com/repos/{full}/commits",
                           {"author": username, "per_page": 10}) or []
            for c in commits:
                _add(((c.get("commit", {}) or {}).get("author", {}) or {}).get("email", ""))
    except Exception:
        pass

    ranked = sorted(emails.items(), key=lambda kv: -kv[1])
    out: dict = {"username": username, "emails": [{"email": e, "commits": n} for e, n in ranked]}
    if not emails and rate_limited:
        out["error"] = "github rate-limited"
    if rate_limited:
        out["rate_limited"] = True
    return out


def _emails_from_html(html: str) -> dict[str, int]:
    """Extract emails from one page's HTML: mailto links (weight 2) + plain-text matches (weight 1).
    Returns {email: weight}. Lazy BeautifulSoup; falls back to regex on the raw text."""
    found: dict[str, int] = {}
    if not html:
        return found
    text = html
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html, "html.parser")
        for a in soup.select("a[href^=mailto]"):
            em = a.get("href", "")[7:].split("?")[0].strip().lower()
            if em:
                found[em] = found.get(em, 0) + 2  # mailto weighted higher
        text = soup.get_text(" ")
    except Exception:
        pass
    for em in EMAIL_RE.findall(text):
        found[em.lower()] = found.get(em.lower(), 0) + 1
    return found


def _fetch_page_text(url: str, timeout: float = 15.0) -> str:
    """Fetch one page's HTML via hardened shared fetch (redirects, retries, SSRF guard)."""
    from mcp_base.fetch import fetch as hfetch

    r = hfetch(url, timeout=timeout, max_bytes=MAX_PAGE_BYTES)
    if not r.get("ok"):
        return ""
    return (r.get("html") or "")[:MAX_PAGE_BYTES]


@mcp.tool
def scrape_site(domain: str, max_pages: int = 5) -> dict:
    """Scrape a company's homepage + about/team/contact/people pages for published emails (mailto +
    text) on the company's own domain. Free, no key. Often the fastest way to a real address."""
    domain = (domain or "").strip()
    if not domain:
        return {"domain": "", "pages_scanned": 0, "emails": [], "error": "domain is required"}
    base = domain if domain.startswith("http") else f"https://{domain.lstrip('@')}"
    parts = urlsplit(base)
    if parts.scheme not in ("http", "https"):
        return {"domain": domain, "pages_scanned": 0, "emails": [],
                "error": "only http(s) URLs are allowed"}
    host = (parts.hostname or "").lower()
    if not host or _is_internal_host(host):
        return {"domain": host, "pages_scanned": 0, "emails": [],
                "error": "refusing to scrape internal/private host"}
    try:
        max_pages = max(1, min(int(max_pages), 8))
    except (TypeError, ValueError):
        max_pages = 5
    paths = ["", "/about", "/about-us", "/team", "/contact", "/people", "/company", "/leadership"]
    found: dict[str, int] = {}
    pages_hit = 0
    for path in paths[:max_pages]:
        html = _fetch_page_text(base.rstrip("/") + path)
        if not html:
            continue
        pages_hit += 1
        for em, w in _emails_from_html(html).items():
            found[em] = found.get(em, 0) + w
    on_domain = {e: n for e, n in found.items() if e.split("@")[-1].lower().endswith(host)}
    ranked = sorted((on_domain or found).items(), key=lambda kv: -kv[1])
    return {"domain": host, "pages_scanned": pages_hit,
            "emails": [{"email": e, "weight": n, "role": _is_role(e)} for e, n in ranked]}


@mcp.tool
def hunter_domain_search(domain: str, limit: int = 10) -> dict:
    """Hunter.io free-tier domain search (HUNTER_API_KEY required). Returns known emails + the
    detected pattern for the domain. Graceful hint if no key."""
    key = get_env("HUNTER_API_KEY")
    if not key:
        return {"error": "no HUNTER_API_KEY",
                "hint": "Add a free Hunter API key to .env, or use scrape_site() / find()."}
    try:
        payload = http.get_json("https://api.hunter.io/v2/domain-search",
                                params={"domain": domain, "api_key": key, "limit": limit},
                                timeout=20, cache_ttl=0.0)
        data = (payload or {}).get("data", {}) if isinstance(payload, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"domain": domain, "pattern": data.get("pattern"),
            "emails": [{"email": e.get("value"), "type": e.get("type"),
                        "first_name": e.get("first_name"), "last_name": e.get("last_name"),
                        "position": e.get("position"), "confidence": e.get("confidence")}
                       for e in (data.get("emails", []) or [])]}


@mcp.tool
def tomba_find(name: str, domain: str) -> dict:
    """Tomba.io email finder (FREE 50/mo; needs TOMBA_API_KEY + TOMBA_SECRET). Returns the real
    email Tomba has for a person at a domain, with a confidence score + the public sources it was
    seen on. A genuine finder database (not just a guess). Graceful hint if no key."""
    key, secret = get_env("TOMBA_API_KEY"), get_env("TOMBA_SECRET")
    if not (key and secret):
        return {"error": "no TOMBA_API_KEY/TOMBA_SECRET",
                "hint": "Free 50/mo at tomba.io — sign up with a work/school email (Gmail is blocked), "
                        "then add TOMBA_API_KEY + TOMBA_SECRET to .env and reconnect."}
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    if not first or not dom:
        return {"error": "need a name and a domain"}
    try:
        payload = http.get_json(f"https://api.tomba.io/v1/email-finder/{dom}",
                                params={"first_name": first, "last_name": last},
                                headers={"X-Tomba-Key": key, "X-Tomba-Secret": secret},
                                timeout=20, cache_ttl=3600.0)
        data = (payload or {}).get("data", {}) if isinstance(payload, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": data.get("email"), "score": data.get("score"),
            "sources": [s.get("uri") or s.get("url") for s in (data.get("sources") or [])][:5]}


def _record_found(name: str, domain: str, email: str, source: str, confidence: str) -> None:
    try:
        store.execute(
            "INSERT OR IGNORE INTO found_emails(name,domain,email,source,confidence,found_at) "
            "VALUES(?,?,?,?,?,?)", (name, domain, email, source, confidence, _now()))
    except Exception:
        pass


def _decode_ddg_href(href: str) -> str:
    """Resolve a DuckDuckGo result href to its real http(s) target, or '' if not usable.
    DDG wraps targets as //duckduckgo.com/l/?uddg=<urlencoded-target>."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
    except Exception:
        return ""
    if parts.path.startswith("/l/") or "uddg=" in (parts.query or ""):
        target = (parse_qs(parts.query).get("uddg") or [""])[0]
        return unquote(target) if target else ""
    if parts.scheme in ("http", "https") and parts.hostname and "duckduckgo.com" not in parts.hostname:
        return href
    return ""


def _ddg_result_links(query: str, max_links: int = 5) -> list[str]:
    """Keyless DuckDuckGo HTML search -> decoded result target URLs. [] on failure/rate-limit."""
    headers = {"User-Agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
                             "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36"}
    links: list[str] = []
    for endpoint in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            html_text = http.get_text(endpoint, params={"q": query}, headers=headers,
                                      timeout=20, cache_ttl=900.0)
            if not html_text:
                continue
            try:
                from bs4 import BeautifulSoup
                anchors = [a.get("href", "") for a in BeautifulSoup(html_text, "html.parser")
                           .find_all("a", href=True)]
            except Exception:
                anchors = re.findall(r'href="([^"]+)"', html_text)
            for href in anchors:
                u = _decode_ddg_href(href)
                if u and u not in links:
                    links.append(u)
                if len(links) >= max_links:
                    break
            if links:
                break
        except Exception:
            continue
    return links[:max_links]


_BROWSER_UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 "
               "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
_SEARCH_HOST_HINTS = ("duckduckgo.com", "bing.com", "mojeek.com", "google.", "yahoo.com",
                      "microsoft.com", "msn.com")


def _decode_bing_u(u: str) -> str:
    """Decode Bing's /ck/a 'u' redirect param (base64url, 'a1' prefix). '' on failure."""
    import base64
    try:
        if u.startswith("a1"):
            u = u[2:]
        return base64.urlsafe_b64decode(u + "=" * (-len(u) % 4)).decode("utf-8", "ignore")
    except Exception:
        return ""


def _norm_result_link(href: str) -> str:
    """Normalize a Bing/Mojeek result href to an external http(s) URL, or '' to skip.
    Unwraps Bing /ck/a redirects; rejects internal hosts and search-engine self-links."""
    if not href:
        return ""
    if href.startswith("//"):
        href = "https:" + href
    try:
        parts = urlsplit(href)
    except Exception:
        return ""
    if "bing.com" in (parts.hostname or "").lower() and parts.path.startswith("/ck/"):
        href = _decode_bing_u((parse_qs(parts.query).get("u") or [""])[0])
        if not href:
            return ""
    try:
        p = urlsplit(href if href.startswith("http") else "https://" + href)
    except Exception:
        return ""
    h = (p.hostname or "").lower()
    if (p.scheme not in ("http", "https") or not h or _is_internal_host(h)
            or any(s in h for s in _SEARCH_HOST_HINTS)):
        return ""
    return href


def _engine_html(url: str, params: dict) -> str:
    """Fetch raw search-engine HTML. '' on failure."""
    try:
        return http.get_text(url, params=params, headers={"User-Agent": _BROWSER_UA},
                             timeout=20, cache_ttl=900.0) or ""
    except Exception:
        return ""


def _engine_links(url: str, params: dict, selectors: list[str], max_links: int) -> list[str]:
    """Run one keyless search engine, parse result anchors, return normalized target URLs."""
    html_text = _engine_html(url, params)
    if not html_text:
        return []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        anchors: list = []
        for sel in selectors:
            anchors = soup.select(sel)
            if anchors:
                break
        hrefs = [a.get("href", "") for a in (anchors or soup.find_all("a", href=True))]
    except Exception:
        hrefs = re.findall(r'href="([^"]+)"', html_text)
    links: list[str] = []
    for href in hrefs:
        u = _norm_result_link(href)
        if u and u not in links:
            links.append(u)
        if len(links) >= max_links:
            break
    return links


def _ddg_html(query: str) -> str:
    headers = {"User-Agent": _BROWSER_UA}
    for endpoint in ("https://html.duckduckgo.com/html/", "https://lite.duckduckgo.com/lite/"):
        try:
            html_text = http.get_text(endpoint, params={"q": query}, headers=headers,
                                      timeout=20, cache_ttl=900.0)
            if html_text:
                return html_text
        except Exception:
            continue
    return ""


def _bing_html(query: str) -> str:
    return _engine_html("https://www.bing.com/search", {"q": query})


def _mojeek_html(query: str) -> str:
    return _engine_html("https://www.mojeek.com/search", {"q": query})


def _snippets_from_html(html_text: str, max_snippets: int = 6) -> list[str]:
    """Extract short text snippets from search result HTML."""
    if not html_text:
        return []
    snippets: list[str] = []
    try:
        from bs4 import BeautifulSoup
        soup = BeautifulSoup(html_text, "html.parser")
        for sel in (".b_caption p", ".result__snippet", "p.ob", "li.result p", "div.snippet"):
            for el in soup.select(sel):
                t = el.get_text(" ", strip=True)
                if t and len(t) > 20 and t not in snippets:
                    snippets.append(t[:240])
                if len(snippets) >= max_snippets:
                    return snippets
    except Exception:
        pass
    if not snippets:
        plain = re.sub(r"<[^>]+>", " ", html_text)
        for chunk in re.split(r"\s{2,}", plain):
            chunk = chunk.strip()
            if len(chunk) > 40 and chunk not in snippets:
                snippets.append(chunk[:240])
            if len(snippets) >= max_snippets:
                break
    return snippets


def _search_snippets_merged(query: str, max_links: int = 8) -> dict:
    """Union URLs + LinkedIn URLs + text snippets across DDG, Bing, Mojeek."""
    merged_urls: list[str] = []
    merged_li: list[str] = []
    merged_snippets: list[str] = []
    engines = (
        ("ddg", _ddg_html, _ddg_result_links),
        ("bing", _bing_html, _bing_links),
        ("mojeek", _mojeek_html, _mojeek_links),
    )
    for _name, html_fn, links_fn in engines:
        html_text = ""
        try:
            html_text = html_fn(query)
        except Exception:
            html_text = ""
        if html_text:
            for li in _extract_linkedin_urls(html_text):
                if li not in merged_li:
                    merged_li.append(li)
            for sn in _snippets_from_html(html_text):
                if sn not in merged_snippets:
                    merged_snippets.append(sn)
        try:
            links = links_fn(query, max_links)
        except Exception:
            links = []
        for u in links:
            if u and u not in merged_urls:
                merged_urls.append(u)
            if "linkedin.com/in/" in u.lower():
                norm = u.split("?")[0].rstrip("/")
                if norm not in merged_li:
                    merged_li.append(norm)
            if len(merged_urls) >= max_links and len(merged_li) >= max_links:
                break
    return {
        "urls": merged_urls[:max_links],
        "linkedin_urls": merged_li[:max_links],
        "snippets": merged_snippets[:max_links],
    }


def _bing_links(query: str, max_links: int = 5) -> list[str]:
    try:
        return _engine_links("https://www.bing.com/search", {"q": query},
                             ["li.b_algo h2 a", "h2 a"], max_links)
    except Exception:
        return []


def _mojeek_links(query: str, max_links: int = 5) -> list[str]:
    try:
        return _engine_links("https://www.mojeek.com/search", {"q": query},
                             ["a.ob", "ul.results-standard li a", "h2 a"], max_links)
    except Exception:
        return []


def _search_links(query: str, max_links: int = 5) -> list[str]:
    """Find result URLs via DuckDuckGo -> Bing -> Mojeek (all keyless). First non-empty wins; one
    engine breaking never breaks search. [] if all fail/rate-limited."""
    for fn in (_ddg_result_links, _bing_links, _mojeek_links):
        try:
            links = fn(query, max_links)
        except Exception:
            links = []
        if links:
            return links[:max_links]
    return []


def _search_links_merged(query: str, max_links: int = 8) -> list[str]:
    """Union + dedupe result URLs across DDG, Bing, and Mojeek. More robust than first-wins."""
    merged: list[str] = []
    for fn in (_ddg_result_links, _bing_links, _mojeek_links):
        try:
            links = fn(query, max_links)
        except Exception:
            links = []
        for u in links:
            if u and u not in merged:
                merged.append(u)
            if len(merged) >= max_links:
                return merged[:max_links]
    return merged


_LINKEDIN_IN_RE = re.compile(r"https?://(?:[a-z]+\.)?linkedin\.com/in/[A-Za-z0-9\-_%]+/?", re.I)


def _extract_linkedin_urls(text: str) -> list[str]:
    seen: set[str] = set()
    out: list[str] = []
    for m in _LINKEDIN_IN_RE.finditer(text or ""):
        u = m.group(0).rstrip("/")
        if u not in seen:
            seen.add(u)
            out.append(u)
    return out


def _discover_linkedin_urls(name: str, company: str = "", title: str = "") -> list[dict]:
    """Targeted multi-engine search for LinkedIn /in/ profile URLs. Never raises."""
    name = (name or "").strip()
    if not name:
        return []
    queries: list[str] = []
    if company:
        queries.append(f'site:linkedin.com/in "{name}" "{company}"')
        queries.append(f'site:linkedin.com "{name}" "{company}"')
        queries.append(f'"{name}" "{company}" linkedin.com/in')
        queries.append(f'"{name}" "{company}" linkedin')
    if title and company:
        queries.append(f'"{name}" {title} {company}')
        queries.append(f'"{name}" CTO "{company}"')
    queries.append(f'"{name}" linkedin profile')
    hits: dict[str, dict] = {}
    for q in queries:
        pack = _search_snippets_merged(q, max_links=8)
        snippet_blob = " ".join(pack.get("snippets") or [])
        for url in pack.get("urls") or []:
            if "linkedin.com/in/" not in url.lower():
                continue
            norm = url.split("?")[0].rstrip("/")
            if norm not in hits:
                hits[norm] = {
                    "linkedin_url": norm,
                    "source_url": url,
                    "query": q,
                    "snippet": snippet_blob[:240] or q,
                }
        for norm in pack.get("linkedin_urls") or []:
            if norm not in hits:
                hits[norm] = {
                    "linkedin_url": norm,
                    "source_url": norm,
                    "query": q,
                    "snippet": snippet_blob[:240] or q,
                }
            if len(hits) >= 8:
                break
        if len(hits) >= 8:
            break
    return list(hits.values())


def _web_search_emails(name: str, domain: str = "", company: str = "",
                       max_results: int = 8) -> list[dict]:
    """Discover a person's published emails via keyless DuckDuckGo search. Never raises; [] on fail.
    Keeps only emails whose local-part matches the name or that sit on the target domain."""
    first, last = _split_name(name)
    if not first:
        return []
    dom = (domain or "").lower().lstrip("@")
    queries = []
    if dom:
        queries.append(f'"{name}" "@{dom}"')
    if company:
        queries.append(f'"{name}" {company} email')
    queries.append(f'"{name}" email contact')
    urls: list[str] = []
    for q in queries:
        for u in _search_links_merged(q, max_links=5):
            if u not in urls:
                urls.append(u)
        if len(urls) >= max_results:
            break
    out: dict[str, dict] = {}
    for url in urls[:max_results]:
        html_text = _fetch_page_text(url)
        if not html_text:
            continue
        for em, w in _emails_from_html(html_text).items():
            local, _, edom = em.partition("@")
            on_domain = bool(dom) and edom.lower().endswith(dom)
            name_match = (first and first in local) or (last and last in local)
            if not (on_domain or name_match):
                continue
            score = w + (3 if on_domain else 0) + (2 if name_match else 0)
            cur = out.get(em)
            if not cur or score > cur["weight"]:
                out[em] = {"email": em, "source_url": url, "weight": score,
                           "role": _is_role(em), "on_domain": on_domain}
    return sorted(out.values(), key=lambda d: -d["weight"])


def _wayback_emails(domain: str, name: str, max_snaps: int = 4) -> list[dict]:
    """Recover emails from a domain's archived about/team/contact pages via the Wayback Machine
    CDX API (free, keyless). Never raises; [] on failure."""
    dom = (domain or "").strip().lower().lstrip("@")
    if not dom:
        return []
    first, last = _split_name(name)
    try:
        rows = http.get_json("http://web.archive.org/cdx/search/cdx",
                             params={"url": f"{dom}/*", "output": "json",
                                     "filter": "statuscode:200", "collapse": "urlkey",
                                     "limit": 40, "fl": "timestamp,original"},
                             timeout=20, cache_ttl=3600.0)
    except Exception:
        return []
    if not isinstance(rows, list) or len(rows) < 2:
        return []
    wanted = re.compile(r"/(about|team|contact|people|leadership|staff|company)", re.I)
    snaps: list[tuple] = []
    for row in rows[1:]:  # row 0 is the CDX header
        try:
            ts, original = row[0], row[1]
        except (IndexError, TypeError):
            continue
        if wanted.search(original or ""):
            snaps.append((ts, original))
        if len(snaps) >= max_snaps:
            break
    out: dict[str, dict] = {}
    for ts, original in snaps:
        html_text = _fetch_page_text(f"https://web.archive.org/web/{ts}id_/{original}")
        if not html_text:
            continue
        for em, w in _emails_from_html(html_text).items():
            local, _, edom = em.partition("@")
            on_domain = edom.lower().endswith(dom)
            name_match = (first and first in local) or (last and last in local)
            if not (on_domain or name_match):
                continue
            score = w + (3 if on_domain else 0) + (2 if name_match else 0)
            cur = out.get(em)
            if not cur or score > cur["weight"]:
                out[em] = {"email": em, "source_url": f"web.archive.org/{ts}", "weight": score,
                           "role": _is_role(em), "on_domain": on_domain}
    return sorted(out.values(), key=lambda d: -d["weight"])


@mcp.tool
def discover_linkedin(name: str, company: str = "", title: str = "") -> dict:
    """Discover LinkedIn /in/ profile URLs for a named person via multi-engine web search (DDG+Bing+Mojeek).
    No API key. Use before Apollo to disambiguate common names."""
    hits = _discover_linkedin_urls(name, company, title)
    return {"name": name, "company": company or None, "title": title or None,
            "results": hits, "count": len(hits)}


@mcp.tool
def search_web(name: str, domain: str = "", company: str = "", max_results: int = 8) -> dict:
    """Discover a person's published email anywhere on the web via a FREE, keyless DuckDuckGo
    search (no API key). Fetches the top result pages and extracts emails that match the person or
    the target domain. Returns ranked candidates with their source URLs."""
    try:
        max_results = max(1, min(int(max_results), 15))
    except (TypeError, ValueError):
        max_results = 8
    return {"name": name, "domain": domain, "company": company,
            "results": _web_search_emails(name, domain, company, max_results)}


def _render_hunter_pattern(pattern: str, first: str, last: str) -> str:
    """Render a local-part pattern (e.g. '{first}.{last}', '{f}{last}') for a name. Empty if unusable."""
    if not pattern or not first:
        return ""
    local = (pattern.replace("{first}", first).replace("{last}", last)
                    .replace("{f}", first[:1]).replace("{l}", last[:1] if last else ""))
    return local.strip(".-_").replace("..", ".")


# Canonical local-part templates (same set as _patterns), in _render_hunter_pattern's language.
_PATTERN_TEMPLATES = ["{first}.{last}", "{first}", "{f}{last}", "{first}{last}", "{first}_{last}",
                      "{f}.{last}", "{first}{l}", "{first}-{last}", "{last}.{first}", "{last}{first}",
                      "{last}{f}", "{l}{first}", "{last}"]
PATTERN_TTL_DAYS = 180


def _infer_pattern(local: str, first: str, last: str) -> str | None:
    """Reverse of _render_hunter_pattern: which template produced `local` for this name? None if unsure.
    Requires both first and last (a single-name local can't pin down an org's format)."""
    local = (local or "").strip().lower()
    if not local or not first or not last:
        return None
    for tmpl in _PATTERN_TEMPLATES:
        if _render_hunter_pattern(tmpl, first, last) == local:
            return tmpl
    return None


def _learn_pattern(domain: str, pattern: str) -> None:
    """Remember a domain's confirmed local-part pattern (upsert, bump hit count)."""
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain or not pattern:
        return
    try:
        store.execute(
            "INSERT INTO domain_patterns(domain,pattern,hits,updated_at) VALUES(?,?,1,?) "
            "ON CONFLICT(domain) DO UPDATE SET pattern=excluded.pattern, hits=hits+1, "
            "updated_at=excluded.updated_at", (domain, pattern, _now()))
    except Exception:
        pass


def _learned_pattern(domain: str) -> str | None:
    """Return a domain's learned pattern, unless it's stale (> PATTERN_TTL_DAYS) or missing."""
    domain = (domain or "").strip().lower().lstrip("@")
    if not domain:
        return None
    try:
        row = store.query_one("SELECT pattern, updated_at FROM domain_patterns WHERE domain=?",
                              (domain,))
        if not row or not row.get("pattern"):
            return None
        updated = datetime.fromisoformat(row["updated_at"])
        if datetime.now(timezone.utc) - updated > timedelta(days=PATTERN_TTL_DAYS):
            return None
        return row["pattern"]
    except Exception:
        return None


# Higher = a more trustworthy origin for the address.
_SOURCE_RANK = {"learned": 6, "site": 5, "web": 5, "github": 5, "wayback": 5, "tomba": 5,
                "hunter-known": 4, "hunter-pattern": 4, "pattern": 1}
# Sources where the address was actually observed published (vs synthesized from a name+pattern).
_REAL_PUBLISHED = {"site", "web", "github", "hunter-known", "wayback", "tomba"}


def _score_candidate(cand: dict) -> tuple:
    """Rank key: verified-deliverable, then origin trust, then corroboration, confidence, weight."""
    v = cand.get("verify") or {}
    deliver = v.get("deliverable")
    deliver_rank = 2 if deliver is True else (1 if deliver is None else 0)
    src_rank = _SOURCE_RANK.get(cand.get("source", "pattern"), 1)
    conf = {"high": 3, "medium": 2, "low": 1, "inconclusive": 1, "none": 0}.get(
        cand.get("confidence", ""), 0)
    return (deliver_rank, src_rank, min(cand.get("corrob", 1), 3), conf, cand.get("weight", 0))


@mcp.tool
def find(name: str, company: str = "", domain: str = "", github: str = "",
         scrape: bool = True) -> dict:
    """Resolve the best free work email for a person. Gathers candidates from a learned domain
    pattern, Hunter's confirmed pattern, the company site, GitHub commits, a free multi-engine web
    search, the Wayback Machine, and name+domain patterns; verifies them all; returns the best —
    plus a ranked candidates[] list with evidence. Learns each domain's format as it goes."""
    evidence: dict = {"name": name, "company": company, "domain": domain}
    first, last = _split_name(name)
    dom = (domain or "").lower().lstrip("@")
    raw: list[tuple[str, str]] = []  # (email, source)

    def _add(email: str, source: str) -> None:
        email = (email or "").strip().lower()
        if email and "@" in email and not _is_disposable(email.split("@", 1)[1]):
            raw.append((email, source))

    # 0) a previously-learned pattern for this domain (free, no API call, highest priority)
    learned = _learned_pattern(dom) if dom else None
    if learned:
        rendered = _render_hunter_pattern(learned, first, last)
        if rendered:
            evidence["learned"] = {"pattern": learned}
            _add(f"{rendered}@{dom}", "learned")

    # 1) Hunter's confirmed pattern + known emails (skipped if we already learned this domain)
    if dom and not learned and get_env("HUNTER_API_KEY"):
        h = hunter_domain_search(dom)
        evidence["hunter"] = h
        if isinstance(h, dict):
            hp = h.get("pattern") or ""
            if hp:
                _learn_pattern(dom, hp)  # remember Hunter's pattern for next time
            rendered = _render_hunter_pattern(hp, first, last)
            if rendered:
                _add(f"{rendered}@{dom}", "hunter-pattern")
            for e in h.get("emails", []) or []:
                ev = (e.get("email") or "")
                local = ev.split("@", 1)[0].lower()
                if ev and ((first and first in local) or (last and last in local)):
                    _add(ev, "hunter-known")

    # 1b) Tomba finder database (free 50/mo, returns a real address)
    if dom and get_env("TOMBA_API_KEY") and get_env("TOMBA_SECRET"):
        t = tomba_find(name, dom)
        evidence["tomba"] = t
        if isinstance(t, dict) and t.get("email"):
            _add(t["email"], "tomba")

    # 2) company site (published address matching the person)
    if scrape and dom:
        site = scrape_site(dom)
        evidence["site"] = site
        for item in site.get("emails", []):
            local = item["email"].split("@", 1)[0].lower()
            if not item["role"] and ((first and first in local) or (last and last in local)):
                _add(item["email"], "site")

    # 3) GitHub commit emails (often the real address directly)
    if github:
        gh = from_github(github)
        evidence["github"] = gh
        for e in (gh.get("emails", []) or [])[:3]:
            _add(e["email"], "github")

    # 4) free multi-engine web search anywhere on the web
    web = _web_search_emails(name, dom, company)
    if web:
        evidence["web"] = web
        for item in web[:5]:
            if not item["role"]:
                _add(item["email"], "web")

    # 5) Wayback Machine archived about/team/contact pages
    if dom:
        wb = _wayback_emails(dom, name)
        if wb:
            evidence["wayback"] = wb
            for item in wb[:5]:
                if not item["role"]:
                    _add(item["email"], "wayback")

    # 6) generic name+domain patterns (fallback)
    if dom:
        pats = _patterns(name, dom)
        evidence["patterns"] = pats
        for p in pats:
            _add(p, "pattern")

    # dedup: track ALL sources per email (for corroboration), keep first-seen order
    sources_for: dict[str, list[str]] = {}
    order: list[str] = []
    for email, source in raw:
        if email not in sources_for:
            sources_for[email] = []
            order.append(email)
        if source not in sources_for[email]:
            sources_for[email].append(source)

    # verify highest-trust candidates first so the budget never starves learned/real-published ones
    order.sort(key=lambda e: -max(_SOURCE_RANK.get(s, 1) for s in sources_for[e]))
    VERIFY_CAP = 12
    url_for: dict[str, str] = {}
    for key in ("web", "wayback"):
        for w in (evidence.get(key) or []):
            url_for.setdefault(w["email"], w.get("source_url"))

    candidates: list[dict] = []
    for email in order[:VERIFY_CAP]:
        srcs = sources_for[email]
        best_src = max(srcs, key=lambda s: _SOURCE_RANK.get(s, 1))
        corrob = len(srcs)
        v = verify(email)
        deliver = v.get("deliverable")
        conf, note = v.get("confidence"), None
        # corroboration: same address from >=2 sources incl. a real-published one -> lift from None
        if deliver is None and corrob >= 2 and any(s in _REAL_PUBLISHED for s in srcs):
            conf, note = "medium", "corroborated by multiple sources"
        if any(b in email.split("@", 1)[1].lower() for b in BIG_HOSTS):
            conf, note = "inconclusive", "provider blocks probing"  # honest about Gmail/M365 walls
        candidates.append({"email": email, "source": best_src, "sources": srcs, "corrob": corrob,
                           "weight": 0, "verify": v, "deliverable": deliver, "confidence": conf,
                           "note": note, "source_url": url_for.get(email)})

    ranked = sorted(candidates, key=_score_candidate, reverse=True)
    viable = [c for c in ranked if c["deliverable"] is not False]
    pool = viable or ranked

    out_candidates = [{"email": c["email"], "source": c["source"], "sources": c["sources"],
                       "deliverable": c["deliverable"], "confidence": c["confidence"],
                       "source_url": c["source_url"]} for c in ranked]

    if not pool:
        if dom:  # nothing verifiable but we can still offer a best-guess pattern
            pats = _patterns(name, dom)
            if pats:
                return {"best": pats[0], "source": "pattern-only", "confidence": "low",
                        "note": "unverified best-guess", "candidates": out_candidates,
                        "evidence": evidence}
        return {"best": None, "confidence": "none",
                "note": "need a domain (and/or github username) to resolve", "evidence": evidence}

    top = pool[0]
    _record_found(name, dom or top["email"].split("@", 1)[1], top["email"],
                  top["source"], top["confidence"] or "low")
    # learn this domain's format whenever we confirmed a real mailbox from a name-derived address
    if dom and top["deliverable"] is True:
        tmpl = _infer_pattern(top["email"].split("@", 1)[0], first, last)
        if tmpl:
            _learn_pattern(dom, tmpl)
    result = {"best": top["email"], "source": top["source"], "confidence": top["confidence"] or "low",
              "verify": top["verify"], "candidates": out_candidates, "evidence": evidence}
    if top["note"]:
        result["note"] = top["note"]
    return result


# Contacts schema mirrored from servers/contacts (kept in sync; unique by (email, company)).
_CONTACTS_SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY,
  name TEXT, email TEXT, company TEXT, domain TEXT, role TEXT, title TEXT,
  linkedin TEXT, github TEXT, twitter TEXT, phone TEXT, tags TEXT DEFAULT '',
  source TEXT, confidence TEXT DEFAULT 'unknown',
  status TEXT DEFAULT 'new', notes TEXT DEFAULT '', last_contacted_at TEXT,
  created_at TEXT, updated_at TEXT,
  UNIQUE(email, company)
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
"""
_contacts_store: BaseStore | None = None


def _contacts() -> BaseStore:
    """Open the shared contacts DB read/write, creating it with the canonical schema if absent.

    Uses the same ON CONFLICT(email, company) upsert as the contacts server's add_contact so a
    write here is indistinguishable from one made there. Cached per-process."""
    global _contacts_store
    if _contacts_store is None:
        _contacts_store = BaseStore(db_path("contacts"), schema=_CONTACTS_SCHEMA)
    return _contacts_store


def _write_contact(name: str, company: str, email: str, domain: str, role: str,
                   github: str, source: str, confidence: str, notes: str) -> int:
    now = _now()
    email = (email or "").strip().lower()
    if not domain and email and "@" in email:
        domain = email.split("@", 1)[1]
    domain = (domain or "").strip().lower()
    return _contacts().execute(
        "INSERT INTO contacts(name,email,company,domain,role,title,linkedin,github,twitter,phone,tags,"
        "source,confidence,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(email,company) DO UPDATE SET name=excluded.name,role=excluded.role,"
        "domain=excluded.domain,github=excluded.github,source=excluded.source,"
        "confidence=excluded.confidence,updated_at=excluded.updated_at",
        (name, email, company, domain, role, "", "", github, "", "", "",
         source, confidence, notes, now, now),
    )


@mcp.tool
def persist_to_contacts(name: str, company: str = "", domain: str = "", github: str = "",
                        role: str = "", email: str = "", scrape: bool = True,
                        notes: str = "") -> dict:
    """Resolve a person's work email AND write it to the shared contacts CRM
    (~/.mcp-suite/contacts/store.db, read/write), returning the new/updated contact id.

    If `email` is supplied it is used directly (still verified); otherwise find() runs the full
    free resolution flow (site scrape -> GitHub -> patterns -> verify). The contact is upserted
    unique by (email, company) using the same schema/conflict rules as contacts.add_contact, so
    it shows up identically in the contacts server. Returns {contact_id, email, confidence, source}."""
    if email:
        v = verify(email)
        best, source, confidence = email, "provided", v.get("confidence", "low")
        resolved = {"best": email, "source": source, "confidence": confidence, "verify": v}
    else:
        resolved = find(name, company=company, domain=domain, github=github, scrape=scrape)
        best = resolved.get("best")
        source = resolved.get("source", "email-finder")
        confidence = resolved.get("confidence", "none")
    if not best:
        return {"contact_id": None, "email": None, "confidence": confidence,
                "source": source, "resolved": resolved,
                "note": "could not resolve an email; nothing written to contacts"}
    cid = _write_contact(name, company, best, domain, role, github,
                         f"email-finder:{source}", confidence, notes)
    return {"contact_id": cid, "email": best, "confidence": confidence, "source": source,
            "company": company, "resolved": resolved}


@mcp.tool
def bulk_find(people: list[dict]) -> dict:
    """Resolve emails for a batch. Each item: {name, domain?, company?, github?}. Reuses the cache."""
    if not isinstance(people, list):
        return {"count": 0, "confirmed": 0, "results": [], "error": "people must be a list"}
    people = people[:100]  # cap batch size
    results = []
    for p in people:
        if not isinstance(p, dict) or not p.get("name"):
            results.append({"input": p, "error": "missing name"})
            continue
        results.append(find(p["name"], p.get("company", ""), p.get("domain", ""),
                            p.get("github", "")))
    confirmed = sum(1 for r in results if r.get("confidence") in ("high", "medium")
                    and r.get("best"))
    return {"count": len(results), "confirmed": confirmed, "results": results}


@mcp.tool
def cache_stats() -> dict:
    """Stats on the verification cache and recorded found-emails."""
    vc = store.query_one("SELECT COUNT(*) n FROM verify_cache")["n"]
    fe = store.query_one("SELECT COUNT(*) n FROM found_emails")["n"]
    return {"verify_cache_entries": vc, "found_emails": fe, "ttl_days": CACHE_TTL_DAYS}


@mcp.tool
def clear_cache() -> dict:
    """Clear the verification cache (does not touch recorded found-emails)."""
    n = store.query_one("SELECT COUNT(*) n FROM verify_cache")["n"]
    store.execute("DELETE FROM verify_cache")
    return {"cleared": n}


if __name__ == "__main__":
    mcp.run()
