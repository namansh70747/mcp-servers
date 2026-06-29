"""Hardened email extraction & deobfuscation — one source of truth for both servers.

Handles:
1. Cloudflare `data-cfemail` XOR decode (very common; regex never sees these)
2. Text deobfuscation: "name [at] domain [dot] com", name(at)domain, AT/DOT variants,
   HTML-entity encoded, unicode-escaped, space-padded forms.
3. mailto: links (highest weight)
4. Plaintext regex scan
5. JSON-LD Person/Organization `email` fields
6. PDF text extraction (lazy pypdf — skipped if absent)

Returns {email: weight_int} merged dict, lowercased, filtered for basic validity.
Also exports: `is_role(email)`, `is_disposable(domain)`, `filter_emails(candidates, domain)`
"""
from __future__ import annotations

import html
import json
import re
import unicodedata
from typing import Any

EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

ROLE_LOCALS = frozenset({
    "info", "sales", "support", "hello", "contact", "admin", "team", "help",
    "office", "press", "media", "jobs", "careers", "hr", "billing", "no-reply",
    "noreply", "marketing", "enquiries", "inquiries", "abuse", "postmaster",
    "webmaster", "hostmaster", "legal", "privacy", "security", "devnull",
    "donotreply", "do-not-reply", "newsletter", "notifications", "alerts",
    "bounce", "bounces", "unsubscribe", "replies", "autoresponder",
})

# Hardcoded disposable domains (kept as a minimal fallback when live lists are unavailable)
_DISPOSABLE_FALLBACK = frozenset({
    "mailinator.com", "10minutemail.com", "guerrillamail.com", "tempmail.com",
    "throwaway.email", "trashmail.com", "yopmail.com", "getnada.com",
    "temp-mail.org", "fakeinbox.com", "sharklasers.com", "guerrillamailblock.com",
    "grr.la", "guerrillamail.info", "guerrillamail.biz", "guerrillamail.de",
    "spam4.me", "dispostable.com", "mailnull.com", "maildrop.cc",
    "spamgourmet.com", "trashmail.me", "discard.email", "spamthisplease.com",
    "crap.email", "wegwerfmail.de", "wegwerfmail.net", "wegwerfmail.org",
})

# Cached live disposable list
_disposable_cache: set[str] = set()
_disposable_loaded = False


def _load_disposable_list() -> set[str]:
    global _disposable_cache, _disposable_loaded
    if _disposable_loaded:
        return _disposable_cache
    try:
        from .http import get_text
        # Primary: disposable/disposable-email-domains raw list
        raw = get_text(
            "https://raw.githubusercontent.com/disposable/disposable-email-domains/master/domains.txt",
            cache_ttl=86400,
        )
        domains: set[str] = set()
        if raw:
            domains = {d.strip().lower() for d in raw.splitlines() if d.strip()}
        # Merge fallback
        domains.update(_DISPOSABLE_FALLBACK)
        _disposable_cache = domains
    except Exception:
        _disposable_cache = set(_DISPOSABLE_FALLBACK)
    _disposable_loaded = True
    return _disposable_cache


def is_disposable(domain: str) -> bool:
    return domain.lower() in _load_disposable_list()


def is_role(email: str) -> bool:
    local = email.split("@")[0].lower() if "@" in email else email.lower()
    return local in ROLE_LOCALS


# ---------------------------------------------------------------------------
# Cloudflare cfemail XOR decoder
# ---------------------------------------------------------------------------

def _decode_cfemail(encoded: str) -> str | None:
    """Decode a Cloudflare data-cfemail attribute value (hex string, XOR cipher).
    First byte is the XOR key; rest are the encoded bytes."""
    try:
        enc = bytes.fromhex(encoded)
        if not enc:
            return None
        key = enc[0]
        decoded = bytes(b ^ key for b in enc[1:])
        return decoded.decode("utf-8", "ignore")
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Text deobfuscation
# ---------------------------------------------------------------------------

_DEOBF_AT = re.compile(
    r"[\(\[\s]?(?:at|AT|\[at\]|\(at\)|@)[\)\]\s]?",
    re.IGNORECASE,
)
_DEOBF_DOT = re.compile(
    r"[\(\[\s](?:dot|DOT|\[dot\]|\(dot\))[\)\]\s]",
    re.IGNORECASE,
)

# Patterns like: name [at] domain [dot] com  OR  name(at)domain.com
_OBFUSC_PATTERN = re.compile(
    r"[a-zA-Z0-9._%+\-]+\s*"
    r"[\(\[\s]?(?:at|AT|\[at\]|\(at\)|@)[\)\]\s]?\s*"
    r"[a-zA-Z0-9.\-]+\s*"
    r"(?:[\(\[\s](?:dot|DOT|\[dot\]|\(dot\))[\)\]\s][a-zA-Z]{2,})*",
)


def deobfuscate_text(text: str) -> list[str]:
    """Extract emails from obfuscated text, returning normalized addresses."""
    found = []
    # First pass: direct regex on the original text
    for m in EMAIL_RE.finditer(text):
        found.append(m.group().lower())

    # Second pass: normalize obfuscated forms, then regex again
    normalized = _DEOBF_AT.sub("@", text)
    normalized = _DEOBF_DOT.sub(".", normalized)
    for m in EMAIL_RE.finditer(normalized):
        e = m.group().lower()
        if e not in found:
            found.append(e)

    # Third pass: HTML entity decode, then retry
    decoded = html.unescape(text)
    if decoded != text:
        for m in EMAIL_RE.finditer(decoded):
            e = m.group().lower()
            if e not in found:
                found.append(e)

    return found


