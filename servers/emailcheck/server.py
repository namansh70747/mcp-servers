"""emailcheck — FREE email hygiene + deliverability helper. Syntax validation, MX/SPF/DMARC DNS
lookups (dnspython), a heuristic spam-score for subject+body, address extraction, and unsubscribe
parsing. Complements mailmerge (list hygiene) and reachout (sending). No paid APIs; DNS tools need
network, everything else is fully offline."""
from __future__ import annotations

import re

from mcp_base import Jobs, err, make_server
from mcp_base import dns_resolve, emailverify

mcp = make_server(
    "emailcheck",
    instructions=("Vet outreach before sending: validate_email / check_mx (can the domain receive?), "
                  "spam_score (heuristic content review), phishing_score (is THIS message/URL a phish?), "
                  "extract_emails (pull addresses from text), domain_report (MX+SPF+DMARC). "
                  "DNS tools need network; the rest are offline."),
)
JOBS = Jobs("emailcheck", max_concurrent=2, inline_wait=12.0)
# Hard per-mailbox verify cap so one slow/blocked SMTP can never stall a bulk batch.
PER_VERIFY_S = 8.0

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

# Brands commonly impersonated in phishing; used for look-alike (typosquat) detection.
_PHISH_BRANDS = (
    "paypal", "apple", "microsoft", "office365", "outlook", "google", "gmail",
    "amazon", "netflix", "facebook", "instagram", "linkedin", "dropbox", "docusign",
    "wellsfargo", "chase", "bankofamerica", "citibank", "hsbc", "barclays", "coinbase",
    "binance", "metamask", "icloud", "adobe", "dhl", "fedex", "ups", "usps", "irs",
    "whatsapp", "steam", "github", "stripe",
)

# Urgency / pressure language typical of phishing lures.
_PHISH_URGENCY = (
    "urgent", "immediately", "right away", "as soon as possible", "act now",
    "action required", "verify your account", "verify now", "confirm your account",
    "confirm your identity", "update your payment", "update your billing",
    "suspended", "your account has been", "unusual activity", "unauthorized",
    "limited time", "expires", "expire", "within 24 hours", "within 48 hours",
    "final notice", "failure to", "avoid suspension", "click the link below",
    "log in to", "login to", "reset your password", "secure your account",
    "validate your", "account will be closed", "we detected", "security alert",
)

# Free / disposable / risky hosting often used for credential-harvest pages.
_PHISH_RISKY_TLDS = (
    ".zip", ".mov", ".xyz", ".top", ".tk", ".ml", ".ga", ".cf", ".gq", ".click",
    ".link", ".country", ".kim", ".work", ".support", ".rest",
)
_PHISH_SHORTENERS = (
    "bit.ly", "tinyurl.com", "goo.gl", "t.co", "ow.ly", "is.gd", "buff.ly",
    "rebrand.ly", "cutt.ly", "rb.gy", "shorturl.at", "tiny.cc",
)
_PHISH_SENSITIVE_HOST_WORDS = (
    "secure", "account", "login", "signin", "verify", "update", "billing",
    "support", "webscr", "confirm", "wallet", "auth", "recovery", "unlock",
)

_URL_RE = re.compile(r"https?://[^\s<>\"')]+", re.I)
_ANCHOR_RE = re.compile(r'<a\b[^>]*?href=["\']?(https?://[^"\'>\s]+)["\']?[^>]*>(.*?)</a>', re.I | re.S)
_IPV4_HOST_RE = re.compile(r"^(?:\d{1,3}\.){3}\d{1,3}$")
_TAG_RE = re.compile(r"<[^>]+>")


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


# ---------- phishing (offline) ----------
def _host_of(url: str) -> str:
    """Extract a lowercased hostname from a URL without raising. No network."""
    try:
        from urllib.parse import urlsplit
        h = (urlsplit(url).hostname or "").lower()
        return h
    except Exception:
        # crude fallback: strip scheme, userinfo, path, port
        s = re.sub(r"^[a-z]+://", "", (url or "").strip(), flags=re.I)
        s = s.split("/", 1)[0].split("?", 1)[0]
        if "@" in s:
            s = s.rsplit("@", 1)[1]
        return s.rsplit(":", 1)[0].lower()


