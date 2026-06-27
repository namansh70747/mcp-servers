"""Shared email verification core — used by both email-finder and emailcheck servers.

Verification tier order (stops early on definitive verdict):
  1. Syntax check
  2. Typo suggestion (Levenshtein vs common providers)
  3. Role-address flag
  4. Disposable domain check
  5. MX lookup (+ DoH fallback)  ← fixes the DNS false-negative bug
  6. Provider fingerprinting (F4): skip SMTP on Google/M365; per-provider strategy
  7. DNSBL / reputation check
  8. Reacher self-hosted verifier (if Docker service is running)
  9. SMTP RCPT probe (probabilistic signal — ports 587→25→465)
 10. Account-existence enumeration via holehe (keyless, 120+ sites)
 11. GHunt (for @gmail.com — confirms + names owner)
 12. Gravatar existence check (SHA256)
 13. Optional free-tier API tier (Hunter→Tomba→Verifalia→Abstract→Reoon)
 14. Weighted confidence score → summary

Return contract (never raises):
  {
    email, deliverable: True|False|None,
    score: 0-100, confidence: "high"|"medium"|"low"|"none",
    checks: {syntax, mx, smtp, gravatar, enumeration, api, ...},
    methods_tried: [str],
    reasons: [str],
    degraded: [str],
    suggestion: str|None,
    summary: str,
    cached: bool,
  }
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import smtplib
import socket
import threading
import time
import unicodedata
from datetime import datetime, timezone
from typing import Any

from .config import db_path, get_env
from .dns_resolve import BIG_HOSTS, mx as dns_mx
from .email_extract import is_disposable, is_role
from .http import get_json
from .quota import QUOTA
from .store import BaseStore

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------

_SCHEMA = """
CREATE TABLE IF NOT EXISTS verify_cache (
    email         TEXT PRIMARY KEY,
    deliverable   INTEGER,      -- 1=True, 0=False, NULL=None
    score         INTEGER DEFAULT 0,
    confidence    TEXT,
    checks        TEXT,         -- JSON blob
    methods_tried TEXT,         -- JSON list
    reasons       TEXT,         -- JSON list
    degraded      TEXT,         -- JSON list
    summary       TEXT,
    checked_at    TEXT
);
"""
_store: BaseStore | None = None
_store_lock = threading.Lock()

CACHE_TTL_DAYS = 7  # re-verify after this many days


def _get_store() -> BaseStore:
    global _store
    if _store is None:
        with _store_lock:
            if _store is None:
                _store = BaseStore(db_path("emailverify"), schema=_SCHEMA)
    return _store


# ---------------------------------------------------------------------------
# Common constants
# ---------------------------------------------------------------------------

_COMMON_DOMAINS = [
    "gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
    "protonmail.com", "live.com", "me.com", "mac.com", "msn.com",
    "ymail.com", "googlemail.com",
]

# Scoring weights
_W = {
    "api_valid":         45,
    "enumeration_hit":   35,
    "extension_reveal":  35,
    "smtp_250":          25,
    "published_site":    20,
    "corroboration_2":   15,
    "pattern_match":     12,
    "gravatar":          10,
    "mx_ok":             10,
    "reacher_ok":        30,
    "role":              -8,
    "big_host_unverif": -5,
    "catch_all":         -10,
    "smtp_550":          -40,
    "disposable":        -50,
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def split_name(name: str) -> tuple[str, str]:
    """Split 'First Last' → ('first', 'last'). Handles multi-word last names."""
    parts = [p.strip() for p in re.split(r"\s+", name.strip()) if p.strip()]
    if not parts:
        return "", ""
    if len(parts) == 1:
        return parts[0].lower(), ""
    return parts[0].lower(), parts[-1].lower()


def _ascii_slug(s: str) -> str:
    """Normalize unicode to ASCII slug (accents→base letters)."""
    nfkd = unicodedata.normalize("NFKD", s)
    return "".join(c for c in nfkd if not unicodedata.combining(c)).lower()


PATTERNS = [
    "{f}{last}",          # jsmith
    "{first}.{last}",     # john.smith
    "{first}",            # john
    "{first}{l}",         # johns
    "{f}.{last}",         # j.smith
    "{first}_{last}",     # john_smith
    "{last}.{first}",     # smith.john
    "{last}{f}",          # smithj
    "{last}",             # smith
    "{first}{last}",      # johnsmith
    "{first}-{last}",     # john-smith
    "{last}-{first}",     # smith-john
    "{f}{m}{last}",       # jmsmith  (middle initial — needs middle name)
]


def render_pattern(pattern: str, first: str, last: str, middle: str = "") -> str:
    f = _ascii_slug(first[:1]) if first else ""
    l_init = _ascii_slug(last[:1]) if last else ""
    m = _ascii_slug(middle[:1]) if middle else ""
    return pattern.format(
        first=_ascii_slug(first), last=_ascii_slug(last),
        f=f, l=l_init, m=m,
    )


def infer_pattern(known_email: str, first: str, last: str) -> str | None:
    """Given a verified email and the person's name, return the matching pattern string."""
    if not known_email or "@" not in known_email:
        return None
    local = known_email.split("@")[0].lower()
    for pat in PATTERNS:
        rendered = render_pattern(pat, first, last)
        if rendered and rendered == local:
            return pat
    return None


def email_candidates(first: str, last: str, domain: str,
                     extra_patterns: list[str] | None = None) -> list[str]:
    """Generate candidate email addresses for first+last at domain."""
    pats = (extra_patterns or []) + PATTERNS
    seen: dict[str, int] = {}
    for i, pat in enumerate(pats):
        local = render_pattern(pat, first, last)
        if local and "@" not in local:
            e = f"{local}@{domain}"
            if e not in seen:
                seen[e] = i
    return sorted(seen, key=lambda e: seen[e])


# ---------------------------------------------------------------------------
# Typo detection
# ---------------------------------------------------------------------------

def _levenshtein(a: str, b: str) -> int:
    if len(a) < len(b):
        a, b = b, a
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a):
        cur = [i + 1]
        for j, cb in enumerate(b):
            cur.append(min(prev[j] + (0 if ca == cb else 1), cur[-1] + 1, prev[j + 1] + 1))
        prev = cur
    return prev[-1]


