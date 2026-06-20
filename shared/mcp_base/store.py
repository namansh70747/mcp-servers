"""A thin SQLite wrapper most servers build on.

Handles connection setup, migrations, and dict-returning queries so the
tracker/CRUD servers stay a few lines each.
"""
from __future__ import annotations

import sqlite3
from pathlib import Path
from typing import Any, Iterable, Sequence


class BaseStore:
    """SQLite helper. Rows come back as plain dicts; WAL mode for safe concurrent reads."""

    def __init__(self, path: str | Path, schema: str | None = None):
        self.path = str(path)
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        if schema:
            self.migrate(schema)

    def migrate(self, schema_sql: str) -> None:
        """Run one or more CREATE statements (idempotent if they use IF NOT EXISTS)."""
        self._conn.executescript(schema_sql)
        self._conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write; return lastrowid."""
        cur = self._conn.execute(sql, params)
        self._conn.commit()
        return cur.lastrowid

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        self._conn.executemany(sql, seq)
        self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        cur = self._conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        cur = self._conn.execute(sql, params)
        row = cur.fetchone()
        return dict(row) if row else None

    def close(self) -> None:
        self._conn.close()
