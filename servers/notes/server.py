"""notes — a local second-brain: markdown notes with [[wiki-link]] backlinks, #tags, daily notes,
templates, full-text search, a backlink graph export, and write/import to real .md files."""
from __future__ import annotations

import json
import re
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp_base import BaseStore, data_dir, db_path, err, make_server, not_found, semantic

mcp = make_server(
    "notes",
    instructions=("Markdown notes w/ [[backlinks]], #tags, daily notes, templates + FTS. new_note, "
                  "edit_note, search, get, backlinks, graph, export_note/export_all, daily_note."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS notes(
  id INTEGER PRIMARY KEY, title TEXT UNIQUE, body TEXT DEFAULT '', updated_at TEXT, created_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS notes_fts USING fts5(title, body, content='notes', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS notes_ai AFTER INSERT ON notes BEGIN
  INSERT INTO notes_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
CREATE TRIGGER IF NOT EXISTS notes_ad AFTER DELETE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body); END;
CREATE TRIGGER IF NOT EXISTS notes_au AFTER UPDATE ON notes BEGIN
  INSERT INTO notes_fts(notes_fts,rowid,title,body) VALUES('delete',old.id,old.title,old.body);
  INSERT INTO notes_fts(rowid,title,body) VALUES(new.id,new.title,new.body); END;
CREATE TABLE IF NOT EXISTS templates(name TEXT PRIMARY KEY, body TEXT, created_at TEXT);
"""
store = BaseStore(db_path("notes"), schema=SCHEMA)
LINK_RE = re.compile(r"\[\[([^\]]+)\]\]")
TAG_RE = re.compile(r"(?:^|\s)#([A-Za-z0-9][\w/-]*)")


def _now():
    return datetime.now(timezone.utc).isoformat()


def _note_titles(limit: int = 10) -> list[str]:
    """Recent note titles, to suggest valid targets in not-found errors."""
    return [r["title"] for r in store.query(
        "SELECT title FROM notes ORDER BY updated_at DESC LIMIT ?", (limit,))]


def _no_note(title) -> dict:
    return not_found("note", title, available=_note_titles(),
                     hint="use list_notes() or search() to find the exact note title")


def _ensure_cols(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_cols("notes", {"tags": "TEXT DEFAULT ''"})
store.migrate(semantic.vec_table_sql("notes_vec"))


def _reindex(nid: int, title: str, body: str) -> None:
    """Keep the semantic vector for a note in sync (no-op without an embedding model)."""
    if nid:
        semantic.index_row(store, "notes_vec", nid, f"{title}\n{body}")


def _tags(body: str, extra: str = "") -> str:
    tags = {t.lower() for t in TAG_RE.findall(body or "")}
    for t in re.split(r"[,\s]+", extra or ""):
        t = t.strip().lstrip("#").lower()
        if t:
            tags.add(t)
    return ",".join(sorted(tags))


# ---------------- Core (preserved + deepened) ----------------
def _upsert(title: str, body: str, tags_extra: str = "") -> int:
    store.execute(
        "INSERT INTO notes(title,body,tags,updated_at,created_at) VALUES(?,?,?,?,?) "
        "ON CONFLICT(title) DO UPDATE SET body=excluded.body, tags=excluded.tags, updated_at=excluded.updated_at",
        (title, body, _tags(body, tags_extra), _now(), _now()))
    row = store.query_one("SELECT id FROM notes WHERE title=?", (title,))
    nid = row["id"] if row else 0
    _reindex(nid, title, body)
    return nid


@mcp.tool
def new_note(title: str, body: str = "", tags: str = "") -> dict:
    """Create a note (unique title). Use [[Other Title]] to link notes and #tags inline. Extra tags
    can also be passed in `tags` (comma/space separated)."""
    title = (title or "").strip()
    if not title:
        return err("title is required", hint="pass a non-empty note title")
    nid = _upsert(title, body, tags)
    return {"id": nid, "title": title, "links": LINK_RE.findall(body), "tags": _tags(body, tags).split(",") if _tags(body, tags) else []}


@mcp.tool
def edit_note(title: str, body: str) -> dict:
    """Replace a note's body (re-derives tags). Creates the note if it doesn't exist."""
    existing = store.query_one("SELECT id FROM notes WHERE title=?", (title,))
    if not existing:
        nid = _upsert(title, body)
    else:
        store.execute("UPDATE notes SET body=?, tags=?, updated_at=? WHERE title=?",
                      (body, _tags(body), _now(), title))
        nid = existing["id"]
        _reindex(nid, title, body)
    return {"ok": True, "id": nid, "title": title, "links": LINK_RE.findall(body)}


@mcp.tool
def append_note(title: str, text: str) -> dict:
    """Append text to a note (creates it if missing)."""
    n = store.query_one("SELECT body FROM notes WHERE title=?", (title,))
    body = ((n["body"] + "\n") if n and n["body"] else "") + text
    nid = _upsert(title, body)
    return {"ok": True, "id": nid, "title": title}


@mcp.tool
def get(title: str) -> dict:
    """Get a note by title, with its outgoing links and tags."""
    n = store.query_one("SELECT * FROM notes WHERE title=?", (title,))
    if not n:
        return _no_note(title)
    n["links"] = LINK_RE.findall(n["body"])
    n["tag_list"] = (n.get("tags") or "").split(",") if n.get("tags") else []
    return n


def _fts_ids(query: str, n: int) -> list[int]:
    try:
        return [r["id"] for r in store.query(
            "SELECT n.id FROM notes_fts f JOIN notes n ON n.id=f.rowid "
            "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?", (query, n))]
    except Exception:
        like = f"%{query}%"
        return [r["id"] for r in store.query(
            "SELECT id FROM notes WHERE title LIKE ? OR body LIKE ? LIMIT ?", (like, like, n))]


def _hydrate(ids: list[int]) -> list[dict]:
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in store.query(
        f"SELECT id,title FROM notes WHERE id IN ({ph})", tuple(ids))}
    return [rows[i] for i in ids if i in rows]


@mcp.tool
def search(query: str, limit: int = 15) -> list[dict]:
    """Hybrid search: keyword (FTS) fused with semantic vector similarity (reciprocal-rank fusion).
    Falls back to keyword-only when no embedding model is installed."""
    q = (query or "").strip()
    if not q:
        return []
    limit = max(1, limit)
    fts = _fts_ids(q, 50)
    vec = [rid for rid, _ in semantic.vector_hits(store, "notes_vec", q, limit=50)]
    ids = semantic.rrf(fts, vec, limit) if vec else fts[:limit]
    return _hydrate(ids)


@mcp.tool
def related(title: str, limit: int = 8) -> list[dict]:
    """Find notes semantically related to a given note (by meaning, not just shared words)."""
    n = store.query_one("SELECT id,title,body FROM notes WHERE title=?", (title,))
    if not n:
        return [_no_note(title)]
    hits = semantic.vector_hits(store, "notes_vec", f"{n['title']}\n{n['body']}", limit=limit + 1)
    ids = [rid for rid, _ in hits if rid != n["id"]][:max(1, limit)]
    return _hydrate(ids)


@mcp.tool
def reindex_semantic() -> dict:
    """(Re)build semantic embeddings for all notes so search/related use vector ranking. Needs a local
    model (uv sync --group embed); reports unavailable otherwise."""
    if not semantic.available():
        return {"ok": False, "engine": "unavailable",
                "hint": "uv sync --group embed (model2vec, free, ~30MB) then call again"}
    n = 0
    for r in store.query("SELECT id,title,body FROM notes"):
        if semantic.index_row(store, "notes_vec", r["id"], f"{r['title']}\n{r['body']}"):
            n += 1
    return {"ok": True, "indexed": n}


@mcp.tool
def backlinks(title: str) -> list[dict]:
    """Notes that link TO this title via [[title]]."""
    return store.query("SELECT id,title FROM notes WHERE body LIKE ?", (f"%[[{title}]]%",))


@mcp.tool
def list_notes(limit: int = 50) -> list[dict]:
    """List recent notes."""
    return store.query("SELECT id,title,updated_at,tags FROM notes ORDER BY updated_at DESC LIMIT ?", (max(1, limit),))


@mcp.tool
def rename_note(old_title: str, new_title: str, update_links: bool = True) -> dict:
    """Rename a note; optionally rewrite [[old]] links in other notes to [[new]]."""
    if not store.query_one("SELECT id FROM notes WHERE title=?", (old_title,)):
        return _no_note(old_title)
    if store.query_one("SELECT id FROM notes WHERE title=?", (new_title,)):
        return err(f"target title already exists: '{new_title}'",
                   hint="choose a new_title that is not already used")
    store.execute("UPDATE notes SET title=?, updated_at=? WHERE title=?", (new_title, _now(), old_title))
    rewired = 0
    if update_links:
        for n in store.query("SELECT id,body FROM notes WHERE body LIKE ?", (f"%[[{old_title}]]%",)):
            store.execute("UPDATE notes SET body=?, updated_at=? WHERE id=?",
                          (n["body"].replace(f"[[{old_title}]]", f"[[{new_title}]]"), _now(), n["id"]))
            rewired += 1
    return {"ok": True, "old": old_title, "new": new_title, "links_rewired": rewired}


@mcp.tool
def delete_note(title: str) -> dict:
    """Delete a note by title."""
    row = store.query_one("SELECT id FROM notes WHERE title=?", (title,))
    if not row:
        return _no_note(title)
    store.execute("DELETE FROM notes WHERE title=?", (title,))
    semantic.drop_row(store, "notes_vec", row["id"])
    return {"ok": True, "title": title}


# ---------------- Tags ----------------
@mcp.tool
def tag_note(title: str, tags: str) -> dict:
    """Add tags to a note (comma/space separated; merged with existing & inline #tags)."""
    n = store.query_one("SELECT body,tags FROM notes WHERE title=?", (title,))
    if not n:
        return _no_note(title)
    merged = _tags(n["body"], (n["tags"] or "") + " " + tags)
    store.execute("UPDATE notes SET tags=?, updated_at=? WHERE title=?", (merged, _now(), title))
    return {"ok": True, "title": title, "tags": merged.split(",") if merged else []}


@mcp.tool
def list_tags() -> list[dict]:
    """All tags with note counts."""
    counts: dict[str, int] = {}
    for r in store.query("SELECT tags FROM notes WHERE tags!=''"):
        for t in r["tags"].split(","):
            if t:
                counts[t] = counts.get(t, 0) + 1
    return [{"tag": t, "count": c} for t, c in sorted(counts.items(), key=lambda x: -x[1])]


@mcp.tool
def notes_by_tag(tag: str) -> list[dict]:
    """Notes carrying a given tag."""
    tag = tag.strip().lstrip("#").lower()
    rows = store.query("SELECT id,title,tags FROM notes WHERE tags!=''")
    return [{"id": r["id"], "title": r["title"]} for r in rows if tag in r["tags"].split(",")]


# ---------------- Daily notes ----------------
@mcp.tool
def daily_note(date: str = "") -> dict:
    """Get-or-create today's (or `date`=YYYY-MM-DD) daily note. Returns its title and body."""
    d = (date or datetime.now(timezone.utc).date().isoformat()).strip()
    title = d
    n = store.query_one("SELECT * FROM notes WHERE title=?", (title,))
    if not n:
        _upsert(title, f"# {d}\n", "daily")
        n = store.query_one("SELECT * FROM notes WHERE title=?", (title,))
    return {"title": title, "body": n["body"]}


@mcp.tool
def append_to_daily(text: str, date: str = "") -> dict:
    """Append a timestamped line to today's (or `date`) daily note."""
    d = (date or datetime.now(timezone.utc).date().isoformat()).strip()
    n = store.query_one("SELECT body FROM notes WHERE title=?", (d,))
    if not n:
        _upsert(d, f"# {d}\n", "daily")
        n = store.query_one("SELECT body FROM notes WHERE title=?", (d,))
    ts = datetime.now(timezone.utc).strftime("%H:%M")
    body = n["body"].rstrip() + f"\n- {ts} {text}\n"
    store.execute("UPDATE notes SET body=?, tags=?, updated_at=? WHERE title=?", (body, _tags(body, "daily"), _now(), d))
    return {"ok": True, "title": d}


# ---------------- Templates ----------------
@mcp.tool
def save_template(name: str, body: str) -> dict:
    """Save a reusable note template. Body may contain Jinja2 placeholders like {{ topic }}."""
    store.execute("INSERT INTO templates(name,body,created_at) VALUES(?,?,?) "
                  "ON CONFLICT(name) DO UPDATE SET body=excluded.body", (name, body, _now()))
    return {"ok": True, "name": name}


@mcp.tool
def list_templates() -> list[dict]:
    """List saved templates."""
    return store.query("SELECT name FROM templates ORDER BY name")


@mcp.tool
def new_from_template(title: str, template: str, vars: dict | None = None) -> dict:
    """Create a note from a saved template, rendering Jinja2 vars (falls back to literal body)."""
    t = store.query_one("SELECT body FROM templates WHERE name=?", (template,))
    if not t:
        avail = [r["name"] for r in store.query("SELECT name FROM templates ORDER BY name LIMIT 10")]
        return not_found("template", template, available=avail,
                         hint="use list_templates() to see saved templates, or save_template() first")
    body = t["body"]
    try:
        from jinja2 import Template
        body = Template(t["body"]).render(**(vars or {}))
    except Exception:
        pass
    nid = _upsert(title.strip(), body)
    return {"id": nid, "title": title.strip()}


# ---------------- Graph & exports ----------------
def _graph() -> dict:
    notes = store.query("SELECT title,body FROM notes")
    titles = {n["title"] for n in notes}
    nodes = sorted(titles)
    edges, broken = [], []
    for n in notes:
        for link in LINK_RE.findall(n["body"]):
            if link in titles:
                edges.append({"from": n["title"], "to": link})
            else:
                broken.append({"from": n["title"], "to": link})
    return {"nodes": nodes, "edges": edges, "broken": broken}


@mcp.tool
def graph() -> dict:
    """Full backlink graph: nodes (titles), edges ([[links]]), and broken links."""
    return _graph()


@mcp.tool
def orphans() -> list[str]:
    """Notes with no incoming and no outgoing links."""
    g = _graph()
    linked = {e["from"] for e in g["edges"]} | {e["to"] for e in g["edges"]}
    return [n for n in g["nodes"] if n not in linked]


@mcp.tool
def broken_links() -> list[dict]:
    """[[links]] that point to non-existent notes."""
    return _graph()["broken"]


@mcp.tool
def export_graph_json(path: str = "") -> dict:
    """Write the backlink graph to a JSON file. Default: <data_dir>/exports/graph.json."""
    target = Path(path) if path else (data_dir("notes") / "exports" / "graph.json")
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_text(json.dumps(_graph(), indent=2), encoding="utf-8")
    return {"ok": True, "path": str(target)}


def _md(title: str, body: str, tags: str, created: str, updated: str) -> str:
    fm = ["---", f"title: {title}", f"created: {created}", f"updated: {updated}"]
    if tags:
        fm.append(f"tags: [{tags}]")
    fm.append("---")
    return "\n".join(fm) + "\n\n" + (body or "")


def _safe_name(title: str) -> str:
    # Strip path separators and null bytes too, so a title can't escape its export dir.
    cleaned = re.sub(r"[^\w\- ]", "_", (title or "").replace("\x00", ""))
    return cleaned.strip()[:120] or "untitled"


@mcp.tool
def export_note(title: str, dir: str = "") -> dict:
    """Write one note to a real .md file (with YAML frontmatter)."""
    n = store.query_one("SELECT * FROM notes WHERE title=?", (title,))
    if not n:
        return {"error": "not found"}
    base = Path(dir) if dir else (data_dir("notes") / "exports")
    base.mkdir(parents=True, exist_ok=True)
    p = base / f"{_safe_name(title)}.md"
    p.write_text(_md(title, n["body"], n.get("tags") or "", n["created_at"], n["updated_at"]), encoding="utf-8")
    return {"ok": True, "path": str(p)}


@mcp.tool
def export_all(dir: str = "") -> dict:
    """Write every note to .md files in a directory (default <data_dir>/exports)."""
    base = Path(dir) if dir else (data_dir("notes") / "exports")
    base.mkdir(parents=True, exist_ok=True)
    count = 0
    for n in store.query("SELECT * FROM notes"):
        (base / f"{_safe_name(n['title'])}.md").write_text(
            _md(n["title"], n["body"], n.get("tags") or "", n["created_at"], n["updated_at"]), encoding="utf-8")
        count += 1
    return {"ok": True, "dir": str(base), "exported": count}


@mcp.tool
def import_markdown(dir: str) -> dict:
    """Import a folder of .md files as notes (filename stem becomes the title; frontmatter stripped)."""
    base = Path(dir)
    if not base.is_dir():
        return not_found("directory", dir, hint="pass a path to an existing folder of .md files")
    imported = 0
    for p in base.glob("*.md"):
        try:
            if p.stat().st_size > 20_000_000:  # skip oversized files (>20MB)
                continue
        except OSError:
            continue
        text = p.read_text(encoding="utf-8", errors="ignore")
        body = re.sub(r"^---\n.*?\n---\n", "", text, count=1, flags=re.DOTALL)
        _upsert(p.stem, body.strip())
        imported += 1
    return {"ok": True, "imported": imported}


@mcp.tool
def link_suggestions(title: str, limit: int = 5) -> list[dict]:
    """Suggest notes to link to from a note, via FTS over its body (excludes already-linked + self)."""
    n = store.query_one("SELECT body FROM notes WHERE title=?", (title,))
    if not n:
        return [_no_note(title)]
    linked = set(LINK_RE.findall(n["body"])) | {title}
    terms = " OR ".join({w for w in re.findall(r"[A-Za-z]{4,}", n["body"])[:20]}) or title
    try:
        rows = store.query("SELECT nt.title FROM notes_fts f JOIN notes nt ON nt.id=f.rowid "
                           "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?", (terms, limit * 3))
    except Exception:
        rows = store.query("SELECT title FROM notes LIMIT ?", (limit * 3,))
    return [{"title": r["title"]} for r in rows if r["title"] not in linked][:limit]


@mcp.tool
def merge_notes(source: str, target: str, delete_source: bool = True, update_links: bool = True) -> dict:
    """Merge `source` note into `target` (appends its body). Optionally rewrites [[source]] links to
    [[target]] in other notes and deletes the source."""
    s = store.query_one("SELECT id,body FROM notes WHERE title=?", (source,))
    t = store.query_one("SELECT id,body FROM notes WHERE title=?", (target,))
    if not s:
        return not_found("source note", source, available=_note_titles(),
                         hint="use list_notes() to find the exact source title")
    if not t:
        return not_found("target note", target, available=_note_titles(),
                         hint="use list_notes() to find the exact target title")
    merged_body = (t["body"].rstrip() + f"\n\n---\n\n" + (s["body"] or "")).strip()
    store.execute("UPDATE notes SET body=?, tags=?, updated_at=? WHERE title=?",
                  (merged_body, _tags(merged_body), _now(), target))
    rewired = 0
    if update_links:
        for n in store.query("SELECT id,body FROM notes WHERE body LIKE ?", (f"%[[{source}]]%",)):
            store.execute("UPDATE notes SET body=?, updated_at=? WHERE id=?",
                          (n["body"].replace(f"[[{source}]]", f"[[{target}]]"), _now(), n["id"]))
            rewired += 1
    if delete_source:
        store.execute("DELETE FROM notes WHERE title=?", (source,))
    return {"ok": True, "target": target, "links_rewired": rewired, "deleted_source": delete_source}


@mcp.tool
def outline(title: str) -> list[dict]:
    """Extract the markdown heading outline of a note (level + text, in order)."""
    n = store.query_one("SELECT body FROM notes WHERE title=?", (title,))
    if not n:
        return [_no_note(title)]
    out = []
    for line in (n["body"] or "").splitlines():
        m = re.match(r"^(#{1,6})\s+(.+?)\s*#*$", line)
        if m:
            out.append({"level": len(m.group(1)), "text": m.group(2).strip()})
    return out


@mcp.tool
def recent(days: int = 7, limit: int = 50) -> list[dict]:
    """Notes updated within the last N days, most recent first."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    return store.query("SELECT id,title,updated_at,tags FROM notes WHERE updated_at>=? "
                       "ORDER BY updated_at DESC LIMIT ?", (cutoff, max(1, limit)))


@mcp.tool
def find_by_link(target: str) -> list[dict]:
    """Notes that contain a [[target]] link (alias-friendly view of backlinks, including broken ones)."""
    return store.query("SELECT id,title FROM notes WHERE body LIKE ?", (f"%[[{target}]]%",))


@mcp.tool
def stats() -> dict:
    """Counts: notes, tags, links, orphans, broken links, total words."""
    g = _graph()
    linked = {e["from"] for e in g["edges"]} | {e["to"] for e in g["edges"]}
    words = sum(len((r["body"] or "").split()) for r in store.query("SELECT body FROM notes"))
    return {"notes": len(g["nodes"]), "tags": _tag_count(),
            "links": len(g["edges"]), "broken_links": len(g["broken"]),
            "orphans": len([n for n in g["nodes"] if n not in linked]), "total_words": words}


def _tag_count() -> int:
    seen = set()
    for r in store.query("SELECT tags FROM notes WHERE tags!=''"):
        seen.update(r["tags"].split(","))
    seen.discard("")
    return len(seen)


if __name__ == "__main__":
    mcp.run()
