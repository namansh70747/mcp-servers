"""email-finder — resolve & verify a person's work email for FREE (no paid reveal).

Strategy, cheapest-first: published emails on the company about/team page -> GitHub commit emails
(great for technical founders) -> name+domain pattern generation -> local syntax/MX gate -> catch-all
detection + best-effort self-hosted SMTP probe -> free-tier verifier APIs (Reoon 600/mo, Hunter 50/mo,
Tomba). Results are cached in SQLite. Returns a confidence score, not a binary — realistic coverage is
~50-70%, honest about Gmail/M365/catch-all walls.
"""
from __future__ import annotations

import concurrent.futures as _cf
import hashlib as _hashlib
import ipaddress
import random
import re
import smtplib
import string
import time as _time
from datetime import datetime, timedelta, timezone
from urllib.parse import parse_qs, unquote, urlsplit

from mcp_base import BaseStore, Jobs, data_dir, db_path, fetch, get_env, http, make_server
from mcp_base import dns_resolve, email_extract, emailverify, harvest, websearch
from mcp_base.quota import QUOTA

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
CREATE TABLE IF NOT EXISTS neg_cache(
  key TEXT PRIMARY KEY, stored_at TEXT
);
"""
store = BaseStore(db_path("email-finder"), schema=SCHEMA)
JOBS = Jobs("email-finder", max_concurrent=2, inline_wait=12.0)

CACHE_TTL_DAYS = 14
CACHE_STALE_DAYS = 7   # after this many days, cache hit returns one notch lower confidence + stale:True
NEG_CACHE_TTL_S = 6 * 3600  # 6h negative-result cache (same name+domain returned empty)
# Hard wall-clock budget for find() so it can NEVER grind for minutes. The slow keyless waterfall
# (web/Wayback/harvest/OSINT) is skipped once exceeded. Tighter when driving the Apollo extension.
# Parallelised finder waterfall means 45s → 25s covers the same work.
FIND_BUDGET_S = float(get_env("FIND_BUDGET_S", "25") or 25)
FIND_BUDGET_EXT_S = float(get_env("FIND_BUDGET_EXT_S", "20") or 20)
# Bounded genuine fallback after Apollo miss (web+harvest+scrape+github, MX-only).
FIND_BUDGET_FALLBACK_S = float(get_env("FIND_BUDGET_FALLBACK_S", "15") or 15)
# Hard cap for any single candidate verify inside the parallel pool, so one slow/blocked mailbox
# can never stall the find() budget. A timeout → honest deliverable=None.
PER_VERIFY_S = float(get_env("EMAIL_PER_VERIFY_S", "8") or 8)
# Overall wall-clock guards so single-purpose scrapers can never grind for minutes.
SCRAPE_BUDGET_S = float(get_env("EMAIL_SCRAPE_BUDGET_S", "25") or 25)
# Polite pause between sequential Apollo reveals in a bulk run (respect the free tier's rate).
BULK_REVEAL_DELAY_S = float(get_env("BULK_REVEAL_DELAY_S", "1.5") or 1.5)
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


def _pick_role_person(people: list, role: str) -> dict | None:
    """Pick the best person from an Apollo people-search result for the requested role.
    Scores by (1) title containing a role keyword, (2) seniority anchors (chief/founder/ceo),
    (3) already having a revealed email — so the most authoritative, role-matching exec wins."""
    if not people:
        return None
    r = (role or "ceo").strip().lower()
    role_words = [w for w in re.split(r"[^a-z]+", r) if w]
    # map a few role aliases to title fragments we expect to see
    anchors = {
        "ceo": ["chief executive", "ceo", "founder", "owner"],
        "cto": ["chief technology", "cto", "head of engineering"],
        "cfo": ["chief financial", "cfo"],
        "coo": ["chief operating", "coo"],
        "cmo": ["chief marketing", "cmo"],
        "founder": ["founder", "co-founder", "ceo"],
    }.get(r, role_words)

    def score(p: dict) -> tuple:
        title = (p.get("title") or "").lower()
        kw = sum(1 for a in anchors if a and a in title)
        word = sum(1 for w in role_words if w and w in title)
        has_email = 1 if p.get("email") else 0
        return (kw, word, has_email)

    return max(people, key=score)


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
    """Look up a domain's MX records with DoH fallback (never returns a false empty on DNS block)."""
    r = dns_resolve.mx(domain)
    return {
        "domain": domain,
        "mx": r.get("mx", []),
        "big_host": r.get("big_host", False),
        "provider": r.get("provider", "other"),
        "method": r.get("method", "none"),
    }


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


def _cache_get_with_decay(email: str) -> dict | None:
    """Like _cache_get but applies age-aware confidence decay: >CACHE_STALE_DAYS → one notch lower + stale:True."""
    row = store.query_one("SELECT * FROM verify_cache WHERE email=?", (email,))
    if not row:
        return None
    try:
        checked = datetime.fromisoformat(row["checked_at"])
        age = datetime.now(timezone.utc) - checked
        if age > timedelta(days=CACHE_TTL_DAYS):
            return None
    except Exception:
        return None
    import json
    deliverable = row["deliverable"]
    conf = row["confidence"] or "low"
    stale = False
    try:
        if age > timedelta(days=CACHE_STALE_DAYS):
            stale = True
            _decay = {"high": "medium", "medium": "low", "low": "low"}
            conf = _decay.get(conf, conf)
    except Exception:
        pass
    result = {"email": email, "cached": True, "confidence": conf,
              "deliverable": None if deliverable is None else bool(deliverable),
              "checks": json.loads(row["checks"] or "{}")}
    if stale:
        result["stale"] = True
    return result


def _neg_cache_key(name: str, domain: str) -> str:
    raw = f"{(name or '').strip().lower()}|{(domain or '').strip().lower()}"
    return _hashlib.sha256(raw.encode()).hexdigest()[:32]


def _neg_cache_get(name: str, domain: str) -> bool:
    """Returns True if this name+domain was recently resolved to no-email (within NEG_CACHE_TTL_S)."""
    key = _neg_cache_key(name, domain)
    row = store.query_one("SELECT stored_at FROM neg_cache WHERE key=?", (key,))
    if not row:
        return False
    try:
        stored = datetime.fromisoformat(row["stored_at"])
        age_s = (datetime.now(timezone.utc) - stored).total_seconds()
        return age_s < NEG_CACHE_TTL_S
    except Exception:
        return False


def _neg_cache_put(name: str, domain: str) -> None:
    key = _neg_cache_key(name, domain)
    store.execute("INSERT OR REPLACE INTO neg_cache(key, stored_at) VALUES(?,?)", (key, _now()))


@mcp.tool
def verify(email: str, check_smtp: bool = True, use_cache: bool = True,
           consensus: int = 1, deep: bool = False) -> dict:
    """Verify an email: syntax + MX (with DoH fallback), role/disposable flags, SMTP probe,
    account-existence enumeration, Gravatar, and verifier APIs if keys set. Returns a numeric
    confidence score, honest tri-state deliverable, and a human summary. Cached (14d).
    Big-host/catch-all results are honest None (never a false False). Never raises.
    consensus>1 polls multiple independent verifiers and reports how many agreed (for a CONFIRMED
    verdict); deep=True adds account-existence enumeration."""
    return emailverify.verify(email, check_smtp=check_smtp, use_cache=use_cache,
                              consensus=consensus, deep=deep)


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
    """Fetch one page's HTML, SSRF-guarded, html-only, size-capped. '' on any failure.

    Routes through mcp_base.fetch for retry/backoff + encoding detection + a post-redirect SSRF
    re-check. The local pre-check stays so an internal host short-circuits before any network call,
    and we keep this server's html-only + size-cap contract identical (returns '' on non-HTML/failure)."""
    try:
        full = url if url.startswith("http") else f"https://{url}"
        parts = urlsplit(full)
        if parts.scheme not in ("http", "https"):
            return ""
        host = (parts.hostname or "").lower()
        if not host or _is_internal_host(host):
            return ""
        r = fetch.fetch(full, timeout=timeout, max_bytes=MAX_PAGE_BYTES)
        if not r.get("ok") or r.get("not_modified"):
            return ""
        if "text/html" not in (r.get("content_type") or ""):
            return ""
        return (r.get("html") or "")[:MAX_PAGE_BYTES]
    except Exception:
        return ""


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
    _deadline = _time.monotonic() + SCRAPE_BUDGET_S  # overall wall-clock guard
    for path in paths[:max_pages]:
        if _time.monotonic() > _deadline:
            break
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
        payload = http.get_json("https://api.tomba.io/v1/email-finder",
                                params={"domain": dom, "first_name": first, "last_name": last},
                                headers={"X-Tomba-Key": key, "X-Tomba-Secret": secret},
                                timeout=20, cache_ttl=3600.0)
        data = (payload or {}).get("data", {}) if isinstance(payload, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": data.get("email"), "score": data.get("score"),
            "sources": [s.get("uri") or s.get("url") for s in (data.get("sources") or [])][:5]}


_SNOV_TOKEN: dict = {"tok": None, "exp": 0.0}


def _snov_token() -> str | None:
    """Snov.io OAuth client-credentials access token (cached ~50 min). None if no keys/failure."""
    if _SNOV_TOKEN["tok"] and _time.monotonic() < _SNOV_TOKEN["exp"]:
        return _SNOV_TOKEN["tok"]
    cid, csec = get_env("SNOV_USER_ID"), get_env("SNOV_SECRET")
    if not (cid and csec):
        return None
    try:
        r = http.request("POST", "https://api.snov.io/v1/oauth/access_token",
                         json_body={"grant_type": "client_credentials",
                                    "client_id": cid, "client_secret": csec}, timeout=20)
        tok = ((r.get("json") or {}) if isinstance(r, dict) else {}).get("access_token")
        if tok:
            _SNOV_TOKEN.update(tok=tok, exp=_time.monotonic() + 3000)
            return tok
    except Exception:  # noqa: BLE001
        pass
    return None


@mcp.tool
def snov_find(name: str, domain: str) -> dict:
    """Snov.io email finder (FREE ~50 credits/mo; needs SNOV_USER_ID + SNOV_SECRET). Returns the real
    email Snov has for a person at a domain, with its emailStatus. A genuine finder DB (no Apollo
    credit). Graceful hint if no key."""
    if not (get_env("SNOV_USER_ID") and get_env("SNOV_SECRET")):
        return {"error": "no SNOV_USER_ID/SNOV_SECRET",
                "hint": "Free at snov.io — create an API user, then add SNOV_USER_ID + SNOV_SECRET "
                        "to .env and reconnect."}
    tok = _snov_token()
    if not tok:
        return {"error": "snov:auth-failed"}
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    if not first or not dom:
        return {"error": "need a name and a domain"}
    try:
        r = http.request("POST", "https://api.snov.io/v1/get-emails-from-names",
                         json_body={"access_token": tok, "firstName": first,
                                    "lastName": last, "domain": dom}, timeout=25)
        j = (r.get("json") or {}) if isinstance(r, dict) else {}
        data = j.get("data") or j
        emails = data.get("emails") or data.get("result") or []
        best = emails[0] if emails else {}
        em = (best.get("email") if isinstance(best, dict) else best) or None
        status = best.get("emailStatus") if isinstance(best, dict) else None
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": em, "email_status": status}