def _registrable(host: str) -> str:
    """Best-effort eTLD+1 (no PSL dependency): last two labels, or three for common
    two-level public suffixes. Good enough for look-alike heuristics. No network."""
    parts = [p for p in (host or "").split(".") if p]
    if len(parts) <= 2:
        return ".".join(parts)
    two_level = {"co", "com", "org", "net", "gov", "edu", "ac"}
    if parts[-2] in two_level and len(parts[-1]) == 2:  # e.g. co.uk, com.au, com.br
        return ".".join(parts[-3:])
    return ".".join(parts[-2:])


def _edit_distance(a: str, b: str) -> int:
    """Levenshtein distance (iterative, O(len(a)*len(b))). Pure offline."""
    if a == b:
        return 0
    if not a:
        return len(b)
    if not b:
        return len(a)
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i]
        for j, cb in enumerate(b, 1):
            cur.append(min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb)))
        prev = cur
    return prev[-1]


def _homoglyph_norm(s: str) -> str:
    """Fold common look-alike character swaps so 'paypa1' -> 'paypal', 'micros0ft' -> 'microsoft'."""
    return (s.replace("0", "o").replace("1", "l").replace("5", "s")
            .replace("3", "e").replace("$", "s").replace("|", "l")
            .replace("rn", "m"))  # 'paypaI'->kept; 'rn'->'m' classic spoof


def _lookalike_brand(host: str) -> tuple[str, str] | None:
    """If a hostname's registrable part resembles (but isn't) a known brand, return
    (brand, label). Catches typosquats (paypa1, micros0ft, app1e), homoglyphs, and
    brand-plus-decoy hosts (paypal-secure, apple-id-verify). No network. Never matches a
    host that is *actually* on the brand's own registrable domain."""
    host = (host or "").lower()
    reg = _registrable(host)
    if not reg:
        return None
    base = reg.rsplit(".", 1)[0]  # the registrable label, e.g. 'paypal-secure' from 'paypal-secure.com'
    # tokens within the base, splitting on hyphen/underscore (paypal-secure -> paypal, secure)
    tokens = [t for t in re.split(r"[-_]", base) if t]
    for brand in _PHISH_BRANDS:
        # a token EXACTLY equal to the brand but the registrable domain is NOT the brand's own
        # (e.g. 'paypal' token inside 'paypal-secure.com'; legit 'paypal.com' is excluded)
        if brand in tokens and base != brand:
            return brand, base
        for tok in tokens:
            nt = _homoglyph_norm(tok)
            if nt == brand and tok != brand:
                return brand, base
            if len(brand) >= 5 and 0 < _edit_distance(nt, brand) <= 1:
                return brand, base
        # homoglyph fold of the whole base equals the brand (paypa1secure won't, but paypa1 will)
        nb = _homoglyph_norm(base.replace("-", "").replace("_", ""))
        if nb == brand and base != brand:
            return brand, base
        if len(brand) >= 5 and 0 < _edit_distance(_homoglyph_norm(base), brand) <= 1:
            return brand, base
    return None


def _has_punycode(host: str) -> bool:
    return any(lbl.startswith("xn--") for lbl in (host or "").split("."))


def _strip_tags(s: str) -> str:
    return _TAG_RE.sub(" ", s or "")


