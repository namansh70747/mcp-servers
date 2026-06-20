"""apollo — connector to Apollo.io for finding a company's decision-makers (CTO/CEO/team).

Uses Apollo's FREE people-search API (no credits) which returns the person + title + LinkedIn
but NOT the email (email reveal is paid). Pair with email-finder to resolve the address for free.
Set APOLLO_API_KEY in .env; if absent, tools return a clear hint to use the Apollo web UI manually.
Seniority/location filters, pagination, and org enrichment are supported. Offline helper tools
(seniorities/titles_catalog/has_key) work without a key.
"""
from __future__ import annotations

from mcp_base import get_env, http, make_server

mcp = make_server(
    "apollo",
    instructions=("Find CTO/CEO/team at a company (free people-search; no emails on free tier). "
                  "Filter by seniority/title/location, paginate, enrich orgs. Then resolve emails "
                  "with email-finder.find()."),
)

BASE = "https://api.apollo.io/api/v1"

MAX_PER_PAGE = 100


def _clamp(n: int, default: int, hi: int = MAX_PER_PAGE) -> int:
    try:
        v = int(n)
    except (TypeError, ValueError):
        return default
    if v <= 0:
        return default
    return min(v, hi)

DEFAULT_TITLES = ["CEO", "CTO", "Founder", "Co-Founder", "VP Engineering"]
SENIORITIES = ["owner", "founder", "c_suite", "partner", "vp", "head", "director",
               "manager", "senior", "entry", "intern"]
TITLE_PRESETS = {
    "founders": ["Founder", "Co-Founder", "CEO", "Owner"],
    "engineering": ["CTO", "VP Engineering", "Head of Engineering", "Engineering Manager",
                    "Lead Engineer", "Director of Engineering"],
    "product": ["CPO", "VP Product", "Head of Product", "Product Manager"],
    "sales": ["CRO", "VP Sales", "Head of Sales", "Sales Director", "Account Executive"],
    "marketing": ["CMO", "VP Marketing", "Head of Marketing", "Growth Lead"],
    "decision_makers": ["CEO", "CTO", "CFO", "COO", "Founder", "VP", "Head"],
}


def _key_or_hint() -> tuple[str | None, dict | None]:
    key = get_env("APOLLO_API_KEY")
    if not key:
        return None, {"error": "no APOLLO_API_KEY",
                      "hint": "Add a free Apollo API key to .env, or look the person up in the Apollo "
                              "web UI and pass their name+domain to email-finder.find()."}
    return key, None


def _headers(key: str) -> dict:
    return {"X-Api-Key": key, "Content-Type": "application/json",
            "Cache-Control": "no-cache", "Accept": "application/json"}


def _api(method: str, path: str, key: str, *, json_body: dict | None = None,
         params: dict | None = None, timeout: float = 25) -> tuple[dict | None, dict | None]:
    """Call the Apollo API via the shared resilient http client (retry/backoff/UA).
    Returns (json, None) on success or (None, {"error": ...}) preserving the old error shape."""
    r = http.request(method, f"{BASE}{path}", headers=_headers(key), json_body=json_body,
                     params=params, timeout=timeout)
    if not r.get("ok"):
        return None, {"error": r.get("error") or f"HTTP {r.get('status')}"}
    if "json" not in r:
        return None, {"error": "non-JSON response"}
    return r["json"], None


def _person_row(p: dict) -> dict:
    org = p.get("organization") or {}
    return {
        "name": p.get("name"),
        "title": p.get("title"),
        "seniority": p.get("seniority"),
        "linkedin_url": p.get("linkedin_url"),
        "github": (p.get("github_url") or "").rsplit("/", 1)[-1] if p.get("github_url") else "",
        "location": ", ".join(x for x in (p.get("city"), p.get("state"), p.get("country")) if x),
        "organization": org.get("name"),
        "domain": org.get("primary_domain") or org.get("website_url"),
    }


@mcp.tool
def find_people(domain: str, titles: list[str] | None = None, limit: int = 5,
                seniorities: list[str] | None = None, locations: list[str] | None = None,
                page: int = 1) -> dict:
    """Find people at a company domain by title (default CEO/CTO/founder). Free tier: no emails —
    returns name, title, seniority, LinkedIn, location; feed those to email-finder. Supports
    seniority/location filters and pagination (page)."""
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    key, hint = _key_or_hint()
    if hint:
        return hint
    body: dict = {
        "q_organization_domains_list": [domain],
        "person_titles": titles or DEFAULT_TITLES,
        "page": _clamp(page, 1, 50000),
        "per_page": _clamp(limit, 5),
    }
    if seniorities:
        body["person_seniorities"] = seniorities
    if locations:
        body["person_locations"] = locations
    data, err_ = _api("POST", "/mixed_people/api_search", key, json_body=body)
    if err_:
        return err_
    people = [_person_row(p) for p in (data.get("people", []) or [])]
    pg = data.get("pagination", {}) or {}
    return {"domain": domain, "count": len(people), "people": people,
            "pagination": {"page": pg.get("page"), "per_page": pg.get("per_page"),
                           "total_entries": pg.get("total_entries"),
                           "total_pages": pg.get("total_pages")},
            "note": "Emails not included on Apollo free tier — resolve with email-finder.find(name, domain)."}