@mcp.tool
def skrapp_find(name: str, domain: str) -> dict:
    """Skrapp.io email finder (FREE ~100/mo; needs SKRAPP_API_KEY). Returns the real email Skrapp has
    for a person at a domain, with a quality score. A genuine finder DB (no Apollo credit). Graceful
    hint if no key."""
    key = get_env("SKRAPP_API_KEY")
    if not key:
        return {"error": "no SKRAPP_API_KEY",
                "hint": "Free at skrapp.io — copy your API key, then add SKRAPP_API_KEY to .env "
                        "and reconnect."}
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    if not first or not dom:
        return {"error": "need a name and a domain"}
    try:
        payload = http.get_json("https://api.skrapp.io/api/v3/find",
                                params={"firstName": first, "lastName": last, "domain": dom},
                                headers={"X-Access-Key": key}, timeout=20, cache_ttl=3600.0)
        data = payload if isinstance(payload, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    q = data.get("quality") or {}
    return {"name": name, "domain": dom, "email": data.get("email"),
            "score": q.get("score") if isinstance(q, dict) else q}


_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")


def _email_in_json(obj, prefer_domain: str = "") -> str | None:
    """Walk an arbitrary JSON value and return the first email found (preferring one on prefer_domain).
    Lets finder integrations tolerate differing response field names."""
    found: list[str] = []

    def _walk(o):
        if isinstance(o, str):
            found.extend(_EMAIL_RE.findall(o))
        elif isinstance(o, dict):
            for v in o.values():
                _walk(v)
        elif isinstance(o, list):
            for v in o:
                _walk(v)

    _walk(obj)
    if not found:
        return None
    dom = (prefer_domain or "").lower().lstrip("@")
    for e in found:
        if dom and e.split("@", 1)[1].lower().endswith(dom):
            return e.lower()
    return found[0].lower()


@mcp.tool
def hunter_find(name: str, domain: str) -> dict:
    """Hunter.io email FINDER (FREE 25-50/mo; uses the existing HUNTER_API_KEY). Returns the real email
    Hunter has for a person at a domain + a confidence score. Name+domain → email, NO Apollo credit."""
    key = get_env("HUNTER_API_KEY")
    if not key:
        return {"error": "no HUNTER_API_KEY", "hint": "Add a free Hunter API key to .env."}
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    if not first or not dom:
        return {"error": "need a name and a domain"}
    try:
        payload = http.get_json("https://api.hunter.io/v2/email-finder",
                                params={"domain": dom, "first_name": first, "last_name": last,
                                        "api_key": key}, timeout=20, cache_ttl=3600.0)
        data = (payload or {}).get("data", {}) if isinstance(payload, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": data.get("email"), "score": data.get("score")}


@mcp.tool
def prospeo_find(name: str, domain: str) -> dict:
    """Prospeo email FINDER (FREE 75-100 credits; needs PROSPEO_API_KEY). Real name+company → VERIFIED
    email (Prospeo only charges on a verified hit). No Apollo credit. Graceful hint if no key."""
    key = get_env("PROSPEO_API_KEY")
    if not key:
        return {"error": "no PROSPEO_API_KEY",
                "hint": "Free 75-100 at prospeo.io — copy your API key, add PROSPEO_API_KEY, reconnect."}
    dom = (domain or "").strip().lower().lstrip("@")
    if not (name or "").strip() or not dom:
        return {"error": "need a name and a domain"}
    try:
        r = http.request("POST", "https://api.prospeo.io/enrich-person",
                         headers={"Content-Type": "application/json", "X-KEY": key},
                         json_body={"only_verified_email": True,
                                    "data": {"full_name": name, "company_website": dom}}, timeout=25)
        j = (r.get("json") or {}) if isinstance(r, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    em = (((j.get("person") or {}).get("email") or {}).get("email")) or _email_in_json(j, dom)
    return {"name": name, "domain": dom, "email": em}


@mcp.tool
def prospeo_find_by_linkedin(linkedin_url: str) -> dict:
    """Prospeo LinkedIn→email (FREE; needs PROSPEO_API_KEY). Turns a LinkedIn profile URL into a
    verified email — the credit-independent reveal that pairs with Apollo's free LinkedIn URLs."""
    key = get_env("PROSPEO_API_KEY")
    if not key:
        return {"error": "no PROSPEO_API_KEY", "hint": "Free at prospeo.io; add PROSPEO_API_KEY."}
    if not (linkedin_url or "").strip():
        return {"error": "need a linkedin_url"}
    try:
        r = http.request("POST", "https://api.prospeo.io/enrich-person",
                         headers={"Content-Type": "application/json", "X-KEY": key},
                         json_body={"only_verified_email": True,
                                    "data": {"linkedin_url": linkedin_url}}, timeout=25)
        j = (r.get("json") or {}) if isinstance(r, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    em = (((j.get("person") or {}).get("email") or {}).get("email")) or _email_in_json(j)
    return {"linkedin_url": linkedin_url, "email": em}


@mcp.tool
def getprospect_find(name: str, domain: str) -> dict:
    """GetProspect email FINDER (FREE 50/mo; needs GETPROSPECT_API_KEY). Name+company → email."""
    key = get_env("GETPROSPECT_API_KEY")
    if not key:
        return {"error": "no GETPROSPECT_API_KEY",
                "hint": "Free 50/mo at getprospect.com — add GETPROSPECT_API_KEY, reconnect."}
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    if not first or not dom:
        return {"error": "need a name and a domain"}
    try:
        payload = http.get_json("https://api.getprospect.com/public/v1/email/find",
                                params={"name": f"{first} {last}".strip(), "company": dom,
                                        "apiKey": key}, timeout=20, cache_ttl=3600.0)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": _email_in_json(payload, dom)}


@mcp.tool
def minelead_find(name: str, domain: str) -> dict:
    """Minelead email FINDER (FREE 25/mo; needs MINELEAD_API_KEY). Name+domain → email."""
    key = get_env("MINELEAD_API_KEY")
    if not key:
        return {"error": "no MINELEAD_API_KEY",
                "hint": "Free 25/mo at minelead.io — add MINELEAD_API_KEY, reconnect."}
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    if not dom:
        return {"error": "need a domain"}
    try:
        payload = http.get_json("https://api.minelead.io/v1/search",
                                params={"domain": dom, "name": f"{first} {last}".strip(),
                                        "key": key}, timeout=20, cache_ttl=3600.0)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": _email_in_json(payload, dom)}


@mcp.tool
def emailverify_io_find(name: str, domain: str) -> dict:
    """EmailVerify.io email FINDER (FREE 10 finds/mo; needs EMAILVERIFY_IO_API_KEY). Name+domain → email."""
    key = get_env("EMAILVERIFY_IO_API_KEY")
    if not key:
        return {"error": "no EMAILVERIFY_IO_API_KEY",
                "hint": "Free 10 finds + 100 verifies/mo at emailverify.io — add EMAILVERIFY_IO_API_KEY."}
    dom = (domain or "").strip().lower().lstrip("@")
    if not (name or "").strip() or not dom:
        return {"error": "need a name and a domain"}
    try:
        payload = http.get_json("https://app.emailverify.io/api/v1/finder",
                                params={"key": key, "name": name, "domain": dom},
                                timeout=20, cache_ttl=3600.0)
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": _email_in_json(payload, dom)}


@mcp.tool
def generect_find(name: str, domain: str) -> dict:
    """Generect email FINDER (FREE 50 credits; needs GENERECT_API_KEY). Name+domain → email."""
    key = get_env("GENERECT_API_KEY")
    if not key:
        return {"error": "no GENERECT_API_KEY",
                "hint": "Free 50 at generect.com — add GENERECT_API_KEY, reconnect."}
    first, last = _split_name(name)
    dom = (domain or "").strip().lower().lstrip("@")
    if not first or not dom:
        return {"error": "need a name and a domain"}
    try:
        r = http.request("POST", "https://api.generect.com/api/linkedin/email_finder/",
                         headers={"Authorization": f"Token {key}", "Content-Type": "application/json"},
                         json_body=[{"first_name": first, "last_name": last, "domain": dom}], timeout=25)
        j = (r.get("json") or {}) if isinstance(r, dict) else {}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    return {"name": name, "domain": dom, "email": _email_in_json(j, dom)}


def _record_found(name: str, domain: str, email: str, source: str, confidence: str) -> None:
    try:
        store.execute(
            "INSERT OR IGNORE INTO found_emails(name,domain,email,source,confidence,found_at) "
            "VALUES(?,?,?,?,?,?)", (name, domain, email, source, confidence, _now()))
    except Exception:
        pass


def _cached_reveal(name: str, domain: str) -> str | None:
    """A previously-revealed email for this person via a credit-costing extension — so we never
    spend a free Apollo/ContactOut credit twice on the same person."""
    if not name:
        return None
    try:
        row = store.query_one(
            "SELECT email FROM found_emails WHERE name=? AND domain=? "
            "AND source IN ('apollo-cdp','extension') ORDER BY found_at DESC LIMIT 1",
            (name, (domain or "").lower().lstrip("@")))
        return row["email"] if row and row.get("email") else None
    except Exception:
        return None


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


def _engine_links(url: str, params: dict, selectors: list[str], max_links: int) -> list[str]:
    """Run one keyless search engine, parse result anchors, return normalized target URLs."""
    html_text = http.get_text(url, params=params, headers={"User-Agent": _BROWSER_UA},
                              timeout=20, cache_ttl=900.0)
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
    """Parallel multi-engine search (8 engines, curl_cffi impersonation, agreement ranking).
    Falls back to sequential DDG→Bing→Mojeek if the shared websearch module fails."""
    try:
        links = websearch.search_links(query, n=max_links)
        if links:
            return links[:max_links]
    except Exception:
        pass
    for fn in (_ddg_result_links, _bing_links, _mojeek_links):
        try:
            links = fn(query, max_links)
        except Exception:
            links = []
        if links:
            return links[:max_links]
    return []


def _web_search_emails(name: str, domain: str = "", company: str = "",
                       max_results: int = 10, budget_s: float = 30.0) -> list[dict]:
    """Discover a person's published emails via keyless web search. Queries run in parallel,
    URL fetches run in parallel. `budget_s` caps each phase so a bounded fallback can't overrun.
    Never raises; [] on fail."""
    first, last = _split_name(name)
    if not first:
        return []
    _ws_deadline = _time.monotonic() + max(4.0, float(budget_s))   # TOTAL cap across both phases
    dom = (domain or "").lower().lstrip("@")
    queries: list[str] = []
    if dom:
        queries.append(f'"{name}" "@{dom}"')
    if company:
        queries.append(f'"{name}" {company} email contact')
    queries.append(f'"{name}" email contact')

    from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed

    # Phase 1: parallel search queries
    urls: list[str] = []
    seen_urls: set[str] = set()
    with ThreadPoolExecutor(max_workers=len(queries)) as pool:
        futs = [pool.submit(_search_links, q, 5) for q in queries]
        for f in _as_completed(futs, timeout=max(2.0, min(25.0, _ws_deadline - _time.monotonic()))):
            try:
                for u in (f.result(timeout=0) or []):
                    if u not in seen_urls:
                        seen_urls.add(u)
                        urls.append(u)
            except Exception:
                pass

    if not urls:
        return []

    # Phase 2: parallel URL fetches (8s timeout each instead of 15s)
    out: dict[str, dict] = {}

    def _fetch_one(url: str) -> tuple[str, str]:
        return url, _fetch_page_text(url, timeout=8.0)

    with ThreadPoolExecutor(max_workers=8) as pool:
        futs2 = {pool.submit(_fetch_one, u): u for u in urls[:max_results]}
        for f in _as_completed(futs2, timeout=max(2.0, min(30.0, _ws_deadline - _time.monotonic()))):
            try:
                url, text = f.result(timeout=0)
                if not text:
                    continue
                for em, w in _emails_from_html(text).items():
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
            except Exception:
                pass

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
    _deadline = _time.monotonic() + SCRAPE_BUDGET_S  # never grind on slow archive fetches
    for ts, original in snaps:
        if _time.monotonic() > _deadline:
            break
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
                "snov": 5, "skrapp": 5, "hunter-find": 5, "prospeo": 5, "getprospect": 5,
                "minelead": 5, "emailverify_io": 5, "generect": 5, "commoncrawl": 5,
                "harvest": 5, "agent": 5, "extension": 6, "apollo-cdp": 6, "pgp": 4, "crtsh": 4,
                "deep_crawl": 4, "hunter-known": 4, "hunter-pattern": 4, "pattern": 1}
_REAL_PUBLISHED = {"site", "web", "github", "hunter-known", "wayback", "tomba", "snov", "skrapp",
                   "hunter-find", "prospeo", "getprospect", "minelead", "emailverify_io", "generect",
                   "commoncrawl", "harvest", "agent", "extension", "apollo-cdp", "pgp", "crtsh",
                   "deep_crawl"}


# Source taxonomy for the consensus-confirmation policy (Phase: credit-independent confirm).
_FINDER_SOURCES = {"tomba", "snov", "skrapp", "hunter-find", "prospeo", "getprospect", "minelead",
                   "emailverify_io", "generect", "hunter-known", "hunter-pattern"}  # name+domain→email
_REVEAL_SOURCES = {"apollo-cdp", "extension", "apollo-search"}                    # authoritative reveals
_PUBLISHED_SOURCES = {"site", "web", "github", "wayback", "harvest", "pgp", "crtsh", "deep_crawl"}


def _confirm_signals(sources, v: dict) -> list[str]:
    """List the INDEPENDENT strong confirmation signals backing an email — each distinct finder DB,
    each distinct published source, a multi-verifier API verdict, and SMTP-250 / enumeration. Used by
    the consensus policy: ≥2 distinct signals ⇒ a genuinely CONFIRMED (high-confidence) address."""
    sigs: list[str] = []
    srcs = set(sources or [])
    for f in sorted(srcs & _FINDER_SOURCES):
        sigs.append(f"finder:{f}")
    for p in sorted(srcs & _PUBLISHED_SOURCES):
        sigs.append(f"published:{p}")
    checks = (v or {}).get("checks", {}) or {}
    api = checks.get("api", {}) or {}
    if api.get("verdict") is True:
        sigs.append(f"api-valid:{api.get('valid', 1)}")
        if (api.get("valid") or 0) >= 2:
            sigs.append("api-consensus")   # 2+ independent verifiers agreed → extra signal
    if str(checks.get("smtp")) == "250":
        sigs.append("smtp-250")
    if (checks.get("enumeration") or {}).get("found_on"):
        sigs.append("enumeration")
    return sigs


def _consensus_confidence(signals: list[str], v: dict) -> str:
    """Consensus policy: HIGH when ≥2 independent signals OR a direct SMTP-250 confirmation on a
    non-catch-all domain; MEDIUM on one signal; LOW on zero. A verifier 'invalid'/undeliverable
    verdict blocks high (never confident-wrong)."""
    api = ((v or {}).get("checks", {}) or {}).get("api", {}) or {}
    checks = (v or {}).get("checks", {}) or {}
    if api.get("verdict") is False or (v or {}).get("deliverable") is False:
        return "low"
    # SMTP-250 on a confirmed non-catch-all is direct proof of deliverability — no consensus needed.
    if (v or {}).get("deliverable") is True and str(checks.get("smtp")) == "250":
        return "high"
    n = len(set(signals))
    if n >= 2:
        return "high"
    if n == 1:
        return "medium"
    return "low"


def _confidence_reason(signals: list[str], v: dict) -> str:
    """Human-readable explanation of why a confidence level was assigned."""
    checks = (v or {}).get("checks", {}) or {}
    parts = list(signals)
    if (v or {}).get("deliverable") is True and str(checks.get("smtp")) == "250":
        if "smtp-250" not in parts:
            parts.insert(0, "smtp-250")
    if not parts:
        return "no confirmed signals"
    return " + ".join(parts)


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
         linkedin_url: str = "", scrape: bool = True, deep: bool = False,
         use_agent: bool = False, use_extensions: bool = False) -> dict:
    """Resolve the best free work email for a person (fast keyless waterfall, verified + ranked).

    Fast modes return the full result inline (bounded by FIND_BUDGET_S — the SMTP circuit-breaker +
    per-verify caps guarantee it never hangs). The genuinely-long modes (deep / use_agent /
    use_extensions) return inline if quick, else a {job_id} you poll with find_status(job_id) — so a
    long crawl never blocks or times out. Same result shape either way (best, candidates, summary)."""
    # Fast keyless path: synchronous + budget-bounded (no client-facing job indirection).
    if not (deep or use_agent or use_extensions):
        return _find_core(name, company=company, domain=domain, github=github,
                          linkedin_url=linkedin_url, scrape=scrape)

    # Heavy path (crawl / agentic browsing / extension reveal): inline-if-quick, else background.
    def _worker(job: dict) -> None:
        JOBS.set(job["id"], status="running", percent=5.0)
        try:
            res = _find_core(name, company=company, domain=domain, github=github,
                             linkedin_url=linkedin_url, scrape=scrape, deep=deep,
                             use_agent=use_agent, use_extensions=use_extensions)
        except Exception as e:  # noqa: BLE001 — never surface a raw error to the client
            JOBS.finish(job["id"], ok_=False, error=str(e))
            return
        JOBS.finish(job["id"], ok_=True, **res)

    return JOBS.run_or_job("find", _worker, name=name, company=company, domain=domain)


def _confirm_winner(emails: list[str]) -> dict | None:
    """Deep-verify the candidate mailboxes IN PARALLEL (account-existence enumeration + GHunt +
    Gravatar + the free-tier APIs) and return the best-confirmed one as {email, verify, via}.

    This is the catch-all/SMTP-blocked disambiguator: among pattern variants (atai@, atai.barkai@…)
    only the real mailbox has live accounts, so enumeration confirms which to return — free, and it
    works exactly where SMTP and Hunter/Reoon return 'unknown'. Bounded by PER_VERIFY_S per mailbox."""
    emails = [e for e in (emails or []) if e][:4]
    if not emails:
        return None
    from concurrent.futures import ThreadPoolExecutor, as_completed as _ac
    results: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=4)
    try:
        futs = {pool.submit(emailverify.verify, e, check_smtp=True, use_cache=True, deep=True): e
                for e in emails}
        try:
            for f in _ac(futs, timeout=max(20.0, PER_VERIFY_S * len(emails))):
                e = futs[f]
                try:
                    results[e] = f.result(timeout=PER_VERIFY_S + 4)
                except Exception:
                    results[e] = {"deliverable": None, "score": 0, "confidence": "low"}
        except Exception:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    if not results:
        return None

    def _high_precision(v: dict) -> str:
        """A MAILBOX-LEVEL confirmation only — an API 'valid' verdict or a real SMTP 250. NOT
        account-enumeration/Gravatar: on a catch-all domain those are noisy and would 'confirm' the
        wrong local-part (e.g. barkai@ over atai@). Returns the signal name, or '' if none."""
        api = (v.get("checks", {}) or {}).get("api", {}) or {}
        if api.get("verdict") is True:
            return api.get("provider") or "api"
        if str((v.get("checks", {}) or {}).get("smtp")) == "250":
            return "smtp"
        return ""

    # Among candidates, pick the one with a high-precision confirmation (input order breaks ties so
    # the most-common pattern wins). If NONE is precisely confirmed, return None → the caller falls
    # back to honest best-pattern ranking at low confidence (never a confident wrong answer).
    confirmed = [e for e in emails if results.get(e, {}).get("deliverable") is True
                 and _high_precision(results.get(e, {}))]
    if confirmed:
        best_email = confirmed[0]  # emails are ordered most-common-pattern first
        bestv = results[best_email]
        return {"email": best_email, "verify": bestv, "via": _high_precision(bestv)}
    return None


def _find_core(name: str, company: str = "", domain: str = "", github: str = "",
               linkedin_url: str = "", scrape: bool = True, deep: bool = False,
               use_agent: bool = False, use_extensions: bool = False,
               linkedin_candidates: list | None = None,
               apollo_seed: dict | None = None) -> dict:
    """Resolve the best free work email for a person. Waterfall: learned pattern → Hunter/Tomba →
    site scrape → GitHub commits → parallel web search (8 engines) → Wayback → deep site harvest
    (sitemap/llms.txt/Cloudflare cfemail decode) → OSINT (PGP keyservers + crt.sh) → patterns.
    Verifies all candidates in parallel. Learns each domain's format as it goes.
    deep=True adds BFS crawl + web-search deep fallback (slower but more thorough).
    use_agent=True (opt-in) adds AI observe→think→act browsing of the real Chrome for hard cases.
    linkedin_url: LinkedIn profile URL for this person (auto-discovered when use_extensions=True).
    use_extensions=True: replicates your manual flow — opens the LinkedIn profile in your real
    Chrome and clicks Apollo / ContactOut / Lusha / etc. to reveal the email using your free
    monthly extension credits (~90% accuracy; requires Chrome open with extensions installed)."""
    evidence: dict = {"name": name, "company": company, "domain": domain}
    first, last = _split_name(name)
    dom = (domain or "").lower().lstrip("@")
    raw: list[tuple[str, str]] = []  # (email, source)

    # Negative-result cache: if this name+domain returned empty recently (and we're in the basic
    # path), return the cached miss instantly instead of re-running the entire waterfall.
    if dom and not deep and not use_extensions and not use_agent and _neg_cache_get(name, dom):
        return {"best": None, "confidence": "none", "cached_miss": True,
                "note": "recent search found no email (negative cache); use deep=True to force retry",
                "evidence": evidence,
                "summary": f"Cached miss for {name} — no email found recently (retry with deep=True)"}

    # Wall-clock budget — find() must NEVER grind for minutes. Tight when driving the Apollo
    # extension (Apollo-first: if it has no card, fail fast rather than run the slow keyless crawl).
    _t0 = _time.monotonic()
    _budget = FIND_BUDGET_EXT_S if (use_extensions and not deep) else FIND_BUDGET_S
    # The slow keyless waterfall (web/Wayback/harvest/OSINT) runs only when NOT Apollo-driven, or
    # explicitly deep=True. Apollo misses return fast on the cheap targeted steps + patterns.
    _run_slow = (not use_extensions) or deep
    # On an Apollo miss we run a GENUINE but BOUNDED fallback (set True at the fall-through below):
    # web+harvest+scrape+github+reveal under FIND_BUDGET_FALLBACK_S, MX-only verify, no Wayback/OSINT.
    _ext_fallback = False
    # SMTP probing (slow) is reserved for the full waterfall — never the bounded extension fallback.
    def _smtp_on() -> bool:
        return _run_slow and not _ext_fallback

    def _over_budget() -> bool:
        return (_time.monotonic() - _t0) > _budget

    def _add(email: str, source: str) -> None:
        email = (email or "").strip().lower()
        if email and "@" in email and not _is_disposable(email.split("@", 1)[1]):
            raw.append((email, source))

    def _maybe_learn_pattern(email: str) -> None:
        """Learn the domain's name format from a confirmed email (pattern multiplier)."""
        if dom and email and email.split("@", 1)[-1].endswith(dom):
            tmpl = _infer_pattern(email.split("@", 1)[0], first, last)
            if tmpl:
                _learn_pattern(dom, tmpl)

    def _score_with_graph(c: dict):
        return (*_score_candidate(c), c.get("graph_score", 0.0))

    def _evaluate(verify_cap: int, check_smtp: bool = True) -> dict:
        """Pure: dedup `raw`, verify the top candidates, rank them. Returns
        {top, pool, out_candidates}. Re-callable (verify() is cached) so the fast and full
        passes share work. check_smtp=False does a FAST MX-only verify (Apollo-driven path)."""
        from concurrent.futures import ThreadPoolExecutor, as_completed as _as_completed
        sources_for: dict[str, list[str]] = {}
        order: list[str] = []
        for email, source in raw:
            if email not in sources_for:
                sources_for[email] = []
                order.append(email)
            if source not in sources_for[email]:
                sources_for[email].append(source)
        if not order:
            return {"top": None, "pool": [], "out_candidates": []}
        order.sort(key=lambda e: -max(_SOURCE_RANK.get(s, 1) for s in sources_for[e]))
        url_for: dict[str, str] = {}
        for key in ("web", "wayback"):
            for w in (evidence.get(key) or []):
                url_for.setdefault(w["email"], w.get("source_url"))
        _verify_order = order[:verify_cap]

        def _do_verify(em: str) -> tuple[str, dict]:
            return em, verify(em, check_smtp=check_smtp)

        # Bound the verify pool so one slow mailbox can't blow find()'s budget: each verify is
        # capped at PER_VERIFY_S, and the whole pool is capped by the time left in the find budget.
        _vresults: dict[str, dict] = {}
        _pool_deadline = _t0 + _budget
        _overall = max(2.0, _pool_deadline - _time.monotonic())
        # NOTE: don't use `with` — its exit calls shutdown(wait=True) and would block on a hung
        # verify, defeating the timeout. Drain via as_completed(timeout=_overall), then shut down
        # without waiting (cancel_futures) so a slow mailbox can never stall the caller.
        _pool = ThreadPoolExecutor(max_workers=6)
        try:
            _futs = {_pool.submit(_do_verify, em): em for em in _verify_order}
            try:
                for _fut in _as_completed(_futs, timeout=_overall):
                    try:
                        em, v = _fut.result(timeout=PER_VERIFY_S)
                        _vresults[em] = v
                    except Exception:
                        _vresults[_futs[_fut]] = {"deliverable": None, "confidence": "low",
                                                  "checks": {}, "degraded": ["verify:timeout"]}
            except Exception:
                # overall pool timeout — keep whatever finished; the rest fall back to None below
                pass
        finally:
            _pool.shutdown(wait=False, cancel_futures=True)
        # any candidate that never resolved → honest None (never blocks the caller)
        for em in _verify_order:
            _vresults.setdefault(em, {"deliverable": None, "confidence": "low",
                                      "checks": {}, "degraded": ["verify:timeout"]})

        candidates: list[dict] = []
        for email in _verify_order:
            srcs = sources_for[email]
            best_src = max(srcs, key=lambda s: _SOURCE_RANK.get(s, 1))
            corrob = len(srcs)
            v = _vresults.get(email, {"deliverable": None, "confidence": "low", "checks": {}})
            deliver = v.get("deliverable")
            conf, note = v.get("confidence"), None
            if deliver is None and corrob >= 2 and any(s in _REAL_PUBLISHED for s in srcs):
                conf, note = "medium", "corroborated by multiple sources"
            if any(b in email.split("@", 1)[1].lower() for b in BIG_HOSTS):
                conf, note = "inconclusive", "provider blocks probing"
            candidates.append({"email": email, "source": best_src, "sources": srcs, "corrob": corrob,
                               "weight": 0, "verify": v, "deliverable": deliver, "confidence": conf,
                               "note": note, "source_url": url_for.get(email)})

        try:
            from mcp_base.frontier.graph import corroboration_scores
            cscores = corroboration_scores(candidates)
            for c in candidates:
                c["graph_score"] = cscores.get(c["email"].lower(), 0.0)
        except Exception:
            for c in candidates:
                c["graph_score"] = 0.0

        ranked = sorted(candidates, key=_score_with_graph, reverse=True)
        viable = [c for c in ranked if c["deliverable"] is not False]
        pool = viable or ranked
        out_candidates = [{"email": c["email"], "source": c["source"], "sources": c["sources"],
                           "deliverable": c["deliverable"], "confidence": c["confidence"],
                           "source_url": c["source_url"]} for c in ranked]
        return {"top": pool[0] if pool else None, "pool": pool, "out_candidates": out_candidates}

    def _qualifies(top: dict | None) -> bool:
        """Early-exit gate. Extension reveals (Apollo/ContactOut) are high-trust (~90%) — accept
        unless verify proved them UNdeliverable. Other sources need a verified on-domain address."""
        if not top:
            return False
        if top.get("source") in ("apollo-cdp", "extension"):
            return top.get("deliverable") is not False
        if top.get("deliverable") is not True:
            return False
        edom = top["email"].split("@", 1)[1].lower()
        return bool(dom) and edom.endswith(dom)

    def _success(top: dict, out_candidates: list[dict]) -> dict:
        """Record/learn from the winning candidate and build the success result. Applies the
        CONSENSUS policy for credit-independent (non-reveal) winners: high when ≥2 independent
        signals agree OR when SMTP-250 directly confirms deliverability; an authoritative
        Apollo/extension reveal stays high on its own."""
        confirmation = None
        if top.get("source") in _REVEAL_SOURCES:
            conf = top.get("confidence") or "high"   # authoritative reveal — keep as-is
            sigs: list[str] = []
        else:
            vv = top.get("verify") or {}
            _api = ((vv.get("checks", {}) or {}).get("api", {}) or {})
            _neg = _api.get("verdict") is False or vv.get("deliverable") is False
            # Skip consensus re-verify when SMTP already confirmed deliverability (saves ~8s + quota).
            # Also skip when we already have ≥2 strong signals — the re-verify can't change the verdict.
            _already_confirmed = vv.get("deliverable") is True
            _pre_sigs = _confirm_signals(top.get("sources"), vv)
            if (not _neg and not _already_confirmed and not _api.get("providers")
                    and len(_pre_sigs) < 2):
                try:
                    cv = verify(top["email"], check_smtp=False, consensus=2, use_cache=True)
                    # merge the consensus API verdict into the existing verify checks (keep SMTP etc.)
                    if (cv.get("checks", {}) or {}).get("api"):
                        vv.setdefault("checks", {})["api"] = cv["checks"]["api"]
                        top["verify"] = vv
                except Exception:  # noqa: BLE001
                    pass
            sigs = _confirm_signals(top.get("sources"), vv)
            conf = _consensus_confidence(sigs, vv)
            reason = _confidence_reason(sigs, vv)
            confirmation = {"signals": sigs, "level": conf, "reason": reason,
                            "policy": "high requires >=2 independent signals OR smtp-250 confirmed"}
        top["confidence"] = conf
        _record_found(name, dom or top["email"].split("@", 1)[1], top["email"], top["source"], conf)
        if dom and top["deliverable"] is True:
            _maybe_learn_pattern(top["email"])
        _deliver_str = ("verified deliverable" if top["deliverable"] is True
                        else "undeliverable" if top["deliverable"] is False
                        else "unverified (inbox may exist)")
        _who = f"{name}{' at ' + company if company else ''}"
        _summary = (f"Best email for {_who}: {top['email']} "
                    f"({conf} confidence, {_deliver_str}, from {top['source']})")
        if confirmation:
            evidence["confirmation"] = confirmation
        _top_verify = top.get("verify") or {}
        _conf_reason = _confidence_reason(sigs, _top_verify) if sigs else None
        result = {"best": top["email"], "source": top["source"],
                  "confidence": conf, "confidence_reason": _conf_reason,
                  "verify": top["verify"],
                  "candidates": out_candidates, "evidence": evidence, "summary": _summary}
        if top.get("note"):
            result["note"] = top["note"]
        return result

    # 0) a previously-learned pattern for this domain (free, no API call, highest priority)
    learned = _learned_pattern(dom) if dom else None
    if learned:
        rendered = _render_hunter_pattern(learned, first, last)
        if rendered:
            evidence["learned"] = {"pattern": learned}
            _add(f"{rendered}@{dom}", "learned")

    # 0.5) LinkedIn auto-discovery + Chrome extension reveal (opt-in via use_extensions=True)
    # Replicates the manual flow: search LinkedIn → click Apollo/ContactOut/Lusha → read email.
    # Uses the user's real logged-in Chrome + free extension pool (~10k Apollo credits/mo).
    # Personal-mail providers where an off-company-domain email is still legitimately the person.
    _BIG_PERSONAL = ("gmail.com", "googlemail.com", "outlook.com", "hotmail.com", "yahoo.com",
                     "icloud.com", "proton.me", "protonmail.com", "live.com", "aol.com")

    def _domain_ok(email: str) -> bool:
        """True if `email`'s domain is sane for this company (matches the domain root, or is a
        personal provider). When we don't know the company domain, accept anything."""
        if not email or "@" not in email:
            return False
        if not dom:
            return True
        ed = email.split("@", 1)[1].lower()
        return ed == dom or ed.split(".")[0] == dom.split(".")[0] or ed in _BIG_PERSONAL

    if use_extensions:
        from mcp_base import apollo_cdp as _ap
        # Build a RANKED candidate profile list (the manual "search → pick the right top link" move).
        cand_urls: list[str] = []
        if linkedin_candidates:  # caller (find_by_company) already discovered the ranked list
            cand_urls = [u for u in linkedin_candidates if u][:5]
            evidence["linkedin_discovery"] = {"candidates": cand_urls}
        elif linkedin_url:
            cand_urls = [linkedin_url]
        elif (name or company or dom):
            try:
                _q = (name or "").strip() or f"{company or dom}".strip()
                _disc = _ap.find_linkedin_profile(_q, company=company or dom, role="")
                cand_urls = _disc.get("profiles", [])[:5]
                evidence["linkedin_discovery"] = {"source": _disc.get("source"),
                                                  "candidates": cand_urls,
                                                  "degraded": _disc.get("degraded")}
            except Exception:
                pass
        evidence["linkedin_url"] = (cand_urls[0] if cand_urls else None)

        _apollo_email = ""
        _apollo_url = None

        # 0.4) Apollo people-search SEED — find_by_company already queried Apollo's own people DB by
        # domain+role and handed us the authoritative person (often with the verified email inline).
        # This is the most reliable path: no LinkedIn discovery, no reveal click, no credit spend.
        if apollo_seed and apollo_seed.get("email") and _domain_ok(apollo_seed["email"]):
            _apollo_email = apollo_seed["email"].strip().lower()
            _apollo_url = apollo_seed.get("linkedin_url")
            evidence["apollo"] = {"email": _apollo_email, "org": apollo_seed.get("org"),
                                  "person_name": apollo_seed.get("name"),
                                  "title": apollo_seed.get("title"),
                                  "email_status": apollo_seed.get("email_status"),
                                  "source": "apollo-search", "credit_used": False, "degraded": []}

        # Cache fast-path: a prior domain-valid reveal is instant and spends no credit.
        _cached = _cached_reveal(name, dom)
        # Credit-aware gate: a fresh reveal spends a credit. If the team is out of Apollo credits,
        # SKIP the doomed reveal — the FREE apollo_people_search identity (above) + the finder/verify
        # backbone below still resolve a CONFIRMED email at 0 credits.
        _credits_left = _ap.apollo_reveal_credits_left() if cand_urls and not _apollo_email else None
        if _apollo_email:
            pass  # seed already wins — skip cache/reveal
        elif _cached and _domain_ok(_cached):
            _apollo_email = _cached
            _apollo_url = cand_urls[0] if cand_urls else (linkedin_url or None)
            evidence["apollo"] = {"email": _cached, "source": "cache", "cached": True,
                                  "credit_used": False, "degraded": []}
        elif _credits_left == 0:
            evidence["apollo"] = {"source": "skipped", "credit_used": False,
                                  "degraded": ["apollo:credits-exhausted — using free finders + "
                                               "consensus verify instead"]}
        elif cand_urls:
            # Background-reveal the candidates and pick the person whose Apollo ORG / email-domain
            # matches the company — self-corrects when discovery's top link is the wrong person.
            best = _ap.reveal_best_of(cand_urls, name=name, company=company, domain=dom or "")
            evidence["apollo_candidates"] = best.get("tried", [])
            if best.get("email") and (_domain_ok(best["email"]) or best.get("match") in
                                      ("org", "org+domain", "domain")):
                _apollo_email = best["email"].strip().lower()
                _apollo_url = best.get("linkedin_url")
                evidence["apollo"] = {"email": _apollo_email, "org": best.get("org"),
                                      "person_name": best.get("person_name"),
                                      "match": best.get("match"), "source": "apollo-backend",
                                      "credit_used": best.get("credit_used", False), "degraded": []}
                # adopt Apollo's authoritative person name when discovery had none/weak
                if best.get("person_name"):
                    evidence["apollo"]["person_name"] = best["person_name"]
            elif best.get("tried"):
                _m = next((c for c in best["tried"] if c.get("email")), None)
                if _m:
                    evidence.setdefault("apollo", {})["domain_mismatch"] = (
                        f"{_m['email']} (org={_m.get('org')}) doesn't match {company or dom} — "
                        "rejected; see evidence.apollo_candidates.")

        if _apollo_email:
            _add(_apollo_email, "apollo-cdp")
            # Apollo already validated this (~90%). Do a FAST MX-only check (skip the slow SMTP
            # probe) and short-circuit instantly with high confidence — the speed path for bulk.
            try:
                _v = verify(_apollo_email, check_smtp=False)
            except Exception:
                _v = {"deliverable": None, "confidence": "high", "checks": {}}
            if _v.get("deliverable") is not False:
                _edom = _apollo_email.split("@", 1)[1]
                _record_found(name, dom or _edom, _apollo_email, "apollo-cdp", "high")
                if dom and _v.get("deliverable") is True:
                    _tmpl = _infer_pattern(_apollo_email.split("@", 1)[0], first, last)
                    if _tmpl:
                        _learn_pattern(dom, _tmpl)
                evidence["fast_path"] = "apollo"
                _who = f"{name}{' at ' + company if company else ''}"
                _other = [{"linkedin_url": c.get("linkedin_url"), "email": c.get("email")}
                          for c in evidence.get("apollo_candidates", [])
                          if c.get("email") and c.get("email") != _apollo_email]
                return {"best": _apollo_email, "source": "apollo-cdp", "confidence": "high",
                        "verify": _v,
                        "candidates": [{"email": _apollo_email, "source": "apollo-cdp",
                                        "sources": ["apollo-cdp"], "deliverable": _v.get("deliverable"),
                                        "confidence": "high", "source_url": _apollo_url}] + _other,
                        "evidence": evidence,
                        "summary": f"Best email for {_who}: {_apollo_email} "
                                   f"(high confidence, from Apollo extension reveal)"}

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

    # 1b) Finder WATERFALL — free-tier finder DBs that return a REAL address for name+domain
    # (Apollo-level, NO Apollo credit). Each is independent, so two finders agreeing is a strong
    # consensus signal. Runs only the ones whose key is present; each is graceful + budget-gated.
    # Runs ALL active finders IN PARALLEL via ThreadPool (was sequential, now ~8s vs ~225s).
    # (source, fn, needs[env keys]) — Hunter finder reuses the existing HUNTER_API_KEY.
    _finders = [
        ("hunter-find",    hunter_find,          ["HUNTER_API_KEY"]),
        ("prospeo",        prospeo_find,          ["PROSPEO_API_KEY"]),
        ("tomba",          tomba_find,            ["TOMBA_API_KEY", "TOMBA_SECRET"]),
        ("getprospect",    getprospect_find,      ["GETPROSPECT_API_KEY"]),
        ("emailverify_io", emailverify_io_find,   ["EMAILVERIFY_IO_API_KEY"]),
        ("generect",       generect_find,         ["GENERECT_API_KEY"]),
        ("minelead",       minelead_find,         ["MINELEAD_API_KEY"]),
        ("snov",           snov_find,             ["SNOV_USER_ID", "SNOV_SECRET"]),
        ("skrapp",         skrapp_find,           ["SKRAPP_API_KEY"]),
    ]
    if dom and first and not _over_budget():
        _active_finders = [(src, fn) for src, fn, need in _finders
                           if all(get_env(k) for k in need)]
        if _active_finders:
            _finder_deadline = _t0 + _budget
            _finder_timeout = max(1.0, _finder_deadline - _time.monotonic())
            _agreements: dict[str, list[str]] = {}  # email → list of finders that agreed

            def _call_finder(src_fn: tuple) -> tuple:
                _src, _fn = src_fn
                try:
                    return _src, _fn(name, dom)
                except Exception:  # noqa: BLE001
                    return _src, None

            _finder_pool = _cf.ThreadPoolExecutor(max_workers=min(8, len(_active_finders)))
            _finder_futs = {_finder_pool.submit(_call_finder, sf): sf[0]
                            for sf in _active_finders}
            try:
                for _fut in _cf.as_completed(_finder_futs, timeout=_finder_timeout):
                    try:
                        _src, _r = _fut.result()
                    except Exception:  # noqa: BLE001
                        continue
                    evidence[_src.replace("-", "_")] = _r
                    if isinstance(_r, dict) and _r.get("email"):
                        _em = _r["email"].strip().lower()
                        _add(_em, _src)
                        _maybe_learn_pattern(_em)
                        # Early-exit: ≥2 independent finders agree on the same address = strong consensus
                        _agreements.setdefault(_em, []).append(_src)
                        if len(_agreements[_em]) >= 2:
                            for _f in _finder_futs:
                                _f.cancel()
                            evidence["finder_early_exit"] = f"≥2 finders agreed on {_em}"
                            break
            except _cf.TimeoutError:
                evidence["finder_partial"] = True  # budget expired; keep partial results
            finally:
                _finder_pool.shutdown(wait=False, cancel_futures=True)

        # LinkedIn→email (Prospeo) — credit-independent reveal alt for the discovered profile
        _li = linkedin_url or evidence.get("linkedin_url")
        if _li and get_env("PROSPEO_API_KEY") and not _over_budget():
            try:
                _pl = prospeo_find_by_linkedin(_li)
                evidence["prospeo_linkedin"] = _pl
                if isinstance(_pl, dict) and _pl.get("email"):
                    _add(_pl["email"], "prospeo")
            except Exception:  # noqa: BLE001
                pass

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

    # --- Fast path: if the cheap/targeted steps already produced a verified extension hit or a
    # verified on-domain address, return NOW and skip the slow waterfall (web/Wayback/harvest/OSINT).
    _fast = _evaluate(verify_cap=5, check_smtp=_smtp_on())
    if _qualifies(_fast["top"]):
        evidence["fast_path"] = True
        return _success(_fast["top"], _fast["out_candidates"])

    # Apollo / cheap-step fast path didn't land a confident answer → fall through to the GENUINE free
    # keyless backbone so the pipeline still resolves a REAL email (never empty just because the
    # optional Apollo reveal missed). For the extension path this is BOUNDED + fast: web search + site
    # harvest + scrape + github + reveal under FIND_BUDGET_FALLBACK_S with MX-only verify, skipping the
    # slow/low-yield Wayback + OSINT (those are reserved for deep=True). No fabricated guesses — only
    # genuinely-sourced addresses; bare name+domain patterns stay clearly low/inconclusive.
    if not _run_slow:
        _run_slow = True
        _ext_fallback = not deep   # bounded genuine fallback for the extension path
        _budget = max(_budget, FIND_BUDGET_FALLBACK_S if _ext_fallback else FIND_BUDGET_S)
        evidence["fell_through"] = ("extension miss → bounded genuine fallback"
                                    if _ext_fallback else "extension miss → full free waterfall")

    # Slow keyless waterfall (steps 4–5.6), each gated by the wall-clock budget so it can never grind.

    # 4) free multi-engine web search anywhere on the web (capped by the remaining find budget)
    if _run_slow and not _over_budget():
        _remain = max(4.0, _budget - (_time.monotonic() - _t0))
        web = _web_search_emails(name, dom, company, budget_s=_remain)
        if web:
            evidence["web"] = web
            for item in web[:5]:
                if not item["role"]:
                    _add(item["email"], "web")

    # 5) Wayback Machine archived about/team/contact pages (skipped in the bounded extension fallback)
    if _run_slow and not _ext_fallback and dom and not _over_budget():
        wb = _wayback_emails(dom, name, max_snaps=2)
        if wb:
            evidence["wayback"] = wb
            for item in wb[:5]:
                if not item["role"]:
                    _add(item["email"], "wayback")

    # 5.5) Deep site harvest: sitemap + llms.txt + Cloudflare cfemail decode + Common Crawl (free
    # ~unlimited web archive) + theHarvester OSINT (if installed). Skipped in the bounded extension
    # fallback (reserved for deep=True / non-extension).
    # All 3 sources run IN PARALLEL now (was sequential — could take 240s+, now ~20-30s wall-clock).
    if _run_slow and not _ext_fallback and dom and not _over_budget():
        _harvest_all: dict[str, int] = {}
        _harvest_deadline = _t0 + _budget
        _harvest_timeout = max(2.0, _harvest_deadline - _time.monotonic())

        def _run_harvest_emails() -> dict:
            try:
                return harvest.harvest_emails(dom, name=name) or {}
            except Exception:  # noqa: BLE001
                return {}

        def _run_commoncrawl() -> dict:
            try:
                return harvest.commoncrawl_emails(dom, name=name) or {}
            except Exception:  # noqa: BLE001
                return {}

        def _run_theharvester() -> dict:
            try:
                return harvest.theharvester_emails(dom) or {}
            except Exception:  # noqa: BLE001
                return {}

        _harvest_pool = _cf.ThreadPoolExecutor(max_workers=3)
        _harvest_futs = [
            _harvest_pool.submit(_run_harvest_emails),
            _harvest_pool.submit(_run_commoncrawl),
            _harvest_pool.submit(_run_theharvester),
        ]
        try:
            for _hf in _cf.as_completed(_harvest_futs, timeout=_harvest_timeout):
                try:
                    for em, weight in (_hf.result() or {}).items():
                        _harvest_all[em] = max(_harvest_all.get(em, 0), weight)
                except Exception:  # noqa: BLE001
                    continue
        except _cf.TimeoutError:
            evidence["harvest_partial"] = True
        finally:
            _harvest_pool.shutdown(wait=False, cancel_futures=True)

        if _harvest_all:
            evidence["harvest"] = list(_harvest_all.keys())[:10]
            for em, weight in _harvest_all.items():
                local = em.split("@", 1)[0].lower()
                if not _is_role(em) and ((first and first in local) or (last and last in local)):
                    _add(em, "harvest")
                    _maybe_learn_pattern(em)

    # 5.6) OSINT — PGP keyservers + crt.sh CT emails (skipped in the bounded extension fallback)
    if _run_slow and not _ext_fallback and dom and not _over_budget():
        try:
            from mcp_base import osint_engines as _osint
            pgp_emails = _osint.pgp_search(name, domain=dom)
            if pgp_emails:
                evidence["pgp"] = pgp_emails
                for em in pgp_emails:
                    _add(em, "pgp")
            crt_emails = _osint.crtsh_emails(dom)
            if crt_emails:
                evidence["crtsh"] = crt_emails[:20]
                for em in crt_emails:
                    local = em.split("@", 1)[0].lower()
                    if (first and first in local) or (last and last in local):
                        _add(em, "crtsh")
        except Exception:
            pass
    if _over_budget():
        evidence["timed_out"] = True

    # 5.7) deep fallback: BFS crawl + web search (only when deep=True)
    if deep and dom:
        try:
            df = harvest.deep_fallback(name, dom, company)
            evidence["deep_crawl"] = {"degraded": df.get("degraded", [])}
            for cand in df.get("candidates", [])[:15]:
                em = (cand.get("email") or "").strip().lower()
                if em and not _is_role(em):
                    _add(em, "deep_crawl")
        except Exception:
            pass

    # 5.8) AI agentic browsing (opt-in) — observe→think→act over the real Chrome for hard cases
    if use_agent and dom:
        try:
            from mcp_base import agent_browse as _agent
            ar = _agent.browse_for_email(name, company=company, domain=dom)
            if isinstance(ar, dict) and ar.get("emails"):
                evidence["agent_browse"] = {"emails": ar["emails"][:10],
                                            "note": ar.get("note")}
                for em in ar["emails"]:
                    em = (em or "").strip().lower()
                    if em and "@" in em and not _is_role(em):
                        _add(em, "agent")
        except Exception:
            pass

    # 6) generic name+domain patterns (fallback)
    if dom:
        # F3: ML pattern ranker — if we've learned this domain's emails before, predict its most
        # likely local-part template and float that candidate first; else static 13-pattern list.
        try:
            from mcp_base.frontier.pattern_ml import predict_pattern
            rows = store.query("SELECT email, name FROM found_emails WHERE domain=?", (dom,)) or []
            tmpl = predict_pattern(dom, [dict(r) for r in rows])
            if tmpl:
                rendered = _render_hunter_pattern(tmpl, first, last)
                if rendered:
                    evidence["ml_pattern"] = {"template": tmpl}
                    _add(f"{rendered}@{dom}", "learned")
        except Exception:
            pass
        pats = _patterns(name, dom)
        evidence["patterns"] = pats
        for p in pats:
            _add(p, "pattern")

    # Full pass: verify the top candidates from ALL steps and rank. MX-only (fast) when Apollo-driven
    # or in the bounded extension fallback; full SMTP only on the non-extension / deep waterfall.
    final = _evaluate(verify_cap=12, check_smtp=_smtp_on())
    pool = final["pool"]
    out_candidates = final["out_candidates"]

    # CONFIRM-THE-WINNER (catch-all / SMTP-blocked disambiguation): if the leading candidate isn't
    # already confirmed deliverable, deep-verify the top on-domain pattern variants in parallel so
    # free account-existence ENUMERATION picks the local-part that actually exists (the real mailbox
    # has Google/Gravatar/GitHub accounts; wrong guesses don't) — keyless, works where SMTP/APIs can't.
    _top = final.get("top")
    if dom and _top and _top.get("deliverable") is not True and not _over_budget():
        on_dom = []
        for c in pool:
            em = c.get("email", "")
            if em.split("@", 1)[-1].lower().endswith(dom) and em not in on_dom:
                on_dom.append(em)
        confirmed = _confirm_winner(on_dom[:4]) if on_dom else None
        if confirmed and confirmed["verify"].get("deliverable") is not False:
            cbase = next((c for c in pool if c["email"] == confirmed["email"]), None) or {
                "email": confirmed["email"], "source": "enumeration", "sources": ["enumeration"],
                "corrob": 1, "weight": 0, "source_url": _top.get("source_url")}
            winner = {**cbase, "verify": confirmed["verify"],
                      "deliverable": confirmed["verify"].get("deliverable"),
                      "confidence": confirmed["verify"].get("confidence")}
            evidence["confirmed_via"] = confirmed.get("via")
            # float the confirmed email to the front of the returned candidate list
            out_candidates = ([{"email": winner["email"], "source": winner["source"],
                                "sources": winner.get("sources", []),
                                "deliverable": winner["deliverable"],
                                "confidence": winner["confidence"],
                                "source_url": winner.get("source_url")}]
                              + [c for c in out_candidates if c.get("email") != winner["email"]])
            return _success(winner, out_candidates)

    if not pool:
        _degraded: list[str] = []
        if evidence.get("timed_out"):
            _degraded.append("budget:timeout")
        if evidence.get("finder_partial"):
            _degraded.append("finders:partial")
        if dom:
            # Return best genuinely-sourced candidate even if unverified, rather than empty.
            if out_candidates:
                _bc = out_candidates[0]
                _best_is_pure_pattern = _bc.get("source") in ("pattern", "pattern-only")
                if not _best_is_pure_pattern:
                    return {
                        "best": _bc["email"], "source": _bc.get("source", "unknown"),
                        "confidence": "low", "partial": bool(_degraded),
                        "degraded": _degraded or None,
                        "note": "unverified best-guess — no deliverable address confirmed",
                        "candidates": out_candidates, "evidence": evidence,
                        "summary": (f"No confirmed email for {name}; "
                                    f"best candidate: {_bc['email']} (unverified, {_bc.get('source')})")
                    }
            pats = _patterns(name, dom)
            if pats:
                # Store negative cache only for genuine misses (not pattern guesses available)
                if dom and not deep and not use_extensions and not use_agent:
                    _neg_cache_put(name, dom)
                return {"best": pats[0], "source": "pattern-only", "confidence": "low",
                        "partial": bool(_degraded), "degraded": _degraded or None,
                        "note": "unverified best-guess", "candidates": out_candidates,
                        "evidence": evidence,
                        "summary": f"No verified email found for {name}; best-guess pattern: {pats[0]}"}
        # Truly no result — store in neg cache to short-circuit future identical searches
        if dom and not deep and not use_extensions and not use_agent:
            _neg_cache_put(name, dom)
        return {"best": None, "confidence": "none", "partial": bool(_degraded),
                "degraded": _degraded or None,
                "note": "need a domain (and/or github username) to resolve", "evidence": evidence,
                "summary": f"Could not resolve email for {name} — provide a domain or github username"}

    return _success(final["top"], out_candidates)


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
        resolved = _find_core(name, company=company, domain=domain, github=github, scrape=scrape)
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


def _norm_person(p) -> dict | None:
    """Normalize a bulk item: a dict {name?,company?,domain?,linkedin_url?,github?} or a bare
    string (treated as a company name). Returns None for an unusable/empty item."""
    if isinstance(p, str):
        p = p.strip()
        return {"company": p} if p else None
    if isinstance(p, dict):
        out = {k: (str(p.get(k) or "").strip()) for k in
               ("name", "company", "domain", "linkedin_url", "github")}
        return out if any(out.values()) else None
    return None


def _bulk_core(people: list, use_extensions: bool, role: str, persist: bool,
               cap: int, progress=None) -> dict:
    """Shared bulk engine. Phase 1: resolve missing LinkedIn URLs in PARALLEL (network). Phase 1.5:
    cache skim. Phase 2: reveal SEQUENTIALLY through the one hidden Apollo panel (warm). Returns a
    per-row table + summary. `progress(done,total,partial)` is called after each reveal."""
    # Normalize, keeping unusable items as explicit error rows (never silently dropped).
    _normed = [(_norm_person(p), p) for p in (people or [])][:cap]
    items = [x for x, _ in _normed if x]
    invalid_rows = [{"name": None, "company": None, "domain": None, "linkedin_url": None,
                     "email": None, "confidence": None, "source": None, "credit_used": False,
                     "status": "error", "error": "missing name/company/domain", "input": _raw}
                    for x, _raw in _normed if not x]
    # dedupe by linkedin_url, else name+company
    seen, uniq = set(), []
    for it in items:
        key = (it.get("linkedin_url") or f"{it.get('name')}|{it.get('company')}").lower()
        if key in seen:
            continue
        seen.add(key)
        uniq.append(it)
    items = uniq
    rows: list[dict] = [{"name": it.get("name") or None, "company": it.get("company") or None,
                         "domain": it.get("domain") or None,
                         "linkedin_url": it.get("linkedin_url") or None,
                         "email": None, "confidence": None, "source": None,
                         "credit_used": False, "status": "pending"} for it in items]

    from concurrent.futures import ThreadPoolExecutor, as_completed as _ac

    # Phase 1 — resolve company-only items. PRIMARY: Apollo's OWN people DB (cached + authoritative,
    # company→person→email in one call — most rows get the verified email here with NO reveal).
    # FALLBACK: public web discovery (find_exec). Parallel across rows; Apollo calls serialize on the
    # shared CDP lock internally, so the pool just overlaps the slow find_exec fallbacks.
    if use_extensions:
        from mcp_base import people as _people
        from mcp_base import apollo_cdp as _ap
        from mcp_base.dns_resolve import _domain_candidates

        def _resolve(i: int) -> None:
            it = items[i]
            if it.get("linkedin_url") or not it.get("company"):
                return
            # PRIMARY — Apollo people-search (cached; instant on repeat companies)
            try:
                _guess = (_domain_candidates(it["company"]) or [""])[0]
                aps = _ap.apollo_people_search(_guess, role=role, company=it["company"])
                ppl = aps.get("people", [])
                if ppl:
                    it["domain"] = it.get("domain") or aps.get("canonical_domain") or _guess
                    best = _pick_role_person(ppl, role)
                    if best:
                        it["name"] = it.get("name") or best.get("name")
                        it["linkedin_url"] = (best.get("linkedin_url") or "").split("?")[0]
                        it["_apollo_seed"] = best   # carries the verified email for a no-reveal settle
                    rows[i].update(linkedin_url=it.get("linkedin_url") or None,
                                   domain=it.get("domain") or None, name=it.get("name") or None)
                    return
            except Exception:
                pass
            # FALLBACK — public web discovery (bounded; Apollo already tried)
            try:
                person = _people.find_exec(it["company"], role=role, budget_s=12.0)
                it["linkedin_url"] = (person.get("linkedin_url") or "").split("?")[0]
                it["domain"] = it.get("domain") or (person.get("domain") or "")
                if not it.get("name") and person.get("name"):
                    it["name"] = person["name"]
                rows[i]["linkedin_url"] = it.get("linkedin_url") or None
                rows[i]["domain"] = it.get("domain") or None
                rows[i]["name"] = it.get("name") or None
            except Exception:
                pass

        need = [i for i, it in enumerate(items) if use_extensions and not it.get("linkedin_url")
                and it.get("company")]
        if need:
            with ThreadPoolExecutor(max_workers=6) as pool:
                list(_ac([pool.submit(_resolve, i) for i in need]))

    # Phase 1.5 — cache skim (instant, no Chrome).
    for i, it in enumerate(items):
        cached = _cached_reveal(it.get("name") or "", it.get("domain") or "")
        if cached:
            rows[i].update(email=cached, confidence="high", source="apollo-cdp", status="cached")

    # Phase 2 — resolve via the proven find() path.
    # For use_extensions=True: sequential (single Apollo panel can only handle one person at a time).
    # For use_extensions=False: PARALLEL (each _find_core is independent, max 5 concurrent).
    # 50-item batch: was ~37min sequential → ~4-5min parallel.
    # Mutable state shared across the resolve workers (avoids nonlocal in threads).
    _state = {"credits_used": 0, "credits_exhausted": False}

    def _resolve_one(i: int) -> None:
        it = items[i]
        if rows[i]["status"] == "cached":
            return
        if _state["credits_exhausted"]:
            rows[i]["status"] = "skipped-credits"
            return
        try:
            res = _find_core(it.get("name") or "", company=it.get("company") or "",
                             domain=it.get("domain") or "", github=it.get("github") or "",
                             linkedin_url=it.get("linkedin_url") or "", use_extensions=use_extensions,
                             apollo_seed=it.get("_apollo_seed"))
            best = res.get("best")
            ap = (res.get("evidence") or {}).get("apollo") or {}
            if any("exhaust" in str(d) or "all-credits" in str(d) for d in ap.get("degraded", [])):
                _state["credits_exhausted"] = True
            if ap.get("credit_used"):
                _state["credits_used"] += 1
            status = ("found" if best
                      else ("unresolved" if use_extensions and not it.get("linkedin_url")
                            else "no-email"))
            rows[i].update(name=rows[i]["name"] or it.get("name"), email=best,
                           confidence=res.get("confidence"), source=res.get("source"),
                           credit_used=bool(ap.get("credit_used")), status=status)
            if best and persist:
                try:
                    _write_contact(rows[i]["name"] or "", it.get("company") or "", best,
                                   it.get("domain") or "", role, it.get("github") or "",
                                   f"email-finder:{res.get('source') or 'apollo-cdp'}",
                                   res.get("confidence") or "medium", "bulk_find")
                except Exception:
                    pass
        except Exception as e:  # noqa: BLE001
            rows[i].update(status="error", error=str(e)[:80])

    pending_idxs = [i for i in range(len(items)) if rows[i]["status"] != "cached"]

    if use_extensions:
        # Sequential for Apollo CDP (single panel, one person at a time).
        for i in pending_idxs:
            _resolve_one(i)
            if progress:
                progress(i + 1, len(items), rows)
            # Politeness pause ONLY after a real Apollo reveal (not cache or seeded rows).
            _settled_by_seed = bool(items[i].get("_apollo_seed") and items[i]["_apollo_seed"].get("email"))
            if BULK_REVEAL_DELAY_S and i < len(items) - 1 and not _settled_by_seed:
                _time.sleep(BULK_REVEAL_DELAY_S)
    else:
        # Parallel for keyless find — each _find_core is fully independent, max 5 concurrent.
        # 50 items: ~37min sequential → ~4-5min parallel.
        _done_count = 0
        with _cf.ThreadPoolExecutor(max_workers=5) as _bulk_pool:
            _bulk_futs = {_bulk_pool.submit(_resolve_one, i): i for i in pending_idxs}
            for _fut in _cf.as_completed(_bulk_futs):
                _done_count += 1
                try:
                    _fut.result()
                except Exception:  # noqa: BLE001
                    pass
                if progress:
                    progress(_done_count, len(items), rows)

    rows = rows + invalid_rows  # surface unusable inputs as error rows
    found = sum(1 for r in rows if r.get("email"))
    return {"count": len(rows), "found": found,
            "cached": sum(1 for r in rows if r.get("status") == "cached"),
            "no_email": sum(1 for r in rows if r.get("status") in ("no-email", "unresolved")),
            "errors": sum(1 for r in rows if r.get("status") == "error"),
            "credits_used": _state["credits_used"], "results": rows}


@mcp.tool
def bulk_find(people: list, use_extensions: bool = False, role: str = "CEO",
              persist: bool = False) -> dict:
    """Resolve emails for a BATCH, fast. Each item is {name?, company?, domain?, linkedin_url?,
    github?} or a bare company-name string. With use_extensions=True it pipelines the Apollo
    background reveal: LinkedIn URLs for company-only items are resolved in PARALLEL, then emails are
    revealed SEQUENTIALLY through the one hidden side panel (warm ~2-5s each; already-found people are
    instant from cache and cost no credit). role= is used to pick the person for company-only items.
    persist=True writes found emails to your contacts. Returns a per-row table + summary. For large
    lists use bulk_find_async(). (Synchronous; capped at 50 — bigger batches: bulk_find_async.)"""
    if not isinstance(people, list):
        return {"count": 0, "found": 0, "results": [], "error": "people must be a list"}
    return _bulk_core(people, use_extensions, role, persist, cap=50)


@mcp.tool
def bulk_find_async(people: list, use_extensions: bool = True, role: str = "CEO",
                    persist: bool = False) -> dict:
    """Background BULK reveal for large lists (cap 500). Same pipeline as bulk_find but runs off the
    request path and is live-pollable: returns a job_id; poll find_status(job_id) for rising percent
    + partial results, or list_find_jobs(). Fully background (hidden Chrome). Each item is a dict or a
    company-name string; persist=True writes found emails to contacts."""
    if not isinstance(people, list):
        return {"count": 0, "found": 0, "results": [], "error": "people must be a list"}

    def _worker(job: dict) -> None:
        JOBS.set(job["id"], status="running", percent=1.0, total=len(people[:500]))

        def _progress(done: int, total: int, partial: list) -> None:
            JOBS.set(job["id"], percent=round(100.0 * done / max(total, 1), 1),
                     done=done, total=total,
                     found=sum(1 for r in partial if r.get("email")), results=partial)

        try:
            out = _bulk_core(people, use_extensions, role, persist, cap=500, progress=_progress)
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=str(e))
            return
        JOBS.finish(job["id"], ok_=True, **out)

    return JOBS.run_or_job("bulk_find", _worker, count=len(people[:500]))


