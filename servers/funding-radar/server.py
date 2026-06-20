"""funding-radar — discover newly-funded companies from FREE sources: funding RSS feeds
(TechCrunch + a curated free set), SEC EDGAR Form D full-text (structured US rounds), and the
Hacker News Algolia API. Robust headline parsing (company/amount/round/sector), company->domain
guessing, dedup, freshness windows, and rich filtering. Stores leads in SQLite (deduped).

All sources are free and key-less (SEC just wants a polite User-Agent — set SEC_USER_AGENT)."""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from urllib.parse import urlsplit

from mcp_base import BaseStore, db_path, get_env, http, make_server

MAX_LIMIT = 200


def _clamp_limit(limit: int, default: int = 25) -> int:
    """Coerce a user-supplied limit into a sane positive bound."""
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, MAX_LIMIT)


def _safe_http_url(url: str) -> bool:
    """Only allow plain http(s) feed URLs (blocks file://, ftp://, etc. -> SSRF/local-file read)."""
    try:
        s = urlsplit((url or "").strip())
    except Exception:
        return False
    return s.scheme in ("http", "https") and bool(s.netloc)

mcp = make_server(
    "funding-radar",
    instructions=("Find newly-funded companies (free). scan() pulls RSS + SEC EDGAR + Hacker News; "
                  "filter_leads()/list_leads() browse results; enrich_targets() adds guessed domains. "
                  "Feed fresh leads to campaign.filter_uncontacted."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads(
  id INTEGER PRIMARY KEY, company TEXT, amount TEXT, round TEXT, sector TEXT,
  source TEXT, url TEXT, headline TEXT, found_at TEXT,
  UNIQUE(company, source)
);
"""
store = BaseStore(db_path("funding-radar"), schema=SCHEMA)

# Additive migration: new columns on existing installs (idempotent / safe).
for _col, _decl in (("amount_usd", "REAL"), ("domain", "TEXT"), ("published_at", "TEXT")):
    try:
        store.execute(f"ALTER TABLE leads ADD COLUMN {_col} {_decl}")
    except Exception:
        pass

TC_FEED = "https://techcrunch.com/tag/funding/feed/"
DEFAULT_FEEDS = [
    TC_FEED,
    "https://www.finsmes.com/feed",
    "https://www.eu-startups.com/feed/",
    "https://tech.eu/feed/",
    "https://news.crunchbase.com/feed/",
    "https://venturebeat.com/category/venture/feed/",
]
HN_SEARCH = "http://hn.algolia.com/api/v1/search_by_date"

AMOUNT_RE = re.compile(r"\$\s?\d[\d.,]*\s?(?:[KMB]|million|billion|thousand)?", re.I)
ROUND_RE = re.compile(r"\b(pre-seed|seed|series\s+[A-J]|angel|growth|bridge|mezzanine|grant|debt)\b", re.I)
RAISE_RE = re.compile(
    r"^(.*?)\s+(?:raises|raised|raise|lands|landed|secures|secured|nabs|nabbed|closes|closed|"
    r"bags|bagged|grabs|scores|scored|gets|snags|snares|pulls\s+in|brings\s+in|pockets)\b",
    re.I,
)
PREFIX_RE = re.compile(r"^(exclusive|brief|report|breaking|update|scoop|just\s+in)\s*[:\-–—]\s*", re.I)

SECTOR_KEYWORDS = {
    "fintech": ("fintech", "payments", "banking", "lending", "neobank", "insurtech", "wealth"),
    "ai": ("ai ", "a.i.", "artificial intelligence", "machine learning", "ml ", "llm", "genai", "gen ai"),
    "healthtech": ("health", "medtech", "biotech", "pharma", "clinical", "diagnostic", "telehealth"),
    "climate": ("climate", "carbon", "clean energy", "cleantech", "solar", "battery", "ev ", "sustainab"),
    "saas": ("saas", "b2b software", "platform", "workflow", "productivity"),
    "crypto": ("crypto", "web3", "blockchain", "defi", "token", "nft"),
    "security": ("security", "cyber", "infosec", "privacy", "fraud"),
    "devtools": ("developer", "devtool", "devops", "api ", "infrastructure", "database"),
    "robotics": ("robot", "drone", "autonomous", "manufacturing"),
    "ecommerce": ("ecommerce", "e-commerce", "retail", "marketplace", "d2c"),
    "space": ("space", "satellite", "aerospace", "rocket"),
    "edtech": ("edtech", "education", "learning", "tutoring"),
}

_MULT = {"k": 1e3, "thousand": 1e3, "m": 1e6, "million": 1e6, "b": 1e9, "billion": 1e9}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _norm_company(company: str) -> str:
    """Normalize a company name for dedup (lowercase, strip suffixes/punct)."""
    c = company.lower().strip()
    c = re.sub(r"\b(inc|ltd|llc|corp|co|gmbh|ag|sa|plc|limited|incorporated)\b\.?", "", c)
    c = re.sub(r"[^a-z0-9]+", "", c)
    return c


def _amount_to_usd(amount: str) -> float | None:
    """Parse a raw amount string like '$12.5M' / '$1.2 billion' into a USD float."""
    if not amount:
        return None
    m = re.search(r"\$?\s?([\d.,]+)\s?([kmb]|thousand|million|billion)?", amount, re.I)
    if not m:
        return None
    try:
        num = float(m.group(1).replace(",", ""))
    except ValueError:
        return None
    suffix = (m.group(2) or "").lower()
    return num * _MULT.get(suffix, 1.0)


def _detect_sector(text: str) -> str:
    t = f" {text.lower()} "
    for sector, kws in SECTOR_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return sector
    return ""


def parse_headline(title: str) -> dict:
    """Extract {company, amount, amount_usd, round, sector} from a funding headline (best-effort)."""
    title = (title or "").strip()
    clean = PREFIX_RE.sub("", title)
    amt_m = AMOUNT_RE.search(clean)
    amount = amt_m.group(0).strip() if amt_m else ""
    rnd = ROUND_RE.search(clean)
    m = RAISE_RE.match(clean)
    if m:
        company = m.group(1)
    else:
        company = re.split(r"\s+(?:raises|raised|to\s+raise|in\s+funding|valuation)\b", clean, 1)[0]
    company = company.strip(" ,–—-‘’“”\"'")
    company = re.sub(r"^[\w.]+[-–]backed\s+", "", company)
    company = re.sub(r"\s+", " ", company)
    return {
        "company": company,
        "amount": amount,
        "amount_usd": _amount_to_usd(amount),
        "round": rnd.group(0).title() if rnd else "",
        "sector": _detect_sector(clean),
    }


def _store_lead(company, amount, rnd, sector, source, url, headline,
                amount_usd=None, domain="", published_at="") -> bool:
    if not company:
        return False
    if amount_usd is None:
        amount_usd = _amount_to_usd(amount)
    try:
        store.execute(
            "INSERT OR IGNORE INTO leads(company,amount,amount_usd,round,sector,source,url,headline,"
            "domain,published_at,found_at) VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (company, amount, amount_usd, rnd, sector, source, url, headline, domain,
             published_at, _now()),
        )
        return True
    except Exception:
        return False


def _within(published_at: str, since_days: int) -> bool:
    if not since_days or not published_at:
        return True
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return dt >= datetime.now(timezone.utc) - timedelta(days=since_days)
    except Exception:
        return True


@mcp.tool
def scan(source: str = "all", limit: int = 25, since_days: int = 0, query: str = "") -> dict:
    """Fetch recent funded companies and store them (deduped). Returns new leads.

    source ∈ {all, techcrunch, rss, sec, hackernews}. since_days>0 filters by publish freshness
    (where available). query (optional substring, case-insensitive) filters headlines."""
    limit = _clamp_limit(limit)
    found: list[dict] = []
    if source in ("all", "techcrunch"):
        found += _scan_feeds([TC_FEED], limit, "techcrunch")
    if source in ("all", "rss"):
        found += _scan_feeds(DEFAULT_FEEDS, limit, "rss")
    if source in ("all", "sec"):
        found += _scan_sec(limit)
    if source in ("all", "hackernews"):
        found += _scan_hackernews(limit, since_days)

    if query:
        q = query.lower()
        found = [f for f in found if q in (f.get("headline", "") + f.get("company", "")).lower()]
    if since_days:
        found = [f for f in found if _within(f.get("published_at", ""), since_days)]

    # cross-source dedup by normalized company name (keep first / richest)
    seen: dict[str, dict] = {}
    for f in found:
        key = _norm_company(f.get("company", ""))
        if not key:
            continue
        if key not in seen or (f.get("amount") and not seen[key].get("amount")):
            seen[key] = f
    deduped = list(seen.values())

    new = 0
    for f in deduped:
        if _store_lead(f["company"], f.get("amount", ""), f.get("round", ""), f.get("sector", ""),
                       f["source"], f.get("url", ""), f.get("headline", ""),
                       f.get("amount_usd"), f.get("domain", ""), f.get("published_at", "")):
            new += 1
    return {"fetched": len(found), "unique": len(deduped), "stored_or_seen": new,
            "leads": deduped[:limit]}


def _parse_feed_date(entry) -> str:
    try:
        import time
        st = entry.get("published_parsed") or entry.get("updated_parsed")
        if st:
            return datetime.fromtimestamp(time.mktime(st), tz=timezone.utc).isoformat()
    except Exception:
        pass
    return ""


def _scan_feeds(feeds: list[str], limit: int, source: str) -> list[dict]:
    import feedparser
    out: list[dict] = []
    for feed in feeds:
        if not _safe_http_url(feed):
            continue
        try:
            d = feedparser.parse(feed)
            for e in d.entries[:limit]:
                p = parse_headline(e.get("title", ""))
                if not p["sector"]:
                    p["sector"] = _detect_sector(e.get("summary", ""))
                out.append({**p, "source": source, "url": e.get("link", ""),
                            "headline": e.get("title", ""),
                            "published_at": _parse_feed_date(e)})
        except Exception:
            continue
    return out


@mcp.tool
def scan_rss(feeds: list[str] | None = None, limit: int = 25) -> dict:
    """Pull funding leads from any list of RSS feeds (defaults to the curated free funding set).
    Stores + returns new leads. Only http(s) feed URLs are accepted."""
    limit = _clamp_limit(limit)
    if feeds is not None and not isinstance(feeds, list):
        return {"fetched": 0, "stored_or_seen": 0, "feeds": [], "leads": [],
                "error": "feeds must be a list of URLs"}
    feeds = feeds or DEFAULT_FEEDS
    feeds = [f for f in feeds if isinstance(f, str) and _safe_http_url(f)]
    found = _scan_feeds(feeds, limit, "rss")
    new = sum(
        _store_lead(f["company"], f.get("amount", ""), f.get("round", ""), f.get("sector", ""),
                    f["source"], f.get("url", ""), f.get("headline", ""),
                    f.get("amount_usd"), "", f.get("published_at", ""))
        for f in found
    )
    return {"fetched": len(found), "stored_or_seen": new, "feeds": feeds, "leads": found[:limit]}


def _scan_hackernews(limit: int, since_days: int = 7) -> list[dict]:
    out: list[dict] = []
    params = {
        "query": "raises funding round",
        "tags": "story",
        "hitsPerPage": min(limit * 3, 100),
    }
    if since_days:
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
        params["numericFilters"] = f"created_at_i>{cutoff}"
    try:
        data = http.get_json(HN_SEARCH, params=params, timeout=20, cache_ttl=0.0)
        if not isinstance(data, dict):
            data = {}
        for hit in data.get("hits", []):
            title = hit.get("title") or ""
            if not RAISE_RE.search(title) and "fund" not in title.lower():
                continue
            p = parse_headline(title)
            if not p["company"]:
                continue
            out.append({**p, "source": "hackernews",
                        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                        "headline": title, "published_at": hit.get("created_at", "")})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


@mcp.tool
def scan_hackernews(limit: int = 25, since_days: int = 7) -> dict:
    """Scan the Hacker News Algolia API (free, no key) for recent funding stories. Stores + returns."""
    limit = _clamp_limit(limit)
    found = _scan_hackernews(limit, since_days)
    new = sum(
        _store_lead(f["company"], f.get("amount", ""), f.get("round", ""), f.get("sector", ""),
                    f["source"], f.get("url", ""), f.get("headline", ""),
                    f.get("amount_usd"), "", f.get("published_at", ""))
        for f in found
    )
    return {"fetched": len(found), "stored_or_seen": new, "leads": found[:limit]}


def _scan_sec(limit: int) -> list[dict]:
    """SEC EDGAR full-text search for recent Form D (exempt offering) filings."""
    ua = get_env("SEC_USER_AGENT") or "mcp-suite funding-radar contact@example.com"
    out: list[dict] = []
    enddt = datetime.now(timezone.utc).date()
    startdt = enddt - timedelta(days=14)
    try:
        data = http.get_json(
            "https://efts.sec.gov/LATEST/search-index",
            params={"q": "", "forms": "D",
                    "startdt": startdt.isoformat(), "enddt": enddt.isoformat()},
            headers={"User-Agent": ua, "Accept-Encoding": "gzip, deflate"},
            timeout=20, cache_ttl=0.0,
        )
        if not isinstance(data, dict):
            data = {}
        for hit in (data.get("hits", {}).get("hits", []) or [])[:limit]:
            src = hit.get("_source", {})
            names = src.get("display_names", []) or []
            company = re.sub(r"\s*\(CIK.*\)$", "", names[0]) if names else ""
            url = ""
            _id = hit.get("_id", "")
            if _id and ":" in _id:
                accno, fname = _id.split(":", 1)
                cik = (src.get("cik") or "").lstrip("0") if isinstance(src.get("cik"), str) else ""
                acc_nodash = accno.replace("-", "")
                if cik:
                    url = f"https://www.sec.gov/Archives/edgar/data/{cik}/{acc_nodash}/{fname}"
            out.append({"company": company, "amount": "", "amount_usd": None, "round": "Form D",
                        "sector": "", "source": "sec", "url": url, "headline": company,
                        "published_at": (src.get("file_date") or "")})
    except Exception:
        pass
    return out


@mcp.tool
def list_leads(limit: int = 50, source: str = "") -> list[dict]:
    """Browse stored leads (most recent first)."""
    limit = _clamp_limit(limit, 50)
    if source:
        return store.query("SELECT company,amount,amount_usd,round,sector,source,url,domain,"
                           "published_at,found_at FROM leads WHERE source=? ORDER BY found_at DESC "
                           "LIMIT ?", (source, limit))
    return store.query("SELECT company,amount,amount_usd,round,sector,source,url,domain,"
                       "published_at,found_at FROM leads ORDER BY found_at DESC LIMIT ?", (limit,))


@mcp.tool
def filter_leads(sector: str = "", round: str = "", min_amount_usd: float = 0.0,
                 source: str = "", since_days: int = 0, limit: int = 50) -> list[dict]:
    """Rich filter over stored leads by sector, round, minimum USD amount, source, and freshness."""
    limit = _clamp_limit(limit, 50)
    sql = ("SELECT company,amount,amount_usd,round,sector,source,url,domain,published_at,found_at "
           "FROM leads WHERE 1=1")
    params: list = []
    if sector:
        sql += " AND lower(sector)=?"
        params.append(sector.lower())
    if round:
        sql += " AND lower(round) LIKE ?"
        params.append(f"%{round.lower()}%")
    if min_amount_usd:
        sql += " AND amount_usd IS NOT NULL AND amount_usd>=?"
        params.append(min_amount_usd)
    if source:
        sql += " AND source=?"
        params.append(source)
    if since_days:
        cutoff = (datetime.now(timezone.utc) - timedelta(days=since_days)).isoformat()
        sql += " AND found_at>=?"
        params.append(cutoff)
    sql += " ORDER BY found_at DESC LIMIT ?"
    params.append(limit)
    return store.query(sql, params)


def _slugify(company: str) -> str:
    s = company.lower().strip()
    s = re.sub(r"\b(inc|ltd|llc|corp|co|gmbh|ag|the)\b\.?", "", s)
    s = re.sub(r"[^a-z0-9]+", "", s)
    return s


@mcp.tool
def guess_domain(company: str, check_dns: bool = False) -> dict:
    """Heuristically guess a company's domain from its name. With check_dns=True, probe candidate
    domains for an A/MX record (via dnspython) and return the first that resolves."""
    slug = _slugify(company)
    if not slug:
        return {"company": company, "candidates": [], "best": ""}
    tlds = ["com", "io", "ai", "co", "app", "dev", "tech"]
    candidates = [f"{slug}.{t}" for t in tlds]
    best = candidates[0]
    resolved = ""
    if check_dns:
        try:
            import dns.resolver
            for cand in candidates:
                for rtype in ("A", "MX"):
                    try:
                        dns.resolver.resolve(cand, rtype)
                        resolved = cand
                        break
                    except Exception:
                        continue
                if resolved:
                    break
        except Exception:
            pass
        best = resolved or best
    return {"company": company, "candidates": candidates, "best": best, "resolved": resolved}


@mcp.tool
def as_targets(limit: int = 25) -> list[dict]:
    """Return recent leads as targets to pass to campaign.filter_uncontacted. Each item carries
    {company, domain, round, amount, amount_usd, sector} so downstream pitch tools can prefill
    placeholders (domain may be blank — use enrich_targets to fill guesses)."""
    limit = _clamp_limit(limit)
    rows = store.query("SELECT company, domain, round, amount, amount_usd, sector "
                       "FROM leads ORDER BY found_at DESC LIMIT ?", (limit,))
    return [{"company": r["company"], "domain": r.get("domain") or "",
             "round": r.get("round") or "", "amount": r.get("amount") or "",
             "amount_usd": r.get("amount_usd"), "sector": r.get("sector") or ""}
            for r in rows]


@mcp.tool
def enrich_targets(limit: int = 25, check_dns: bool = False) -> list[dict]:
    """Like as_targets but fills a best-effort guessed domain for each lead (optionally DNS-verified)."""
    limit = _clamp_limit(limit)
    rows = store.query("SELECT company, domain FROM leads ORDER BY found_at DESC LIMIT ?", (limit,))
    out = []
    for r in rows:
        domain = r.get("domain") or ""
        if not domain:
            domain = guess_domain(r["company"], check_dns=check_dns)["best"]
        out.append({"company": r["company"], "domain": domain})
    return out


@mcp.tool
def dedupe() -> dict:
    """Collapse near-duplicate company rows (same normalized name) keeping the richest record."""
    rows = store.query("SELECT * FROM leads ORDER BY (amount_usd IS NULL), found_at DESC")
    seen: set[str] = set()
    removed = 0
    for r in rows:
        key = _norm_company(r["company"]) + "|" + (r["source"] or "")
        bare = _norm_company(r["company"])
        if bare in seen:
            store.execute("DELETE FROM leads WHERE id=?", (r["id"],))
            removed += 1
        else:
            seen.add(bare)
    return {"removed": removed, "remaining": len(seen)}


@mcp.tool
def sources() -> dict:
    """Introspection: list available scan sources and the configured default RSS feeds."""
    return {"sources": ["all", "techcrunch", "rss", "sec", "hackernews"],
            "default_feeds": DEFAULT_FEEDS,
            "sec_user_agent_set": bool(get_env("SEC_USER_AGENT"))}


@mcp.tool
def stats() -> dict:
    """Summary stats: total leads, breakdown by source/sector/round, and freshness counts."""
    total = store.query_one("SELECT COUNT(*) n FROM leads")["n"]
    by_source = {r["source"]: r["n"] for r in
                 store.query("SELECT source, COUNT(*) n FROM leads GROUP BY source")}
    by_sector = {(r["sector"] or "unknown"): r["n"] for r in
                 store.query("SELECT sector, COUNT(*) n FROM leads GROUP BY sector ORDER BY n DESC")}
    by_round = {(r["round"] or "unknown"): r["n"] for r in
                store.query("SELECT round, COUNT(*) n FROM leads GROUP BY round ORDER BY n DESC")}
    week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    fresh = store.query_one("SELECT COUNT(*) n FROM leads WHERE found_at>=?", (week_ago,))["n"]
    return {"total": total, "by_source": by_source, "by_sector": by_sector,
            "by_round": by_round, "found_last_7d": fresh}


if __name__ == "__main__":
    mcp.run()
