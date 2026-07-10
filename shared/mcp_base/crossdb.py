"""Read-only cross-server SQLite access (shared by recipes, daily-digest, etc.)."""
from __future__ import annotations

import sqlite3

from .config import db_path


def open_ro(server_name: str, filename: str = "store.db") -> sqlite3.Connection | None:
    """Open a server DB read-only; return None if missing."""
    p = db_path(server_name, filename)
    if not p.exists():
        return None
    try:
        return sqlite3.connect(f"file:{p}?mode=ro", uri=True)
    except Exception:
        return None


def table_exists(conn: sqlite3.Connection, table: str) -> bool:
    row = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name=? LIMIT 1", (table,)
    ).fetchone()
    return row is not None


def columns(conn: sqlite3.Connection, table: str) -> set[str]:
    if not table_exists(conn, table):
        return set()
    return {r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}


def fts_safe(conn: sqlite3.Connection, table: str, query: str, limit: int = 20) -> list[dict]:
    """Run FTS query if table exists; return [] on any failure."""
    if not query.strip() or not table_exists(conn, table):
        return []
    try:
        rows = conn.execute(
            f"SELECT rowid, * FROM {table} WHERE {table} MATCH ? LIMIT ?",
            (query, limit),
        ).fetchall()
        cols = [d[0] for d in conn.execute(f"SELECT * FROM {table} LIMIT 0").description or []]
        if not cols:
            return [{"rowid": r[0], "raw": r[1:]} for r in rows]
        return [dict(zip(cols, r[1:], strict=False)) for r in rows]
    except Exception:
        return []


def ensure_meta(conn: sqlite3.Connection) -> None:
    conn.execute(
        "CREATE TABLE IF NOT EXISTS _mcp_meta (key TEXT PRIMARY KEY, value TEXT)"
    )


def normalize_email(email: str) -> str:
    """Canonical email key for cross-server dedup."""
    return (email or "").strip().lower()


def get_schema_version(conn: sqlite3.Connection) -> int:
    if not table_exists(conn, "_mcp_meta"):
        return 0
    row = conn.execute("SELECT value FROM _mcp_meta WHERE key='schema_version'").fetchone()
    try:
        return int(row[0]) if row else 0
    except (TypeError, ValueError):
        return 0