def _bulk_verify_core(emails: list[str]) -> dict:
    clean = [e.strip() for e in emails if isinstance(e, str) and e.strip()][:200]
    from concurrent.futures import ThreadPoolExecutor, as_completed as _ac
    results: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        futs = {pool.submit(verify, e): e for e in clean}
        # overall cap so the batch can't run for minutes; per-item cap so one mailbox can't stall it
        try:
            for f in _ac(futs, timeout=max(30.0, len(clean) * 1.5)):
                e = futs[f]
                try:
                    results[e] = f.result(timeout=PER_VERIFY_S)
                except Exception:
                    results[e] = {"email": e, "deliverable": None, "confidence": "low",
                                  "degraded": ["verify:timeout"]}
        except Exception:
            pass
    finally:
        pool.shutdown(wait=False, cancel_futures=True)
    for e in clean:
        results.setdefault(e, {"email": e, "deliverable": None, "confidence": "low",
                               "degraded": ["verify:timeout"]})
    ordered = [results[e] for e in clean]
    deliverable = sum(1 for r in ordered if r.get("deliverable") is True)
    undeliverable = sum(1 for r in ordered if r.get("deliverable") is False)
    unknown = len(ordered) - deliverable - undeliverable
    return {"count": len(ordered), "deliverable": deliverable,
            "undeliverable": undeliverable, "unknown": unknown, "results": ordered}


