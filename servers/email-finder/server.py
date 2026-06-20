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
from urllib.parse import urlsplit

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
    instructions=("Free email resolution. find(name, company, domain) orchestrates everything; "
                  "guess() for patterns, verify() to check deliverability, from_github() for devs, "
                  "scrape_site() to harvest published emails, bulk_find() for batches."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_cache(
  email TEXT PRIMARY KEY, deliverable INTEGER, confidence TEXT, checks TEXT, checked_at TEXT
);
CREATE TABLE IF NOT EXISTS found_emails(
  id INTEGER PRIMARY KEY, name TEXT, domain TEXT, email TEXT, source TEXT, confidence TEXT,
  found_at TEXT, UNIQUE(email)
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


def _smtp_rcpt(host: str, email: str) -> int | None:
    try:
        with smtplib.SMTP(host, 25, timeout=10) as s:
            s.helo("example.com")
            s.mail("verify@example.com")
            code, _ = s.rcpt(email)
            return code
    except Exception:
        return None


def _catch_all(host: str, domain: str) -> bool | None:
    """Probe a random nonexistent local-part; if it's accepted, the domain is catch-all."""
    rnd = "".join(random.choices(string.ascii_lowercase, k=16))
    code = _smtp_rcpt(host, f"{rnd}@{domain}")
    if code is None:
        return None
    return code in (250, 251)


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
        host = mxinfo["mx"][0]
        is_catch = _catch_all(host, domain)
        if is_catch is not None:
            result["checks"]["catch_all"] = is_catch
        if is_catch:
            result.update(deliverable=None, confidence="low",
                          note="catch-all domain: any address accepted, cannot confirm mailbox")
            _cache_put(result)
            return result
        code = _smtp_rcpt(host, email)
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
    try:
        resp = http.request("GET", f"https://api.github.com/users/{username}/events/public",
                            headers=headers, params={"per_page": max_events}, timeout=20)
        if resp.get("status") != 200:
            return {"username": username, "emails": [],
                    "error": f"github {resp.get('status') or resp.get('error')}"}
        for ev in resp.get("json", []) or []:
            for commit in (ev.get("payload", {}) or {}).get("commits", []) or []:
                em = (commit.get("author", {}) or {}).get("email", "")
                if em and "noreply.github.com" not in em and "@" in em:
                    emails[em] = emails.get(em, 0) + 1
    except Exception as e:  # noqa: BLE001
        return {"username": username, "emails": [], "error": str(e)}
    ranked = sorted(emails.items(), key=lambda kv: -kv[1])
    return {"username": username, "emails": [{"email": e, "commits": n} for e, n in ranked]}


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
    try:
        from bs4 import BeautifulSoup
    except Exception:
        BeautifulSoup = None  # type: ignore
    with httpx.Client(timeout=15, follow_redirects=False,
                      headers={"User-Agent": "Mozilla/5.0 (mcp-suite email-finder)"}) as client:
        for path in paths[:max_pages]:
            try:
                r = client.get(base.rstrip("/") + path)
                if r.status_code != 200 or "text/html" not in r.headers.get("content-type", ""):
                    continue
                pages_hit += 1
                text = r.text[:MAX_PAGE_BYTES]
                if BeautifulSoup is not None:
                    soup = BeautifulSoup(text, "html.parser")
                    for a in soup.select("a[href^=mailto]"):
                        em = a.get("href", "")[7:].split("?")[0].strip()
                        if em:
                            found[em] = found.get(em, 0) + 2  # mailto weighted higher
                    text = soup.get_text(" ")
                for em in EMAIL_RE.findall(text):
                    found[em.lower()] = found.get(em.lower(), 0) + 1
            except Exception:
                continue
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


def _record_found(name: str, domain: str, email: str, source: str, confidence: str) -> None:
    try:
        store.execute(
            "INSERT OR IGNORE INTO found_emails(name,domain,email,source,confidence,found_at) "
            "VALUES(?,?,?,?,?,?)", (name, domain, email, source, confidence, _now()))
    except Exception:
        pass


@mcp.tool
def find(name: str, company: str = "", domain: str = "", github: str = "",
         scrape: bool = True) -> dict:
    """Orchestrate the full free flow: site scrape -> GitHub commits -> pattern guesses -> verify ->
    best email with a confidence label. Returns the best candidate plus all evidence. Cached."""
    evidence: dict = {"name": name, "company": company, "domain": domain}
    first, last = _split_name(name)

    # 1) scrape the company site for a published address matching the person
    if scrape and domain:
        site = scrape_site(domain)
        evidence["site"] = site
        for item in site.get("emails", []):
            local = item["email"].split("@", 1)[0].lower()
            if not item["role"] and (first and first in local or last and last in local):
                v = verify(item["email"])
                if v.get("deliverable") is not False:
                    _record_found(name, domain, item["email"], "site", v.get("confidence", "medium"))
                    return {"best": item["email"], "source": "site-scrape",
                            "confidence": v.get("confidence", "medium"), "verify": v,
                            "evidence": evidence}

    # 2) GitHub (often the real address directly)
    if github:
        gh = from_github(github)
        evidence["github"] = gh
        if gh.get("emails"):
            best = gh["emails"][0]["email"]
            v = verify(best)
            if v.get("deliverable") is not False:
                _record_found(name, domain, best, "github", v.get("confidence", "medium"))
                return {"best": best, "source": "github", "confidence": v.get("confidence", "medium"),
                        "verify": v, "evidence": evidence}

    # 3) pattern + verify
    if domain:
        cands = _patterns(name, domain)
        evidence["candidates"] = cands
        for c in cands[:5]:
            v = verify(c)
            if v.get("deliverable") is True:
                _record_found(name, domain, c, "pattern+verify", v["confidence"])
                return {"best": c, "source": "pattern+verify", "confidence": v["confidence"],
                        "verify": v, "evidence": evidence}
        if cands:
            return {"best": cands[0], "source": "pattern-only", "confidence": "low",
                    "note": "unverified best-guess", "evidence": evidence}
    return {"best": None, "confidence": "none",
            "note": "need a domain (and/or github username) to resolve", "evidence": evidence}


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
