"""bookmark-vault — save & search learning resources / docs links with tags, notes, a readability
text archive (bs4), dedupe, browser-bookmark import, tag hierarchy, and full-text search of page
content (SQLite + FTS5). Auto-fetches the page title when possible (httpx + BeautifulSoup)."""
from __future__ import annotations

import csv
import io
import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit, urlunsplit

from mcp_base import (BaseStore, data_dir, db_path, err, fetch as _hfetch, make_server, not_found,
                      scrape, semantic)

mcp = make_server(
    "bookmark-vault",
    instructions=("Save/search links: add_bookmark, search (incl. archived page text), archive, "
                  "find_duplicates/dedupe, import_html (browser export), tag/tag_tree, export_csv/html."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS bookmarks(
  id INTEGER PRIMARY KEY, url TEXT UNIQUE, title TEXT DEFAULT '', tags TEXT DEFAULT '',
  notes TEXT DEFAULT '', created_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS bm_fts USING fts5(title, url, tags, notes, content='bookmarks', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS bm_ai AFTER INSERT ON bookmarks BEGIN
  INSERT INTO bm_fts(rowid,title,url,tags,notes) VALUES(new.id,new.title,new.url,new.tags,new.notes); END;
CREATE TRIGGER IF NOT EXISTS bm_ad AFTER DELETE ON bookmarks BEGIN
  INSERT INTO bm_fts(bm_fts,rowid,title,url,tags,notes) VALUES('delete',old.id,old.title,old.url,old.tags,old.notes); END;
CREATE VIRTUAL TABLE IF NOT EXISTS bm_content_fts USING fts5(content, tokenize='porter');
"""
store = BaseStore(db_path("bookmark-vault"), schema=SCHEMA)
store.migrate(semantic.vec_table_sql("bm_vec"))


def _reindex(bid: int) -> None:
    """Sync a bookmark's semantic vector from its title/tags/notes/archived text."""
    b = store.query_one("SELECT title,tags,notes,archive_text FROM bookmarks WHERE id=?", (bid,))
    if b:
        txt = " ".join(str(b.get(k) or "") for k in ("title", "tags", "notes", "archive_text"))
        semantic.index_row(store, "bm_vec", bid, txt)


def _now():
    return datetime.now(timezone.utc).isoformat()


def _bookmark_ids(limit: int = 10) -> list[int]:
    """Recent bookmark ids, to suggest valid targets in not-found errors."""
    return [r["id"] for r in store.query(
        "SELECT id FROM bookmarks ORDER BY created_at DESC LIMIT ?", (limit,))]


def _no_bookmark(bookmark_id) -> dict:
    return not_found("bookmark", bookmark_id, available=_bookmark_ids(),
                     hint="use list_bookmarks() to see valid bookmark ids")


_MAX_LIMIT = 1000  # upper clamp for list/search queries to bound result size


def _clamp_limit(limit: int, default: int = 50) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, _MAX_LIMIT)


def _ensure_cols(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_cols("bookmarks", {"archive_text": "TEXT DEFAULT ''", "archived_at": "TEXT"})

_MAX_IMPORT_BYTES = 50_000_000  # cap import-file reads (~50MB) to bound memory


def _read_capped(p: Path) -> str:
    """Read a text file but refuse anything larger than _MAX_IMPORT_BYTES."""
    if p.stat().st_size > _MAX_IMPORT_BYTES:
        raise ValueError("file too large")
    return p.read_text(encoding="utf-8", errors="ignore")


def _norm_url(url: str) -> str:
    """Normalize a URL for dedupe: lowercase host, strip scheme, fragment, tracking params, trailing /."""
    try:
        s = urlsplit(url.strip())
    except ValueError:
        return url.strip().lower()
    host = (s.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    keep = []
    for kv in s.query.split("&"):
        k = kv.split("=")[0].lower()
        if k and not k.startswith(("utm_", "fbclid", "gclid", "ref", "mc_")):
            keep.append(kv)
    path = s.path.rstrip("/")
    return urlunsplit(("", host, path, "&".join(sorted(keep)), "")).lstrip("/")


_MAX_FETCH_BYTES = 5_000_000  # cap downloaded page size (~5MB) to bound memory


def _fetch(url: str) -> tuple[str, str]:
    """Return (title, main_text) for a URL via the hardened shared fetch (SSRF-guarded, retry, encoding
    detection). Empty strings on failure. Offline-safe."""
    r = _hfetch.fetch(url, timeout=12, max_bytes=_MAX_FETCH_BYTES)
    if not r.get("ok") or r.get("not_modified"):
        return "", ""
    html = r.get("html", "")
    return scrape.title(html)[:300], scrape.main_content(html, r.get("final_url", url), "text")


def _fetch_title(url: str) -> str:
    return _fetch(url)[0]


def _index_content(bid: int, text: str) -> None:
    store.execute("DELETE FROM bm_content_fts WHERE rowid=?", (bid,))
    if text:
        store.execute("INSERT INTO bm_content_fts(rowid,content) VALUES(?,?)", (bid, text))


# ---------------- Core (preserved + deepened) ----------------
@mcp.tool
def add_bookmark(url: str, title: str = "", tags: str = "", notes: str = "", fetch_title: bool = True,
                 archive: bool = False) -> dict:
    """Save a bookmark. If no title and fetch_title=True, fetches the page <title>. If archive=True,
    also stores a readability text snapshot for full-text content search."""
    url = (url or "").strip()
    if not url:
        return err("url is required", hint="pass the URL to bookmark")
    archive_text = ""
    if archive:
        title2, archive_text = _fetch(url)
        if not title:
            title = title2
    elif not title and fetch_title:
        title = _fetch_title(url)
    bid = store.execute(
        "INSERT INTO bookmarks(url,title,tags,notes,archive_text,archived_at,created_at) VALUES(?,?,?,?,?,?,?) "
        "ON CONFLICT(url) DO UPDATE SET title=COALESCE(NULLIF(excluded.title,''),bookmarks.title),"
        "tags=excluded.tags, notes=excluded.notes,"
        "archive_text=COALESCE(NULLIF(excluded.archive_text,''),bookmarks.archive_text),"
        "archived_at=COALESCE(NULLIF(excluded.archived_at,''),bookmarks.archived_at)",
        (url, title, tags, notes, archive_text, _now() if archive_text else "", _now()))
    row = store.query_one("SELECT id FROM bookmarks WHERE url=?", (url,))
    bid = row["id"] if row else bid
    if archive_text:
        _index_content(bid, archive_text)
    _reindex(bid)
    return {"id": bid, "url": url, "title": title, "archived": bool(archive_text)}


@mcp.tool
def search(query: str, limit: int = 20, content: bool = False) -> list[dict]:
    """Full-text search bookmarks (title/url/tags/notes). If content=True, also searches archived page text."""
    limit = _clamp_limit(limit, 20)
    results: dict[int, dict] = {}
    try:
        for r in store.query("SELECT b.id,b.title,b.url,b.tags FROM bm_fts f JOIN bookmarks b ON b.id=f.rowid "
                             "WHERE bm_fts MATCH ? ORDER BY rank LIMIT ?", (query, limit)):
            results[r["id"]] = r
    except Exception:
        like = f"%{query}%"
        for r in store.query("SELECT id,title,url,tags FROM bookmarks WHERE url LIKE ? OR title LIKE ? OR tags LIKE ? LIMIT ?",
                             (like, like, like, limit)):
            results[r["id"]] = r
    if content:
        try:
            for r in store.query(
                    "SELECT b.id,b.title,b.url,b.tags FROM bm_content_fts f JOIN bookmarks b ON b.id=f.rowid "
                    "WHERE bm_content_fts MATCH ? ORDER BY rank LIMIT ?", (query, limit)):
                results.setdefault(r["id"], r)
        except Exception:
            pass
    # semantic layer: fuse vector hits with the keyword hits (reciprocal-rank fusion)
    vec = [rid for rid, _ in semantic.vector_hits(store, "bm_vec", query, limit=50)]
    if vec:
        fused = semantic.rrf(list(results.keys()), vec, limit)
        need = [i for i in fused if i not in results]
        if need:
            ph = ",".join("?" * len(need))
            for r in store.query(f"SELECT id,title,url,tags FROM bookmarks WHERE id IN ({ph})", tuple(need)):
                results[r["id"]] = r
        return [results[i] for i in fused if i in results][:limit]
    return list(results.values())[:limit]


@mcp.tool
def find_similar(bookmark_id: int = 0, url: str = "", limit: int = 8) -> dict:
    """Find bookmarks semantically similar to a given one (by meaning of title/notes/archived text)."""
    b = (store.query_one("SELECT id,title,tags,notes,archive_text FROM bookmarks WHERE id=?", (bookmark_id,))
         if bookmark_id else
         store.query_one("SELECT id,title,tags,notes,archive_text FROM bookmarks WHERE url=?", ((url or "").strip(),)))
    if not b:
        return err("bookmark not found", hint="pass a valid bookmark_id or url")
    txt = " ".join(str(b.get(k) or "") for k in ("title", "tags", "notes", "archive_text"))
    hits = [rid for rid, _ in semantic.vector_hits(store, "bm_vec", txt, limit=limit + 1) if rid != b["id"]]
    ids = hits[:max(1, limit)]
    if not ids:
        return {"ok": True, "results": [], "note": "no semantic neighbors (try archive=True / reindex_semantic)"}
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in store.query(f"SELECT id,title,url,tags FROM bookmarks WHERE id IN ({ph})", tuple(ids))}
    return {"ok": True, "results": [rows[i] for i in ids if i in rows]}


@mcp.tool
def reindex_semantic() -> dict:
    """(Re)build semantic embeddings for all bookmarks. Needs a local model (uv sync --group embed)."""
    if not semantic.available():
        return {"ok": False, "engine": "unavailable", "hint": "uv sync --group embed then call again"}
    n = 0
    for r in store.query("SELECT id FROM bookmarks"):
        _reindex(r["id"])
        n += 1
    return {"ok": True, "indexed": n}


@mcp.tool
def tag(bookmark_id: int, tags: str) -> dict:
    """Set tags on a bookmark (comma-separated). Supports hierarchy via 'parent/child'."""
    if not store.query_one("SELECT id FROM bookmarks WHERE id=?", (bookmark_id,)):
        return _no_bookmark(bookmark_id)
    store.execute("UPDATE bookmarks SET tags=? WHERE id=?", (tags, bookmark_id))
    return {"ok": True, "id": bookmark_id, "tags": tags}


@mcp.tool
def list_bookmarks(limit: int = 50, tag: str = "") -> list[dict]:
    """List recent bookmarks, optionally filtered by tag (matches hierarchy prefixes)."""
    limit = _clamp_limit(limit, 50)
    if tag:
        rows = store.query("SELECT id,title,url,tags FROM bookmarks WHERE tags LIKE ? ORDER BY created_at DESC LIMIT ?",
                           (f"%{tag}%", limit))
        return rows
    return store.query("SELECT id,title,url,tags FROM bookmarks ORDER BY created_at DESC LIMIT ?", (limit,))


@mcp.tool
def get_bookmark(bookmark_id: int) -> dict:
    """Get a single bookmark with all fields (archive text truncated)."""
    b = store.query_one("SELECT * FROM bookmarks WHERE id=?", (bookmark_id,))
    if not b:
        return _no_bookmark(bookmark_id)
    if b.get("archive_text"):
        b["archive_text"] = b["archive_text"][:2000]
    return b


@mcp.tool
def update_bookmark(bookmark_id: int, title: str = "", notes: str = "", tags: str = "") -> dict:
    """Update a bookmark's title/notes/tags (non-empty args only)."""
    if not store.query_one("SELECT id FROM bookmarks WHERE id=?", (bookmark_id,)):
        return _no_bookmark(bookmark_id)
    sets, params = [], []
    if title:
        sets.append("title=?"); params.append(title)
    if notes:
        sets.append("notes=?"); params.append(notes)
    if tags:
        sets.append("tags=?"); params.append(tags)
    if not sets:
        return err("nothing to update", hint="pass at least one of title/notes/tags")
    params.append(bookmark_id)
    store.execute(f"UPDATE bookmarks SET {','.join(sets)} WHERE id=?", params)
    return {"ok": True, "id": bookmark_id}


@mcp.tool
def delete_bookmark(bookmark_id: int) -> dict:
    """Delete a bookmark and its archived content."""
    if not store.query_one("SELECT id FROM bookmarks WHERE id=?", (bookmark_id,)):
        return _no_bookmark(bookmark_id)
    store.execute("DELETE FROM bm_content_fts WHERE rowid=?", (bookmark_id,))
    store.execute("DELETE FROM bookmarks WHERE id=?", (bookmark_id,))
    semantic.drop_row(store, "bm_vec", bookmark_id)
    return {"ok": True, "id": bookmark_id}


# ---------------- Archive ----------------
@mcp.tool
def archive(bookmark_id: int = 0, url: str = "") -> dict:
    """Fetch & store a readability text snapshot of a bookmark (by id) or a URL (saved if new), so its
    page content becomes full-text searchable. Network call; safe no-op on failure."""
    if bookmark_id:
        b = store.query_one("SELECT id,url FROM bookmarks WHERE id=?", (bookmark_id,))
        if not b:
            return _no_bookmark(bookmark_id)
        target_url, bid = b["url"], b["id"]
    elif url:
        target_url = url.strip()
        existing = store.query_one("SELECT id FROM bookmarks WHERE url=?", (target_url,))
        bid = existing["id"] if existing else store.execute(
            "INSERT INTO bookmarks(url,created_at) VALUES(?,?)", (target_url, _now()))
    else:
        return err("provide bookmark_id or url", hint="pass an existing bookmark_id, or a url to archive")
    title, text = _fetch(target_url)
    if not text:
        return {"error": "could not fetch page content", "id": bid}
    store.execute("UPDATE bookmarks SET archive_text=?, archived_at=?, "
                  "title=COALESCE(NULLIF(title,''),?) WHERE id=?", (text, _now(), title, bid))
    _index_content(bid, text)
    _reindex(bid)
    return {"ok": True, "id": bid, "chars": len(text)}


@mcp.tool
def get_archive(bookmark_id: int, max_chars: int = 8000) -> dict:
    """Return the archived page text for a bookmark."""
    b = store.query_one("SELECT url,title,archive_text,archived_at FROM bookmarks WHERE id=?", (bookmark_id,))
    if not b:
        return _no_bookmark(bookmark_id)
    return {"url": b["url"], "title": b["title"], "archived_at": b["archived_at"],
            "text": (b["archive_text"] or "")[:max_chars], "has_archive": bool(b["archive_text"])}


@mcp.tool
def refresh_titles(limit: int = 20) -> dict:
    """Backfill titles for bookmarks missing them (network; safe on failure)."""
    rows = store.query("SELECT id,url FROM bookmarks WHERE title='' OR title IS NULL LIMIT ?", (_clamp_limit(limit, 20),))
    updated = 0
    for r in rows:
        t = _fetch_title(r["url"])
        if t:
            store.execute("UPDATE bookmarks SET title=? WHERE id=?", (t, r["id"]))
            updated += 1
    return {"checked": len(rows), "updated": updated}


# ---------------- Dedupe ----------------
def find_duplicates_impl() -> list[dict]:
    groups: dict[str, list] = {}
    for r in store.query("SELECT id,url,title FROM bookmarks ORDER BY id"):
        groups.setdefault(_norm_url(r["url"]), []).append({"id": r["id"], "url": r["url"], "title": r["title"]})
    return [{"normalized": k, "bookmarks": v} for k, v in groups.items() if len(v) > 1]


@mcp.tool
def find_duplicates() -> list[dict]:
    """Find groups of bookmarks that normalize to the same URL (ignoring scheme/www/utm/trailing slash)."""
    return find_duplicates_impl()


@mcp.tool
def dedupe(apply: bool = False) -> dict:
    """Report (apply=False) or remove (apply=True) duplicates, keeping the oldest of each group."""
    dups = find_duplicates_impl()
    removable = [b["id"] for g in dups for b in g["bookmarks"][1:]]
    if apply and removable:
        for bid in removable:
            store.execute("DELETE FROM bm_content_fts WHERE rowid=?", (bid,))
            store.execute("DELETE FROM bookmarks WHERE id=?", (bid,))
    return {"duplicate_groups": len(dups), "removable": removable, "applied": apply}


# ---------------- Tag hierarchy ----------------
def _all_tags() -> dict[str, int]:
    counts: dict[str, int] = {}
    for r in store.query("SELECT tags FROM bookmarks WHERE tags!=''"):
        for t in re.split(r"[,\s]+", r["tags"]):
            t = t.strip()
            if t:
                counts[t] = counts.get(t, 0) + 1
    return counts


@mcp.tool
def tag_tree() -> dict:
    """Return tags arranged into a hierarchy from 'parent/child' tag paths, with counts."""
    counts = _all_tags()
    tree: dict = {}
    for tag, n in counts.items():
        node = tree
        for part in tag.split("/"):
            node = node.setdefault(part, {"_count": 0, "_children": {}})
            node["_count"] += n
            node = node["_children"]
    return tree


@mcp.tool
def bookmarks_by_tag(tag: str, recursive: bool = True) -> list[dict]:
    """Bookmarks under a tag. recursive=True also matches child tags ('parent' matches 'parent/child')."""
    tag = tag.strip()
    rows = store.query("SELECT id,title,url,tags FROM bookmarks WHERE tags!='' LIMIT ?", (_MAX_LIMIT,))
    out = []
    for r in rows:
        tags = [t.strip() for t in re.split(r"[,\s]+", r["tags"]) if t.strip()]
        if any(t == tag or (recursive and t.startswith(tag + "/")) for t in tags):
            out.append({"id": r["id"], "title": r["title"], "url": r["url"], "tags": r["tags"]})
    return out


# ---------------- Browser import / export ----------------
@mcp.tool
def import_html(path: str, tags: str = "imported") -> dict:
    """Import a Netscape-format browser bookmarks export (Chrome/Firefox/Safari 'Export Bookmarks')."""
    p = Path(path)
    if not p.is_file():
        return not_found("file", path, hint="pass a path to an exported bookmarks HTML file")
    try:
        from bs4 import BeautifulSoup
    except Exception:
        return {"error": "beautifulsoup4 not available"}
    try:
        raw = _read_capped(p)
    except ValueError:
        return {"error": "file too large"}
    soup = BeautifulSoup(raw, "html.parser")
    imported = skipped = 0
    for a in soup.find_all("a"):
        href = a.get("href", "").strip()
        if not href.startswith(("http://", "https://")):
            skipped += 1
            continue
        title = a.get_text(strip=True)[:300]
        store.execute("INSERT INTO bookmarks(url,title,tags,created_at) VALUES(?,?,?,?) "
                      "ON CONFLICT(url) DO NOTHING", (href, title, tags, _now()))
        imported += 1
    return {"ok": True, "imported": imported, "skipped": skipped}


@mcp.tool
def import_json(path: str, tags: str = "imported") -> dict:
    """Import a Chrome 'Bookmarks' JSON file (the one in your Chrome profile dir)."""
    p = Path(path)
    if not p.is_file():
        return not_found("file", path, hint="pass a path to a Chrome 'Bookmarks' JSON file")
    try:
        data = json.loads(_read_capped(p))
    except ValueError as e:
        return {"error": f"invalid json: {e}"}
    except Exception as e:
        return {"error": f"invalid json: {e}"}
    imported = [0]

    def walk(node):
        if isinstance(node, dict):
            if node.get("type") == "url" and node.get("url", "").startswith(("http://", "https://")):
                store.execute("INSERT INTO bookmarks(url,title,tags,created_at) VALUES(?,?,?,?) "
                              "ON CONFLICT(url) DO NOTHING",
                              (node["url"], (node.get("name") or "")[:300], tags, _now()))
                imported[0] += 1
            for v in node.values():
                walk(v)
        elif isinstance(node, list):
            for v in node:
                walk(v)

    walk(data.get("roots", data))
    return {"ok": True, "imported": imported[0]}


@mcp.tool
def export_csv(path: str = "") -> dict:
    """Export bookmarks to CSV (returns text; also writes to file if path given or default exports dir)."""
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "url", "title", "tags", "notes", "created_at"])
    for r in store.query("SELECT id,url,title,tags,notes,created_at FROM bookmarks ORDER BY created_at"):
        w.writerow([r["id"], r["url"], r["title"], r["tags"], r["notes"], r["created_at"]])
    text = buf.getvalue()
    target = Path(path) if path else (data_dir("bookmark-vault") / "exports" / "bookmarks.csv")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(text, encoding="utf-8")
    return {"ok": True, "path": str(target), "csv": text}


@mcp.tool
def export_html(path: str = "") -> dict:
    """Export bookmarks in Netscape format (re-importable into any browser)."""
    rows = store.query("SELECT url,title,tags FROM bookmarks ORDER BY created_at")
    lines = ["<!DOCTYPE NETSCAPE-Bookmark-file-1>", "<TITLE>Bookmarks</TITLE>", "<H1>Bookmarks</H1>", "<DL><p>"]
    for r in rows:
        t = (r["title"] or r["url"]).replace("<", "&lt;").replace(">", "&gt;")
        tagattr = f' TAGS="{r["tags"]}"' if r["tags"] else ""
        lines.append(f'    <DT><A HREF="{r["url"]}"{tagattr}>{t}</A>')
    lines.append("</DL><p>")
    html = "\n".join(lines) + "\n"
    target = Path(path) if path else (data_dir("bookmark-vault") / "exports" / "bookmarks.html")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(html, encoding="utf-8")
    return {"ok": True, "path": str(target)}


@mcp.tool
def domains(limit: int = 30) -> list[dict]:
    """Bookmark counts grouped by host, descending — see where your reading concentrates."""
    counts: dict[str, int] = {}
    for r in store.query("SELECT url FROM bookmarks"):
        try:
            host = (urlsplit(r["url"]).hostname or "").lower()
        except ValueError:
            host = ""
        if host.startswith("www."):
            host = host[4:]
        host = host or "(unknown)"
        counts[host] = counts.get(host, 0) + 1
    return [{"domain": h, "count": c} for h, c in sorted(counts.items(), key=lambda x: -x[1])][:_clamp_limit(limit, 30)]


@mcp.tool
def untagged(limit: int = 50) -> list[dict]:
    """Bookmarks that have no tags yet (candidates for organizing)."""
    return store.query("SELECT id,title,url FROM bookmarks WHERE tags='' OR tags IS NULL "
                       "ORDER BY created_at DESC LIMIT ?", (_clamp_limit(limit, 50),))


@mcp.tool
def recent(days: int = 7, limit: int = 50) -> list[dict]:
    """Bookmarks added within the last N days, most recent first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    return store.query("SELECT id,title,url,tags FROM bookmarks WHERE created_at>=? "
                       "ORDER BY created_at DESC LIMIT ?", (cutoff, _clamp_limit(limit, 50)))


@mcp.tool
def random_bookmark(tag: str = "") -> dict:
    """Surface a random saved bookmark (optionally within a tag) for rediscovery."""
    if tag:
        rows = bookmarks_by_tag(tag)
        if not rows:
            return not_found("bookmarks for tag", tag, available=sorted(_all_tags()),
                             hint="use list_bookmarks() or tag_tree() to see tags in use")
        import random
        return random.choice(rows)
    row = store.query_one("SELECT id,title,url,tags FROM bookmarks ORDER BY RANDOM() LIMIT 1")
    return row or {"error": "no bookmarks"}


@mcp.tool
def stats() -> dict:
    """Counts: bookmarks, archived, tags, duplicate groups."""
    one = lambda q: store.query_one(q)["n"]
    return {"bookmarks": one("SELECT COUNT(*) AS n FROM bookmarks"),
            "archived": one("SELECT COUNT(*) AS n FROM bookmarks WHERE archive_text!=''"),
            "distinct_tags": len(_all_tags()),
            "duplicate_groups": len(find_duplicates_impl())}


if __name__ == "__main__":
    mcp.run()