@mcp.tool
def bulk_verify(emails: list[str]) -> dict:
    """Verify a batch of emails in parallel (shared cache + quota-aware). Each item is verified via
    the full pipeline. Returns per-email results + a roll-up {deliverable/undeliverable/unknown}.
    Small batches return inline; large batches (>25) return a {job_id} to poll with find_status()."""
    if not isinstance(emails, list):
        return {"count": 0, "results": [], "error": "emails must be a list"}
    if len([e for e in emails if isinstance(e, str) and e.strip()]) <= 25:
        return _bulk_verify_core(emails)

    def _worker(job: dict) -> None:
        JOBS.set(job["id"], status="running", percent=5.0)
        try:
            out = _bulk_verify_core(emails)
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=str(e))
            return
        JOBS.finish(job["id"], ok_=True, **out)

    return JOBS.run_or_job("bulk_verify", _worker, count=len(emails[:200]))


@mcp.tool
def find_deep_async(name: str, company: str = "", domain: str = "", github: str = "",
                    use_agent: bool = False) -> dict:
    """Never-give-up deep find as a BACKGROUND job: runs the full deep cascade (harvest + OSINT +
    BFS crawl + web search, optionally AI agentic browsing) off the request path. Returns a job_id
    immediately if it runs long; poll find_status(job_id). Short runs return the result inline."""
    def _worker(job: dict) -> None:
        JOBS.set(job["id"], status="running", percent=10.0)
        try:
            res = _find_core(name, company=company, domain=domain, github=github,
                             deep=True, use_agent=use_agent)
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=str(e))
            return
        JOBS.finish(job["id"], ok_=True, **{k: res.get(k) for k in
                    ("best", "source", "confidence", "summary", "candidates", "evidence")})

    return JOBS.run_or_job("deep_find", _worker, name=name, company=company, domain=domain)


