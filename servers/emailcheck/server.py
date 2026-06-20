"""emailcheck — FREE email hygiene + deliverability helper. Syntax validation, MX/SPF/DMARC DNS
lookups (dnspython), a heuristic spam-score for subject+body, address extraction, and unsubscribe
parsing. Complements mailmerge (list hygiene) and reachout (sending). No paid APIs; DNS tools need
network, everything else is fully offline."""
from __future__ import annotations

import re

from mcp_base import make_server

mcp = make_server(
    "emailcheck",
    instructions=("Vet outreach before sending: validate_email / check_mx (can the domain receive?), "
                  "spam_score (heuristic content review), extract_emails (pull addresses from text), "
                  "domain_report (MX+SPF+DMARC). DNS tools need network; the rest are offline."),
)

_EMAIL_RE = re.compile(r"[A-Za-z0-9._%+\-]+@[A-Za-z0-9.\-]+\.[A-Za-z]{2,}")
_DNS_TIMEOUT = 5.0  # seconds per query
_DNS_LIFETIME = 10.0  # seconds total per lookup

_SPAM_PHRASES = (
    "free", "guarantee", "guaranteed", "no obligation", "risk-free", "act now",
    "limited time", "click here", "buy now", "order now", "100%", "cash", "winner",
    "congratulations", "urgent", "exclusive deal", "make money", "earn $", "cheap",
    "credit card", "investment", "lowest price", "no cost", "this isn't spam",
    "dear friend", "increase sales", "double your", "best price",
)


# ---------- offline helpers ----------
def _domain(addr: str) -> str:
    return addr.rsplit("@", 1)[1].strip().lower() if "@" in addr else ""


@mcp.tool
def validate_email(email: str) -> dict:
    """Validate an email's syntax (offline) and return its normalized form + domain. Uses email-validator
    when available, else a regex fallback."""
    email = (email or "").strip()
    try:
        from email_validator import EmailNotValidError, validate_email as _v
        try:
            r = _v(email, check_deliverability=False)
            return {"valid": True, "normalized": r.normalized, "local": r.local_part,
                    "domain": r.domain, "ascii_email": r.ascii_email}
        except EmailNotValidError as e:
            return {"valid": False, "reason": str(e)}
    except Exception:
        ok = bool(_EMAIL_RE.fullmatch(email))
        return {"valid": ok, "normalized": email.lower() if ok else None, "domain": _domain(email),
                "note": "regex fallback (email-validator unavailable)"}


@mcp.tool
def extract_emails(text: str, validate: bool = True) -> dict:
    """Pull all email addresses out of free text, de-duplicated. Optionally drops syntactically invalid
    ones. Offline."""
    found = []
    seen = set()
    for m in _EMAIL_RE.findall(text or ""):
        low = m.lower()
        if low in seen:
            continue
        seen.add(low)
        if validate and not _EMAIL_RE.fullmatch(m):
            continue
        found.append(m)
    return {"count": len(found), "emails": found}


@mcp.tool
def parse_unsubscribe(content: str) -> dict:
    """Extract unsubscribe info from raw headers and/or HTML: List-Unsubscribe header value(s),
    mailto: opt-outs, and any anchor/href containing 'unsubscribe'. Offline."""
    content = content or ""
    header_vals = re.findall(r"(?im)^List-Unsubscribe:\s*(.+)$", content)
    links = re.findall(r"<\s*(https?://[^>]+|mailto:[^>]+)\s*>", " ".join(header_vals))
    html_links = re.findall(r'href=["\']([^"\']*unsubscrib[^"\']*)["\']', content, re.I)
    mailtos = re.findall(r"mailto:[^\s>\"']+", content, re.I)
    all_links = []
    for x in links + html_links + mailtos:
        if x not in all_links:
            all_links.append(x)
    return {"has_unsubscribe": bool(all_links or header_vals),
            "list_unsubscribe_header": header_vals,
            "links": all_links}