@mcp.tool
def find_people_paged(domain: str, titles: list[str] | None = None, per_page: int = 10,
                      max_pages: int = 3, seniorities: list[str] | None = None) -> dict:
    """Auto-paginate find_people across up to max_pages, deduping by name+title. Use when you want
    the whole team rather than the first page."""
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    key, hint = _key_or_hint()
    if hint:
        return hint
    per_page = _clamp(per_page, 10)
    max_pages = _clamp(max_pages, 3, 100)
    seen: set[tuple] = set()
    people: list[dict] = []
    total_pages = None
    for page in range(1, max_pages + 1):
        res = find_people(domain, titles, per_page, seniorities, None, page)
        if "error" in res:
            return res if not people else {"domain": domain, "count": len(people),
                                           "people": people, "partial_error": res["error"]}
        for p in res.get("people", []):
            k = (p.get("name"), p.get("title"))
            if k not in seen:
                seen.add(k)
                people.append(p)
        total_pages = res.get("pagination", {}).get("total_pages")
        if total_pages and page >= total_pages:
            break
        if not res.get("people"):
            break
    return {"domain": domain, "count": len(people), "people": people, "pages_fetched": page}


@mcp.tool
def find_company(domain: str = "", name: str = "") -> dict:
    """Look up an organization by domain or name (industry, size, funding fields)."""
    domain = (domain or "").strip()
    name = (name or "").strip()
    if not domain and not name:
        return {"error": "provide a domain or name"}
    key, hint = _key_or_hint()
    if hint:
        return hint
    body = {"q_organization_name": name} if name else {"q_organization_domains_list": [domain]}
    data, err_ = _api("POST", "/mixed_companies/search", key, json_body=body)
    if err_:
        return err_
    orgs = [{
        "name": o.get("name"), "domain": o.get("primary_domain"),
        "industry": o.get("industry"), "employees": o.get("estimated_num_employees"),
        "founded_year": o.get("founded_year"),
        "linkedin_url": o.get("linkedin_url"),
        "keywords": (o.get("keywords") or [])[:8],
        "total_funding": o.get("total_funding_printed") or o.get("total_funding"),
        "latest_funding": o.get("latest_funding_stage"),
    } for o in (data.get("organizations", []) or [])[:5]]
    return {"organizations": orgs}


@mcp.tool
def enrich_org(domain: str) -> dict:
    """Enrich a single organization by domain (richer firmographics than find_company): industry,
    size, founded year, funding, location, keywords, social links."""
    domain = (domain or "").strip()
    if not domain:
        return {"error": "domain is required"}
    key, hint = _key_or_hint()
    if hint:
        return hint
    data, err_ = _api("GET", "/organizations/enrich", key, params={"domain": domain})
    if err_:
        return err_
    o = (data or {}).get("organization", {}) or {}
    if not o:
        return {"domain": domain, "found": False}
    return {"domain": domain, "found": True, "organization": {
        "name": o.get("name"), "website": o.get("website_url"),
        "industry": o.get("industry"), "employees": o.get("estimated_num_employees"),
        "founded_year": o.get("founded_year"),
        "location": ", ".join(x for x in (o.get("city"), o.get("state"), o.get("country")) if x),
        "linkedin_url": o.get("linkedin_url"), "twitter_url": o.get("twitter_url"),
        "total_funding": o.get("total_funding_printed") or o.get("total_funding"),
        "latest_funding": o.get("latest_funding_stage"),
        "keywords": (o.get("keywords") or [])[:12],
        "description": (o.get("short_description") or "")[:400],
    }}


@mcp.tool
def enrich_person(name: str = "", domain: str = "", linkedin_url: str = "") -> dict:
    """Match a single person by name+domain (or LinkedIn URL). Free tier: no email returned — use the
    resulting name+domain with email-finder.find()."""
    key, hint = _key_or_hint()
    if hint:
        return hint
    body: dict = {}
    if name:
        body["name"] = name
    if domain:
        body["domain"] = domain
    if linkedin_url:
        body["linkedin_url"] = linkedin_url
    if not body:
        return {"error": "provide name+domain or linkedin_url"}
    data, err_ = _api("POST", "/people/match", key, json_body=body)
    if err_:
        return err_
    p = (data or {}).get("person", {}) or {}
    if not p:
        return {"found": False}
    row = _person_row(p)
    row["found"] = True
    row["note"] = "Email not revealed on free tier — resolve with email-finder.find(name, domain)."
    return row


@mcp.tool
def seniorities() -> dict:
    """List the valid Apollo seniority filter values (offline; no key needed)."""
    return {"seniorities": SENIORITIES}


@mcp.tool
def titles_catalog() -> dict:
    """Common decision-maker title presets you can pass to find_people (offline; no key needed)."""
    return {"presets": TITLE_PRESETS}


@mcp.tool
def has_key() -> dict:
    """Introspection: whether APOLLO_API_KEY is configured (so the agent can choose its path)."""
    return {"configured": bool(get_env("APOLLO_API_KEY"))}


if __name__ == "__main__":
    mcp.run()
