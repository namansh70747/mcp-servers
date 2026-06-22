"""project-memory — durable, cross-session memory of a project's decisions, intent,
conventions, and the live working thread, so any agent can recall "why" and resume where
it left off even after the chat compacts.

Memories are scoped per project (defaults to the launch cwd; pass `project` to override).
Supports a typed relation graph, importance/pinning, auto-tagging, duplicate merging,
ranked recall (relevance + importance + recency + pin bonus), and a committable digest.
"""
from __future__ import annotations

import math
import os
import re
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import BaseStore, db_path, make_server, semantic

mcp = make_server(
    "project-memory",
    instructions=(
        "Durable project memory. At session start call resume() to rehydrate the working "
        "thread; remember() decisions/conventions as you make them; pin() the load-bearing ones; "
        "link() related memories and graph() to traverse; checkpoint() at milestones so context "
        "survives compaction. export_digest() writes a committable MEMORY.md."
    ),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS memories(
  id INTEGER PRIMARY KEY,
  project TEXT NOT NULL,
  kind TEXT NOT NULL,
  note TEXT NOT NULL,
  tags TEXT DEFAULT '',
  importance INTEGER DEFAULT 0,
  pinned INTEGER DEFAULT 0,
  access_count INTEGER DEFAULT 0,
  last_access TEXT,
  updated_at TEXT,
  created_at TEXT NOT NULL
);
CREATE VIRTUAL TABLE IF NOT EXISTS memories_fts
  USING fts5(note, tags, content='memories', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS memories_ai AFTER INSERT ON memories BEGIN
  INSERT INTO memories_fts(rowid, note, tags) VALUES (new.id, new.note, new.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_ad AFTER DELETE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, note, tags) VALUES('delete', old.id, old.note, old.tags);
END;
CREATE TRIGGER IF NOT EXISTS memories_au AFTER UPDATE ON memories BEGIN
  INSERT INTO memories_fts(memories_fts, rowid, note, tags) VALUES('delete', old.id, old.note, old.tags);
  INSERT INTO memories_fts(rowid, note, tags) VALUES (new.id, new.note, new.tags);
END;
CREATE TABLE IF NOT EXISTS links(
  id INTEGER PRIMARY KEY, project TEXT, a_id INTEGER, b_id INTEGER,
  rel TEXT DEFAULT 'relates', created_at TEXT
);
CREATE TABLE IF NOT EXISTS checkpoints(
  id INTEGER PRIMARY KEY, project TEXT NOT NULL, summary TEXT NOT NULL,
  open_items TEXT DEFAULT '', created_at TEXT NOT NULL
);
"""

KINDS = {"decision", "todo", "glossary", "convention", "context"}
store = BaseStore(db_path("project-memory"), schema=SCHEMA)
store.migrate(semantic.vec_table_sql("pm_vec"))


def _reindex(mid: int) -> None:
    m = store.query_one("SELECT note,tags FROM memories WHERE id=?", (mid,))
    if m:
        semantic.index_row(store, "pm_vec", mid, f"{m.get('note') or ''} {m.get('tags') or ''}")

# Defensive migration for stores created before these columns existed.
for _alter in (
    "ALTER TABLE memories ADD COLUMN importance INTEGER DEFAULT 0",
    "ALTER TABLE memories ADD COLUMN pinned INTEGER DEFAULT 0",
    "ALTER TABLE memories ADD COLUMN access_count INTEGER DEFAULT 0",
    "ALTER TABLE memories ADD COLUMN last_access TEXT",
    "ALTER TABLE memories ADD COLUMN updated_at TEXT",
    "ALTER TABLE links ADD COLUMN rel TEXT DEFAULT 'relates'",
):
    try:
        store.execute(_alter)
    except Exception:
        pass

_TECH_WORDS = {
    "sqlite", "postgres", "redis", "fastmcp", "mcp", "python", "typescript", "react",
    "docker", "kubernetes", "api", "oauth", "jwt", "fts", "fts5", "regex", "cli", "json",
    "yaml", "async", "cache", "schema", "migration", "index", "embedding", "graph",
    "git", "ci", "test", "auth", "http", "websocket", "queue", "worker",
}
_STOP = {
    "the", "a", "an", "and", "or", "but", "for", "to", "of", "in", "on", "with", "is",
    "are", "was", "were", "be", "this", "that", "we", "it", "as", "at", "by", "from",
    "so", "use", "used", "using", "over", "into", "our", "all", "via", "not", "than",
}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _project(p: str | None) -> str:
    return p or os.getcwd()


def _safe_out_path(proj: str, target: str) -> Path | None:
    """Resolve a write target relative to the project root and confirm it cannot escape it.

    Blocks absolute paths and `..` traversal so export_digest can never clobber files outside
    the project. Returns the resolved Path, or None if it is unsafe.
    """
    if not isinstance(target, str) or not target.strip():
        return None
    root = Path(proj).resolve()
    try:
        full = (root / target).resolve()
        full.relative_to(root)
    except (ValueError, OSError):
        return None
    return full


def _derive_tags(note: str, limit: int = 6) -> list[str]:
    """Heuristic auto-tags: tech words, code identifiers, file paths (stdlib only)."""
    tags: list[str] = []
    low = note.lower()
    for w in _TECH_WORDS:
        if re.search(rf"\b{re.escape(w)}\b", low) and w not in tags:
            tags.append(w)
    for m in re.findall(r"\b[a-z]+_[a-z_]+\b|\b[a-z]+[A-Z]\w+\b", note):
        if m.lower() not in tags and len(m) > 2:
            tags.append(m)
    for m in re.findall(r"\b[\w./-]+\.(?:py|ts|js|tsx|jsx|go|rs|md|json|toml|yaml)\b", note):
        if m not in tags:
            tags.append(m)
    return tags[:limit]


def _tokens(text: str) -> set[str]:
    return {t for t in re.findall(r"[a-z0-9_]+", text.lower())
            if t not in _STOP and len(t) > 2}


@mcp.tool
def remember(note: str, kind: str = "context", tags: str = "", project: str | None = None,
             importance: int = 0, pinned: bool = False, auto_tag: bool = False) -> dict:
    """Save a durable memory. kind ∈ {decision, todo, glossary, convention, context}. Optionally
    set importance (0-5, surfaced first in recall/resume), pin it, and auto_tag to derive tags
    from the note."""
    if not isinstance(note, str) or not note.strip():
        return {"error": "note must be a non-empty string"}
    if len(note) > 100_000:
        return {"error": "note too large (max 100k chars)"}
    if kind not in KINDS:
        kind = "context"
    proj = _project(project)
    if auto_tag:
        derived = _derive_tags(note)
        existing = [t.strip() for t in tags.split(",") if t.strip()]
        tags = ",".join(dict.fromkeys(existing + derived))
    mid = store.execute(
        "INSERT INTO memories(project, kind, note, tags, importance, pinned, created_at) "
        "VALUES(?,?,?,?,?,?,?)",
        (proj, kind, note, tags, max(0, min(5, importance)), 1 if pinned else 0, _now()),
    )
    _reindex(mid)
    return {"id": mid, "project": proj, "kind": kind, "tags": tags,
            "importance": max(0, min(5, importance)), "pinned": pinned}


@mcp.tool
def recall(query: str, project: str | None = None, limit: int = 10, kind: str | None = None,
           boost_pinned: bool = True) -> list[dict]:
    """Full-text search your memories for this project, ranked by a blend of relevance +
    importance + recency + pin bonus (most useful first). Bumps each hit's access counter."""
    if not isinstance(query, str) or not query.strip():
        return []
    limit = max(1, min(int(limit), 200))
    proj = _project(project)
    rows: list[dict] = []
    try:
        rows = store.query(
            "SELECT m.id, m.kind, m.note, m.tags, m.importance, m.pinned, m.access_count, "
            "m.created_at FROM memories_fts f JOIN memories m ON m.id = f.rowid "
            "WHERE memories_fts MATCH ? AND m.project = ? ORDER BY rank LIMIT ?",
            (query, proj, max(limit * 3, 30)),
        )
    except Exception:
        rows = []
    if not rows:
        like = f"%{query}%"
        rows = store.query(
            "SELECT id, kind, note, tags, importance, pinned, access_count, created_at "
            "FROM memories WHERE project = ? AND (note LIKE ? OR tags LIKE ?) "
            "ORDER BY created_at DESC LIMIT ?",
            (proj, like, like, max(limit * 3, 30)),
        )
    # semantic layer: cosine hits scoped to this project, merged into candidates + scored
    vec = dict(semantic.vector_hits(store, "pm_vec", query,
                                    where_sql="SELECT id FROM memories WHERE project=?",
                                    params=(proj,), limit=max(limit * 3, 30)))
    have = {r["id"] for r in rows}
    missing = [mid for mid in vec if mid not in have]
    if missing:
        ph = ",".join("?" * len(missing))
        rows += store.query(
            f"SELECT id, kind, note, tags, importance, pinned, access_count, created_at "
            f"FROM memories WHERE id IN ({ph})", tuple(missing))
        if kind:
            rows = [r for r in rows if r["kind"] == kind]

    now = datetime.now(timezone.utc)
    scored = []
    for idx, r in enumerate(rows):
        rel = 1.0 / (1 + idx)  # FTS already rank-ordered
        imp = (r.get("importance") or 0) / 5.0
        try:
            age_days = (now - datetime.fromisoformat(r["created_at"])).days
        except Exception:
            age_days = 30
        recency = math.exp(-age_days / 45.0)
        pin = 0.5 if (boost_pinned and r.get("pinned")) else 0.0
        sem = vec.get(r["id"], 0.0)  # cosine 0..1
        r["score"] = round(rel + 0.6 * imp + 0.4 * recency + pin + 0.8 * sem, 4)
        scored.append(r)
    scored.sort(key=lambda x: x["score"], reverse=True)
    out = scored[:limit]
    ids = [r["id"] for r in out]
    if ids:
        store.execute(
            f"UPDATE memories SET access_count = access_count + 1, last_access = ? "
            f"WHERE id IN ({','.join('?' * len(ids))})", (_now(), *ids))
    return out


@mcp.tool
def list_memories(kind: str | None = None, project: str | None = None, limit: int = 50,
                  pinned_only: bool = False) -> list[dict]:
    """List memories for this project, optionally filtered by kind or pinned-only (pinned + most
    important first, then newest)."""
    proj = _project(project)
    where = "project = ?"
    params: list = [proj]
    if kind:
        where += " AND kind = ?"
        params.append(kind)
    if pinned_only:
        where += " AND pinned = 1"
    params.append(limit)
    return store.query(
        f"SELECT id, kind, note, tags, importance, pinned, access_count, created_at, updated_at "
        f"FROM memories WHERE {where} ORDER BY pinned DESC, importance DESC, created_at DESC LIMIT ?",
        tuple(params),
    )


@mcp.tool
def update_memory(memory_id: int, note: str | None = None, kind: str | None = None,
                  tags: str | None = None) -> dict:
    """Edit a memory in place (note/kind/tags). Keeps FTS in sync and stamps updated_at."""
    row = store.query_one("SELECT note, kind, tags FROM memories WHERE id=?", (memory_id,))
    if not row:
        return {"error": f"no memory {memory_id}"}
    new_note = note if note is not None else row["note"]
    new_kind = kind if (kind in KINDS) else row["kind"]
    new_tags = tags if tags is not None else row["tags"]
    store.execute("UPDATE memories SET note=?, kind=?, tags=?, updated_at=? WHERE id=?",
                  (new_note, new_kind, new_tags, _now(), memory_id))
    _reindex(memory_id)
    return {"ok": True, "id": memory_id, "kind": new_kind}


@mcp.tool
def reindex_semantic(project: str | None = None) -> dict:
    """(Re)build semantic embeddings for memories (optionally one project). Needs a local model
    (uv sync --group embed)."""
    if not semantic.available():
        return {"ok": False, "engine": "unavailable", "hint": "uv sync --group embed then call again"}
    if project:
        rows = store.query("SELECT id FROM memories WHERE project=?", (_project(project),))
    else:
        rows = store.query("SELECT id FROM memories")
    for r in rows:
        _reindex(r["id"])
    return {"ok": True, "indexed": len(rows)}


@mcp.tool
def pin(memory_id: int, pinned: bool = True) -> dict:
    """Pin (or unpin) a memory so it always surfaces in resume() and gets a recall boost."""
    if not store.query_one("SELECT id FROM memories WHERE id=?", (memory_id,)):
        return {"error": f"no memory {memory_id}"}
    store.execute("UPDATE memories SET pinned=?, updated_at=? WHERE id=?",
                  (1 if pinned else 0, _now(), memory_id))
    return {"ok": True, "id": memory_id, "pinned": pinned}


@mcp.tool
def set_importance(memory_id: int, importance: int) -> dict:
    """Set a memory's importance (0-5). Higher importance ranks higher in recall and resume."""
    if not store.query_one("SELECT id FROM memories WHERE id=?", (memory_id,)):
        return {"error": f"no memory {memory_id}"}
    imp = max(0, min(5, importance))
    store.execute("UPDATE memories SET importance=?, updated_at=? WHERE id=?",
                  (imp, _now(), memory_id))
    return {"ok": True, "id": memory_id, "importance": imp}


@mcp.tool
def auto_tag(memory_id: int, project: str | None = None) -> dict:
    """Derive tags from a memory's note (tech words, identifiers, file paths) and merge them in."""
    row = store.query_one("SELECT note, tags FROM memories WHERE id=?", (memory_id,))
    if not row:
        return {"error": f"no memory {memory_id}"}
    derived = _derive_tags(row["note"])
    existing = [t.strip() for t in (row["tags"] or "").split(",") if t.strip()]
    merged = ",".join(dict.fromkeys(existing + derived))
    store.execute("UPDATE memories SET tags=?, updated_at=? WHERE id=?", (merged, _now(), memory_id))
    return {"ok": True, "id": memory_id, "tags": merged, "added": derived}


@mcp.tool
def link(a_id: int, b_id: int, project: str | None = None, rel: str = "relates") -> dict:
    """Record a typed relationship between two memories (knowledge-graph edge). Common rels:
    relates, supersedes, depends_on, blocks, refines, contradicts."""
    lid = store.execute(
        "INSERT INTO links(project, a_id, b_id, rel, created_at) VALUES(?,?,?,?,?)",
        (_project(project), a_id, b_id, rel, _now()),
    )
    return {"id": lid, "a_id": a_id, "b_id": b_id, "rel": rel}


@mcp.tool
def unlink(a_id: int, b_id: int, project: str | None = None) -> dict:
    """Remove edge(s) between two memories (either direction)."""
    n = store.execute(
        "DELETE FROM links WHERE project=? AND ((a_id=? AND b_id=?) OR (a_id=? AND b_id=?))",
        (_project(project), a_id, b_id, b_id, a_id))
    return {"ok": True, "removed_any": True}


@mcp.tool
def links_of(memory_id: int, project: str | None = None) -> list[dict]:
    """Direct neighbors of a memory (both directions) with the relation type and a note preview."""
    proj = _project(project)
    rows = store.query(
        "SELECT a_id, b_id, rel FROM links WHERE project=? AND (a_id=? OR b_id=?)",
        (proj, memory_id, memory_id))
    out = []
    for r in rows:
        other = r["b_id"] if r["a_id"] == memory_id else r["a_id"]
        m = store.query_one("SELECT kind, note FROM memories WHERE id=?", (other,))
        out.append({"id": other, "rel": r["rel"],
                    "direction": "out" if r["a_id"] == memory_id else "in",
                    "kind": m["kind"] if m else None,
                    "note": (m["note"][:120] if m else None)})
    return out


@mcp.tool
def graph(memory_id: int, depth: int = 2, project: str | None = None) -> dict:
    """Traverse the relation graph from a memory up to `depth` hops (BFS). Returns the reachable
    nodes (id, kind, note preview) and the edges between them."""
    proj = _project(project)
    visited: dict[int, dict] = {}
    edges: list[dict] = []
    frontier = [memory_id]
    seen_edges: set[tuple] = set()
    for _ in range(max(1, depth)):
        nxt: list[int] = []
        for nid in frontier:
            if nid not in visited:
                m = store.query_one("SELECT id, kind, note FROM memories WHERE id=?", (nid,))
                if m:
                    visited[nid] = {"id": m["id"], "kind": m["kind"], "note": m["note"][:120]}
            for r in store.query(
                    "SELECT a_id, b_id, rel FROM links WHERE project=? AND (a_id=? OR b_id=?)",
                    (proj, nid, nid)):
                key = (r["a_id"], r["b_id"], r["rel"])
                if key not in seen_edges:
                    seen_edges.add(key)
                    edges.append({"a_id": r["a_id"], "b_id": r["b_id"], "rel": r["rel"]})
                other = r["b_id"] if r["a_id"] == nid else r["a_id"]
                if other not in visited:
                    nxt.append(other)
        frontier = nxt
        if not frontier:
            break
    # ensure terminal nodes are described
    for e in edges:
        for nid in (e["a_id"], e["b_id"]):
            if nid not in visited:
                m = store.query_one("SELECT id, kind, note FROM memories WHERE id=?", (nid,))
                if m:
                    visited[nid] = {"id": m["id"], "kind": m["kind"], "note": m["note"][:120]}
    return {"root": memory_id, "nodes": list(visited.values()), "edges": edges}


@mcp.tool
def forget(memory_id: int) -> dict:
    """Delete a memory by id (also removes its graph edges)."""
    store.execute("DELETE FROM memories WHERE id = ?", (memory_id,))
    store.execute("DELETE FROM links WHERE a_id=? OR b_id=?", (memory_id, memory_id))
    semantic.drop_row(store, "pm_vec", memory_id)
    return {"ok": True, "deleted": memory_id}


@mcp.tool
def forget_where(project: str | None = None, kind: str | None = None, tag: str | None = None,
                 older_than_days: int | None = None) -> dict:
    """Bulk-delete memories matching a filter (kind / tag substring / age). At least one filter is
    required (guards against wiping everything). Pinned memories are never deleted."""
    proj = _project(project)
    if not any([kind, tag, older_than_days]):
        return {"error": "provide at least one filter: kind, tag, or older_than_days"}
    where = "project=? AND pinned=0"
    params: list = [proj]
    if kind:
        where += " AND kind=?"
        params.append(kind)
    if tag:
        where += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    if older_than_days:
        cutoff = datetime.now(timezone.utc).timestamp() - older_than_days * 86400
        cutoff_iso = datetime.fromtimestamp(cutoff, timezone.utc).isoformat()
        where += " AND created_at < ?"
        params.append(cutoff_iso)
    ids = [r["id"] for r in store.query(f"SELECT id FROM memories WHERE {where}", tuple(params))]
    for mid in ids:
        store.execute("DELETE FROM memories WHERE id=?", (mid,))
        store.execute("DELETE FROM links WHERE a_id=? OR b_id=?", (mid, mid))
        semantic.drop_row(store, "pm_vec", mid)
    return {"ok": True, "deleted": len(ids), "ids": ids[:50]}


@mcp.tool
def find_duplicates(project: str | None = None, threshold: float = 0.6,
                    limit: int = 20) -> list[dict]:
    """Find near-duplicate memories via token Jaccard similarity (>= threshold). Returns candidate
    pairs so you can merge() them."""
    proj = _project(project)
    rows = store.query("SELECT id, note FROM memories WHERE project=? ORDER BY id", (proj,))
    toks = {r["id"]: _tokens(r["note"]) for r in rows}
    notes = {r["id"]: r["note"] for r in rows}
    pairs = []
    ids = list(toks)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = toks[ids[i]], toks[ids[j]]
            if not a or not b:
                continue
            inter = len(a & b)
            if not inter:
                continue
            jac = inter / len(a | b)
            if jac >= threshold:
                pairs.append({"a_id": ids[i], "b_id": ids[j], "similarity": round(jac, 3),
                              "a_note": notes[ids[i]][:80], "b_note": notes[ids[j]][:80]})
    pairs.sort(key=lambda p: p["similarity"], reverse=True)
    return pairs[:limit]


@mcp.tool
def merge(keep_id: int, drop_ids: list[int], project: str | None = None) -> dict:
    """Merge duplicate memories into `keep_id`: union their tags, rewire their graph edges to
    keep_id, then delete the dropped ones. The kept memory wins for note/kind."""
    keep = store.query_one("SELECT tags FROM memories WHERE id=?", (keep_id,))
    if not keep:
        return {"error": f"no keep memory {keep_id}"}
    tags = [t.strip() for t in (keep["tags"] or "").split(",") if t.strip()]
    for did in drop_ids:
        if did == keep_id:
            continue
        d = store.query_one("SELECT tags FROM memories WHERE id=?", (did,))
        if d and d["tags"]:
            tags += [t.strip() for t in d["tags"].split(",") if t.strip()]
        store.execute("UPDATE links SET a_id=? WHERE a_id=?", (keep_id, did))
        store.execute("UPDATE links SET b_id=? WHERE b_id=?", (keep_id, did))
        store.execute("DELETE FROM links WHERE a_id=b_id")
        store.execute("DELETE FROM memories WHERE id=?", (did,))
    merged_tags = ",".join(dict.fromkeys(tags))
    store.execute("UPDATE memories SET tags=?, updated_at=? WHERE id=?",
                  (merged_tags, _now(), keep_id))
    return {"ok": True, "kept": keep_id, "dropped": drop_ids, "tags": merged_tags}


@mcp.tool
def checkpoint(summary: str, open_items: str = "", project: str | None = None) -> dict:
    """Save the current working thread (what we're doing + what's still open) before the
    chat compacts. Call at milestones."""
    proj = _project(project)
    cid = store.execute(
        "INSERT INTO checkpoints(project, summary, open_items, created_at) VALUES(?,?,?,?)",
        (proj, summary, open_items, _now()),
    )
    return {"id": cid, "project": proj}


@mcp.tool
def resume(project: str | None = None) -> dict:
    """Rehydrate context: the latest checkpoint (with open_items parsed to a list) + pinned and
    high-importance memories + recent decisions, todos, and conventions."""
    proj = _project(project)
    last = store.query_one(
        "SELECT summary, open_items, created_at FROM checkpoints "
        "WHERE project = ? ORDER BY created_at DESC LIMIT 1",
        (proj,),
    )
    if last and last.get("open_items"):
        items = [s.strip(" -*\t") for s in re.split(r"[\n;]", last["open_items"]) if s.strip()]
        last["open_items_list"] = items
    pinned = store.query(
        "SELECT id, kind, note, importance FROM memories WHERE project=? AND pinned=1 "
        "ORDER BY importance DESC, created_at DESC LIMIT 10", (proj,))
    important = store.query(
        "SELECT id, kind, note, importance FROM memories WHERE project=? AND pinned=0 "
        "AND importance >= 3 ORDER BY importance DESC, created_at DESC LIMIT 8", (proj,))
    recent = store.query(
        "SELECT kind, note, created_at FROM memories "
        "WHERE project = ? AND kind IN ('decision','todo','convention') "
        "ORDER BY created_at DESC LIMIT 15",
        (proj,),
    )
    return {"project": proj, "last_checkpoint": last, "pinned": pinned,
            "important": important, "recent": recent}


@mcp.tool
def timeline(project: str | None = None, limit: int = 40) -> list[dict]:
    """Unified chronological feed of memories + checkpoints (newest first)."""
    proj = _project(project)
    mem = store.query(
        "SELECT id, 'memory' AS type, kind, note AS text, created_at FROM memories "
        "WHERE project=?", (proj,))
    ck = store.query(
        "SELECT id, 'checkpoint' AS type, 'checkpoint' AS kind, summary AS text, created_at "
        "FROM checkpoints WHERE project=?", (proj,))
    feed = mem + ck
    feed.sort(key=lambda x: x["created_at"], reverse=True)
    for f in feed:
        f["text"] = (f["text"] or "")[:200]
    return feed[:limit]


@mcp.tool
def export_digest(project: str | None = None, write: bool = False, target: str = "MEMORY.md",
                  prime_clients: bool = False) -> dict:
    """Build a committable markdown digest of this project's memory: pinned items, decisions,
    conventions, glossary, open todos, and the latest checkpoint. With write=True saves it to
    `target`; with prime_clients=True also appends a short block to CLAUDE.md/AGENTS.md."""
    proj = _project(project)
    name = Path(proj).name

    def section(title, rows):
        if not rows:
            return []
        out = [f"## {title}", ""]
        for r in rows:
            tag = f" _(tags: {r['tags']})_" if r.get("tags") else ""
            out.append(f"- {r['note']}{tag}")
        return out + [""]

    pinned = store.query("SELECT note, tags FROM memories WHERE project=? AND pinned=1 "
                         "ORDER BY importance DESC", (proj,))
    decisions = store.query("SELECT note, tags FROM memories WHERE project=? AND kind='decision' "
                            "ORDER BY importance DESC, created_at DESC LIMIT 30", (proj,))
    conventions = store.query("SELECT note, tags FROM memories WHERE project=? AND kind='convention' "
                              "ORDER BY created_at DESC LIMIT 30", (proj,))
    glossary = store.query("SELECT note, tags FROM memories WHERE project=? AND kind='glossary' "
                           "ORDER BY created_at DESC LIMIT 30", (proj,))
    todos = store.query("SELECT note, tags FROM memories WHERE project=? AND kind='todo' "
                        "ORDER BY created_at DESC LIMIT 30", (proj,))
    last = store.query_one("SELECT summary, open_items FROM checkpoints WHERE project=? "
                           "ORDER BY created_at DESC LIMIT 1", (proj,))

    lines = [f"# Project memory: {name}", "",
             "_Auto-generated by project-memory._", ""]
    lines += section("Pinned", pinned)
    lines += section("Decisions", decisions)
    lines += section("Conventions", conventions)
    lines += section("Glossary", glossary)
    lines += section("Open todos", todos)
    if last:
        lines += ["## Latest checkpoint", "", last["summary"], ""]
        if last.get("open_items"):
            lines += ["**Open items:** " + last["open_items"], ""]
    digest = "\n".join(lines) + "\n"

    written = []
    if write:
        t = _safe_out_path(proj, target)
        if t is None:
            return {"error": f"unsafe target path: {target!r}", "project": proj}
        t.parent.mkdir(parents=True, exist_ok=True)
        t.write_text(digest, encoding="utf-8")
        written.append(str(t))
        if prime_clients:
            block = f"\n<!-- project-memory digest -->\n{digest}"
            for fn in ("CLAUDE.md", "AGENTS.md"):
                fp = Path(proj) / fn
                try:
                    prev = fp.read_text(encoding="utf-8") if fp.exists() else ""
                    fp.write_text(prev + block, encoding="utf-8")
                    written.append(str(fp))
                except OSError:
                    pass
    return {"project": proj, "bytes": len(digest), "written": written, "preview": digest[:500]}


@mcp.tool
def stats(project: str | None = None) -> dict:
    """Counts of memories by kind + checkpoint/link/pinned counts for this project."""
    proj = _project(project)
    by_kind = store.query(
        "SELECT kind, COUNT(*) AS n FROM memories WHERE project = ? GROUP BY kind", (proj,)
    )
    ck = store.query_one("SELECT COUNT(*) AS n FROM checkpoints WHERE project = ?", (proj,))
    ln = store.query_one("SELECT COUNT(*) AS n FROM links WHERE project = ?", (proj,))
    pn = store.query_one("SELECT COUNT(*) AS n FROM memories WHERE project=? AND pinned=1", (proj,))
    return {"project": proj, "by_kind": {r["kind"]: r["n"] for r in by_kind},
            "checkpoints": ck["n"], "links": ln["n"], "pinned": pn["n"]}


if __name__ == "__main__":
    mcp.run()
