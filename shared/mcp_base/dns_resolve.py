"""DNS resolution that never hard-fails: dnspython → Cloudflare DoH → Google DoH.

Key design:
- mx(domain) / txt(domain) return a structured dict with a `method` field so callers can
  distinguish "no MX record" (domain is dead) from "DNS unreachable" (stay None, never penalize).
- resolve_domain(company) converts a company name to its email-sending domain.
- Short negative cache prevents hammering the same dead domain on every call.
- provider fingerprinting maps MX hostnames to Google/M365/Zoho/Yahoo/other.
"""
from __future__ import annotations

import re
import threading
import time
from typing import Any

from .http import get_json

_CACHE: dict[str, tuple[float, Any, float]] = {}
_CACHE_LOCK = threading.Lock()
_NEG_TTL = 120.0   # seconds to remember "no MX / DNS failed"
_POS_TTL = 900.0   # seconds for positive results

BIG_HOSTS = frozenset({
    "gmail.com", "googlemail.com",
    "outlook.com", "hotmail.com", "live.com", "msn.com",
    "yahoo.com", "yahoo.co.uk", "ymail.com",
    "icloud.com", "me.com", "mac.com",
    "protonmail.com", "proton.me",
    "zoho.com",
    "aol.com",
})

# MX hostname fragment → provider tag
_MX_PROVIDER_MAP: list[tuple[str, str]] = [
    ("google.com",          "google"),
    ("googlemail.com",      "google"),
    ("aspmx.l.google",      "google"),
    ("mail.protection.outlook.com", "m365"),
    ("outlook.com",         "m365"),
    ("microsoft.com",       "m365"),
    ("mx.zoho.com",         "zoho"),
    ("zoho.com",            "zoho"),
    ("yahoodns.net",        "yahoo"),
    ("mxbulk.yahoo.com",    "yahoo"),
    ("mxb.mailgun.org",     "mailgun"),
    ("mailgun.org",         "mailgun"),
    ("mxb.sendgrid.net",    "sendgrid"),
    ("sendgrid.net",        "sendgrid"),
    ("mxb.mailjet.com",     "mailjet"),
    ("mailjet.com",         "mailjet"),
    ("pphosted.com",        "proofpoint"),
    ("mimecast.com",        "mimecast"),
    ("icloud.com",          "icloud"),
    ("me.com",              "icloud"),
]


def _cached(key: str, ttl: float, compute):
    with _CACHE_LOCK:
        if key in _CACHE:
            ts, val, stored_ttl = _CACHE[key]
            # honor whichever TTL the entry was stored with (so a positive result keeps its
            # long window even if a later read passes a short ttl), falling back to the arg.
            if time.monotonic() - ts < (stored_ttl or ttl):
                return val, True
    result = compute()
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), result, ttl)
    return result, False


def _cached_dynamic(key: str, compute, ttl_fn):
    """Like _cached but the TTL is derived from the computed result (ttl_fn(result) → seconds).
    Lets positive lookups cache long and negatives cache short, with the stored TTL honored on read."""
    with _CACHE_LOCK:
        if key in _CACHE:
            ts, val, stored_ttl = _CACHE[key]
            if time.monotonic() - ts < stored_ttl:
                return val, True
    result = compute()
    with _CACHE_LOCK:
        _CACHE[key] = (time.monotonic(), result, float(ttl_fn(result)))
    return result, False


def _mx_provider(mx_list: list[str]) -> str:
    """Identify the mail provider from MX hostnames."""
    combined = " ".join(mx_list).lower()
    for fragment, tag in _MX_PROVIDER_MAP:
        if fragment in combined:
            return tag
    return "other"


def _dnspython_mx(domain: str) -> list[str]:
    import dns.resolver
    try:
        ans = dns.resolver.resolve(domain, "MX", lifetime=8.0)
        return sorted([str(r.exchange).rstrip(".") for r in ans],
                      key=lambda h: h.split(".")[0] if "." in h else h)
    except (dns.resolver.NXDOMAIN, dns.resolver.NoAnswer):
        return []  # definitive empty — domain exists but no MX
    except Exception:
        raise  # propagate so caller falls to DoH