@mcp.tool
def phishing_score(text_or_url: str) -> dict:
    """Heuristic phishing-likelihood for a raw URL, an email body, or pasted HTML.
    Fully OFFLINE (no DNS, no fetch) and never raises. Flags look-alike / typosquatted
    brand domains, punycode (xn--) hosts, raw IP-literal links, URL shorteners, risky TLDs,
    credential-bait host words, urgency/pressure language, and mismatched anchor link text
    (display host != href host). Returns a 0-1 risk score, a 0-100 percentage, a verdict,
    itemized reasons, and the URLs inspected."""
    try:
        text = text_or_url if isinstance(text_or_url, str) else ("" if text_or_url is None else str(text_or_url))
        text = text.strip()
        if not text:
            return err("empty input", risk_score=0.0, risk_pct=0, verdict="low",
                       reasons=[], urls=[], hint="pass a URL, email body, or HTML")

        reasons: list[str] = []
        weight = 0  # accumulates; mapped to 0-1 at the end

        low = text.lower()
        is_bare_url = bool(_URL_RE.fullmatch(text))

        # --- collect URLs (bare + within text) ---
        urls = _URL_RE.findall(text)
        # de-dup, preserve order
        seen_u, uniq_urls = set(), []
        for u in urls:
            u = u.rstrip(".,);]'\"")
            if u not in seen_u:
                seen_u.add(u)
                uniq_urls.append(u)

        # --- anchor link-text vs href mismatch (HTML) ---
        for href, inner in _ANCHOR_RE.findall(text):
            disp_text = _strip_tags(inner).strip()
            disp_urls = _URL_RE.findall(disp_text)
            href_host = _registrable(_host_of(href))
            if disp_urls:  # the visible label is itself a URL → compare hosts
                disp_host = _registrable(_host_of(disp_urls[0]))
                if disp_host and href_host and disp_host != href_host:
                    weight += 35
                    reasons.append(f"link text shows '{disp_host}' but href points to '{href_host}'")
            elif href_host:
                # visible text names a brand the href host doesn't belong to
                dl = disp_text.lower()
                for brand in _PHISH_BRANDS:
                    if brand in dl and brand not in href_host:
                        weight += 25
                        reasons.append(f"link labeled '{brand}' actually points to '{href_host}'")
                        break

        # --- per-URL host analysis ---
        flagged_hosts = set()
        for u in uniq_urls:
            host = _host_of(u)
            if not host or host in flagged_hosts:
                continue
            flagged_hosts.add(host)

            if _IPV4_HOST_RE.match(host):
                weight += 30
                reasons.append(f"link uses a raw IP address ({host}) instead of a domain")
                continue

            if _has_punycode(host):
                weight += 30
                reasons.append(f"punycode/IDN host (possible homoglyph spoof): {host}")

            la = _lookalike_brand(host)
            if la:
                brand, label = la
                weight += 35
                reasons.append(f"look-alike domain '{label}' impersonating '{brand}'")

            reg = _registrable(host)
            if reg in _PHISH_SHORTENERS:
                weight += 12
                reasons.append(f"URL shortener hides the real destination ({reg})")

            for t in _PHISH_RISKY_TLDS:
                if host.endswith(t):
                    weight += 10
                    reasons.append(f"risky/abused TLD ({t}) on {host}")
                    break

            # credential-bait words living in a subdomain or hyphenated host — but skip
            # legit brand auth domains (e.g. accounts.google.com, login.microsoftonline.com)
            reg_base = reg.rsplit(".", 1)[0] if reg else ""
            on_known_brand = reg_base in _PHISH_BRANDS
            if not on_known_brand:
                sub = host[: -len(reg)] if reg and host.endswith(reg) else host
                hit_words = sorted({w for w in _PHISH_SENSITIVE_HOST_WORDS if w in sub})
                if hit_words:
                    weight += min(12, 4 * len(hit_words))
                    reasons.append(f"credential-bait words in host: {', '.join(hit_words)}")

            # excessive subdomain nesting (e.g. login.account.secure.example.evil.com)
            if host.count(".") >= 4:
                weight += 8
                reasons.append(f"deeply nested subdomains ({host.count('.') + 1} labels): {host}")

            # '@' embedded in URL is a classic redirect/obfuscation trick
            if re.search(r"https?://[^/\s]*@", u, re.I):
                weight += 20
                reasons.append("'@' in URL authority can mask the true destination")

        # --- urgency / pressure language (skip if it's just a bare URL) ---
        if not is_bare_url:
            urg = sorted({p for p in _PHISH_URGENCY if p in low})
            if urg:
                weight += min(30, 7 * len(urg))
                reasons.append(f"urgency/pressure language: {', '.join(urg[:6])}")

            # generic salutation + credential ask is a strong combo
            if re.search(r"\b(dear (customer|user|member|client)|valued customer)\b", low):
                weight += 8
                reasons.append("generic salutation (no real name)")

            if re.search(r"\b(password|ssn|social security|credit card|cvv|pin|one-time|otp|seed phrase|bank account)\b", low):
                weight += 15
                reasons.append("requests sensitive credentials/payment info")

        # plain text that mentions a brand but links elsewhere
        if uniq_urls and not is_bare_url:
            link_regs = {_registrable(_host_of(u)) for u in uniq_urls}
            for brand in _PHISH_BRANDS:
                if brand in low and not any(brand in r for r in link_regs):
                    weight += 10
                    reasons.append(f"mentions '{brand}' but no link goes to a '{brand}' domain")
                    break

        if not reasons:
            reasons.append("no phishing indicators found")

        score = round(min(1.0, weight / 100.0), 3)
        pct = int(round(score * 100))
        verdict = "low" if score < 0.25 else "medium" if score < 0.55 else "high"
        return {
            "ok": True,
            "risk_score": score,
            "risk_pct": pct,
            "verdict": verdict,
            "reasons": reasons,
            "urls": uniq_urls,
            "note": "offline heuristic — not a verdict; verify the real sender/domain before trusting.",
        }
    except Exception as e:  # universal no-crash guard
        return err(f"{type(e).__name__}: {e}", risk_score=0.0, risk_pct=0,
                   verdict="low", reasons=[], urls=[])


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
    """Look up a domain's MX records with DoH fallback (Cloudflare → Google) when port 53 is
    blocked. Never returns a false empty on DNS block — method field shows how it was resolved."""
    domain = (domain or "").strip().lower().lstrip("@")
    if "@" in domain:
        domain = _domain(domain)
    if not domain:
        return {"error": "empty domain"}
    r = dns_resolve.mx(domain)
    mx_list = [{"host": h, "priority": i} for i, h in enumerate(r.get("mx", []))]
    return {
        "domain": domain,
        "has_mx": bool(mx_list),
        "mx_records": mx_list,
        "big_host": r.get("big_host", False),
        "provider": r.get("provider", "other"),
        "method": r.get("method", "none"),
    }