@mcp.tool
def find_status(job_id: str) -> dict:
    """Poll a find_deep_async() background job by its job_id. Returns status + result when done."""
    return JOBS.status(job_id)


@mcp.tool
def list_find_jobs(limit: int = 20) -> dict:
    """List recent background deep-find jobs and their statuses."""
    return JOBS.listing(limit)


@mcp.tool
def provider_fingerprint(domain: str) -> dict:
    """Identify a domain's mail provider (Google/M365/Zoho/gateway) via MX+SPF+DKIM+DMARC+MTA-STS
    and return its verification playbook (SMTP reliability, accept-all behavior, pattern prior)."""
    try:
        from mcp_base.frontier.fingerprint import fingerprint
        return fingerprint(domain)
    except Exception as e:  # noqa: BLE001
        return {"domain": domain, "error": str(e)}


@mcp.tool
def frontier_status() -> dict:
    """Report which frontier layers (F1-F10) are currently active (their optional dep/service)."""
    try:
        from mcp_base import frontier
        return {"capabilities": frontier.capabilities()}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@mcp.tool
def cdp_setup(do_clone: bool = False) -> dict:
    """One-time setup for background Apollo reveal — clone your REAL Chrome profile (Apollo + logins)
    into a persistent debug profile and launch it. Idempotent + persistent: after the first run the
    login is saved forever and future calls just auto-start it (no re-clone, no re-login).

    Call cdp_setup() to check/advance state; cdp_setup(do_clone=True) to actually clone+launch.
    The clone needs your normal Chrome fully quit ONCE (for a clean cookie copy)."""
    from mcp_base import cdp as _cdp
    # Already set up? just ensure it's running — nothing repeats.
    ok, msg = _cdp.ensure_running()
    if ok:
        return {"status": "ready", "detail": msg, "set_up": True,
                "next": "Open a LinkedIn profile + the Apollo side panel once, then "
                        "find(name=..., company=..., use_extensions=True)."}
    if _cdp.is_set_up():
        return {"status": "starting", "detail": msg, "set_up": True,
                "next": "Profile exists; Chrome is starting — re-run cdp_status() in a moment."}
    if not do_clone:
        return {"status": "needs-setup", "set_up": False,
                "steps": [
                    "1. Fully QUIT Google Chrome (one time, for a clean copy of cookies/logins).",
                    "2. Re-run cdp_setup(do_clone=True) — clones your Default profile (Apollo + "
                    "LinkedIn logins, caches excluded) into a persistent debug profile and launches it.",
                    "3. In the debug Chrome, open a LinkedIn profile + the Apollo side panel once.",
                    "After this, it persists forever and auto-starts — you never repeat these steps."],
                "note": "Pass do_clone=True once Chrome is quit."}
    # Perform the one-time clone + launch.
    if _cdp.chrome_running():
        return {"status": "chrome-still-running", "set_up": False,
                "action_needed": "Quit Google Chrome completely, then call cdp_setup(do_clone=True) "
                                 "again. (Needed for a clean copy of your logged-in cookies.)"}
    cloned_ok, info = _cdp.clone_profile("Default")
    if not cloned_ok:
        return {"status": "clone-failed", "set_up": False, "error": info}
    launched_ok, lmsg = _cdp.launch()
    ok2, _ = _cdp.ensure_running()
    return {"status": "set-up" if ok2 else "launched", "set_up": True,
            "clone_size": info, "launch": lmsg, "reachable": ok2,
            "next": "In the debug Chrome: log into LinkedIn/Apollo if prompted (persists), open a "
                    "LinkedIn profile + the Apollo side panel once, then "
                    "find(..., use_extensions=True). You won't repeat setup again."}