def typo_suggestion(email: str) -> str | None:
    """Return a suggested correction if the domain looks like a common-domain typo."""
    if "@" not in email:
        return None
    _, _, domain = email.partition("@")
    domain = domain.lower()
    if domain in _COMMON_DOMAINS:
        return None
    best_dist, best_d = 99, None
    for d in _COMMON_DOMAINS:
        dist = _levenshtein(domain, d)
        if dist < best_dist and dist <= 2:
            best_dist, best_d = dist, d
    if best_d:
        local = email.split("@")[0]
        return f"{local}@{best_d}"
    return None


# ---------------------------------------------------------------------------
# Gravatar existence check
# ---------------------------------------------------------------------------

def gravatar_exists(email: str, timeout: float = 8.0) -> bool:
    """True if the email has a Gravatar (SHA256; keyless)."""
    try:
        h = hashlib.sha256(email.strip().lower().encode()).hexdigest()
        from .fetch import fetch
        r = fetch(f"https://www.gravatar.com/avatar/{h}?d=404", timeout=min(timeout, 8.0))
        return r.get("status") == 200
    except Exception:
        return False


# ---------------------------------------------------------------------------
# SMTP probe (probabilistic signal only)
# ---------------------------------------------------------------------------

# --- SMTP egress circuit-breaker -------------------------------------------------
# On networks that block outbound SMTP (25/587/465) every probe would otherwise burn
# ~ports×hosts×timeout seconds before giving up — and re-pay it on every call. We detect a
# fully-blocked egress ONCE and short-circuit all subsequent probes for a re-check window, so a
# blocked network costs ~one timeout total instead of stacking 60s per verify.
_SMTP_TIMEOUT = float(get_env("EMAIL_SMTP_TIMEOUT", "5") or 5)
_SMTP_EGRESS_RECHECK_S = float(get_env("EMAIL_SMTP_RECHECK_S", "600") or 600)
_smtp_egress_blocked_until = 0.0
_smtp_lock = threading.Lock()


def smtp_egress_blocked() -> bool:
    """True while the process believes outbound SMTP is blocked (within the re-check window)."""
    with _smtp_lock:
        return time.monotonic() < _smtp_egress_blocked_until


def _mark_smtp_blocked() -> None:
    global _smtp_egress_blocked_until
    with _smtp_lock:
        _smtp_egress_blocked_until = time.monotonic() + _SMTP_EGRESS_RECHECK_S


def _clear_smtp_blocked() -> None:
    global _smtp_egress_blocked_until
    with _smtp_lock:
        _smtp_egress_blocked_until = 0.0


def _smtp_probe(email: str, mx_hosts: list[str]) -> str:
    """Return "250" | "550" | "catch_all" | "blocked" | "unknown".

    Probes the top 2 MX hosts across ports 587/25/465 ALL IN PARALLEL (was sequential).
    First decisive result (250/550/catch_all) short-circuits immediately; "unknown" (greylisting)
    is held as a fallback while other threads finish. This collapses worst-case ~30s → ~6s.

    A short per-connection timeout + an egress circuit-breaker keep this fast: if outbound SMTP
    was already found blocked, returns "blocked" immediately without opening any sockets.
    """
    if not mx_hosts:
        return "unknown"

    # Circuit-breaker: a previously-detected blocked egress short-circuits with no sockets.
    if smtp_egress_blocked():
        return "blocked"

    domain = email.split("@")[-1]
    from_addr = "probe@example.com"
    catch_local = "zqxjvk_probe_" + hashlib.md5(email.encode()).hexdigest()[:8]

    def _probe_one(host: str, port: int) -> tuple[str, bool]:
        """Returns (verdict, any_connection) — never raises."""
        try:
            if port == 465:
                s = smtplib.SMTP_SSL(host, port, timeout=_SMTP_TIMEOUT)
            else:
                s = smtplib.SMTP(host, port, timeout=_SMTP_TIMEOUT)
                try:
                    s.ehlo_or_helo_if_needed()
                    s.starttls()
                except Exception:
                    pass
            s.ehlo_or_helo_if_needed()
            s.mail(from_addr)
            code, _ = s.rcpt(email)
            catch_code, _ = s.rcpt(f"{catch_local}@{domain}")
            s.quit()
            if code == 250 and catch_code == 250:
                return "catch_all", True
            if code == 250:
                return "250", True
            if code == 550:
                return "550", True
            if code == 450:
                return "unknown", True   # greylisting
            return "unknown", True
        except smtplib.SMTPConnectError:
            return "no_conn", False
        except (ConnectionRefusedError, socket.timeout, OSError):
            return "no_conn", False
        except Exception:
            return "no_conn", False

    # All host×port combinations in parallel; first decisive verdict wins.
    combos = [(h, p) for h in mx_hosts[:2] for p in (587, 25, 465)]
    import concurrent.futures as _cf_smtp
    any_connection = False
    best_result = "blocked"   # default if nothing connects
    _pool = _cf_smtp.ThreadPoolExecutor(max_workers=min(6, len(combos)))
    _futs = {_pool.submit(_probe_one, h, p): (h, p) for h, p in combos}
    try:
        # Timeout = 2× SMTP_TIMEOUT + small overhead; ensures the group always terminates quickly.
        for _fut in _cf_smtp.as_completed(_futs, timeout=_SMTP_TIMEOUT * 2 + 3):
            try:
                verdict, connected = _fut.result()
            except Exception:  # noqa: BLE001
                continue
            if connected:
                any_connection = True
                _clear_smtp_blocked()
            if verdict in ("250", "550", "catch_all"):
                # Decisive — cancel remaining probes and return immediately.
                for _f in _futs:
                    _f.cancel()
                _pool.shutdown(wait=False, cancel_futures=True)
                return verdict
            if verdict == "unknown" and connected:
                best_result = "unknown"   # greylisting — keep as fallback
    except Exception:
        pass
    finally:
        _pool.shutdown(wait=False, cancel_futures=True)

    if not any_connection:
        _mark_smtp_blocked()
        return "blocked"
    return best_result