@mcp.tool
def domain_report(domain: str) -> dict:
    """Deliverability snapshot: MX (DoH fallback), SPF, DMARC policy, provider fingerprint.
    All DNS lookups use DoH when port 53 is blocked — never a false-empty on restricted networks."""
    domain = (domain or "").strip().lower().lstrip("@")
    if "@" in domain:
        domain = _domain(domain)
    if not domain:
        return {"error": "empty domain"}

    mx_info = check_mx(domain)
    # TXT records via dns_resolve (DoH fallback) when available, else _resolve_txt
    try:
        txt_records = dns_resolve.txt(domain)
    except Exception:
        txt_records = _resolve_txt(domain)
    spf = next((t for t in txt_records if t.lower().startswith("v=spf1")), None)

    try:
        dmarc_txt = dns_resolve.txt(f"_dmarc.{domain}")
    except Exception:
        dmarc_txt = _resolve_txt(f"_dmarc.{domain}")
    dmarc = next((t for t in dmarc_txt if t.lower().startswith("v=dmarc1")), None)
    policy = None
    if dmarc:
        m = re.search(r"p=(\w+)", dmarc)
        policy = m.group(1) if m else None
    return {
        "domain": domain,
        "has_mx": mx_info.get("has_mx", False),
        "mx_records": mx_info.get("mx_records", []),
        "provider": mx_info.get("provider", "other"),
        "big_host": mx_info.get("big_host", False),
        "dns_method": mx_info.get("method", "none"),
        "spf": spf, "has_spf": bool(spf),
        "dmarc": dmarc, "dmarc_policy": policy, "has_dmarc": bool(dmarc),
        "summary": ("Receives mail" if mx_info.get("has_mx") else "No MX") +
                   (", SPF set" if spf else ", no SPF") +
                   (f", DMARC p={policy}" if policy else ", no DMARC"),
    }


@mcp.tool
def verify_deliverability(email: str, smtp: bool = True, deep: bool = False) -> dict:
    """Full deliverability check for a single email: syntax → typo suggestion → disposable →
    MX (DoH fallback) → SMTP probe → account-existence enumeration (works on Gmail/M365) →
    Gravatar → verifier APIs if keys set. Returns honest tri-state deliverable + numeric score
    + human summary. Cached 14d. Never raises. Big-host results are honest None, not a false False."""
    return emailverify.verify(email, check_smtp=smtp, use_cache=True, deep=deep)


@mcp.tool
def provider_fingerprint(domain: str) -> dict:
    """Identify a domain's exact mail provider (Google/M365/Zoho/Yahoo/gateway) by probing
    MX + SPF include-chain + DKIM selectors + DMARC + MTA-STS + BIMI, and return a verification
    playbook (whether SMTP is reliable, accept-all behavior, pattern prior). All DoH-backed DNS."""
    try:
        from mcp_base.frontier.fingerprint import fingerprint
        return fingerprint(domain)
    except Exception as e:  # noqa: BLE001
        return {"domain": domain, "error": str(e)}