@mcp.tool
def cdp_status() -> dict:
    """Health/diagnose the background reveal pipeline (Chrome DevTools Protocol).

    Background Apollo/ContactOut/Lusha reveal needs a Chrome launched with remote debugging
    (the side panel is only reachable over CDP, not AppleScript). This reports whether that debug
    Chrome is up, lists the relevant targets (LinkedIn page + the extension side panel), and — if
    not ready — returns the exact one-time launch command + setup steps."""
    from mcp_base import cdp as _cdp
    out: dict = {"port": _cdp.CDP_PORT, "profile": _cdp.CDP_PROFILE,
                 "set_up": _cdp.is_set_up()}
    # Auto-start the persistent profile if it's set up but not running (no clone, no login).
    ok, info = _cdp.ensure_running()
    out["reachable"] = ok
    if not ok:
        out["reason"] = info
        if _cdp.is_set_up():
            out["hint"] = "Profile is set up but Chrome isn't up yet — retry in a moment."
        else:
            out["setup"] = [
                "One-time setup (login persists forever after):",
                "1. Run cdp_setup(do_clone=True) — it clones your real Chrome profile (Apollo + logins) "
                "into a persistent debug profile and launches it.",
                "2. In that debug Chrome, open any LinkedIn profile and open the Apollo side panel once.",
                "3. Done — future runs auto-start this profile; you never repeat these steps.",
            ]
        return out
    out["browser"] = info
    tgts = _cdp.targets()
    out["target_count"] = len(tgts)
    li = _cdp.find_target(url_substr="linkedin.com")
    out["linkedin_target"] = {"title": li.get("title"), "url": (li.get("url") or "")[:80]} if li else None
    ext_targets = [{"title": t.get("title"), "url": (t.get("url") or "")[:60]}
                   for t in tgts if (t.get("url") or "").startswith("chrome-extension://")]
    out["extension_targets"] = ext_targets[:10]
    from mcp_base import apollo_cdp as _ap
    panel = _ap._find_panel("apollo")
    out["apollo_panel_open"] = bool(panel)
    if not panel:
        out["hint"] = "Apollo side panel not detected — open it once on a LinkedIn profile."
    out["ready"] = bool(li and panel)
    return out