# ---------------------------------------------------------------------------
# HTML extraction
# ---------------------------------------------------------------------------

def _extract_cfemail_from_html(html_text: str) -> list[str]:
    """Find all data-cfemail attributes and decode them."""
    results = []
    for encoded in re.findall(r'data-cfemail=["\']([0-9a-fA-F]+)["\']', html_text):
        decoded = _decode_cfemail(encoded)
        if decoded and "@" in decoded:
            results.append(decoded.lower())
    # Also handle /cdn-cgi/l/email-protection# links
    for encoded in re.findall(r'/cdn-cgi/l/email-protection#([0-9a-fA-F]+)', html_text):
        decoded = _decode_cfemail(encoded)
        if decoded and "@" in decoded:
            e = decoded.lower()
            if e not in results:
                results.append(e)
    return results


def _extract_mailto(html_text: str) -> list[str]:
    """Extract email addresses from mailto: links."""
    results = []
    for raw in re.findall(r'mailto:([a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,})',
                          html_text, re.IGNORECASE):
        results.append(raw.lower())
    return results


def _extract_json_ld(html_text: str) -> list[str]:
    """Extract emails from JSON-LD Person/Organization schema blocks."""
    results = []
    for script_content in re.findall(
            r'<script[^>]+type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
            html_text, re.DOTALL | re.IGNORECASE):
        try:
            data = json.loads(script_content.strip())
            results.extend(_walk_json_ld_email(data))
        except Exception:
            pass
    return results


def _walk_json_ld_email(obj: Any) -> list[str]:
    emails = []
    if isinstance(obj, dict):
        for k in ("email", "Email", "contactEmail", "contactPoint"):
            v = obj.get(k)
            if isinstance(v, str) and "@" in v:
                emails.append(v.lower())
            elif isinstance(v, list):
                for item in v:
                    if isinstance(item, str) and "@" in item:
                        emails.append(item.lower())
                    elif isinstance(item, dict):
                        emails.extend(_walk_json_ld_email(item))
        for v in obj.values():
            if isinstance(v, (dict, list)):
                emails.extend(_walk_json_ld_email(v))
    elif isinstance(obj, list):
        for item in obj:
            emails.extend(_walk_json_ld_email(item))
    return emails


# ---------------------------------------------------------------------------
# PDF extraction (lazy pypdf)
# ---------------------------------------------------------------------------

def extract_from_pdf(pdf_bytes: bytes) -> list[str]:
    """Extract emails from PDF binary content. Requires pypdf (optional dep)."""
    try:
        from pypdf import PdfReader
        from io import BytesIO
        reader = PdfReader(BytesIO(pdf_bytes))
        text = "\n".join(page.extract_text() or "" for page in reader.pages)
        return deobfuscate_text(text)
    except ImportError:
        return []
    except Exception:
        return []


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

def extract_emails(html_text: str, *, domain_filter: str | None = None) -> dict[str, int]:
    """Extract and deobfuscate all emails from an HTML string.

    Returns {email: weight} where weight:
      mailto:   3 (explicitly linked)
      cfemail:  3 (CF-protected = intentionally published)
      json-ld:  2 (structured data)
      plaintext:1

    If domain_filter is given, only emails matching that domain are returned.
    """
    weights: dict[str, int] = {}

    def _add(email: str, weight: int) -> None:
        email = _normalize_email(email)
        if email and _basic_valid(email):
            if domain_filter and not email.endswith(f"@{domain_filter}"):
                return
            weights[email] = max(weights.get(email, 0), weight)

    # Highest-value sources first
    for e in _extract_mailto(html_text):
        _add(e, 3)
    for e in _extract_cfemail_from_html(html_text):
        _add(e, 3)
    for e in _extract_json_ld(html_text):
        _add(e, 2)
    for e in deobfuscate_text(html_text):
        _add(e, 1)

    return weights


def _normalize_email(email: str) -> str:
    """Lowercase, strip trailing dots/spaces, NFKC normalize."""
    email = unicodedata.normalize("NFKC", (email or "").strip()).lower()
    # Strip trailing punctuation that isn't part of the TLD
    email = email.rstrip(".,;:!?\"')")
    return email


def _basic_valid(email: str) -> bool:
    """Quick structural validity check (not deliverability)."""
    if not EMAIL_RE.fullmatch(email):
        return False
    local, _, domain = email.partition("@")
    if not local or not domain or "." not in domain:
        return False
    if len(local) > 64 or len(domain) > 253:
        return False
    return True


def filter_emails(candidates: list[str], domain: str | None = None,
                  exclude_role: bool = False,
                  exclude_disposable: bool = True) -> list[str]:
    """Filter a list of email strings, optionally restricting to a domain."""
    out = []
    for e in candidates:
        e = _normalize_email(e)
        if not _basic_valid(e):
            continue
        _, _, d = e.partition("@")
        if domain and d != domain.lower():
            continue
        if exclude_role and is_role(e):
            continue
        if exclude_disposable and is_disposable(d):
            continue
        out.append(e)
    return out
