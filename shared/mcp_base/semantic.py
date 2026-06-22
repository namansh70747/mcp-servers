"""Tiny reusable hybrid-search layer so any BaseStore-backed server can add semantic search in ~15
lines: keep a sidecar `<name>_vec(rowid, vec BLOB)` table, index rows on write, and at query time fuse
keyword (FTS/LIKE) ranks with vector cosine via reciprocal-rank fusion.

Degrades gracefully: with no embedding model installed, index_row is a no-op and vector_hits returns []
so callers fall back to pure keyword search. Every function is wrapped and NEVER raises on edge input
(the knowledge servers are fuzzed by the no-crash guard).
"""
from __future__ import annotations

from . import embed

_SCAN_CAP = 6000  # max stored vectors to score per query (bounded latency)


def vec_table_sql(table: str) -> str:
    """DDL for a server's sidecar vector table. rowid mirrors the parent row's id."""
    return f"CREATE TABLE IF NOT EXISTS {table}(rowid INTEGER PRIMARY KEY, vec BLOB);"


def index_row(store, table: str, rowid: int, text: str) -> bool:
    """Encode `text` and upsert its vector. No-op (returns False) when no model is available or text is
    empty. Never raises."""
    try:
        if not text or not text.strip() or not embed.available():
            return False
        v = embed.encode_one(text[:8000])
        if not v:
            return False
        store.execute(f"INSERT INTO {table}(rowid, vec) VALUES(?,?) "
                      f"ON CONFLICT(rowid) DO UPDATE SET vec=excluded.vec", (rowid, embed.pack(v)))
        return True
    except Exception:
        return False


def drop_row(store, table: str, rowid: int) -> None:
    try:
        store.execute(f"DELETE FROM {table} WHERE rowid=?", (rowid,))
    except Exception:
        pass


def vector_hits(store, table: str, query: str, where_sql: str = "", params: tuple = (),
                limit: int = 50) -> list[tuple[int, float]]:
    """Cosine-rank stored vectors against `query`. Returns [(rowid, score)] best-first, or [] if no
    model/vectors. `where_sql` (e.g. "AND p.kind=?") + `params` let callers scope by joining the parent
    table is not needed — scope is applied to the vec table's rowid via a subquery when provided."""
    try:
        q = (query or "").strip()
        if not q or not embed.available():
            return []
        qv = embed.encode_one(q)
        if not qv:
            return []
        sql = f"SELECT rowid, vec FROM {table}"
        if where_sql:
            sql += f" WHERE rowid IN ({where_sql})"
        sql += f" LIMIT {_SCAN_CAP}"
        scored: list[tuple[int, float]] = []
        for r in store.query(sql, tuple(params)):
            if not r.get("vec"):
                continue
            v = embed.unpack(r["vec"])
            if len(v) == len(qv):
                scored.append((r["rowid"], embed.cosine(qv, v)))
        scored.sort(key=lambda x: -x[1])
        return scored[:limit]
    except Exception:
        return []


def rrf(fts_ids: list[int], vec_ids: list[int], limit: int = 20) -> list[int]:
    """Reciprocal-rank fusion of two ranked id lists -> fused ordered ids (best-first)."""
    fr = {rid: i for i, rid in enumerate(fts_ids)}
    vr = {rid: i for i, rid in enumerate(vec_ids)}
    fused = []
    for rid in set(fr) | set(vr):
        score = 0.0
        if rid in fr:
            score += 1.0 / (60 + fr[rid])
        if rid in vr:
            score += 1.0 / (60 + vr[rid])
        fused.append((score, rid))
    fused.sort(key=lambda x: -x[0])
    return [rid for _, rid in fused[:limit]]


def available() -> bool:
    return embed.available()