def _dnspython_txt(domain: str) -> list[str]:
    import dns.resolver
    try:
        ans = dns.resolver.resolve(domain, "TXT", lifetime=8.0)
        return [b.decode("utf-8", "ignore") for r in ans for b in r.strings]
    except Exception:
        return []


def _doh_mx(domain: str, base_url: str) -> list[str]:
    """Fetch MX via DNS-over-HTTPS JSON API."""
    data = get_json(base_url, params={"name": domain, "type": "MX"},
                    headers={"Accept": "application/dns-json"}, cache_ttl=900)
    if isinstance(data, dict) and data.get("error"):
        raise RuntimeError(data["error"])
    answers = data.get("Answer", []) if isinstance(data, dict) else []
    records = []
    for a in answers:
        if a.get("type") == 15:  # MX
            data_parts = str(a.get("data", "")).split(" ", 1)
            if len(data_parts) == 2:
                records.append(data_parts[1].rstrip("."))
    return records


def _doh_txt(domain: str, base_url: str) -> list[str]:
    data = get_json(base_url, params={"name": domain, "type": "TXT"},
                    headers={"Accept": "application/dns-json"}, cache_ttl=900)
    if isinstance(data, dict) and data.get("error"):
        return []
    answers = data.get("Answer", []) if isinstance(data, dict) else []
    results = []
    for a in answers:
        if a.get("type") == 16:  # TXT
            results.append(str(a.get("data", "")).strip('"'))
    return results


def mx(domain: str) -> dict:
    """Resolve MX records for domain.

    Returns:
        {
            mx: [str, ...],           # sorted MX hostnames (empty list = no MX)
            big_host: bool,           # domain/provider is a big free-mail host
            provider: str,            # "google"|"m365"|"zoho"|"yahoo"|"other"
            method: str,              # "dns"|"doh-cf"|"doh-google"|"none"
            deliverable: bool|None,   # False only when *definitively* no MX + not a big-host
        }
    """
    domain = domain.strip().lower()
    cache_key = f"mx:{domain}"

    def _compute():
        mx_list: list[str] = []
        method = "none"
        dns_unreachable = False

        # Layer 1: dnspython
        try:
            mx_list = _dnspython_mx(domain)
            method = "dns"
        except ImportError:
            dns_unreachable = True
        except Exception:
            dns_unreachable = True

        # Layer 2: Cloudflare DoH
        if dns_unreachable or (not mx_list and method == "none"):
            try:
                mx_list = _doh_mx(domain, "https://cloudflare-dns.com/dns-query")
                method = "doh-cf"
                dns_unreachable = False
            except Exception:
                pass

        # Layer 3: Google DoH
        if dns_unreachable or (not mx_list and method == "none"):
            try:
                mx_list = _doh_mx(domain, "https://dns.google/resolve")
                method = "doh-google"
                dns_unreachable = False
            except Exception:
                pass

        is_big = domain in BIG_HOSTS
        provider = _mx_provider(mx_list) if mx_list else "other"

        # deliverable: None when DNS was unreachable (never penalize), False only on definitive no-MX
        if dns_unreachable and not mx_list:
            deliverable = None
        elif not mx_list and not is_big:
            deliverable = False
        else:
            deliverable = None  # MX found → let other checks decide

        return {
            "mx": mx_list,
            "big_host": is_big,
            "provider": provider,
            "method": method,
            "deliverable": deliverable,
            "dns_unreachable": dns_unreachable,
        }

    # positive results (MX found, or DNS unreachable so we should retry sooner-but-not-hammer)
    # cache for _POS_TTL; definitive "no MX" caches only for _NEG_TTL so a fixed domain recovers fast.
    result, _from_cache = _cached_dynamic(
        cache_key, _compute,
        lambda r: _POS_TTL if (r.get("mx") or r.get("dns_unreachable")) else _NEG_TTL,
    )
    return result


