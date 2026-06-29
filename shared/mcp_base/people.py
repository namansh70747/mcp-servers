"""Free identity resolution: company → domain → exec name/title/LinkedIn.

All sources are free and keyless (GitHub token only lifts rate limits).
No paid API calls. No logged-in LinkedIn scraping.

Main function:
  find_exec(company, role="CEO", domain=None) → {name, title, linkedin_url, ...}

Supporting:
  find_domain(company)           → str | None
  dev_email_sources(name, domain)→ [str, ...]   (GitHub/GitLab/npm/PyPI emails)
  academic_sources(name)         → [str, ...]   (ORCID/Scholar emails)
"""
from __future__ import annotations

import re
from typing import Any

from .fetch import fetch
from .http import get_json, get_text

_EMAIL_RE = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")

# Tokens that mean a regex matched JavaScript/code/boilerplate, not a human name. Scraping team/
# about pages of JS-heavy sites otherwise leaks things like "typeof WorkerGlobalScope" as a "name",
# which then gets synthesized into a fabricated email. Reject these hard.
_CODE_TOKENS = frozenset({
    "typeof", "function", "var", "const", "let", "null", "undefined", "return", "void",
    "window", "document", "prototype", "object", "array", "string", "number", "boolean",
    "global", "workerglobalscope", "self", "async", "await", "import", "export", "class",
    "true", "false", "nan", "infinity", "symbol", "promise", "json", "math", "date",
    "default", "static", "public", "private", "new", "delete", "instanceof", "module",
})
# Generic page words that are not personal names (avoid "Privacy Policy", "Contact Us", etc.)
_NONNAME_TOKENS = frozenset({
    "privacy", "policy", "terms", "cookie", "cookies", "contact", "about", "team", "home",
    "careers", "support", "login", "sign", "menu", "search", "newsletter", "subscribe",
    "copyright", "rights", "reserved", "company", "solutions", "products", "services",
})
_NAME_PREFIXES = ("Mc", "Mac", "De", "La", "Le", "Van", "Von", "O'", "Di", "Du", "St")


def _looks_like_name(s: str | None) -> bool:
    """True only for a plausible human name (2–4 capitalized alpha tokens, no code/boilerplate).

    Guards every name returned by find_exec so JS/CSS/boilerplate scraped off a page can never
    become a fabricated email. Allows McX/MacX/O'X/De-style real names; rejects camelCase code."""
    s = (s or "").strip()
    if not s or len(s) > 40:
        return False
    toks = s.split()
    if not (2 <= len(toks) <= 4):
        return False
    for t in toks:
        low = t.lower().strip(".'-")
        if low in _CODE_TOKENS or low in _NONNAME_TOKENS:
            return False
        if not re.fullmatch(r"[A-Za-z][A-Za-z'’.\-]{0,19}", t):
            return False
        if not t[0].isupper():
            return False
        # camelCase (lower→Upper inside a token) signals a code identifier, unless a real name prefix
        if re.search(r"[a-z][A-Z]", t) and not t.startswith(_NAME_PREFIXES):
            return False
    return True


# ---------------------------------------------------------------------------
# Company → domain
# ---------------------------------------------------------------------------

def find_domain(company: str, hint: str | None = None) -> str | None:
    """Best-effort company name → email-sending domain."""
    from .dns_resolve import resolve_domain
    return resolve_domain(company, hint_domain=hint)


# ---------------------------------------------------------------------------
# Executive / person discovery
# ---------------------------------------------------------------------------

def _name_from_li_slug(url: str) -> str | None:
    """Derive a person name from a linkedin.com/in/<slug> URL WITHOUT fetching (LinkedIn authwalls
    block the fetch). Drops trailing id tokens (digits / short alnum). e.g.
    /in/sam-altman-a2249613 → 'Sam Altman'."""
    m = re.search(r"/in/([a-z0-9\-]+)", url, re.I)
    if not m:
        return None
    toks = [t for t in m.group(1).split("-") if t and not any(c.isdigit() for c in t) and len(t) > 1]
    if len(toks) >= 2:
        return " ".join(t.capitalize() for t in toks[:3])
    return None