def _configured_sources() -> dict:
    """Which free finder/verifier integrations are active (so you can see your headroom). Keyless
    sources (Hunter email-finder reuses HUNTER_API_KEY; Common Crawl; Rapid/Disify verifiers) need
    no key and are always on."""
    finders = {"apollo_people_search": True, "commoncrawl": True,  # keyless, always on
               "hunter": bool(get_env("HUNTER_API_KEY")),          # domain-search + finder
               "tomba": bool(get_env("TOMBA_API_KEY") and get_env("TOMBA_SECRET")),
               "prospeo": bool(get_env("PROSPEO_API_KEY")),
               "getprospect": bool(get_env("GETPROSPECT_API_KEY")),
               "minelead": bool(get_env("MINELEAD_API_KEY")),
               "emailverify_io": bool(get_env("EMAILVERIFY_IO_API_KEY")),
               "generect": bool(get_env("GENERECT_API_KEY"))}
    verifiers = {"rapid": True, "disify": True,                    # keyless, always on
                 "reoon": bool(get_env("REOON_API_KEY")), "hunter": bool(get_env("HUNTER_API_KEY")),
                 "myemailverifier": bool(get_env("MYEMAILVERIFIER_API_KEY")),
                 "abstract": bool(get_env("ABSTRACT_API_KEY")),
                 "mailboxlayer": bool(get_env("MAILBOXLAYER_API_KEY")),
                 "verifalia": bool(get_env("VERIFALIA_USERNAME") and get_env("VERIFALIA_PASSWORD")),
                 "reacher_selfhost": bool(get_env("REACHER_BASE_URL"))}
    return {"finders": finders, "verifiers": verifiers,
            "finders_active": sum(finders.values()), "verifiers_active": sum(verifiers.values())}


@mcp.tool
def apollo_credit_status() -> dict:
    """Remaining Apollo reveal credits for your team — FREE to check (spends nothing). Returns
    {ok, total, used, remaining, pct_used, exhausted}. When credits run low/out the pipeline keeps
    working: the FREE apollo_people_search identity + the finder/verify backbone resolve CONFIRMED
    emails with no Apollo credit."""
    from mcp_base import apollo_cdp as _ap
    return _ap.apollo_credit_status()


@mcp.tool
def extension_pool_status() -> dict:
    """Free reveal-credit balance per extension + Apollo credit balance + which finder/verifier keys
    are configured + background-pipeline readiness.

    Note: the LinkedIn side panel can't be detected via AppleScript; installation/login state is
    reported through the CDP pipeline instead. See cdp_status() for setup."""
    try:
        from mcp_base import extensions as _ext
        from mcp_base import apollo_cdp as _ap
        status = _ext.pool_status()   # {name: {cap, used, remaining}}
        cdp = cdp_status()
        return {"pool": status,
                "apollo_credits": _ap.apollo_credit_status(),
                "free_sources": _configured_sources(),
                "cdp": {"reachable": cdp.get("reachable"), "ready": cdp.get("ready"),
                        "apollo_panel_open": cdp.get("apollo_panel_open"),
                        "launch_command": cdp.get("launch_command")},
                "note": "Opt-in via find(use_extensions=True). At 0 Apollo credits the FREE "
                        "people-search identity + finder/verify backbone still resolve confirmed "
                        "emails. Add free finder/verifier keys (see free_sources) to widen headroom."}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


@mcp.tool
def apollo_reveal_one(linkedin_url: str, domain: str = "") -> dict:
    """Reveal one person's email in the BACKGROUND by replaying Apollo's own authenticated call in
    its service-worker context (no window, no side panel, ~1s). Returns {email, emails, source, ms,
    degraded}. The reliable primary reveal path; spends one free Apollo credit on a fresh reveal."""
    from mcp_base import apollo_cdp as _ap
    return _ap._reveal_via_apollo_backend(linkedin_url=linkedin_url, domain=domain)


@mcp.tool
def apollo_people_search(company: str = "", domain: str = "", role: str = "CEO",
                         per_page: int = 5) -> dict:
    """Discover the right person at a company via Apollo's OWN people DB (no Bing). Filters by
    `domain`+role titles, then falls back to a fuzzy company-name match (self-correcting a wrong
    domain guess and returning the org's canonical domain). Returns the matching people with
    name, title, linkedin_url, and the verified email when Apollo already has it unlocked — the
    authoritative company→person→email source that powers find_by_company. Pass either a domain
    or a company name (both is best)."""
    from mcp_base import apollo_cdp as _ap
    return _ap.apollo_people_search(domain or "", role=role, company=company, per_page=per_page)


@mcp.tool
def apollo_capture_reveal(seconds: int = 75) -> dict:
    """(Re)capture Apollo's reveal call: records the extension's network for `seconds` while you do
    ONE manual 'Access email' click on a LinkedIn profile in the debug Chrome, then learns the
    endpoint. Only needed if Apollo changes its API — the captured call is already wired in."""
    from mcp_base import apollo_cdp as _ap
    return _ap.capture_reveal_manual(seconds=float(seconds))


@mcp.tool
def apollo_backend_status() -> dict:
    """Readiness of the background Apollo reveal: is the debug Chrome up, is the Apollo extension
    service worker present (carries the authenticated session), and the known reveal endpoint."""
    try:
        from mcp_base import apollo_cdp as _ap
        from mcp_base import cdp as _cdp
        ok, why = _cdp.ensure_running()
        sw = _ap._apollo_service_worker() if ok else None
        return {"cdp_reachable": ok, "cdp_detail": why,
                "service_worker_present": bool(sw),
                "endpoint": _ap._REVEAL_ENDPOINT,
                "ready": bool(ok and sw),
                "hint": ("ready — apollo_reveal_one(linkedin_url) works in the background"
                         if (ok and sw) else
                         "open the debug Chrome with the Apollo extension installed (cdp_setup)")}
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}


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


@mcp.tool
def find_by_company(company: str, role: str = "CEO", use_extensions: bool = False,
                    use_agent: bool = False, deep: bool = False) -> dict:
    """Find the email of the person holding `role` at `company` from a company name alone.
    Resolves: company → domain → exec name (free SERP + Wikipedia + Crunchbase + About page) →
    full email waterfall (harvest/OSINT/verify/patterns). Returns name, title, email, confidence,
    linkedin_url, candidates, summary. deep=True adds BFS crawl + web-search deep fallback.
    use_agent=True adds AI agentic browsing. use_extensions=True (opt-in, ToS-flagged,
    human-in-the-loop) drives your real Chrome's free reveal extensions on the exec's LinkedIn.

    Always inline-if-quick, else returns a {job_id} to poll with find_status() — since it now does
    free LinkedIn discovery + the full keyless waterfall, it can exceed the inline window; it then
    backgrounds instead of blocking."""

    def _worker(job: dict) -> None:
        JOBS.set(job["id"], status="running", percent=5.0)
        try:
            res = _find_by_company_core(company, role=role, use_extensions=use_extensions,
                                        use_agent=use_agent, deep=deep)
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=str(e))
            return
        JOBS.finish(job["id"], ok_=True, **res)

    return JOBS.run_or_job("find_by_company", _worker, company=company, role=role)


