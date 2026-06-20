"""Offline test for daily-digest: seed a couple of source DBs into a temp MCP_DATA_DIR
and assert today() aggregates them with correct sources/urgency. No network/credentials."""
import asyncio
import os
import sqlite3
import sys
import tempfile
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _iso(dt):
    return dt.replace(microsecond=0).isoformat()


def _seed(base: Path):
    """Create realistic source schemas + a few due/overdue rows in <base>/<server>/store.db."""
    now = datetime.now(timezone.utc)
    today = date.today().isoformat()
    overdue = _iso(now - timedelta(days=2))
    soon = _iso(now + timedelta(days=1))

    # --- task-manager ---
    tm = base / "task-manager"
    tm.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(tm / "store.db")
    c.executescript(
        "CREATE TABLE tasks(id INTEGER PRIMARY KEY, title TEXT, priority TEXT DEFAULT 'med', "
        "due TEXT, status TEXT DEFAULT 'open', notes TEXT DEFAULT '', created_at TEXT);"
    )
    c.execute("INSERT INTO tasks(title, due, status) VALUES(?,?,?)", ("Overdue task", overdue, "open"))
    c.execute("INSERT INTO tasks(title, due, status) VALUES(?,?,?)", ("Done task", overdue, "done"))
    c.execute("INSERT INTO tasks(title, due, status) VALUES(?,?,?)", ("No-due open task", None, "open"))
    c.commit()
    c.close()

    # --- habit-tracker ---
    ht = base / "habit-tracker"
    ht.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(ht / "store.db")
    c.executescript(
        "CREATE TABLE habits(id INTEGER PRIMARY KEY, name TEXT UNIQUE, cadence TEXT DEFAULT 'daily', "
        "target INTEGER DEFAULT 1, status TEXT DEFAULT 'active', created_at TEXT);"
        "CREATE TABLE checkins(id INTEGER PRIMARY KEY, habit_id INTEGER, day TEXT, "
        "count INTEGER DEFAULT 1, note TEXT DEFAULT '', UNIQUE(habit_id, day));"
    )
    c.execute("INSERT INTO habits(name, status) VALUES(?,?)", ("Meditate", "active"))
    c.execute("INSERT INTO habits(name, status) VALUES(?,?)", ("Read", "active"))
    c.execute("INSERT INTO habits(name, status) VALUES(?,?)", ("Paused habit", "paused"))
    # "Read" already checked in today -> should NOT surface; "Meditate" should.
    c.execute("INSERT INTO checkins(habit_id, day) VALUES(?,?)", (2, today))
    c.commit()
    c.close()

    # --- interview-prep (cards due) ---
    ip = base / "interview-prep"
    ip.mkdir(parents=True, exist_ok=True)
    c = sqlite3.connect(ip / "store.db")
    c.executescript(
        "CREATE TABLE cards(id INTEGER PRIMARY KEY, front TEXT, back TEXT, topic TEXT DEFAULT '', "
        "ease REAL DEFAULT 2.5, interval_days INTEGER DEFAULT 0, reps INTEGER DEFAULT 0, "
        "due_at TEXT, created_at TEXT);"
    )
    c.execute("INSERT INTO cards(front, back, topic, due_at) VALUES(?,?,?,?)",
              ("What is a B-tree?", "balanced tree", "db", overdue))
    c.execute("INSERT INTO cards(front, back, topic, due_at) VALUES(?,?,?,?)",
              ("Not due yet", "x", "db", soon))
    c.commit()
    c.close()

    # jobtrack / reachout / learn-tracker intentionally absent -> must degrade gracefully.
    return overdue


async def run():
    tmp = tempfile.mkdtemp(prefix="daily-digest-test-")
    base = Path(tmp)
    os.environ["MCP_DATA_DIR"] = str(base)

    _seed(base)

    sys.path.insert(0, str(ROOT / "servers" / "daily-digest"))
    # Reload mcp_base config so base_data_dir() picks up MCP_DATA_DIR (it reads env at call time,
    # so no reload needed — but import server fresh).
    import server  # noqa
    from fastmcp import Client

    async with Client(server.mcp) as c:
        # health
        h = await c.call_tool("health", {})
        assert h.data["ok"] and h.data["server"] == "daily-digest"

        t = await c.call_tool("today", {})
        d = t.data
        assert d["ok"] is True
        sources = {it["source"] for it in d["items"]}
        # The three seeded sources must all appear; the three absent ones must not error.
        assert "task-manager" in sources, sources
        assert "habit-tracker" in sources, sources
        assert "interview-prep" in sources, sources
        assert "jobtrack" not in sources and "reachout" not in sources, sources

        # task-manager: overdue + no-due open = 2; done excluded
        assert d["counts"].get("task-manager") == 2, d["counts"]
        # habit-tracker: only Meditate (Read checked in, Paused inactive) = 1
        assert d["counts"].get("habit-tracker") == 1, d["counts"]
        # interview-prep: only the overdue card = 1
        assert d["counts"].get("interview-prep") == 1, d["counts"]
        assert d["total"] == 4, d["total"]

        # Ranking: an overdue item must come before the "open" no-due task.
        urgencies = [it["urgency"] for it in d["items"]]
        assert urgencies[0] == "overdue", urgencies
        assert "open" in urgencies and urgencies.index("overdue") < urgencies.index("open")

        # whats_due limit
        wd = await c.call_tool("whats_due", {"limit": 2})
        assert wd.data["returned"] == 2 and wd.data["total"] == 4

        # counts_by_source lists all sources + availability flags
        cbs = await c.call_tool("counts_by_source", {})
        assert set(cbs.data["counts"].keys()) == {
            "task-manager", "jobtrack", "reachout",
            "learn-tracker", "habit-tracker", "interview-prep",
        }
        assert cbs.data["available"]["task-manager"] is True
        assert cbs.data["available"]["jobtrack"] is False

        # per-source helper: drilling into an absent source degrades, not errors
        src = await c.call_tool("source", {"name": "jobtrack"})
        assert src.data["ok"] is True and src.data["available"] is False and src.data["total"] == 0
        src_tm = await c.call_tool("source", {"name": "task-manager"})
        assert src_tm.data["total"] == 2

        # unknown source -> graceful error envelope
        bad = await c.call_tool("source", {"name": "nope"})
        assert bad.data["ok"] is False

    del sys.modules["server"]
    print("daily-digest OK — aggregated", d["total"], "items from",
          len(sources), "live sources:", sorted(sources))
    print("DAILY-DIGEST (offline) OK")


asyncio.run(run())