def txt(domain: str) -> list[str]:
    """Resolve TXT records (SPF, DMARC hints)."""
    domain = domain.strip().lower()

    def _compute():
        try:
            records = _dnspython_txt(domain)
            if records is not None:
                return records
        except Exception:
            pass
        try:
            return _doh_txt(domain, "https://cloudflare-dns.com/dns-query")
        except Exception:
            pass
        try:
            return _doh_txt(domain, "https://dns.google/resolve")
        except Exception:
            return []

    result, _ = _cached(f"txt:{domain}", _POS_TTL, _compute)
    return result


def spf_includes(domain: str) -> list[str]:
    """Extract 'include:' values from the domain's SPF TXT record."""
    for rec in txt(domain):
        if rec.startswith("v=spf1"):
            return re.findall(r"include:([^\s]+)", rec)
    return []


def resolve_domain(company: str, hint_domain: str | None = None) -> str | None:
    """Best-effort company name → primary email-sending domain.

    Strategy:
    1. If hint_domain given, verify it has MX and return it.
    2. Build domain candidates from company name variants.
    3. Pick the first candidate that has a live MX record.
    4. Fall back to a web-search hint if none have MX.
    """
    if hint_domain:
        r = mx(hint_domain.strip().lower())
        if r["mx"] or r["big_host"]:
            return hint_domain.strip().lower()

    candidates = _domain_candidates(company)
    for d in candidates:
        r = mx(d)
        if r["mx"] or r["big_host"]:
            return d

    # Web-search fallback — try to find "company.com" from a SERP snippet
    try:
        domain = _serp_domain_hint(company)
        if domain:
            r = mx(domain)
            if r["mx"] or r["big_host"]:
                return domain
    except Exception:
        pass

    # Return the most plausible candidate even without MX confirmation
    return candidates[0] if candidates else None


_CORP_SUFFIXES = frozenset({"inc", "llc", "ltd", "limited", "corp", "corporation", "co",
                            "group", "labs", "technologies", "solutions", "services",
                            "systems", "software", "gmbh", "sa", "ag", "plc", "pvt"})


def _domain_candidates(company: str) -> list[str]:
    """Generate plausible domain names from a company string.

    Only strips a corporate suffix when it is a WHOLE trailing word — never a substring (so
    "CopilotKit" stays "copilotkit", not "pilotkit" from a mid-word "co")."""
    words = re.sub(r"[^a-z0-9 ]+", " ", company.lower().strip()).split()
    # drop trailing corporate-suffix words only (keep at least one word)
    while len(words) > 1 and words[-1] in _CORP_SUFFIXES:
        words.pop()
    if not words:
        return []
    slug = "".join(words)
    slug_hyphen = "-".join(words)

    candidates = []
    for base in dict.fromkeys([slug, slug_hyphen]):  # preserve order, slug first
        if base:
            candidates += [f"{base}.com", f"{base}.io", f"{base}.co", f"{base}.ai"]
    return list(dict.fromkeys(candidates))  # dedup preserving order


def _serp_domain_hint(company: str) -> str | None:
    """Ask a search engine for the company's website and extract the domain."""
    try:
        from .http import get_json
        query = f"{company} official website"
        # DuckDuckGo instant-answer API (no key)
        data = get_json("https://api.duckduckgo.com/",
                        params={"q": query, "format": "json", "no_redirect": 1},
                        cache_ttl=3600)
        if isinstance(data, dict):
            abstract_url = data.get("AbstractURL") or data.get("AbstractSource", "")
            if abstract_url:
                from urllib.parse import urlsplit
                host = urlsplit(abstract_url).hostname or ""
                if host and not host.startswith("www."):
                    return host
                if host.startswith("www."):
                    return host[4:]
    except Exception:
        pass
    return None
