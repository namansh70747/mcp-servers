"""F4 — mail-provider fingerprinting → per-provider verification playbook.

Probes MX + SPF include-chain + DKIM selectors + DMARC + MTA-STS + BIMI (all via dns_resolve, so
DoH-backed and never a false-empty on a blocked port 53) to identify the exact provider and return
a strategy hint: which providers accept-all at RCPT (skip SMTP, lean on account-enumeration), which
catch-all, and pattern priors. Pure DNS — always available.
"""
from __future__ import annotations

from .. import dns_resolve

# Common DKIM selectors by provider (probed as <selector>._domainkey.<domain>)
_DKIM_SELECTORS = ["google", "selector1", "selector2", "default", "k1", "k2", "dkim",
                   "mandrill", "mail", "smtp", "s1", "s2", "zoho", "fm1", "fm2", "fm3"]

# provider tag → verification playbook
_PLAYBOOK = {
    "google":  {"smtp_reliable": False, "accept_all": True,
                "strategy": "Skip SMTP (Gmail/Workspace accept-all); use account-enumeration + GHunt.",
                "pattern_prior": "{first}.{last}"},
    "m365":    {"smtp_reliable": False, "accept_all": True,
                "strategy": "Skip SMTP (M365 accepts at RCPT then bounces); use enumeration.",
                "pattern_prior": "{first}.{last}"},
    "zoho":    {"smtp_reliable": True, "accept_all": False,
                "strategy": "SMTP probe is fairly reliable on Zoho.", "pattern_prior": "{first}"},
    "yahoo":   {"smtp_reliable": False, "accept_all": True,
                "strategy": "Yahoo accept-all; lean on enumeration + Gravatar.",
                "pattern_prior": "{first}.{last}"},
    "proofpoint": {"smtp_reliable": False, "accept_all": True,
                   "strategy": "Security gateway — masks RCPT; SMTP unreliable.",
                   "pattern_prior": "{first}.{last}"},
    "mimecast": {"smtp_reliable": False, "accept_all": True,
                 "strategy": "Security gateway — masks RCPT; SMTP unreliable.",
                 "pattern_prior": "{first}.{last}"},
    "other":   {"smtp_reliable": True, "accept_all": None,
                "strategy": "Unknown provider — SMTP probe + catch-all detection apply.",
                "pattern_prior": "{first}.{last}"},
}


def fingerprint(domain: str) -> dict:
    """Identify a domain's mail provider and return a verification playbook.

    Returns {domain, provider, mx, has_spf, spf_includes, dkim_selectors, has_dmarc, dmarc_policy,
             has_mta_sts, has_bimi, method, playbook{...}}.
    """
    domain = (domain or "").strip().lower().lstrip("@")
    out: dict = {"domain": domain}
    if not domain:
        return {"error": "empty domain"}

    mxr = dns_resolve.mx(domain)
    provider = mxr.get("provider", "other")
    out["mx"] = mxr.get("mx", [])
    out["method"] = mxr.get("method", "none")

    # SPF
    try:
        txts = dns_resolve.txt(domain)
    except Exception:
        txts = []
    spf = next((t for t in txts if t.lower().startswith("v=spf1")), None)
    out["has_spf"] = bool(spf)
    out["spf_includes"] = []
    if spf:
        try:
            out["spf_includes"] = dns_resolve.spf_includes(domain)
        except Exception:
            pass
        # refine provider from SPF includes if MX was ambiguous
        inc = " ".join(out["spf_includes"]).lower()
        if provider == "other":
            if "google" in inc or "_spf.google" in inc:
                provider = "google"
            elif "protection.outlook" in inc or "spf.protection" in inc:
                provider = "m365"
            elif "zoho" in inc:
                provider = "zoho"

    # DKIM selector probe (best-effort; TXT presence only)
    found_sel = []
    for sel in _DKIM_SELECTORS:
        try:
            recs = dns_resolve.txt(f"{sel}._domainkey.{domain}")
            if recs:
                found_sel.append(sel)
        except Exception:
            continue
        if len(found_sel) >= 4:
            break
    out["dkim_selectors"] = found_sel

    # DMARC
    try:
        dmarc_txts = dns_resolve.txt(f"_dmarc.{domain}")
    except Exception:
        dmarc_txts = []
    dmarc = next((t for t in dmarc_txts if t.lower().startswith("v=dmarc1")), None)
    out["has_dmarc"] = bool(dmarc)
    out["dmarc_policy"] = None
    if dmarc:
        import re
        m = re.search(r"p=(\w+)", dmarc)
        out["dmarc_policy"] = m.group(1) if m else None

    # MTA-STS + BIMI (presence of the well-known TXT)
    try:
        out["has_mta_sts"] = bool(dns_resolve.txt(f"_mta-sts.{domain}"))
    except Exception:
        out["has_mta_sts"] = False
    try:
        out["has_bimi"] = bool(dns_resolve.txt(f"default._bimi.{domain}"))
    except Exception:
        out["has_bimi"] = False

    out["provider"] = provider
    out["playbook"] = _PLAYBOOK.get(provider, _PLAYBOOK["other"])
    return out