# ---------------------------------------------------------------------------
# Reacher self-hosted verifier
# ---------------------------------------------------------------------------

def _reacher_verify(email: str, timeout: float = 30.0) -> dict | None:
    """Call the self-hosted Reacher HTTP API if configured."""
    base_url = os.environ.get("REACHER_BASE_URL", "").strip()
    if not base_url:
        return None
    try:
        from .http import request
        r = request("POST", f"{base_url.rstrip('/')}/v0/check_email",
                    json_body={"to_email": email}, timeout=min(timeout, 30.0))
        if r.get("ok") and r.get("json"):
            return r["json"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Free-tier API verifiers (quota-gated)
# ---------------------------------------------------------------------------

def _hunter_verify(email: str, key: str) -> dict:
    r = get_json("https://api.hunter.io/v2/email-verifier",
                 params={"email": email, "api_key": key}, cache_ttl=3600)
    return r


def _tomba_verify(email: str, key: str, secret: str) -> dict:
    r = get_json(f"https://api.tomba.io/v1/email-verifier/{email}",
                 headers={"X-Tomba-Key": key, "X-Tomba-Secret": secret},
                 cache_ttl=3600)
    return r


def _reoon_verify(email: str, key: str) -> dict:
    r = get_json("https://emailverifier.reoon.com/api/v1/verify",
                 params={"email": email, "key": key, "mode": "quick"}, cache_ttl=3600)
    return r


def _verifalia_verify(email: str, username: str, password: str,
                      deadline=None) -> dict:
    """Verifalia v2.6: POST the email, then (if the job is still processing) poll once. Basic auth
    accepts the account email+password OR a browser-app key as username with an empty password.
    Returns {entries:{data:[{classification,status}]}} for _parse_api_verdict.

    When ``deadline`` is provided (a ``Deadline`` instance) the 2-second polling sleep is skipped
    when there is insufficient budget, preventing Verifalia from blowing the global find budget.
    """
    import base64

    from .http import request
    creds = base64.b64encode(f"{username}:{password}".encode()).decode()
    headers = {"Authorization": f"Basic {creds}", "Content-Type": "application/json"}
    _post_timeout = 25 if deadline is None else deadline.op(20.0)
    r = request("POST", "https://api.verifalia.com/v2.6/email-validations?waitTime=20000",
                headers=headers, json_body={"entries": [{"inputData": email}]},
                timeout=_post_timeout)
    j = (r.get("json") or {}) if isinstance(r, dict) else {}
    # If still processing (202 with no data), poll the job once by its id.
    data = ((j.get("entries") or {}).get("data")) if isinstance(j, dict) else None
    if not data:
        jid = ((j.get("overview") or {}).get("id")) or j.get("id")
        if jid:
            # Skip the sleep when deadline is tight (< 4s remaining) to avoid blowing budget.
            _remaining = deadline.remaining() if deadline is not None else 9999.0
            if _remaining >= 4.0:
                time.sleep(min(2.0, _remaining - 2.0))
            _get_timeout = 20 if deadline is None else deadline.op(15.0)
            r2 = request("GET", f"https://api.verifalia.com/v2.6/email-validations/{jid}",
                         headers=headers, timeout=_get_timeout)
            j = (r2.get("json") or {}) if isinstance(r2, dict) else j
    return j


def _abstract_verify(email: str, key: str) -> dict:
    r = get_json("https://emailvalidation.abstractapi.com/v1/",
                 params={"api_key": key, "email": email}, cache_ttl=3600)
    return r


def _mailboxlayer_verify(email: str, key: str) -> dict:
    r = get_json("https://apilayer.net/api/check",
                 params={"access_key": key, "email": email, "smtp": 1, "format": 1},
                 cache_ttl=3600)
    return r


def _rapid_verify(email: str, _key: str = "") -> dict:
    """Rapid Email Verifier — FREE, NO key, NO signup, open-source. Syntax/domain/MX/disposable/role."""
    return get_json("https://rapid-email-verifier.fly.dev/api/validate",
                    params={"email": email}, cache_ttl=3600)


def _disify_verify(email: str, _key: str = "") -> dict:
    """Disify — FREE, NO key. Format + DNS/MX + disposable check."""
    return get_json(f"https://disify.com/api/email/{email}", cache_ttl=3600)


def _myemailverifier_verify(email: str, key: str) -> dict:
    """MyEmailVerifier — FREE 100/day. Returns {Status, Disposable_Domain, ...}."""
    return get_json(
        f"https://client.myemailverifier.com/verifier/validate_single/{email}/{key}", cache_ttl=3600)


# Verifiers that need NO API key (keyless, always available) — run unconditionally in the consensus.
_KEYLESS_VERIFIERS = {"rapid", "disify"}


# Provider name → call lambda factory. The registry supplies the *order*; this maps each verify
# provider to how it's actually invoked. Adding a verifier = one registry entry + one line here.
# Verifalia/Tomba pull their second secret from env; rapid/disify ignore the key (keyless).
def _verify_call_for(name: str, email: str, deadline=None):
    table = {
        "hunter_verify": lambda k: _hunter_verify(email, k),
        "verifalia":     lambda k: _verifalia_verify(email, k, os.environ.get("VERIFALIA_PASSWORD", ""),
                                                     deadline=deadline),
        "abstract":      lambda k: _abstract_verify(email, k),
        "mailboxlayer":  lambda k: _mailboxlayer_verify(email, k),
        "reoon":         lambda k: _reoon_verify(email, k),
        "tomba":         lambda k: _tomba_verify(email, k, os.environ.get("TOMBA_SECRET", "")),
        "myemailverifier": lambda k: _myemailverifier_verify(email, k),
        "rapid":         lambda k: _rapid_verify(email),
        "disify":        lambda k: _disify_verify(email),
    }
    return table.get(name)


# Fallback order if the registry yields nothing (defensive): keyless first (always free), then biggest
# free headroom. The keyless pair runs even with zero keys configured.
_API_FALLBACK_ORDER = ["rapid", "disify", "myemailverifier", "reoon", "hunter_verify",
                       "verifalia", "abstract", "mailboxlayer", "tomba"]


def _api_provider_order() -> list[str]:
    """Derive the free-tier verify order: the KEYLESS verifiers (rapid/disify) ALWAYS run first (no
    key, always free), then the configured keyed providers from the registry (cheapest/most-headroom
    first). Falls back to a static list so verification never depends on the registry being importable."""
    keyless = [n for n in ("rapid", "disify") if n in _KEYLESS_VERIFIERS]
    try:
        from .providers import REGISTRY, ProviderKind, ProviderCost
        ordered = REGISTRY.ordered(kind=ProviderKind.VERIFY,
                                   cost_filter={ProviderCost.FREE_TIER},
                                   configured_only=True)
        names = [p.name for p in ordered
                 if _verify_call_for(p.name, "x@x") is not None and p.name not in _KEYLESS_VERIFIERS]
        return keyless + names if (keyless or names) else _API_FALLBACK_ORDER
    except Exception:
        pass
    return _API_FALLBACK_ORDER


def _run_api_tier(email: str, degraded: list[str], checks: dict,
                  want: int = 1, deadline=None) -> tuple[bool | None, int]:
    """Run free-tier API verifiers in registry order (quota-gated). `want` = number of independent
    verdicts to collect for CONSENSUS (default 1 = first verdict, original behavior; >1 keeps polling
    verifiers until that many real verdicts agree/disagree). Returns (deliverable, score_delta) and
    records checks['api'] = {provider, providers, verdict, valid, invalid} so callers can see how many
    independent verifiers agreed. Quota-smart: high-headroom verifiers come first via the registry."""
    want = max(1, int(want))
    providers_used: list[str] = []
    valids = invalids = 0
    for name in _api_provider_order():
        if (valids + invalids) >= want:
            break
        # Skip if deadline already expired — don't start a new API call we can't finish.
        if deadline is not None and deadline.expired():
            break
        call = _verify_call_for(name, email, deadline=deadline)
        if call is None:
            continue
        keyless = name in _KEYLESS_VERIFIERS
        key = "" if keyless else QUOTA.next_key(name)
        if not keyless and not key:
            continue  # exhausted, cooling, or not configured → skip (no HTTP call)
        try:
            r = call(key)
            status = 200
            if isinstance(r, dict) and r.get("error"):
                status = 0
            if not keyless:
                reason = QUOTA.record_call(name, key, status, r)
                if reason in ("quota", "rate_limit"):
                    degraded.append(f"{name}:{reason}")
                    continue
            verdict = _parse_api_verdict(name, r)
            if verdict is True:
                valids += 1
                providers_used.append(name)
            elif verdict is False:
                invalids += 1
                providers_used.append(name)
        except Exception as e:
            degraded.append(f"{name}:error:{str(e)[:40]}")
    if not providers_used:
        return None, 0
    # majority verdict across the verifiers that returned a real answer (tie → inconclusive)
    verdict = True if valids > invalids else (False if invalids > valids else None)
    checks["api"] = {"provider": providers_used[0], "providers": providers_used,
                     "verdict": verdict, "valid": valids, "invalid": invalids}
    if verdict is True:
        return True, _W["api_valid"] + (10 if valids >= 2 else 0)  # bonus when 2+ agree
    if verdict is False:
        return False, -_W["api_valid"] // 2
    return None, 0  # split decision — let other signals decide


def _parse_api_verdict(provider: str, r: dict) -> bool | None:
    """Parse each provider's response into a boolean verdict."""
    if not isinstance(r, dict):
        return None
    if provider == "rapid":
        # Rapid Email Verifier: status VALID|PROBABLY_VALID|INVALID_FORMAT|INVALID_DOMAIN|DISPOSABLE
        status = str(r.get("status", "")).upper()
        if status in ("VALID", "PROBABLY_VALID"):
            return True
        if status in ("INVALID_FORMAT", "INVALID_DOMAIN", "DISPOSABLE"):
            return False
        return None
    if provider == "disify":
        # Disify: {format, dns, disposable}. No mailbox SMTP, so only a NEGATIVE is decisive (bad
        # format/dns/disposable → invalid); a clean pass is not a positive mailbox proof → None.
        if r.get("format") is False or r.get("dns") is False or r.get("disposable") is True:
            return False
        return None
    if provider == "myemailverifier":
        status = str(r.get("Status", r.get("status", ""))).lower()
        if "invalid" in status:
            return False
        if "valid" in status:   # "Valid" (after the invalid check so "invalid" wins)
            return True
        return None
    if provider == "hunter_verify":
        d = r.get("data", {})
        status = str(d.get("status", "")).lower()
        if status == "valid":
            return True
        if status == "invalid":
            return False
        return None
    if provider == "tomba":
        d = r.get("data", {}).get("email", r.get("data", {}))
        status = str(d.get("status", "")).lower()
        if "valid" in status:
            return True
        if "invalid" in status:
            return False
        return None
    if provider == "reoon":
        status = str(r.get("status", "")).lower()
        if status in ("valid", "safe_to_send"):
            return True
        if status in ("invalid", "disposable"):
            return False
        return None
    if provider == "verifalia":
        entries = r.get("entries", {}).get("data", [])
        if entries:
            c = str(entries[0].get("classification", "")).lower()
            if c == "deliverable":
                return True
            if c == "undeliverable":
                return False
        return None
    if provider == "abstract":
        q = str(r.get("deliverability", "")).upper()
        if q == "DELIVERABLE":
            return True
        if q == "UNDELIVERABLE":
            return False
        return None
    if provider == "mailboxlayer":
        # smtp_check True + format_valid True + not disposable → deliverable; explicit smtp False → not
        if r.get("smtp_check") is True and r.get("format_valid") is True:
            return True
        if r.get("smtp_check") is False and r.get("format_valid") is True:
            return False
        return None
    return None


# ---------------------------------------------------------------------------
# Main verify function
# ---------------------------------------------------------------------------

def verify(
    email: str,
    *,
    check_smtp: bool = True,
    use_cache: bool = True,
    deep: bool = False,
    consensus: int = 1,
    extra_signals: dict | None = None,
    deadline=None,
    budget_s: float | None = None,
) -> dict:
    """Full verification pipeline. Never raises.

    Args:
        email: the email address to verify
        check_smtp: whether to attempt SMTP probe (can be slow / blocked)
        use_cache: return cached result if fresh
        deep: run account-existence enumeration + GHunt (slower)
        consensus: number of independent API verifiers to poll for agreement (default 1; pass 2-3
            for a CONFIRMED verdict — used on the final winner so a single flaky verifier can't decide)
        extra_signals: pre-known signals e.g. {"published_site": True, "extension_reveal": True}

    Returns:
        see module docstring for full contract
    """
    email = (email or "").strip().lower()
    # F10: normalize IDN/EAI (punycode the domain, NFC the local-part) so non-ASCII addresses
    # round-trip and cache/compare consistently. Best-effort; falls back to the raw address.
    try:
        from .frontier.idn import normalize_email as _idn_norm
        email = _idn_norm(email) or email
    except Exception:
        pass
    result_base = {
        "email": email, "deliverable": None, "score": 0,
        "confidence": "none", "checks": {}, "methods_tried": [],
        "reasons": [], "degraded": [], "suggestion": None,
        "summary": "", "cached": False,
    }

    if not email or "@" not in email:
        result_base["deliverable"] = False
        result_base["confidence"] = "high"
        result_base["checks"] = {"syntax": False}
        result_base["summary"] = "Empty or malformed email address."
        return result_base

    # --- Cache lookup ---
    if use_cache:
        cached = _cache_get(email)
        if cached:
            return {**cached, "cached": True}

    checks: dict[str, Any] = {}
    methods: list[str] = []
    reasons: list[str] = []
    degraded: list[str] = []
    score = 0
    suggestion = None
    deliverable: bool | None = None
    signals = extra_signals or {}

    # --- 1. Syntax ---
    methods.append("syntax")
    if not re.fullmatch(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}", email):
        return _build(email, False, score, checks, methods, reasons, degraded,
                      suggestion, "Invalid email syntax.", use_cache=False)
    checks["syntax"] = True

    # --- 2. Typo suggestion ---
    sug = typo_suggestion(email)
    if sug:
        suggestion = sug
        reasons.append(f"Possible typo: did you mean {sug}?")

    local, _, domain = email.partition("@")

    # --- 3. Role flag ---
    methods.append("role_check")
    if is_role(email):
        checks["role"] = True
        score += _W["role"]
        reasons.append("Role address (info@, sales@, etc.) — not a personal mailbox")

    # --- 4. Disposable ---
    methods.append("disposable")
    if is_disposable(domain):
        checks["disposable"] = True
        return _build(email, False, score + _W["disposable"], checks, methods,
                      reasons + ["Disposable/throwaway domain"], degraded,
                      suggestion, "Disposable email domain.", use_cache=True)

    # --- 5. MX lookup (+ DoH) ---
    methods.append("mx")
    mx_result = dns_mx(domain)
    checks["mx"] = mx_result
    mx_list = mx_result.get("mx", [])
    is_big = domain in BIG_HOSTS or mx_result.get("big_host", False)
    provider = mx_result.get("provider", "other")
    method = mx_result.get("method", "none")

    if method in ("doh-cf", "doh-google"):
        degraded.append(f"dns:doh-used:{method}")

    if mx_result.get("deliverable") is False:
        # Definitively no MX and not a big host
        return _build(email, False, score - 20, checks, methods,
                      reasons + ["No MX records — domain cannot receive email"], degraded,
                      suggestion, "Domain has no mail server.", use_cache=True)

    if mx_list:
        score += _W["mx_ok"]
        reasons.append(f"Domain has MX records ({provider})")
    elif is_big:
        score += _W["mx_ok"]
        reasons.append(f"Known large email host ({domain})")
    elif mx_result.get("dns_unreachable"):
        degraded.append("dns:unreachable")
        reasons.append("DNS unreachable — could not confirm MX")

    # --- 6. Provider fingerprint ---
    methods.append("provider_fingerprint")
    checks["provider"] = provider
    smtp_skip = provider in ("google", "m365") or is_big
    if smtp_skip:
        reasons.append(f"SMTP skipped for {provider} (accept-all provider)")

    # --- 7. DNSBL (basic check via Spamhaus ZEN) ---
    # Lightweight: just check if the domain appears in a common DNSBL
    # (skipped for big hosts to avoid false positives)
    if not is_big:
        try:
            methods.append("dnsbl")
            _dnsbl_check(domain, checks, reasons, degraded)
        except Exception as e:
            degraded.append(f"dnsbl:error:{str(e)[:40]}")

    # --- 8-13. Parallel: independent slow signals (Reacher ∥ SMTP ∥ Gravatar ∥ Enum ∥ API) ---
    # The cheap serial checks (syntax→MX→fingerprint) already set smtp_skip, mx_list, is_big.
    # Everything from here is independent — run concurrently under a shared deadline so no single
    # slow signal (Verifalia 47s, Reacher 30s, holehe 30s) can stall the caller.
    import concurrent.futures as _cf_par

    # Build a per-call deadline so individual ops never exceed remaining budget.
    try:
        from .deadline import Deadline as _DL
        _VERIFY_DEFAULT = float(get_env("VERIFY_BUDGET_S", "10") or 10)
        _PAR_DL = _DL.of(deadline if deadline is not None else budget_s, _VERIFY_DEFAULT)
    except Exception:
        _PAR_DL = None  # no deadline module yet — run without extra caps

    # Thread-local state for signals that write to degraded/checks (passed to parallel jobs).
    _par_degraded: list[str] = []
    _smtp_result_holder: list = ["unknown"]  # mutable container so the lambda can close over it

    _do_smtp_probe = check_smtp and not smtp_skip and bool(mx_list)
    _do_enum = bool(deep)
    _do_ghunt = bool(deep) and domain in ("gmail.com", "googlemail.com")

    def _job_reacher():
        t = _PAR_DL.op(8.0) if _PAR_DL else 8.0
        return _reacher_verify(email, timeout=t)

    def _job_smtp():
        return _smtp_probe(email, mx_list) if _do_smtp_probe else None

    def _job_gravatar():
        t = _PAR_DL.op(6.0) if _PAR_DL else 8.0
        return gravatar_exists(email, timeout=t)

    def _job_enum():
        if not _do_enum:
            return None
        try:
            from .enumerate_accounts import probe
            t = _PAR_DL.op(15.0) if _PAR_DL else 30.0
            return probe(email, timeout=t)
        except Exception as exc:
            return {"_error": str(exc)}

    def _job_ghunt():
        if not _do_ghunt:
            return None
        try:
            from .osint_engines import ghunt_check
            return ghunt_check(email)
        except Exception as exc:
            return {"_error": str(exc)}

    def _job_api():
        # Use LOCAL lists/dict so writes don't race with the main thread.
        _d: list[str] = []
        _c: dict = {}
        r = _run_api_tier(email, _d, _c, want=consensus,
                          deadline=_PAR_DL)
        return r, _d, _c  # (deliverable, delta), degraded_extra, checks_update

    _signal_jobs = {
        "reacher": _job_reacher,
        "smtp": _job_smtp,
        "gravatar": _job_gravatar,
        "enum": _job_enum,
        "ghunt": _job_ghunt,
        "api": _job_api,
    }

    _par_results: dict = {}
    _overall_timeout = (_PAR_DL.remaining() + 1.0) if _PAR_DL else 60.0

    _par_pool = _cf_par.ThreadPoolExecutor(max_workers=len(_signal_jobs))
    _par_futs: dict = {_par_pool.submit(fn): name for name, fn in _signal_jobs.items()}
    try:
        for _pfut in _cf_par.as_completed(_par_futs, timeout=_overall_timeout):
            _pname = _par_futs[_pfut]
            try:
                _par_results[_pname] = _pfut.result(timeout=0.5)
            except Exception as _pexc:
                _par_results[_pname] = _pexc
            # Short-circuit: Reacher said definitively invalid → cancel all remaining signals.
            if _pname == "reacher":
                _rv = _par_results.get("reacher")
                if isinstance(_rv, dict) and _rv.get("is_reachable") == "invalid":
                    for _f in _par_futs:
                        _f.cancel()
                    break
    except Exception:
        pass  # overall timeout — use whatever arrived
    finally:
        _par_pool.shutdown(wait=False, cancel_futures=True)

    # ── Merge: Reacher ─────────────────────────────────────────────────────────
    methods.append("reacher")
    _reacher_res = _par_results.get("reacher")
    if isinstance(_reacher_res, dict) and not _reacher_res.get("_error"):
        checks["reacher"] = _reacher_res
        _is_reachable = _reacher_res.get("is_reachable", "")
        if _is_reachable == "safe":
            score += _W["reacher_ok"]
            deliverable = True
            reasons.append("Reacher: safe/deliverable")
        elif _is_reachable == "invalid":
            score -= 30
            deliverable = False
            reasons.append("Reacher: invalid")
        else:
            reasons.append(f"Reacher: {_is_reachable} (inconclusive)")
    else:
        _par_degraded.append("reacher:not-configured")

    # Early exit if Reacher gave a definitive verdict
    if deliverable is False:
        degraded.extend(_par_degraded)
        return _build(email, False, score, checks, methods, reasons, degraded,
                      suggestion, _summary(email, False, score, reasons, provider), use_cache=True)

    # ── Merge: SMTP ────────────────────────────────────────────────────────────
    smtp_result = "unknown"
    _smtp_res = _par_results.get("smtp")
    if not _do_smtp_probe:
        if smtp_skip:
            checks["smtp"] = "skipped_big_host"
        else:
            _par_degraded.append("smtp:no_mx")
    elif isinstance(_smtp_res, str):
        smtp_result = _smtp_res
        checks["smtp"] = smtp_result
        if smtp_result == "250":
            score += _W["smtp_250"]
            deliverable = True
            reasons.append("SMTP: accepted (250)")
        elif smtp_result == "550":
            if not is_big:
                score += _W["smtp_550"]
                deliverable = False
                reasons.append("SMTP: rejected (550)")
        elif smtp_result == "catch_all":
            score += _W["catch_all"]
            reasons.append("SMTP: catch-all domain (all addresses accepted)")
            _par_degraded.append("smtp:catch_all")
        elif smtp_result == "blocked":
            _par_degraded.append("smtp:egress-blocked" if smtp_egress_blocked() else "smtp:blocked")
        else:
            _par_degraded.append(f"smtp:{smtp_result}")
    else:
        _par_degraded.append("smtp:timeout")
    methods.append("smtp")

    # Early exit on hard SMTP rejection
    if deliverable is False and smtp_result == "550" and not is_big:
        degraded.extend(_par_degraded)
        return _build(email, False, score, checks, methods, reasons, degraded,
                      suggestion, _summary(email, False, score, reasons, provider), use_cache=True)

    # ── Merge: Enumeration ────────────────────────────────────────────────────
    if _do_enum:
        methods.append("enumeration")
        _enum_res = _par_results.get("enum")
        if isinstance(_enum_res, dict) and not _enum_res.get("_error"):
            checks["enumeration"] = _enum_res
            if _enum_res.get("found_on"):
                score += _W["enumeration_hit"]
                deliverable = True
                reasons.append(f"Account found on: {', '.join(_enum_res['found_on'][:3])}")
        elif isinstance(_enum_res, Exception):
            _par_degraded.append(f"enumeration:error:{str(_enum_res)[:40]}")

    # ── Merge: GHunt ──────────────────────────────────────────────────────────
    if _do_ghunt:
        methods.append("ghunt")
        _ghunt_res = _par_results.get("ghunt")
        if isinstance(_ghunt_res, dict) and not _ghunt_res.get("_error") and _ghunt_res:
            checks["ghunt"] = _ghunt_res
            score += _W["enumeration_hit"]
            deliverable = True
            reasons.append(f"GHunt: Gmail confirmed, owner: {_ghunt_res.get('name', 'unknown')}")
        elif isinstance(_ghunt_res, Exception):
            _par_degraded.append(f"ghunt:error:{str(_ghunt_res)[:40]}")

    # ── Merge: Gravatar ───────────────────────────────────────────────────────
    methods.append("gravatar")
    _gravatar_res = _par_results.get("gravatar")
    if isinstance(_gravatar_res, bool):
        checks["gravatar"] = _gravatar_res
        if _gravatar_res:
            score += _W["gravatar"]
            deliverable = deliverable or True  # don't override a False
            reasons.append("Gravatar profile found")
    elif isinstance(_gravatar_res, Exception):
        _par_degraded.append(f"gravatar:error:{str(_gravatar_res)[:40]}")

    # --- External signals ---
    if signals.get("published_site"):
        score += _W["published_site"]
        reasons.append("Email found published on domain website")
    if signals.get("extension_reveal"):
        score += _W["extension_reveal"]
        deliverable = True
        reasons.append("Email revealed by browser extension (pre-verified)")
    if signals.get("corroboration_count", 0) >= 2:
        score += _W["corroboration_2"]
        reasons.append("Corroborated by 2+ independent sources")
    if signals.get("pattern_match"):
        score += _W["pattern_match"]
        reasons.append("Matches learned domain pattern")

    # ── Merge: API tier ───────────────────────────────────────────────────────
    if deliverable is None or score < 40 or consensus > 1:
        methods.append("api_tier")
        _api_res = _par_results.get("api")
        if isinstance(_api_res, tuple) and len(_api_res) == 3:
            (_api_deliverable, _api_delta), _api_degrade, _api_checks_update = _api_res
            score += _api_delta
            _par_degraded.extend(_api_degrade)
            checks.update(_api_checks_update)
            if _api_deliverable is not None:
                deliverable = _api_deliverable
                if _api_deliverable:
                    reasons.append("API verifier: valid")
                else:
                    reasons.append("API verifier: invalid")

    # Merge parallel-collected degraded messages into the caller's list.
    degraded.extend(_par_degraded)

    # --- 13b. Bayesian likelihood-ratio fusion (F7) — calibrated probability alongside the
    # additive score. Purely additive model stays the verdict driver; this is a second opinion. ---
    try:
        from .frontier import bayes as _bayes
        bsig = {
            "api_valid": checks.get("api", {}).get("verdict") is True,
            "api_invalid": checks.get("api", {}).get("verdict") is False,
            "enumeration_hit": bool(checks.get("enumeration", {}).get("found_on")),
            "smtp_250": checks.get("smtp") == "250",
            "smtp_550": checks.get("smtp") == "550",
            "reacher_safe": isinstance(checks.get("reacher"), dict)
                            and checks["reacher"].get("is_reachable") == "safe",
            "gravatar": checks.get("gravatar") is True,
            "mx_ok": bool(mx_list) or is_big,
            "no_mx": (not mx_list) and (not is_big) and not mx_result.get("dns_unreachable"),
            "disposable": checks.get("disposable") is True,
            "published_on_site": bool(signals.get("published_site")),
            "corroborated": signals.get("corroboration_count", 0) >= 2,
            "pattern_match": bool(signals.get("pattern_match")),
        }
        checks["bayes"] = _bayes.fuse(bsig)
    except Exception:
        pass

    # --- 14. Score → confidence → final verdict ---
    score = max(0, min(100, score))
    if deliverable is False:
        confidence = "high" if score <= 20 else "medium"
    elif deliverable is True:
        if score >= 75:
            confidence = "high"
        elif score >= 45:
            confidence = "medium"
        else:
            confidence = "low"
    else:
        # Unknown — base on score
        if score >= 55:
            confidence = "medium"
            deliverable = None  # still honest
        elif score >= 25:
            confidence = "low"
        else:
            confidence = "none"

    summary = _summary(email, deliverable, score, reasons, provider)
    result = _build(email, deliverable, score, checks, methods, reasons, degraded,
                    suggestion, summary, use_cache=True, confidence=confidence)
    return result


# ---------------------------------------------------------------------------
# DNSBL helper
# ---------------------------------------------------------------------------

def _dnsbl_check(domain: str, checks: dict, reasons: list, degraded: list) -> None:
    """Very lightweight DNSBL check using Spamhaus DBL (domain blocklist).

    Distinguishes a definitive "not listed" (NXDOMAIN) from "couldn't reach the blocklist"
    (timeout/no-nameservers) — the latter must NOT be reported as clean, or a blocked DNS path
    would silently whitewash every domain. Unreachable → 'unknown' + degraded, no score effect."""
    try:
        import dns.resolver
        query = f"{domain}.dbl.spamhaus.org"
        dns.resolver.resolve(query, "A", lifetime=5.0)
        checks["dnsbl"] = "listed"
        reasons.append("Domain listed in Spamhaus DBL")
    except ImportError:
        checks["dnsbl"] = "unknown"
        degraded.append("dnsbl:no-dnspython")
    except Exception as e:  # noqa: BLE001
        name = type(e).__name__
        if name == "NXDOMAIN":          # definitive: not on the blocklist
            checks["dnsbl"] = "clean"
        else:                            # Timeout / NoNameservers / NoAnswer / network → inconclusive
            checks["dnsbl"] = "unknown"
            degraded.append("dnsbl:unreachable")


# ---------------------------------------------------------------------------
# Result builders
# ---------------------------------------------------------------------------

def _build(email: str, deliverable: bool | None, score: int,
           checks: dict, methods: list, reasons: list, degraded: list,
           suggestion: str | None, summary: str, *,
           use_cache: bool = False, confidence: str | None = None) -> dict:
    score = max(0, min(100, score))
    if confidence is None:
        if deliverable is True:
            confidence = "high" if score >= 75 else "medium" if score >= 45 else "low"
        elif deliverable is False:
            confidence = "high" if abs(score - 50) > 25 else "medium"
        else:
            confidence = "none" if score < 20 else "low"

    result = {
        "email": email,
        "deliverable": deliverable,
        "score": score,
        "confidence": confidence,
        "checks": checks,
        "methods_tried": methods,
        "reasons": reasons,
        "degraded": degraded,
        "suggestion": suggestion,
        "summary": summary,
        "cached": False,
    }
    # F7 Bayes — additive transparency layer: calibrated probability from in-verify signals only.
    # Enabled by default (VERIFY_BAYES=1). Does not override confidence — server-side _confirm_signals
    # uses the fuller per-candidate signals (sources list + corroboration) to drive any boost.
    try:
        from .config import get_env as _ge
        if (_ge("VERIFY_BAYES", "1") or "1") != "0":
            from .frontier.bayes import fuse as _bf
            _bs: dict = {}
            _api = (checks.get("api") or {})
            if _api.get("verdict") is True:   _bs["api_valid"]      = True
            if _api.get("verdict") is False:  _bs["api_invalid"]    = True
            _smtp = str(checks.get("smtp") or "")
            if _smtp == "250":                _bs["smtp_250"]        = True
            if _smtp == "550":                _bs["smtp_550"]        = True
            if checks.get("mx_ok") or checks.get("mx"):  _bs["mx_ok"] = True
            _rch = checks.get("reacher") or {}
            if _rch.get("is_reachable") == "safe":       _bs["reacher_safe"] = True
            _grv = checks.get("gravatar") or {}
            if _grv.get("found"):             _bs["gravatar"]       = True
            _enum = checks.get("enumeration") or {}
            if _enum.get("found_on"):         _bs["enumeration_hit"] = True
            _br = _bf(_bs)
            result["bayes"] = {"probability": _br.get("probability"),
                               "confidence": _br.get("confidence"),
                               "used": _br.get("used", [])}
    except Exception:  # noqa: BLE001
        pass
    if use_cache and email:
        _cache_put(result)
    return result


def _summary(email: str, deliverable: bool | None, score: int,
             reasons: list, provider: str) -> str:
    if deliverable is True:
        verb = "looks deliverable"
    elif deliverable is False:
        verb = "appears undeliverable"
    else:
        verb = "deliverability uncertain"
    qual = "high" if score >= 75 else "medium" if score >= 45 else "low"
    lead = f"{email}: {verb} ({qual} confidence, score {score}/100)"
    if reasons:
        lead += f". {reasons[0]}"
    if provider not in ("other",):
        lead += f" [{provider}]"
    return lead


# ---------------------------------------------------------------------------
# Cache helpers
# ---------------------------------------------------------------------------

def _cache_get(email: str) -> dict | None:
    try:
        store = _get_store()
        row = store.query_one("SELECT * FROM verify_cache WHERE email=?", (email,))
        if not row:
            return None
        checked_at = row.get("checked_at", "")
        if checked_at:
            dt = datetime.fromisoformat(checked_at.replace("Z", "+00:00"))
            age_days = (datetime.now(timezone.utc) - dt).total_seconds() / 86400
            if age_days > CACHE_TTL_DAYS:
                return None
        return {
            "email": row["email"],
            "deliverable": None if row["deliverable"] is None else bool(row["deliverable"]),
            "score": row.get("score", 0),
            "confidence": row.get("confidence", "none"),
            "checks": json.loads(row.get("checks") or "{}"),
            "methods_tried": json.loads(row.get("methods_tried") or "[]"),
            "reasons": json.loads(row.get("reasons") or "[]"),
            "degraded": json.loads(row.get("degraded") or "[]"),
            "suggestion": None,
            "summary": row.get("summary", ""),
        }
    except Exception:
        return None


def _cache_put(result: dict) -> None:
    try:
        store = _get_store()
        d = result.get("deliverable")
        store.execute(
            """INSERT OR REPLACE INTO verify_cache
               (email, deliverable, score, confidence, checks, methods_tried, reasons, degraded, summary, checked_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                result["email"],
                None if d is None else int(d),
                result.get("score", 0),
                result.get("confidence", "none"),
                json.dumps(result.get("checks", {})),
                json.dumps(result.get("methods_tried", [])),
                json.dumps(result.get("reasons", [])),
                json.dumps(result.get("degraded", [])),
                result.get("summary", ""),
                datetime.now(timezone.utc).isoformat(),
            ),
        )
    except Exception:
        pass


def record_outcome(email: str, status: str) -> None:
    """Learning feedback loop: caller reports 'replied'|'bounced'|'valid'|'invalid'.
    Updates the cache confidence and domain pattern scoring."""
    email = email.strip().lower()
    if not email or "@" not in email:
        return
    try:
        store = _get_store()
        if status in ("replied", "valid"):
            store.execute(
                "UPDATE verify_cache SET deliverable=1, confidence='high', "
                "summary='Confirmed via feedback: ' || summary WHERE email=?",
                (email,),
            )
        elif status in ("bounced", "invalid"):
            store.execute(
                "UPDATE verify_cache SET deliverable=0, confidence='high', "
                "summary='Confirmed invalid via feedback: ' || summary WHERE email=?",
                (email,),
            )
    except Exception:
        pass


def cache_stats() -> dict:
    """Return verify cache statistics."""
    try:
        store = _get_store()
        total = (store.query_one("SELECT COUNT(*) AS n FROM verify_cache") or {}).get("n", 0)
        valid = (store.query_one(
            "SELECT COUNT(*) AS n FROM verify_cache WHERE deliverable=1") or {}).get("n", 0)
        invalid = (store.query_one(
            "SELECT COUNT(*) AS n FROM verify_cache WHERE deliverable=0") or {}).get("n", 0)
        return {"total": total, "valid": valid, "invalid": invalid, "unknown": total - valid - invalid}
    except Exception:
        return {}


# ---------------------------------------------------------------------------
# Make the provider registry a LIVE catalogue (not dead code): wire each verify
# provider's real call_fn so REGISTRY.call("hunter_verify", email=...) works, and
# so _api_provider_order() can drive the verify tier from the registry. Best-effort.
# ---------------------------------------------------------------------------

def _wire_registry() -> None:
    try:
        from .providers import update_call_fn
    except Exception:
        return

    def _mk(provider_name: str):
        def _call(email: str = "", _api_key: str = "", **_):
            fn = _verify_call_for(provider_name, email)
            if fn is None or not _api_key:
                return {"ok": False, "error": "not configured", "skip": True}
            r = fn(_api_key)
            verdict = _parse_api_verdict(provider_name, r)
            return {"ok": True, "provider": provider_name, "deliverable": verdict, "raw": r}
        return _call

    for _name in ("hunter_verify", "verifalia", "abstract", "mailboxlayer", "reoon", "tomba"):
        try:
            update_call_fn(_name, _mk(_name))
        except Exception:
            pass


_wire_registry()


