"""news-radar — FREE company news / outreach-signal scanner. Surfaces timely triggers (funding,
hiring, product launch, exec change, partnership) for personalized outreach using only free,
key-less sources: Google News RSS and the Hacker News Algolia API. Stores signals in SQLite.

Pairs with funding-radar (who just raised) by answering "what is going on at this company right now"
so you can open with a relevant hook. No API keys required."""
from __future__ import annotations

import re
import time
from datetime import datetime, timedelta, timezone
from urllib.parse import quote_plus

from mcp_base import BaseStore, db_path, http, make_server

MAX_LIMIT = 100


def _clamp_limit(limit: int, default: int = 20) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, MAX_LIMIT)

mcp = make_server(
    "news-radar",
    instructions=("Find timely outreach signals for a company (free, no key). scan_company(name) "
                  "pulls Google News + Hacker News; classify hiring/funding/launch/exec triggers. "
                  "list_signals()/top_signals() browse them."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS signals(
  id INTEGER PRIMARY KEY, company TEXT, title TEXT, signal TEXT, source TEXT, url TEXT,
  published_at TEXT, found_at TEXT,
  UNIQUE(company, url)
);
"""
store = BaseStore(db_path("news-radar"), schema=SCHEMA)

GNEWS = "https://news.google.com/rss/search"
HN_SEARCH = "http://hn.algolia.com/api/v1/search_by_date"

SIGNAL_KEYWORDS = {
    "funding": ("raises", "raised", "funding", "series ", "seed", "investment", "valuation"),
    "hiring": ("hiring", "hires", "appoints", "joins as", "names", "recruits", "expands team"),
    "exec_change": ("ceo", "cto", "cfo", "coo", "steps down", "resigns", "promoted", "new chief"),
    "product_launch": ("launches", "launch", "unveils", "introduces", "releases", "rolls out",
                       "announces"),
    "partnership": ("partners", "partnership", "teams up", "collaborat", "integrat", "acquires",
                    "acquisition", "merger"),
    "growth": ("expands", "opens office", "milestone", "reaches", "record", "grows"),
    "layoffs": ("layoffs", "lays off", "cuts jobs", "restructur"),
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _classify(title: str) -> str:
    t = f" {title.lower()} "
    for signal, kws in SIGNAL_KEYWORDS.items():
        if any(kw in t for kw in kws):
            return signal
    return "news"


def _store_signal(company, title, signal, source, url, published_at) -> bool:
    if not title or not url:
        return False
    try:
        store.execute(
            "INSERT OR IGNORE INTO signals(company,title,signal,source,url,published_at,found_at) "
            "VALUES(?,?,?,?,?,?,?)",
            (company, title, signal, source, url, published_at, _now()),
        )
        return True
    except Exception:
        return False


def _gnews(company: str, limit: int, since_days: int) -> list[dict]:
    import feedparser
    out: list[dict] = []
    when = f" when:{since_days}d" if since_days else ""
    url = f"{GNEWS}?q={quote_plus(company + when)}&hl=en-US&gl=US&ceid=US:en"
    try:
        d = feedparser.parse(url)
        for e in d.entries[:limit]:
            title = e.get("title", "")
            pub = ""
            st = e.get("published_parsed")
            if st:
                pub = datetime.fromtimestamp(time.mktime(st), tz=timezone.utc).isoformat()
            out.append({"company": company, "title": title, "signal": _classify(title),
                        "source": "google_news", "url": e.get("link", ""), "published_at": pub})
    except Exception:
        pass
    return out


def _hn(company: str, limit: int, since_days: int) -> list[dict]:
    out: list[dict] = []
    params = {"query": company, "tags": "story", "hitsPerPage": min(limit * 2, 50)}
    if since_days:
        cutoff = int((datetime.now(timezone.utc) - timedelta(days=since_days)).timestamp())
        params["numericFilters"] = f"created_at_i>{cutoff}"
    try:
        data = http.get_json(HN_SEARCH, params=params, timeout=20)
        for hit in (data or {}).get("hits", []):
            title = hit.get("title") or ""
            if company.lower() not in title.lower():
                continue
            out.append({"company": company, "title": title, "signal": _classify(title),
                        "source": "hackernews",
                        "url": hit.get("url") or f"https://news.ycombinator.com/item?id={hit.get('objectID')}",
                        "published_at": hit.get("created_at", "")})
            if len(out) >= limit:
                break
    except Exception:
        pass
    return out


@mcp.tool
def scan_company(name: str, since_days: int = 30, limit: int = 20, source: str = "all") -> dict:
    """Scan recent news/signals for a company by name. source ∈ {all, google_news, hackernews}.
    Classifies each item (funding/hiring/exec_change/product_launch/partnership/...). Stores + returns."""
    name = (name or "").strip()
    if not name:
        return {"company": "", "fetched": 0, "stored_or_seen": 0, "by_signal": {}, "signals": [],
                "error": "name is required"}
    limit = _clamp_limit(limit)
    found: list[dict] = []
    if source in ("all", "google_news"):
        found += _gnews(name, limit, since_days)
    if source in ("all", "hackernews"):
        found += _hn(name, limit, since_days)
    new = sum(_store_signal(s["company"], s["title"], s["signal"], s["source"], s["url"],
                            s["published_at"]) for s in found)
    by_signal: dict[str, int] = {}
    for s in found:
        by_signal[s["signal"]] = by_signal.get(s["signal"], 0) + 1
    return {"company": name, "fetched": len(found), "stored_or_seen": new,
            "by_signal": by_signal, "signals": found[:limit]}


@mcp.tool
def list_signals(company: str = "", signal: str = "", limit: int = 50) -> list[dict]:
    """Browse stored signals, optionally filtered by company and/or signal type (most recent first)."""
    limit = _clamp_limit(limit, 50)
    sql = "SELECT company,title,signal,source,url,published_at,found_at FROM signals WHERE 1=1"
    params: list = []
    if company:
        sql += " AND lower(company)=?"
        params.append(company.lower())
    if signal:
        sql += " AND signal=?"
        params.append(signal)
    sql += " ORDER BY found_at DESC LIMIT ?"
    params.append(limit)
    return store.query(sql, params)


@mcp.tool
def top_signals(limit: int = 20, exclude_news: bool = True) -> list[dict]:
    """Return the most recent actionable signals across all companies (excludes generic 'news' by
    default), prioritized for outreach timing."""
    limit = _clamp_limit(limit)
    sql = "SELECT company,title,signal,source,url,published_at,found_at FROM signals"
    if exclude_news:
        sql += " WHERE signal != 'news'"
    sql += " ORDER BY found_at DESC LIMIT ?"
    return store.query(sql, (limit,))


@mcp.tool
def signal_types() -> dict:
    """List the signal categories this server detects (offline; no network)."""
    return {"signals": list(SIGNAL_KEYWORDS.keys()) + ["news"],
            "sources": ["all", "google_news", "hackernews"]}


@mcp.tool
def stats() -> dict:
    """Summary: total signals stored and breakdown by type/source."""
    total = store.query_one("SELECT COUNT(*) n FROM signals")["n"]
    by_signal = {r["signal"]: r["n"] for r in
                 store.query("SELECT signal, COUNT(*) n FROM signals GROUP BY signal ORDER BY n DESC")}
    by_source = {r["source"]: r["n"] for r in
                 store.query("SELECT source, COUNT(*) n FROM signals GROUP BY source")}
    return {"total": total, "by_signal": by_signal, "by_source": by_source}


if __name__ == "__main__":
    mcp.run()