def _bulk_verify_core(emails: list[str], smtp: bool = True) -> dict:
    clean = [e.strip() for e in emails if isinstance(e, str) and e.strip()][:200]
    from concurrent.futures import ThreadPoolExecutor, as_completed
    results: dict[str, dict] = {}
    pool = ThreadPoolExecutor(max_workers=8)
    try:
        futs = {pool.submit(emailverify.verify, e, check_smtp=smtp): e for e in clean}
        try:
            for f in as_completed(futs, timeout=max(30.0, len(clean) * 1.5)):
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
    return {"count": len(ordered), "deliverable": deliverable, "undeliverable": undeliverable,
            "unknown": len(ordered) - deliverable - undeliverable, "results": ordered}


@mcp.tool
def bulk_verify(emails: list[str], smtp: bool = True) -> dict:
    """Verify a batch of emails in parallel via the shared deliverability core (cache + quota aware).
    Returns per-email results + a roll-up {deliverable/undeliverable/unknown}.
    Small batches return inline; large batches (>25) return a {job_id} to poll with verify_status()."""
    if not isinstance(emails, list):
        return {"count": 0, "results": [], "error": "emails must be a list"}
    if len([e for e in emails if isinstance(e, str) and e.strip()]) <= 25:
        return _bulk_verify_core(emails, smtp=smtp)

    def _worker(job: dict) -> None:
        JOBS.set(job["id"], status="running", percent=5.0)
        try:
            out = _bulk_verify_core(emails, smtp=smtp)
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=str(e))
            return
        JOBS.finish(job["id"], ok_=True, **out)

    return JOBS.run_or_job("bulk_verify", _worker, count=len(emails[:200]))


@mcp.tool
def verify_status(job_id: str) -> dict:
    """Poll a backgrounded bulk_verify() job by its job_id. Returns status + results when done."""
    return JOBS.status(job_id)


@mcp.tool
def health() -> dict:
    """Report server health: DNS/DoH reachability, emailverify module status, API key states."""
    checks: dict = {}
    degraded: list[str] = []
    try:
        r = dns_resolve.mx("gmail.com")
        checks["dns"] = f"ok ({r.get('method', '?')})"
    except Exception as e:
        checks["dns"] = f"error: {e}"
        degraded.append("dns: pip install dnspython or check network")

    try:
        vs = emailverify.cache_stats()
        checks["emailverify_cache"] = vs.get("total", 0)
    except Exception as e:
        checks["emailverify"] = f"error: {e}"
        degraded.append("emailverify: module error")

    return {"ok": len(degraded) == 0, "checks": checks, "degraded": degraded}


@mcp.tool
def selftest(live: bool = False) -> dict:
    """Preflight matrix: DNS, emailverify, check_mx, spam_score — all pass/fail.
    live=True runs end-to-end against a known-good public address."""
    results: dict[str, str] = {}
    errors: list[str] = []

    try:
        r = dns_resolve.mx("gmail.com")
        results["dns_mx"] = f"pass ({r.get('method', '?')})" if r.get("mx") else "fail: empty"
    except Exception as e:
        results["dns_mx"] = f"fail: {e}"
        errors.append(str(e))

    try:
        r2 = check_mx("gmail.com")
        results["check_mx"] = "pass" if r2.get("has_mx") else "fail: no MX"
    except Exception as e:
        results["check_mx"] = f"fail: {e}"

    try:
        s = spam_score("Buy now! FREE offer!!", "Click here to claim your prize.")
        results["spam_score"] = "pass" if s.get("score", 0) > 0 else "fail: score=0"
    except Exception as e:
        results["spam_score"] = f"fail: {e}"

    if live:
        try:
            v = verify_deliverability("contact@anthropic.com")
            d = v.get("deliverable")
            results["live_verify"] = "pass" if d is not False else f"fail: deliverable={d}"
        except Exception as e:
            results["live_verify"] = f"fail: {e}"
            errors.append(str(e))

    passed = sum(1 for v in results.values() if v.startswith("pass"))
    failed = sum(1 for v in results.values() if v.startswith("fail"))
    return {"ok": failed == 0, "pass": passed, "fail": failed, "components": results, "errors": errors}


if __name__ == "__main__":
    mcp.run()
