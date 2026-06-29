"""Wrappers for best-in-class free OSINT engines.

Each function is self-contained, lazy-imports its dependency, and returns a structured
dict or an empty/None result when the tool is absent — never raises.

Available:
  theharvester(domain, sources=None)  — 40+ passive sources via theHarvester
  holehe_check(email)                 — account-existence (wraps enumerate_accounts)
  ghunt_check(email)                  — reverse Gmail → Google account name/services
  maigret_check(username)             — username → 3000+ profile sites
  h8mail_check(email)                 — breach corroboration via h8mail
  pgp_search(name_or_email, domain)   — PGP keyserver search
  reacher_check(email)                — Reacher self-hosted SMTP verifier (HTTP)
  firecrawl_scrape(url)               — Firecrawl self-hosted scrape (HTTP)
  crtsh_search(domain)                — Certificate Transparency subdomains
"""
from __future__ import annotations

import re
from typing import Any

from .fetch import fetch
from .http import get_json, get_text


# ---------------------------------------------------------------------------
# theHarvester
# ---------------------------------------------------------------------------

def theharvester(domain: str, sources: list[str] | None = None,
                 limit: int = 200) -> dict:
    """Run theHarvester against a domain.

    Returns {emails, hosts, names, errors, method}.
    Falls back to a subset of HTTP-based harvesting if theHarvester isn't installed.
    """
    # Try the installed theHarvester library
    try:
        import subprocess
        import json as _json
        import tempfile
        import os
        src = ",".join(sources or [
            "certspotter", "crtsh", "dnsdumpster", "github-code", "hackertarget",
            "rapiddns", "urlscan", "waybackarchive",
        ])
        with tempfile.NamedTemporaryFile(suffix=".json", delete=False) as f:
            out_file = f.name
        try:
            result = subprocess.run(
                ["theHarvester", "-d", domain, "-b", src, "-l", str(limit), "-f", out_file],
                capture_output=True, text=True, timeout=120,
            )
            if os.path.exists(out_file + ".json"):
                with open(out_file + ".json") as fh:
                    data = _json.load(fh)
                return {
                    "emails": list(data.get("emails", [])),
                    "hosts": list(data.get("hosts", [])),
                    "names": [],
                    "errors": [],
                    "method": "theharvester_cli",
                }
        finally:
            for ext in ("", ".json", ".xml"):
                try:
                    os.unlink(out_file + ext)
                except Exception:
                    pass
    except (FileNotFoundError, ImportError):
        pass
    except Exception as e:
        pass  # fall through to HTTP fallback

    # HTTP fallback: subset of passive sources we can query directly
    emails: set[str] = set()
    hosts: set[str] = set()
    errors: list[str] = []

    # crt.sh
    try:
        data = get_json(f"https://crt.sh/?q=%.{domain}&output=json", cache_ttl=3600)
        if isinstance(data, list):
            for entry in data[:500]:
                name = entry.get("name_value", "") or entry.get("common_name", "")
                for part in name.split("\n"):
                    part = part.strip().lstrip("*.")
                    if part and domain in part:
                        hosts.add(part)
    except Exception as e:
        errors.append(f"crtsh:{str(e)[:40]}")

    # HackerTarget
    try:
        text = get_text(f"https://api.hackertarget.com/hostsearch/?q={domain}",
                        cache_ttl=3600)
        for line in (text or "").splitlines():
            parts = line.split(",")
            if len(parts) >= 1:
                hosts.add(parts[0].strip())
    except Exception as e:
        errors.append(f"hackertarget:{str(e)[:40]}")

    # urlscan.io
    try:
        data = get_json("https://urlscan.io/api/v1/search/",
                        params={"q": f"domain:{domain}", "size": 50},
                        cache_ttl=3600)
        for result in (data or {}).get("results", []):
            page = result.get("page", {})
            if page.get("domain", "").endswith(domain):
                hosts.add(page["domain"])
    except Exception as e:
        errors.append(f"urlscan:{str(e)[:40]}")

    return {
        "emails": list(emails),
        "hosts": list(hosts),
        "names": [],
        "errors": errors,
        "method": "http_fallback",
    }


# ---------------------------------------------------------------------------
# holehe (delegates to enumerate_accounts)
# ---------------------------------------------------------------------------

def holehe_check(email: str) -> dict:
    """Account-existence enumeration wrapping enumerate_accounts.probe()."""
    from .enumerate_accounts import probe
    return probe(email)


# ---------------------------------------------------------------------------
# GHunt — reverse Gmail → Google account
# ---------------------------------------------------------------------------

