"""rss-reader — a local RSS/Atom reader backed by SQLite. Add feeds, refresh them with feedparser,
read your unread items, mark them read, and full-text search across everything you've pulled.

Network is only needed for refresh(); everything else works offline."""
from __future__ import annotations

from datetime import datetime, timezone

from mcp_base import BaseStore, db_path, err, make_server, ok

mcp = make_server(
    "rss-reader",
    instructions=("Local RSS/Atom reader (SQLite). add_feed, list_feeds, refresh (needs network), "
                  "unread, mark_read, search, remove_feed."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS feeds(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT DEFAULT '',
  added_at TEXT, last_refreshed_at TEXT
);
CREATE TABLE IF NOT EXISTS items(
  id INTEGER PRIMARY KEY, feed_id INTEGER, guid TEXT, title TEXT DEFAULT '',
  link TEXT DEFAULT '', summary TEXT DEFAULT '', published TEXT DEFAULT '',
  fetched_at TEXT, read INTEGER DEFAULT 0,
  UNIQUE(feed_id, guid),
  FOREIGN KEY(feed_id) REFERENCES feeds(id) ON DELETE CASCADE
);
CREATE VIRTUAL TABLE IF NOT EXISTS items_fts USING fts5(title, summary, content='items', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS items_ai AFTER INSERT ON items BEGIN
  INSERT INTO items_fts(rowid,title,summary) VALUES(new.id,new.title,new.summary); END;
CREATE TRIGGER IF NOT EXISTS items_ad AFTER DELETE ON items BEGIN
  INSERT INTO items_fts(items_fts,rowid,title,summary) VALUES('delete',old.id,old.title,old.summary); END;
CREATE TRIGGER IF NOT EXISTS items_au AFTER UPDATE ON items BEGIN
  INSERT INTO items_fts(items_fts,rowid,title,summary) VALUES('delete',old.id,old.title,old.summary);
  INSERT INTO items_fts(rowid,title,summary) VALUES(new.id,new.title,new.summary); END;
"""
store = BaseStore(db_path("rss-reader"), schema=SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_MAX_LIMIT = 500  # clamp for unbounded list/search queries


def _clamp_limit(limit: int, default: int = 25) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, _MAX_LIMIT)


@mcp.tool
def add_feed(url: str, title: str = "") -> dict:
    """Subscribe to an RSS/Atom feed by URL. Optional human title (auto-filled on first refresh)."""
    url = (url or "").strip()
    if not url:
        return err("url is required")
    existing = store.query_one("SELECT id FROM feeds WHERE url=?", (url,))
    if existing:
        return ok(id=existing["id"], url=url, already=True)
    fid = store.execute("INSERT INTO feeds(url,title,added_at) VALUES(?,?,?)", (url, title.strip(), _now()))
    return ok(id=fid, url=url, title=title.strip())


@mcp.tool
def list_feeds() -> list[dict]:
    """List subscribed feeds with item + unread counts."""
    feeds = store.query("SELECT id,url,title,added_at,last_refreshed_at FROM feeds ORDER BY id")
    for f in feeds:
        c = store.query_one("SELECT COUNT(*) n, SUM(CASE WHEN read=0 THEN 1 ELSE 0 END) u "
                            "FROM items WHERE feed_id=?", (f["id"],))
        f["items"] = c["n"] or 0
        f["unread"] = c["u"] or 0
    return feeds


@mcp.tool
def refresh(feed_id: int = 0) -> dict:
    """Fetch new items for one feed (feed_id) or all feeds (feed_id=0). Requires network + feedparser.
    Returns per-feed counts of new items. Safe to call repeatedly; existing items are de-duplicated."""
    try:
        import feedparser  # lazy: only needed when actually refreshing
    except Exception:
        return err("feedparser not installed", hint="pip install feedparser")
    if feed_id:
        feeds = store.query("SELECT id,url,title FROM feeds WHERE id=?", (feed_id,))
        if not feeds:
            return err("feed not found", feed_id=feed_id)
    else:
        feeds = store.query("SELECT id,url,title FROM feeds")
    results = []
    for f in feeds:
        new_count = 0
        try:
            parsed = feedparser.parse(f["url"])
            feed_title = (getattr(parsed, "feed", {}) or {}).get("title", "") if hasattr(parsed, "feed") else ""
            if feed_title and not f["title"]:
                store.execute("UPDATE feeds SET title=? WHERE id=?", (feed_title, f["id"]))
            for e in getattr(parsed, "entries", []) or []:
                guid = e.get("id") or e.get("link") or e.get("title") or ""
                if not guid:
                    continue
                if store.query_one("SELECT id FROM items WHERE feed_id=? AND guid=?", (f["id"], guid)):
                    continue
                summary = e.get("summary", "") or ""
                published = e.get("published", "") or e.get("updated", "") or ""
                store.execute(
                    "INSERT OR IGNORE INTO items(feed_id,guid,title,link,summary,published,fetched_at,read) "
                    "VALUES(?,?,?,?,?,?,?,0)",
                    (f["id"], guid, e.get("title", "") or "", e.get("link", "") or "",
                     summary, published, _now()))
                new_count += 1
            store.execute("UPDATE feeds SET last_refreshed_at=? WHERE id=?", (_now(), f["id"]))
            results.append({"feed_id": f["id"], "url": f["url"], "new": new_count})
        except Exception as exc:
            results.append({"feed_id": f["id"], "url": f["url"], "error": str(exc)})
    return ok(feeds=results, total_new=sum(r.get("new", 0) for r in results))


@mcp.tool
def unread(limit: int = 25) -> list[dict]:
    """List unread items (newest fetched first) across all feeds."""
    return store.query(
        "SELECT i.id,i.title,i.link,i.published,i.fetched_at,f.title AS feed "
        "FROM items i JOIN feeds f ON f.id=i.feed_id WHERE i.read=0 "
        "ORDER BY i.fetched_at DESC, i.id DESC LIMIT ?", (_clamp_limit(limit),))


@mcp.tool
def mark_read(item_id: int) -> dict:
    """Mark a single item as read by id."""
    if not store.query_one("SELECT id FROM items WHERE id=?", (item_id,)):
        return err("item not found", item_id=item_id)
    store.execute("UPDATE items SET read=1 WHERE id=?", (item_id,))
    return ok(item_id=item_id, read=True)


@mcp.tool
def search(query: str, limit: int = 25) -> list[dict]:
    """Full-text search item titles + summaries across all feeds."""
    limit = _clamp_limit(limit)
    try:
        return store.query(
            "SELECT i.id,i.title,i.link,i.published,i.read,f.title AS feed "
            "FROM items_fts x JOIN items i ON i.id=x.rowid JOIN feeds f ON f.id=i.feed_id "
            "WHERE items_fts MATCH ? ORDER BY rank LIMIT ?", (query, limit))
    except Exception:
        like = f"%{query}%"
        return store.query(
            "SELECT i.id,i.title,i.link,i.published,i.read,f.title AS feed "
            "FROM items i JOIN feeds f ON f.id=i.feed_id "
            "WHERE i.title LIKE ? OR i.summary LIKE ? ORDER BY i.id DESC LIMIT ?",
            (like, like, limit))


_STOP = {"the", "a", "an", "and", "or", "to", "of", "in", "for", "on", "with", "is", "are", "be",
         "this", "that", "it", "as", "at", "by", "from", "how", "why", "what", "new", "your", "you",
         "we", "our", "i", "s", "t", "will", "can", "has", "have", "but", "not", "all", "do", "if"}


def _terms(text: str) -> set:
    import re as _re
    return {w for w in _re.findall(r"[a-z0-9]{3,}", (text or "").lower()) if w not in _STOP}


@mcp.tool
def summarize_feed(feed_id: int = 0, limit: int = 40) -> dict:
    """Digest recent items: top recurring topics (shared terms across headlines) + the items per topic.
    Offline; gives an at-a-glance 'what's this feed about lately'."""
    limit = _clamp_limit(limit, 40)
    where = "WHERE feed_id=?" if feed_id else ""
    params = ((feed_id,) if feed_id else ()) + (limit,)
    rows = store.query(f"SELECT id,title,link,summary FROM items {where} ORDER BY fetched_at DESC LIMIT ?",
                       params if feed_id else (limit,))
    freq: dict[str, int] = {}
    for r in rows:
        for w in _terms(f"{r['title']} {r['summary']}"):
            freq[w] = freq.get(w, 0) + 1
    topics = [w for w, n in sorted(freq.items(), key=lambda kv: -kv[1]) if n >= 2][:12]
    return {"items_scanned": len(rows), "top_topics": topics,
            "headlines": [{"id": r["id"], "title": r["title"], "link": r["link"]} for r in rows[:15]]}


@mcp.tool
def cluster(limit: int = 60) -> dict:
    """Group recent items into clusters by shared key terms (lightweight topic clustering, offline)."""
    limit = _clamp_limit(limit, 60)
    rows = store.query("SELECT id,title,link FROM items ORDER BY fetched_at DESC LIMIT ?", (limit,))
    items = [{"id": r["id"], "title": r["title"], "link": r["link"], "terms": _terms(r["title"])} for r in rows]
    clusters: list[dict] = []
    used = set()
    for i, a in enumerate(items):
        if a["id"] in used or not a["terms"]:
            continue
        group = [a]
        used.add(a["id"])
        for b in items[i + 1:]:
            if b["id"] in used:
                continue
            overlap = a["terms"] & b["terms"]
            if len(overlap) >= 2:
                group.append(b)
                used.add(b["id"])
        if len(group) > 1:
            common = set.intersection(*[g["terms"] for g in group]) or a["terms"]
            clusters.append({"topic": " ".join(sorted(common)[:4]),
                             "items": [{"id": g["id"], "title": g["title"], "link": g["link"]} for g in group]})
    clusters.sort(key=lambda c: -len(c["items"]))
    return {"clusters": clusters[:15], "clustered": len(used), "total": len(items)}


@mcp.tool
def export_to_notes(limit: int = 30, unread_only: bool = True) -> dict:
    """Build a markdown payload of recent items (a reading list) to hand to the notes server
    (notes.new_note(title, body)). Read-only; returns the title + body, does not write."""
    limit = _clamp_limit(limit, 30)
    where = "WHERE read=0" if unread_only else ""
    rows = store.query(f"SELECT i.title,i.link,f.title AS feed FROM items i "
                       f"LEFT JOIN feeds f ON f.id=i.feed_id {where} ORDER BY i.fetched_at DESC LIMIT ?",
                       (limit,))
    lines = [f"- [{r['title']}]({r['link']})" + (f"  _( {r['feed']} )_" if r.get("feed") else "")
             for r in rows]
    body = "# RSS reading list\n\n" + "\n".join(lines) if lines else "# RSS reading list\n\n(nothing)"
    return {"ok": True, "title": "RSS reading list", "body": body, "count": len(rows),
            "hint": "pass title+body to notes.new_note to save it"}


@mcp.tool
def remove_feed(feed_id: int) -> dict:
    """Unsubscribe from a feed and delete all its items."""
    if not store.query_one("SELECT id FROM feeds WHERE id=?", (feed_id,)):
        return err("feed not found", feed_id=feed_id)
    store.execute("DELETE FROM items WHERE feed_id=?", (feed_id,))
    store.execute("DELETE FROM feeds WHERE id=?", (feed_id,))
    return ok(feed_id=feed_id, removed=True)


if __name__ == "__main__":
    mcp.run()