@mcp.tool
def spam_score(subject: str, body: str) -> dict:
    """Heuristic spam-likelihood score (0-100, higher = worse) for a subject+body, with itemized reasons
    and fixes. Pure offline analysis — not a guarantee, but catches common deliverability foot-guns."""
    subject = subject or ""
    body = body or ""
    reasons, score = [], 0
    text = f"{subject}\n{body}"
    low = text.lower()

    letters = [c for c in text if c.isalpha()]
    caps_ratio = (sum(1 for c in letters if c.isupper()) / len(letters)) if letters else 0
    if caps_ratio > 0.3 and len(letters) > 20:
        score += 20
        reasons.append(f"high ALL-CAPS ratio ({caps_ratio:.0%})")

    exclam = text.count("!")
    if exclam >= 3:
        score += min(15, exclam * 3)
        reasons.append(f"{exclam} exclamation marks")

    hits = sorted({p for p in _SPAM_PHRASES if p in low})
    if hits:
        score += min(30, len(hits) * 6)
        reasons.append(f"spam-trigger phrases: {', '.join(hits[:8])}")

    links = re.findall(r"https?://", body)
    if len(links) >= 5:
        score += 15
        reasons.append(f"{len(links)} links")

    if len(subject) > 70:
        score += 8
        reasons.append(f"long subject ({len(subject)} chars)")
    if not subject.strip():
        score += 15
        reasons.append("empty subject")

    if "unsubscrib" not in low and len(body) > 400:
        score += 10
        reasons.append("no unsubscribe link in a long message")

    if body.count("$") >= 3 or "€" in body and body.count("€") >= 3:
        score += 8
        reasons.append("multiple currency symbols")

    score = min(100, score)
    verdict = "low" if score < 25 else "medium" if score < 55 else "high"
    suggestions = []
    if caps_ratio > 0.3:
        suggestions.append("Use normal sentence case.")
    if exclam >= 3:
        suggestions.append("Cut exclamation marks.")
    if hits:
        suggestions.append("Rephrase salesy trigger words.")
    if "no unsubscribe link in a long message" in reasons:
        suggestions.append("Add a clear opt-out link.")
    return {"score": score, "risk": verdict, "reasons": reasons, "suggestions": suggestions,
            "caps_ratio": round(caps_ratio, 3), "link_count": len(links)}


# ---------- DNS (network) ----------
def _resolve_txt(domain: str) -> list[str]:
    import dns.resolver
    out = []
    try:
        for r in dns.resolver.resolve(domain, "TXT", lifetime=_DNS_LIFETIME):
            out.append(b"".join(r.strings).decode("utf-8", "ignore") if hasattr(r, "strings")
                       else str(r).strip('"'))
    except Exception:
        pass
    return out


@mcp.tool
def check_mx(domain: str) -> dict:
    """Look up a domain's MX records (can it receive mail?). Needs network + dnspython."""
    domain = (domain or "").strip().lower().lstrip("@")
    if "@" in domain:
        domain = _domain(domain)
    if not domain:
        return {"error": "empty domain"}
    try:
        import dns.resolver
        try:
            answers = dns.resolver.resolve(domain, "MX", lifetime=_DNS_LIFETIME)
            mx = sorted(({"host": str(r.exchange).rstrip("."), "priority": r.preference}
                         for r in answers), key=lambda x: x["priority"])
            return {"domain": domain, "has_mx": True, "mx_records": mx}
        except dns.resolver.NXDOMAIN:
            return {"domain": domain, "has_mx": False, "error": "domain does not exist (NXDOMAIN)"}
        except dns.resolver.NoAnswer:
            return {"domain": domain, "has_mx": False, "error": "no MX records"}
        except Exception as e:
            return {"domain": domain, "has_mx": False, "error": f"{type(e).__name__}: {e}"}
    except ImportError:
        return {"error": "dnspython not installed"}


@mcp.tool
def domain_report(domain: str) -> dict:
    """Deliverability snapshot for a domain: MX present, SPF record, DMARC policy. Needs network."""
    domain = (domain or "").strip().lower().lstrip("@")
    if "@" in domain:
        domain = _domain(domain)
    if not domain:
        return {"error": "empty domain"}
    try:
        import dns.resolver  # noqa: F401
    except ImportError:
        return {"error": "dnspython not installed"}

    mx = check_mx(domain)
    txt = _resolve_txt(domain)
    spf = next((t for t in txt if t.lower().startswith("v=spf1")), None)
    dmarc_txt = _resolve_txt(f"_dmarc.{domain}")
    dmarc = next((t for t in dmarc_txt if t.lower().startswith("v=dmarc1")), None)
    policy = None
    if dmarc:
        m = re.search(r"p=(\w+)", dmarc)
        policy = m.group(1) if m else None
    return {
        "domain": domain,
        "has_mx": mx.get("has_mx", False),
        "mx_records": mx.get("mx_records", []),
        "spf": spf, "has_spf": bool(spf),
        "dmarc": dmarc, "dmarc_policy": policy, "has_dmarc": bool(dmarc),
        "summary": ("Receives mail" if mx.get("has_mx") else "No MX") +
                   (", SPF set" if spf else ", no SPF") +
                   (f", DMARC p={policy}" if policy else ", no DMARC"),
    }


if __name__ == "__main__":
    mcp.run()