def find_exec(company: str, role: str = "CEO",
              domain: str | None = None, fast: bool = True,
              budget_s: float = 25.0) -> dict:
    """Discover the person holding `role` at `company` using free public sources.

    fast=True (default): return as soon as a LinkedIn /in/ URL is found, deriving the name from the
    URL slug (no slow fetch, no Wikipedia/Crunchbase/About-page crawl) — all the extension reveal
    path needs. The richer (slower) discovery runs only as a fallback when no LinkedIn URL is found.

    budget_s caps total wall-clock: each discovery source is skipped once the deadline passes, so
    this can never grind (a no-result company returns in ~budget_s, not minutes).

    Returns {name, title, linkedin_url, company, domain, sources, emails} (fields may be None/empty).
    """
    import time as _t
    _deadline = _t.monotonic() + max(5.0, budget_s)

    def _over() -> bool:
        return _t.monotonic() > _deadline

    if not domain:
        domain = find_domain(company)

    result: dict[str, Any] = {
        "name": None, "title": role, "linkedin_url": None,
        "company": company, "domain": domain, "sources": [], "emails": [],
    }

    # 1. SERP — LinkedIn-targeted query FIRST so the fast path hits immediately.
    from .websearch import search_links
    queries = [
        f'site:linkedin.com/in "{company}" {role}',
        f'"{company}" {role} name',
        f'"{company}" {role} founder CEO "contact"',
    ]
    for q in queries[:3]:
        if _over():
            break
        try:
            links = search_links(q, n=3)
            for url in links:
                # Fast path: a LinkedIn profile URL → derive name from slug and return IMMEDIATELY
                # (no fetch — LinkedIn authwalls the fetch and costs ~10s). The Apollo reveal only
                # needs the URL; it reads whoever the panel shows. Name (from slug) is best-effort.
                if fast and re.search(r"linkedin\.com/in/[a-z0-9\-]+", url, re.I):
                    li_url = url.split("?")[0]
                    result["linkedin_url"] = li_url
                    result["sources"].append(li_url)
                    slug_name = _name_from_li_slug(li_url)
                    result["name"] = slug_name if _looks_like_name(slug_name) else None
                    return result
                name, li_url = _extract_person_from_url(url, company, role)
                if name and _looks_like_name(name):
                    result["name"] = name
                    result["sources"].append(url)
                    if li_url and not result["linkedin_url"]:
                        result["linkedin_url"] = li_url
                    break
            if result["name"]:
                break
        except Exception:
            continue

    # If fast mode found a LinkedIn URL (even without a clean slug name), that's enough — return.
    if fast and result["linkedin_url"]:
        return result

    # 2. Wikipedia / Wikidata
    if not result["name"] and company and not _over():
        try:
            name = _wikipedia_exec(company, role)
            if name and _looks_like_name(name):
                result["name"] = name
                result["sources"].append("wikipedia")
        except Exception:
            pass

    # 3. Crunchbase public page
    if not result["name"] and company and not _over():
        try:
            name, emails = _crunchbase_public(company, role)
            if name and _looks_like_name(name):
                result["name"] = name
                result["sources"].append("crunchbase")
            result["emails"].extend(emails)
        except Exception:
            pass

    # 4. Company website About/Team/Leadership page
    if domain and not result["name"] and not _over():
        try:
            name, emails = _scrape_about_page(domain, role)
            if name and _looks_like_name(name):
                result["name"] = name
                result["sources"].append(f"site:{domain}")
            result["emails"].extend(emails)
        except Exception:
            pass

    return result


def _extract_person_from_url(url: str, company: str, role: str) -> tuple[str | None, str | None]:
    """Fetch a URL and try to extract a person name for the given role."""
    try:
        r = fetch(url, timeout=10.0)
        if not r.get("ok"):
            return None, None
        html = r.get("html", "")

        # LinkedIn profile URL → extract name from path or title
        if "linkedin.com/in/" in url:
            # Path: /in/first-last
            m = re.search(r"/in/([a-z0-9\-]+)", url)
            if m:
                slug = m.group(1).replace("-", " ").title()
                if len(slug.split()) >= 2:
                    return slug, url
            # Try OG title
            m = re.search(r'<title>([^<|]+)', html)
            if m:
                title = m.group(1).strip()
                parts = title.split(" - ")
                if parts:
                    name = parts[0].strip()
                    if name and len(name.split()) >= 2:
                        return name, url

        # Generic page: look for role + name pattern
        pattern = rf"(?:{'|'.join(r.split() for r in [role, role.lower()])})[:\s]+([A-Z][a-z]+\s+[A-Z][a-z]+)"
        m = re.search(pattern, html)
        if m:
            return m.group(1).strip(), None

        # OG author
        m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]*content=["\']([^"\']+)["\']', html)
    except Exception:
        pass
    return None, None


def _wikipedia_exec(company: str, role: str) -> str | None:
    """Query Wikipedia API for company executive."""
    try:
        data = get_json(
            "https://en.wikipedia.org/api/rest_v1/page/summary/" +
            company.replace(" ", "_"),
            cache_ttl=3600,
        )
        if isinstance(data, dict):
            extract = data.get("extract", "")
            # Look for "founded by X" or "CEO is X"
            patterns = [
                rf"(?:{'|'.join(['CEO', 'chief executive', 'founder', 'co-founder', role])})[^.]*?([A-Z][a-z]+\s+[A-Z][a-z]+)",
            ]
            for p in patterns:
                m = re.search(p, extract, re.IGNORECASE)
                if m:
                    return m.group(1).strip()
    except Exception:
        pass
    return None