def ghunt_check(email: str) -> dict | None:
    """Reverse a Gmail address to a Google account (name, photo, services).
    Returns None if GHunt is not installed or the account isn't found.
    """
    try:
        import ghunt.globals as ghunt_globals
        from ghunt import user as ghunt_user
        # GHunt requires prior authentication (ghunt login) — check for creds
        import asyncio
        async def _run():
            creds = ghunt_globals.GHuntCreds()
            try:
                await creds.load_creds(silent=True)
            except Exception:
                return None
            target = ghunt_user.GoogleUser()
            await target.from_email(creds, email)
            if not target.found:
                return None
            return {
                "name": target.name,
                "profile_photo": getattr(target, "profile_photo", None),
                "services": getattr(target, "services", []),
                "found": True,
            }
        return asyncio.run(_run())
    except ImportError:
        return None
    except Exception:
        return None


# ---------------------------------------------------------------------------
# Maigret — username → 3000+ profile sites
# ---------------------------------------------------------------------------

def maigret_check(username: str) -> dict:
    """Search for a username across 3000+ sites via maigret."""
    try:
        import asyncio
        import maigret.maigret as mg
        results = asyncio.run(mg.maigret(username, top_sites=100))
        found = [site for site, r in (results or {}).items()
                 if r.get("status", {}).get("status") == "Claimed"]
        return {"username": username, "found_on": found}
    except ImportError:
        return {"username": username, "found_on": [], "error": "maigret not installed"}
    except Exception as e:
        return {"username": username, "found_on": [], "error": str(e)[:80]}


# ---------------------------------------------------------------------------
# h8mail — breach corroboration
# ---------------------------------------------------------------------------

def h8mail_check(email: str) -> dict:
    """Check email against breach databases via h8mail."""
    try:
        import h8mail.utils.class_file as h8
        results = h8.h8mail([email], config_file=None, json_path=None)
        return {"email": email, "breaches": results}
    except ImportError:
        return {"email": email, "breaches": [], "error": "h8mail not installed"}
    except Exception as e:
        return {"email": email, "breaches": [], "error": str(e)[:80]}


# ---------------------------------------------------------------------------
# PGP Keyservers
# ---------------------------------------------------------------------------

def pgp_search(name_or_email: str, domain: str | None = None) -> list[str]:
    """Search PGP keyservers for email addresses.

    Returns a list of email addresses found.
    """
    found: list[str] = []
    EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

    servers = [
        ("keys.openpgp.org", f"https://keys.openpgp.org/vks/v1/search?q={name_or_email}"),
        ("keyserver.ubuntu.com",
         f"https://keyserver.ubuntu.com/pks/lookup?search={name_or_email}&op=index&fingerprint=on"),
    ]
    for server_name, url in servers:
        try:
            text = get_text(url, cache_ttl=3600)
            if text:
                for m in EMAIL_RE.finditer(text):
                    e = m.group().lower()
                    if domain is None or e.endswith(f"@{domain}"):
                        if e not in found:
                            found.append(e)
        except Exception:
            continue

    return found


# ---------------------------------------------------------------------------
# crt.sh — Certificate Transparency
# ---------------------------------------------------------------------------

def crtsh_emails(domain: str) -> list[str]:
    """Extract emails from crt.sh CT log entries for a domain."""
    EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
    found: list[str] = []
    try:
        data = get_json(f"https://crt.sh/?q=%.{domain}&output=json", cache_ttl=3600)
        if isinstance(data, list):
            for entry in data[:200]:
                text = entry.get("name_value", "") + " " + entry.get("common_name", "")
                for m in EMAIL_RE.finditer(text):
                    e = m.group().lower()
                    if e not in found:
                        found.append(e)
    except Exception:
        pass
    return found


# ---------------------------------------------------------------------------
# Reacher HTTP client
# ---------------------------------------------------------------------------

def reacher_verify(email: str) -> dict | None:
    """Call the self-hosted Reacher verifier via HTTP.

    Returns the JSON result or None if not configured/unavailable.
    """
    import os
    base_url = os.environ.get("REACHER_BASE_URL", "").strip()
    if not base_url:
        return None
    try:
        from .http import request
        r = request("POST", f"{base_url.rstrip('/')}/v0/check_email",
                    json_body={"to_email": email}, timeout=30.0)
        if r.get("ok") and r.get("json"):
            return r["json"]
    except Exception:
        pass
    return None


# ---------------------------------------------------------------------------
# Firecrawl HTTP client
# ---------------------------------------------------------------------------

def firecrawl_scrape(url: str, extract_schema: dict | None = None) -> dict | None:
    """Call a self-hosted Firecrawl instance for LLM-ready scraping.

    Returns the scrape result dict or None if not configured/unavailable.
    """
    import os
    fc_url = os.environ.get("FIRECRAWL_URL", "").strip()
    if not fc_url:
        return None
    try:
        from .http import request
        payload: dict = {"url": url, "formats": ["markdown", "html"]}
        if extract_schema:
            payload["extract"] = {"schema": extract_schema}
        r = request("POST", f"{fc_url.rstrip('/')}/v1/scrape",
                    json_body=payload, timeout=30.0)
        if r.get("ok") and r.get("json"):
            return r["json"]
    except Exception:
        pass
    return None
