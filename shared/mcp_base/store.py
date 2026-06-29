"""A thin SQLite wrapper most servers build on.

Handles connection setup, migrations, and dict-returning queries so the
tracker/CRUD servers stay a few lines each.
"""
from __future__ import annotations

import sqlite3
import threading
from pathlib import Path
from typing import Any, Iterable, Sequence


class BaseStore:
    """SQLite helper. Rows come back as plain dicts; WAL mode for safe concurrent reads.

    The connection is opened with check_same_thread=False so it can be shared across worker
    threads (e.g. parallel verify/find). A single sqlite3.Connection is NOT safe to use from
    multiple threads concurrently — doing so is undefined behavior and can hard-crash (segfault)
    the process — so every operation is serialized through a re-entrant lock. SQLite ops are
    fast, so the contention cost is negligible; the safety is essential."""

    def __init__(self, path: str | Path, schema: str | None = None):
        self.path = str(path)
        self._lock = threading.RLock()
        self._conn = sqlite3.connect(self.path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA journal_mode=WAL;")
        self._conn.execute("PRAGMA foreign_keys=ON;")
        if schema:
            self.migrate(schema)

    def migrate(self, schema_sql: str) -> None:
        """Run one or more CREATE statements (idempotent if they use IF NOT EXISTS)."""
        with self._lock:
            self._conn.executescript(schema_sql)
            self._conn.commit()

    def execute(self, sql: str, params: Sequence[Any] = ()) -> int:
        """Run a write; return lastrowid."""
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            return cur.lastrowid

    def executemany(self, sql: str, seq: Iterable[Sequence[Any]]) -> None:
        with self._lock:
            self._conn.executemany(sql, seq)
            self._conn.commit()

    def query(self, sql: str, params: Sequence[Any] = ()) -> list[dict]:
        with self._lock:
            cur = self._conn.execute(sql, params)
            return [dict(r) for r in cur.fetchall()]

    def query_one(self, sql: str, params: Sequence[Any] = ()) -> dict | None:
        with self._lock:
            cur = self._conn.execute(sql, params)
            row = cur.fetchone()
            return dict(row) if row else None

    def close(self) -> None:
        with self._lock:
            self._conn.close()