def _crunchbase_public(company: str, role: str) -> tuple[str | None, list[str]]:
    """Scrape Crunchbase public profile for executive name."""
    slug = re.sub(r"[^a-z0-9]+", "-", company.lower().strip()).strip("-")
    emails: list[str] = []
    try:
        r = fetch(f"https://www.crunchbase.com/organization/{slug}", timeout=12.0)
        if not r.get("ok"):
            return None, emails
        html = r.get("html", "")
        # Look for JSON-LD or script data with people
        for email in _EMAIL_RE.finditer(html):
            emails.append(email.group().lower())
        # Look for a person with the right title
        m = re.search(
            rf'["\']title["\'][^:]*:[^"\']*["\']([^"\']*(?:{role})[^"\']*)["\'].*?["\']name["\'][^:]*:[^"\']*["\']([^"\']+)["\']',
            html, re.IGNORECASE | re.DOTALL,
        )
        if m:
            return m.group(2).strip(), emails
    except Exception:
        pass
    return None, emails


def _scrape_about_page(domain: str, role: str) -> tuple[str | None, list[str]]:
    """Scrape the company's About/Team page for an executive with the given role."""
    from .harvest import discover_contact_pages
    from .email_extract import extract_emails

    pages = discover_contact_pages(domain)
    emails: list[str] = []
    for url in pages[:8]:
        try:
            r = fetch(url, timeout=10.0)
            if not r.get("ok"):
                continue
            html = r.get("html", "")
            # Collect all emails from the page
            for e, _ in extract_emails(html).items():
                if e not in emails:
                    emails.append(e)
            # Try to find role + nearby name
            m = re.search(
                rf'([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)[^<>{{}}]*?(?:{role})',
                html, re.IGNORECASE | re.DOTALL,
            )
            if not m:
                m = re.search(
                    rf'(?:{role})[^<>{{}}]*?([A-Z][a-z]+(?:\s+[A-Z][a-z]+)+)',
                    html, re.IGNORECASE | re.DOTALL,
                )
            if m:
                candidate = m.group(1).strip()
                if len(candidate.split()) >= 2:
                    return candidate, emails
        except Exception:
            continue
    return None, emails


# ---------------------------------------------------------------------------
# Developer email sources (keyless)
# ---------------------------------------------------------------------------

def dev_email_sources(name: str, domain: str) -> list[str]:
    """Find emails for a developer via GitHub/GitLab/npm/PyPI commit and package data."""
    found: list[str] = []
    import os

    # GitHub commit email search
    try:
        token = os.environ.get("GITHUB_PERSONAL_ACCESS_TOKEN", "").strip()
        headers = {"Accept": "application/vnd.github+json"}
        if token:
            headers["Authorization"] = f"Bearer {token}"
        # Search for commits by this person at the domain
        data = get_json(
            "https://api.github.com/search/commits",
            params={"q": f"author-email:{domain}", "per_page": 10},
            headers=headers, cache_ttl=3600,
        )
        for item in (data or {}).get("items", []):
            email = item.get("commit", {}).get("author", {}).get("email", "")
            if email and "@" in email and domain in email and email not in found:
                found.append(email.lower())
    except Exception:
        pass

    # npm package author emails
    try:
        # Search npm registry for packages from this domain
        data = get_json(
            "https://registry.npmjs.org/-/v1/search",
            params={"text": f"author:{domain} maintainer:{name}", "size": 5},
            cache_ttl=3600,
        )
        for pkg in (data or {}).get("objects", []):
            email = pkg.get("package", {}).get("author", {}).get("email", "")
            if email and domain in email and email not in found:
                found.append(email.lower())
    except Exception:
        pass

    # PyPI package author emails
    try:
        name_slug = re.sub(r"\s+", "-", name.lower().strip())
        data = get_json(f"https://pypi.org/pypi/{name_slug}/json", cache_ttl=3600)
        if isinstance(data, dict):
            email = data.get("info", {}).get("author_email", "")
            if email and domain in email and email not in found:
                found.append(email.lower().strip())
    except Exception:
        pass

    return found


# ---------------------------------------------------------------------------
# Academic sources
# ---------------------------------------------------------------------------

def academic_sources(name: str) -> list[str]:
    """Find emails via ORCID and similar academic registries."""
    found: list[str] = []
    try:
        # ORCID public search API
        data = get_json(
            "https://pub.orcid.org/v3.0/search/",
            params={"q": f'family-name:"{name.split()[-1]}" AND given-names:"{name.split()[0]}"',
                    "rows": 5},
            headers={"Accept": "application/json"},
            cache_ttl=3600,
        )
        for result in (data or {}).get("result", []):
            orcid_id = result.get("orcid-identifier", {}).get("path", "")
            if orcid_id:
                profile = get_json(
                    f"https://pub.orcid.org/v3.0/{orcid_id}/person",
                    headers={"Accept": "application/json"},
                    cache_ttl=3600,
                )
                for email_obj in ((profile or {}).get("emails", {}).get("email") or []):
                    e = email_obj.get("email", "")
                    if e and e not in found:
                        found.append(e.lower())
    except Exception:
        pass
    return found