def _find_by_company_core(company: str, role: str = "CEO", use_extensions: bool = False,
                          use_agent: bool = False, deep: bool = False) -> dict:
    from mcp_base import people as _people
    name: str | None = None
    domain: str | None = None
    li: str | None = None
    discovery = None
    apollo_people = None
    person: dict = {}            # public-source record; filled only on the Apollo-miss fallback
    _apollo_cands: list[str] = []
    apollo_seed: dict | None = None

    # PRIMARY + FAST — Apollo's OWN people DB, tried FIRST (before any slow web search). Pass an
    # INSTANT local domain guess (pure string ops, no network) + the company name; apollo_people_search
    # cascades domain→fuzzy-name and returns the role person with name/title/linkedin + (usually) the
    # verified email directly. Authoritative, and short-circuits the whole pipeline in ~3-8s. On an
    # Apollo hit we SKIP find_exec/find_domain entirely (they were the 15-25s bottleneck).
    if use_extensions:
        try:
            from mcp_base import apollo_cdp as _ap
            from mcp_base.dns_resolve import _domain_candidates
            _guess = (_domain_candidates(company) or [""])[0]
            aps = _ap.apollo_people_search(_guess, role=role, company=company)
            ppl = aps.get("people", [])
            if ppl:
                apollo_people = ppl
                # fuzzy match → canonical domain; domain match → the guess we passed was right
                domain = aps.get("canonical_domain") or _guess or None
                best = _pick_role_person(ppl, role)
                if best:
                    name = best.get("name")
                    li = best.get("linkedin_url")
                    # authoritative seed: find_core uses its email directly when present, else
                    # reveals its linkedin_url. Carries name/title/org for evidence + ranking.
                    apollo_seed = best
                _apollo_cands = [p.get("linkedin_url") for p in ppl if p.get("linkedin_url")]
        except Exception:
            pass

    # FALLBACK public discovery — ONLY when Apollo didn't pin the person. Bounded: Apollo already
    # tried, so the public exec lookup runs on a tight budget (12s) instead of the full 25s.
    if not apollo_seed:
        person = _people.find_exec(company, role=role,
                                   budget_s=(12.0 if use_extensions else 25.0))
        name = name or person.get("name")
        domain = domain or person.get("domain") or _people.find_domain(company)
        li = li or person.get("linkedin_url")

    # Fallback LinkedIn discovery — background-Chrome search for the profile (gives the name the free
    # keyless waterfall needs). Skipped when Apollo people-search already gave a usable verified email.
    _have_seed_email = bool(apollo_seed and apollo_seed.get("email"))
    if (not li or not name) and not _have_seed_email:
        try:
            from mcp_base import apollo_cdp as _ap
            discovery = _ap.find_linkedin_profile(f"{company} {role}".strip(),
                                                  company=company, role=role)
            if discovery.get("top"):
                li = li or discovery["top"]
                if not name:
                    from mcp_base.people import _name_from_li_slug, _looks_like_name
                    _disc_name = discovery.get("top_name") or _name_from_li_slug(discovery["top"])
                    if _disc_name and _looks_like_name(_disc_name):
                        name = _disc_name
        except Exception:
            pass

    # Need a name, OR (for the Apollo path) a LinkedIn URL — Apollo reads whoever the profile shows.
    if not name and not li and not _apollo_cands:
        return {
            "company": company, "role": role, "domain": domain,
            "error": "could not resolve a person or LinkedIn profile from public sources",
            "person_sources": person.get("sources", []),
            "linkedin_discovery": (discovery.get("degraded") if discovery else None),
            "summary": f"No public record or LinkedIn profile found for {role} of {company}",
        }
    # Hand the RANKED candidate profiles to find() — Apollo people-search candidates FIRST (most
    # authoritative), then the search-engine ranked list — so reveal_best_of picks the org-matching one.
    _cands = list(dict.fromkeys(
        [c for c in _apollo_cands if c]
        + ((discovery.get("profiles") if discovery else None) or [])
        + ([li] if li else [])))
    resolved = _find_core(name or "", company=company, domain=domain or "", linkedin_url=li or "",
                          use_extensions=use_extensions, deep=deep, use_agent=use_agent,
                          linkedin_candidates=_cands or None, apollo_seed=apollo_seed)
    email = resolved.get("best")
    confidence = resolved.get("confidence", "none")
    candidates = resolved.get("candidates", [])

    out = {
        "name": name,
        # prefer Apollo's authoritative title for the person over the requested role
        "title": (apollo_seed or {}).get("title") or person.get("title", role),
        "company": company,
        "domain": domain,
        "linkedin_url": resolved.get("evidence", {}).get("linkedin_url") or li,
        "email": email,
        "confidence": confidence,
        "source": resolved.get("source"),
        "candidates": candidates,
        "person_sources": person.get("sources", []),
        "summary": (resolved.get("summary")
                    or f"Found {name} as {role} of {company}; email: {email or 'not resolved'}"),
    }
    # The full Apollo people roster for this company+role — lets the caller pick another exec.
    if apollo_people:
        out["apollo_people"] = apollo_people
    ap = resolved.get("evidence", {}).get("apollo")
    if ap:
        out["apollo"] = {"email": ap.get("email"), "org": ap.get("org"),
                         "title": ap.get("title"), "person_name": ap.get("person_name"),
                         "source": ap.get("source"), "extension": ap.get("extension"),
                         "ms": ap.get("ms"), "degraded": ap.get("degraded", [])}
    # Surface the ranked candidate profiles + their reveal outcomes so the user can pick the right
    # person when the top match didn't yield a domain-valid email.
    _ev = resolved.get("evidence", {})
    if _ev.get("apollo_candidates"):
        out["candidate_profiles"] = _ev["apollo_candidates"]
    elif _ev.get("linkedin_discovery"):
        out["candidate_profiles"] = _ev["linkedin_discovery"].get("candidates")
    return out


@mcp.tool
def record_outcome(email: str, status: str) -> dict:
    """Record feedback on an email result: 'replied'|'bounced'|'valid'|'invalid'|'delivered'.
    Updates the verification cache and reinforces the learned domain pattern for future finds."""
    email = (email or "").strip().lower()
    if not email or "@" not in email:
        return {"ok": False, "error": "invalid email"}
    valid_statuses = {"replied", "bounced", "valid", "invalid", "delivered", "undeliverable"}
    if status not in valid_statuses:
        return {"ok": False, "error": f"status must be one of {sorted(valid_statuses)}"}
    try:
        emailverify.record_outcome(email, status)
    except Exception:
        pass
    if status in ("replied", "valid", "delivered"):
        try:
            dom = email.split("@", 1)[1]
            first, last = _split_name(email.split("@")[0])
            pat = _infer_pattern(email.split("@", 1)[0], first, last)
            if pat:
                _learn_pattern(dom, pat)
        except Exception:
            pass
    return {"ok": True, "email": email, "status": status}


@mcp.tool
def stats() -> dict:
    """Provenance/audit roll-up: found emails by source, domain patterns learned, quota states,
    and verification cache size."""
    vc_count = store.query_one("SELECT COUNT(*) n FROM verify_cache")["n"] or 0
    fe_count = store.query_one("SELECT COUNT(*) n FROM found_emails")["n"] or 0
    dp_count = store.query_one("SELECT COUNT(*) n FROM domain_patterns")["n"] or 0
    src_rows = store.query(
        "SELECT source, COUNT(*) n FROM found_emails GROUP BY source ORDER BY n DESC LIMIT 20"
    )
    by_source = {r["source"]: r["n"] for r in (src_rows or [])}
    conf_rows = store.query(
        "SELECT confidence, COUNT(*) n FROM found_emails GROUP BY confidence ORDER BY n DESC"
    )
    by_confidence = {r["confidence"]: r["n"] for r in (conf_rows or [])}
    try:
        quota_state = QUOTA.state()
    except Exception:
        quota_state = {}
    return {
        "verify_cache_entries": vc_count,
        "found_emails": fe_count,
        "learned_patterns": dp_count,
        "by_source": by_source,
        "by_confidence": by_confidence,
        "quota": quota_state,
        "ttl_days": CACHE_TTL_DAYS,
    }


@mcp.tool
def health() -> dict:
    """Report server health: API keys present + quota/cooldown, DNS/DoH reachability, search
    engine status, cache stats. Each degraded item includes an actionable fix hint."""
    checks: dict = {}
    degraded: list[str] = []

    for key_name, display in [("HUNTER_API_KEY", "hunter"), ("REOON_API_KEY", "reoon"),
                               ("TOMBA_API_KEY", "tomba"), ("VERIFALIA_USERNAME", "verifalia"),
                               ("ABSTRACT_API_KEY", "abstract")]:
        val = get_env(key_name)
        checks[f"key_{display}"] = "present" if val else "absent"
        if not val:
            degraded.append(f"key_{display}: not set (optional — free backbone still works)")

    try:
        r = dns_resolve.mx("gmail.com")
        method = r.get("method", "none")
        checks["dns"] = f"ok ({method})"
    except Exception as e:
        checks["dns"] = f"error: {e}"
        degraded.append("dns: check network or dnspython install — pip install dnspython")

    try:
        wh = websearch.health_check()
        up = wh.get("engines_up", 0)
        checks["websearch"] = f"ok ({up} engines up)"
        if up == 0:
            degraded.append("websearch: all engines down — pip install curl_cffi or check network")
    except Exception as e:
        checks["websearch"] = f"error: {e}"
        degraded.append("websearch: module error — check websearch.py")

    checks["verify_cache"] = store.query_one("SELECT COUNT(*) n FROM verify_cache")["n"] or 0
    checks["found_emails"] = store.query_one("SELECT COUNT(*) n FROM found_emails")["n"] or 0
    checks["learned_patterns"] = store.query_one("SELECT COUNT(*) n FROM domain_patterns")["n"] or 0
    try:
        checks["quota"] = QUOTA.state()
    except Exception:
        checks["quota"] = "unavailable"

    return {"ok": len(degraded) == 0, "checks": checks, "degraded": degraded}


@mcp.tool
def selftest(live: bool = False) -> dict:
    """Run a preflight matrix: per-component pass/fail + fix hints.
    live=True runs end-to-end against known-good public fixtures to confirm the full pipeline."""
    results: dict[str, str] = {}
    errors: list[str] = []

    try:
        r = dns_resolve.mx("gmail.com")
        results["dns_mx"] = f"pass ({r.get('method', '?')})" if r.get("mx") else "fail: empty MX"
    except Exception as e:
        results["dns_mx"] = f"fail: {e}"
        errors.append(f"dns_mx: {e}")

    try:
        links = websearch.search_links("site:example.com test", n=3)
        results["websearch"] = f"pass ({len(links)} links)" if isinstance(links, list) else "fail"
    except Exception as e:
        results["websearch"] = f"fail: {e}"
        errors.append(f"websearch: {e}")

    try:
        from mcp_base.email_extract import deobfuscate_text
        clean = deobfuscate_text("test [at] example [dot] com")
        # deobfuscate_text returns a list of recovered addresses
        recovered = clean if isinstance(clean, (list, tuple)) else [clean]
        ok_deob = any("test@example.com" == str(e).lower() for e in recovered)
        results["email_deobfuscate"] = "pass" if ok_deob else f"fail: got {clean!r}"
    except Exception as e:
        results["email_deobfuscate"] = f"fail: {e}"
        errors.append(f"email_deobfuscate: {e}")

    try:
        state = QUOTA.state()
        results["quota"] = f"pass ({len(state)} providers tracked)"
    except Exception as e:
        results["quota"] = f"fail: {e}"

    try:
        _test_email = "selftest_probe@example.com"
        store.execute(
            "INSERT OR REPLACE INTO verify_cache(email,deliverable,confidence,checks,checked_at) "
            "VALUES(?,?,?,?,?)", (_test_email, 1, "high", "{}", _now()))
        row = store.query_one("SELECT * FROM verify_cache WHERE email=?", (_test_email,))
        store.execute("DELETE FROM verify_cache WHERE email=?", (_test_email,))
        results["cache_rw"] = "pass" if row else "fail: write+read returned nothing"
    except Exception as e:
        results["cache_rw"] = f"fail: {e}"
        errors.append(f"cache_rw: {e}")

    if live:
        try:
            v = verify("contact@anthropic.com")
            d = v.get("deliverable")
            results["live_verify"] = "pass" if d is not False else f"fail: deliverable={d}"
        except Exception as e:
            results["live_verify"] = f"fail: {e}"
            errors.append(f"live_verify: {e}")

        try:
            r2 = scrape_site("anthropic.com")
            results["live_scrape"] = f"pass ({len(r2.get('emails', []))} emails)"
        except Exception as e:
            results["live_scrape"] = f"fail: {e}"
            errors.append(f"live_scrape: {e}")

    passed = sum(1 for v in results.values() if v.startswith("pass"))
    failed = sum(1 for v in results.values() if v.startswith("fail"))
    return {
        "ok": failed == 0, "pass": passed, "fail": failed,
        "total": passed + failed, "components": results, "errors": errors,
    }


if __name__ == "__main__":
    mcp.run()
