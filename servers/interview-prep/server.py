"""interview-prep — spaced-repetition flashcards (SM-2 lite + optional FSRS) + a DSA problem tracker
with leetcode-pattern coverage and a mock-interview scheduler (SQLite). All offline; calendar payloads
are emitted as plain dicts to hand to the google_workspace server (this server stays independent)."""
from __future__ import annotations

import math
from datetime import datetime, timedelta, timezone

from mcp_base import BaseStore, db_path, make_server, not_found, semantic

mcp = make_server(
    "interview-prep",
    instructions=("SRS flashcards (add_card/due_cards/review, optional review_fsrs) + decks, a DSA tracker "
                  "(add_problem/track_attempt/tag_problem/pattern_coverage), and a mock-interview scheduler."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS cards(
  id INTEGER PRIMARY KEY, front TEXT, back TEXT, topic TEXT DEFAULT '',
  ease REAL DEFAULT 2.5, interval_days INTEGER DEFAULT 0, reps INTEGER DEFAULT 0,
  due_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS problems(
  id INTEGER PRIMARY KEY, title TEXT, url TEXT DEFAULT '', difficulty TEXT DEFAULT '',
  topic TEXT DEFAULT '', status TEXT DEFAULT 'todo', attempts INTEGER DEFAULT 0, created_at TEXT
);
CREATE TABLE IF NOT EXISTS mock_interviews(
  id INTEGER PRIMARY KEY, topic TEXT, kind TEXT DEFAULT 'technical', when_at TEXT,
  duration_min INTEGER DEFAULT 60, status TEXT DEFAULT 'scheduled', score INTEGER, notes TEXT DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS settings(key TEXT PRIMARY KEY, value TEXT);
"""
store = BaseStore(db_path("interview-prep"), schema=SCHEMA)


def _now():
    return datetime.now(timezone.utc)


def _recent_ids(table: str, limit: int = 10) -> list[int]:
    """Recent ids for a table, newest first — handed to not_found() so a weak agent can recover."""
    return [r["id"] for r in store.query(
        f"SELECT id FROM {table} ORDER BY id DESC LIMIT ?", (limit,))]


def _ensure_cols(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


# Additive migrations (safe on existing on-disk DBs).
_ensure_cols("cards", {
    "deck": "TEXT DEFAULT ''",
    "stability": "REAL DEFAULT 0",
    "fsrs_difficulty": "REAL DEFAULT 0",
    "state": "TEXT DEFAULT 'new'",
    "last_review": "TEXT",
    "lapses": "INTEGER DEFAULT 0",
})
_ensure_cols("problems", {
    "pattern": "TEXT DEFAULT ''",
    "due_at": "TEXT",
    "interval_days": "INTEGER DEFAULT 0",
    "solved_at": "TEXT",
})

# Sidecar vector tables for semantic search (hybrid keyword+vector). Sit alongside the
# parent rows; populated on write, dropped on delete. No-op when no embedding model is present.
store.migrate(semantic.vec_table_sql("iprep_problems_vec"))
store.migrate(semantic.vec_table_sql("iprep_cards_vec"))


def _reindex_problem(pid: int) -> None:
    """(Re)embed a problem from its title/pattern/topic/difficulty. Never raises."""
    r = store.query_one("SELECT title,pattern,topic,difficulty FROM problems WHERE id=?", (pid,))
    if r:
        txt = " ".join(str(r.get(k) or "") for k in ("title", "pattern", "topic", "difficulty"))
        semantic.index_row(store, "iprep_problems_vec", pid, txt)


def _reindex_card(cid: int) -> None:
    """(Re)embed a flashcard from its front/back/topic/deck. Never raises."""
    r = store.query_one("SELECT front,back,topic,deck FROM cards WHERE id=?", (cid,))
    if r:
        txt = " ".join(str(r.get(k) or "") for k in ("front", "back", "topic", "deck"))
        semantic.index_row(store, "iprep_cards_vec", cid, txt)

KNOWN_PATTERNS = [
    "two-pointers", "sliding-window", "binary-search", "bfs", "dfs", "backtracking",
    "dynamic-programming", "greedy", "heap", "stack", "linked-list", "tree", "trie",
    "graph", "union-find", "bit-manipulation", "math", "hashmap", "intervals",
    "monotonic-stack", "topological-sort", "prefix-sum", "divide-conquer", "design",
]
DIFFICULTIES = {"easy", "medium", "hard"}


def _get_setting(key: str, default: str) -> str:
    row = store.query_one("SELECT value FROM settings WHERE key=?", (key,))
    return row["value"] if row else default


# ---------------- Flashcards: SM-2 (original, preserved) ----------------
@mcp.tool
def add_card(front: str, back: str, topic: str = "", deck: str = "") -> dict:
    """Add a flashcard (due immediately). Optionally assign a deck."""
    return _add_card(front, back, topic, deck)


def _add_card(front: str, back: str, topic: str = "", deck: str = "") -> dict:
    front, back = (front or "").strip(), (back or "").strip()
    if not front or not back:
        return {"error": "front and back are required"}
    n = _now().isoformat()
    cid = store.execute("INSERT INTO cards(front,back,topic,deck,due_at,created_at,state) VALUES(?,?,?,?,?,?,'new')",
                        (front, back, topic.strip(), deck.strip(), n, n))
    _reindex_card(cid)
    return {"id": cid}


@mcp.tool
def add_cards(items: list[dict]) -> dict:
    """Bulk-add flashcards. Each item: {front, back, topic?, deck?}. Returns created ids."""
    ids = []
    for it in items or []:
        if not isinstance(it, dict):
            continue
        r = _add_card(it.get("front", ""), it.get("back", ""), it.get("topic", ""), it.get("deck", ""))
        if "id" in r:
            ids.append(r["id"])
    return {"created": len(ids), "ids": ids}


@mcp.tool
def due_cards(limit: int = 20, deck: str = "", topic: str = "") -> list[dict]:
    """Cards due for review now, optionally filtered by deck or topic."""
    sql = "SELECT id,front,back,topic,deck,reps,interval_days,state FROM cards WHERE due_at<=?"
    params: list = [_now().isoformat()]
    if deck:
        sql += " AND deck=?"; params.append(deck)
    if topic:
        sql += " AND topic=?"; params.append(topic)
    sql += " ORDER BY due_at ASC LIMIT ?"; params.append(max(1, limit))
    return store.query(sql, params)


@mcp.tool
def review(card_id: int, grade: int) -> dict:
    """Grade a card 0-5 (SM-2 lite). <3 resets; else interval grows by ease. Schedules next due date."""
    try:
        grade = max(0, min(5, int(grade)))
    except (ValueError, TypeError):
        return {"error": "grade must be an integer 0-5"}
    card = store.query_one("SELECT ease,interval_days,reps,lapses FROM cards WHERE id=?", (card_id,))
    if not card:
        return not_found("card", card_id, available=_recent_ids("cards"), hint="use due_cards()")
    ease = max(1.3, card["ease"] + (0.1 - (5 - grade) * (0.08 + (5 - grade) * 0.02)))
    lapses = card["lapses"] or 0
    if grade < 3:
        interval, reps = 1, 0
        lapses += 1
    else:
        reps = card["reps"] + 1
        interval = 1 if reps == 1 else (6 if reps == 2 else round(card["interval_days"] * ease))
    due = (_now() + timedelta(days=max(1, interval))).isoformat()
    store.execute("UPDATE cards SET ease=?, interval_days=?, reps=?, due_at=?, last_review=?, lapses=?, "
                  "state=CASE WHEN ?<3 THEN 'relearning' ELSE 'review' END WHERE id=?",
                  (round(ease, 2), interval, reps, due, _now().isoformat(), lapses, grade, card_id))
    return {"id": card_id, "next_due": due, "interval_days": interval, "ease": round(ease, 2)}


# ---------------- Flashcards: FSRS-lite (opt-in, self-contained, no deps) ----------------
# Default FSRS-4.5 weights from open-spaced-repetition (public domain). Inlined; no network/dep.
_FSRS_W = [0.4072, 1.1829, 3.1262, 15.4722, 7.2102, 0.5316, 1.0651, 0.0234, 1.616, 0.1544,
           1.0824, 1.9813, 0.0953, 0.2975, 2.2042, 0.2407, 2.9466, 0.5034, 0.6567]
_RATINGS = {"again": 1, "hard": 2, "good": 3, "easy": 4}


def _fsrs_retention() -> float:
    try:
        r = float(_get_setting("fsrs_retention", "0.9"))
        return min(0.97, max(0.7, r))
    except ValueError:
        return 0.9


def _fsrs_interval(stability: float) -> int:
    r = _fsrs_retention()
    factor = 19.0 / 81.0
    days = (stability / factor) * (r ** (1 / -0.5) - 1)
    return max(1, round(days))


def _fsrs_update(rating: int, S: float, D: float, is_new: bool, elapsed_days: float) -> tuple[float, float]:
    w = _FSRS_W
    if is_new or S <= 0:
        S = w[rating - 1]
        D = w[4] - (rating - 3) * w[5]
    else:
        R = 0.9 if elapsed_days <= 0 else (1 + (19 / 81) * (elapsed_days / S)) ** -0.5
        D = D - w[6] * (rating - 3)
        D = w[7] * (w[4] - w[5] * 2) + (1 - w[7]) * D  # mean reversion (approx)
        if rating == 1:  # again
            S = w[11] * (D ** -w[12]) * ((S + 1) ** w[13] - 1) * math.exp(w[14] * (1 - R))
        else:
            hard_pen = w[15] if rating == 2 else 1.0
            easy_b = w[16] if rating == 4 else 1.0
            S = S * (1 + math.exp(w[8]) * (11 - D) * (S ** -w[9]) *
                     (math.exp(w[10] * (1 - R)) - 1) * hard_pen * easy_b)
    D = min(10.0, max(1.0, D))
    S = max(0.1, S)
    return S, D


@mcp.tool
def review_fsrs(card_id: int, rating: str) -> dict:
    """Review a card with FSRS (rating: again/hard/good/easy). Modern memory model; opt-in alternative
    to `review`. Tune target retention with set_retention()."""
    r = _RATINGS.get((rating or "").strip().lower())
    if not r:
        return {"error": "rating must be one of: again, hard, good, easy"}
    card = store.query_one("SELECT stability,fsrs_difficulty,state,last_review,lapses FROM cards WHERE id=?", (card_id,))
    if not card:
        return not_found("card", card_id, available=_recent_ids("cards"), hint="use due_cards()")
    is_new = (card["state"] or "new") == "new" or not card["stability"]
    elapsed = 0.0
    if card["last_review"]:
        try:
            elapsed = (_now() - datetime.fromisoformat(card["last_review"])).total_seconds() / 86400
        except ValueError:
            elapsed = 0.0
    S, D = _fsrs_update(r, card["stability"] or 0.0, card["fsrs_difficulty"] or 0.0, is_new, elapsed)
    interval = _fsrs_interval(S)
    due = (_now() + timedelta(days=interval)).isoformat()
    lapses = (card["lapses"] or 0) + (1 if r == 1 else 0)
    store.execute("UPDATE cards SET stability=?, fsrs_difficulty=?, state='review', last_review=?, "
                  "due_at=?, interval_days=?, lapses=? WHERE id=?",
                  (round(S, 4), round(D, 4), _now().isoformat(), due, interval, lapses, card_id))
    return {"id": card_id, "next_due": due, "interval_days": interval,
            "stability": round(S, 2), "difficulty": round(D, 2), "target_retention": _fsrs_retention()}


@mcp.tool
def set_retention(target: float) -> dict:
    """Set desired FSRS retention (0.70-0.97; default 0.90). Higher = more frequent reviews."""
    try:
        t = min(0.97, max(0.7, float(target)))
    except (ValueError, TypeError):
        return {"error": "target must be a number between 0.70 and 0.97"}
    store.execute("INSERT INTO settings(key,value) VALUES('fsrs_retention',?) "
                  "ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(t),))
    return {"ok": True, "fsrs_retention": t}


# ---------------- Decks ----------------
@mcp.tool
def list_decks() -> list[dict]:
    """List decks with card counts and how many are due now."""
    return store.query(
        "SELECT COALESCE(NULLIF(deck,''),'(none)') AS deck, COUNT(*) AS cards, "
        "SUM(CASE WHEN due_at<=? THEN 1 ELSE 0 END) AS due "
        "FROM cards GROUP BY COALESCE(NULLIF(deck,''),'(none)') ORDER BY cards DESC",
        (_now().isoformat(),))


@mcp.tool
def move_card(card_id: int, deck: str) -> dict:
    """Move a card to a deck."""
    if not store.query_one("SELECT id FROM cards WHERE id=?", (card_id,)):
        return not_found("card", card_id, available=_recent_ids("cards"), hint="use due_cards()")
    store.execute("UPDATE cards SET deck=? WHERE id=?", (deck.strip(), card_id))
    return {"ok": True, "id": card_id, "deck": deck.strip()}


# ---------------- DSA problems ----------------
@mcp.tool
def add_problem(title: str, difficulty: str = "", topic: str = "", url: str = "", pattern: str = "") -> dict:
    """Add a DSA problem to practice. `pattern` is a leetcode pattern tag (e.g. sliding-window)."""
    title = (title or "").strip()
    if not title:
        return {"error": "title is required"}
    diff = difficulty.strip().lower()
    if diff and diff not in DIFFICULTIES:
        diff = difficulty.strip()
    pid = store.execute("INSERT INTO problems(title,url,difficulty,topic,pattern,created_at) VALUES(?,?,?,?,?,?)",
                        (title, url, diff, topic, pattern.strip().lower(), _now().isoformat()))
    _reindex_problem(pid)
    return {"id": pid, "title": title}


@mcp.tool
def track_attempt(problem_id: int, solved: bool, review_in_days: int = 0) -> dict:
    """Record an attempt; marks solved/attempted. If solved and review_in_days>0, schedules a spaced redo."""
    if not store.query_one("SELECT id FROM problems WHERE id=?", (problem_id,)):
        return not_found("problem", problem_id, available=_recent_ids("problems"),
                         hint="use list_problems()")
    due = solved_at = None
    if solved:
        solved_at = _now().isoformat()
        if review_in_days > 0:
            due = (_now() + timedelta(days=review_in_days)).isoformat()
    store.execute("UPDATE problems SET attempts=attempts+1, status=?, solved_at=COALESCE(?,solved_at), "
                  "due_at=?, interval_days=? WHERE id=?",
                  ("solved" if solved else "attempted", solved_at, due, max(0, review_in_days), problem_id))
    return {"ok": True, "id": problem_id, "solved": solved, "review_due": due}


@mcp.tool
def tag_problem(problem_id: int, pattern: str = "", difficulty: str = "", topic: str = "") -> dict:
    """Set/update a problem's pattern, difficulty, and/or topic."""
    p = store.query_one("SELECT id FROM problems WHERE id=?", (problem_id,))
    if not p:
        return not_found("problem", problem_id, available=_recent_ids("problems"),
                         hint="use list_problems()")
    sets, params = [], []
    if pattern:
        sets.append("pattern=?"); params.append(pattern.strip().lower())
    if difficulty:
        sets.append("difficulty=?"); params.append(difficulty.strip().lower())
    if topic:
        sets.append("topic=?"); params.append(topic.strip())
    if not sets:
        return {"error": "nothing to update"}
    params.append(problem_id)
    store.execute(f"UPDATE problems SET {','.join(sets)} WHERE id=?", params)
    _reindex_problem(problem_id)
    return {"ok": True, "id": problem_id}


@mcp.tool
def list_problems(status: str = "", pattern: str = "", difficulty: str = "", limit: int = 50) -> list[dict]:
    """List problems, optionally filtered by status (todo/attempted/solved), pattern, and/or difficulty."""
    sql = "SELECT id,title,difficulty,topic,pattern,status,attempts FROM problems WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"; params.append(status)
    if pattern:
        sql += " AND pattern=?"; params.append(pattern.strip().lower())
    if difficulty:
        sql += " AND difficulty=?"; params.append(difficulty.strip().lower())
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(max(1, limit))
    return store.query(sql, params)


def _problems_by_pattern() -> list[dict]:
    return store.query(
        "SELECT COALESCE(NULLIF(pattern,''),'(untagged)') AS pattern, COUNT(*) AS total, "
        "SUM(CASE WHEN status='solved' THEN 1 ELSE 0 END) AS solved "
        "FROM problems GROUP BY COALESCE(NULLIF(pattern,''),'(untagged)') ORDER BY total DESC")


@mcp.tool
def problems_by_pattern() -> list[dict]:
    """Count of problems grouped by leetcode pattern, with solved counts."""
    return _problems_by_pattern()


def _pattern_coverage() -> dict:
    rows = {r["pattern"]: r for r in _problems_by_pattern()}
    covered, gaps = [], []
    for p in KNOWN_PATTERNS:
        r = rows.get(p)
        if r and r["total"]:
            covered.append({"pattern": p, "total": r["total"], "solved": r["solved"]})
        else:
            gaps.append(p)
    return {"covered": covered, "gaps": gaps,
            "coverage_pct": round(100 * len(covered) / len(KNOWN_PATTERNS), 1)}


@mcp.tool
def pattern_coverage() -> dict:
    """Coverage report across the canonical leetcode patterns: which you've practiced vs. gaps."""
    return _pattern_coverage()


@mcp.tool
def problems_due(limit: int = 20) -> list[dict]:
    """Solved problems scheduled for a spaced redo whose review date has arrived."""
    return store.query("SELECT id,title,difficulty,pattern,due_at FROM problems "
                       "WHERE due_at IS NOT NULL AND due_at<=? ORDER BY due_at ASC LIMIT ?",
                       (_now().isoformat(), max(1, limit)))


# ---------------- Semantic search over problems ----------------
def _problem_search_text(problem_id: int) -> str:
    """Build the query text for a stored problem id (used when a problem id is passed in)."""
    r = store.query_one("SELECT title,pattern,topic,difficulty FROM problems WHERE id=?", (problem_id,))
    if not r:
        return ""
    return " ".join(str(r.get(k) or "") for k in ("title", "pattern", "topic", "difficulty"))


@mcp.tool
def find_similar_problems(query_or_problem_id, limit: int = 10) -> list[dict]:
    """Find DSA problems similar to a free-text query OR to an existing problem (pass its id).
    Hybrid: keyword (LIKE over title/pattern/topic) fused with semantic vector similarity, so
    'find the longest substring without repeats' surfaces sliding-window problems even without
    the exact words. Keyword-only fallback when no embedding model is installed. When a problem
    id is given, that problem itself is excluded from the results."""
    exclude_id = None
    q = ""
    if isinstance(query_or_problem_id, bool):
        # bool is a subclass of int — treat as bad input rather than a row id.
        return []
    if isinstance(query_or_problem_id, int):
        exclude_id = query_or_problem_id
        q = _problem_search_text(query_or_problem_id)
        if not q:
            return [not_found("problem", query_or_problem_id, available=_recent_ids("problems"),
                              hint="use list_problems()")]
    else:
        try:
            q = str(query_or_problem_id or "").strip()
        except Exception:
            q = ""
        # A bare numeric string is treated as a problem id, mirroring weak-agent calling.
        if q.isdigit():
            exclude_id = int(q)
            txt = _problem_search_text(exclude_id)
            if not txt:
                return [not_found("problem", exclude_id, available=_recent_ids("problems"),
                                  hint="use list_problems()")]
            q = txt
    if not q:
        return []
    like = f"%{q}%"
    kw = [r["id"] for r in store.query(
        "SELECT id FROM problems WHERE title LIKE ? OR pattern LIKE ? OR topic LIKE ? "
        "ORDER BY created_at DESC LIMIT 50", (like, like, like))]
    vec = [rid for rid, _ in semantic.vector_hits(store, "iprep_problems_vec", q, limit=50)]
    ids = semantic.rrf(kw, vec, max(1, limit) + (1 if exclude_id else 0)) if vec \
        else kw[:max(1, limit) + (1 if exclude_id else 0)]
    if exclude_id is not None:
        ids = [i for i in ids if i != exclude_id]
    ids = ids[:max(1, limit)]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in store.query(
        f"SELECT id,title,difficulty,topic,pattern,status,attempts FROM problems WHERE id IN ({ph})",
        tuple(ids))}
    return [rows[i] for i in ids if i in rows]


@mcp.tool
def reindex_semantic() -> dict:
    """(Re)build semantic embeddings for all problems and flashcards. Needs a local embedding
    model (uv sync --group embed). Safe to call repeatedly; degrades gracefully if no model."""
    if not semantic.available():
        return {"ok": False, "engine": "unavailable", "hint": "uv sync --group embed then call again"}
    n_p = n_c = 0
    for r in store.query("SELECT id FROM problems"):
        _reindex_problem(r["id"])
        n_p += 1
    for r in store.query("SELECT id FROM cards"):
        _reindex_card(r["id"])
        n_c += 1
    return {"ok": True, "indexed": n_p + n_c, "problems": n_p, "cards": n_c}


# ---------------- Mock interviews ----------------
@mcp.tool
def schedule_mock(topic: str, when_at: str, duration_min: int = 60, kind: str = "technical") -> dict:
    """Schedule a mock interview. `when_at` is an ISO datetime. kind: technical/behavioral/system-design."""
    topic = (topic or "").strip()
    if not topic:
        return {"error": "topic is required"}
    mid = store.execute("INSERT INTO mock_interviews(topic,kind,when_at,duration_min,created_at) VALUES(?,?,?,?,?)",
                        (topic, kind.strip() or "technical", when_at, max(5, duration_min), _now().isoformat()))
    return {"id": mid, "topic": topic, "when_at": when_at}


@mcp.tool
def upcoming_mocks(limit: int = 20) -> list[dict]:
    """Upcoming (scheduled) mock interviews, soonest first."""
    return store.query("SELECT id,topic,kind,when_at,duration_min FROM mock_interviews "
                       "WHERE status='scheduled' AND when_at>=? ORDER BY when_at ASC LIMIT ?",
                       (_now().isoformat(), max(1, limit)))


@mcp.tool
def complete_mock(mock_id: int, score: int = 0, notes: str = "") -> dict:
    """Mark a mock interview done with an optional score (0-100) and notes."""
    if not store.query_one("SELECT id FROM mock_interviews WHERE id=?", (mock_id,)):
        return not_found("mock interview", mock_id, available=_recent_ids("mock_interviews"),
                         hint="use upcoming_mocks()")
    try:
        score = max(0, min(100, int(score)))
    except (ValueError, TypeError):
        return {"error": "score must be an integer 0-100"}
    store.execute("UPDATE mock_interviews SET status='done', score=?, notes=? WHERE id=?",
                  (score, notes, mock_id))
    return {"ok": True, "id": mock_id}


@mcp.tool
def mock_calendar_payload(mock_id: int) -> dict:
    """Return a Google Calendar event payload for a mock interview (pass to google_workspace.create_event)."""
    m = store.query_one("SELECT topic,kind,when_at,duration_min FROM mock_interviews WHERE id=?", (mock_id,))
    if not m:
        return not_found("mock interview", mock_id, available=_recent_ids("mock_interviews"),
                         hint="use upcoming_mocks()")
    return {"summary": f"Mock interview: {m['topic']} ({m['kind']})",
            "start": m["when_at"], "duration_min": m["duration_min"],
            "note": "Pass to google_workspace.create_event (this server stays independent)."}


# ---------------- Stats ----------------
@mcp.tool
def stats() -> dict:
    """Cards due + problems solved/total + mocks (preserved keys, extended)."""
    one = lambda q, p=(): store.query_one(q, p)["n"]
    n = _now().isoformat()
    by_diff = {r["difficulty"] or "(none)": r["solved"] for r in store.query(
        "SELECT difficulty, SUM(CASE WHEN status='solved' THEN 1 ELSE 0 END) AS solved "
        "FROM problems GROUP BY difficulty")}
    return {
        "cards_total": one("SELECT COUNT(*) AS n FROM cards"),
        "cards_due": one("SELECT COUNT(*) AS n FROM cards WHERE due_at<=?", (n,)),
        "problems_solved": one("SELECT COUNT(*) AS n FROM problems WHERE status='solved'"),
        "problems_total": one("SELECT COUNT(*) AS n FROM problems"),
        "problems_due": one("SELECT COUNT(*) AS n FROM problems WHERE due_at IS NOT NULL AND due_at<=?", (n,)),
        "solved_by_difficulty": by_diff,
        "mocks_upcoming": one("SELECT COUNT(*) AS n FROM mock_interviews WHERE status='scheduled' AND when_at>=?", (n,)),
        "pattern_coverage_pct": _pattern_coverage()["coverage_pct"],
    }


@mcp.tool
def review_forecast(days: int = 7) -> list[dict]:
    """How many cards become due on each of the next N days."""
    out = []
    for d in range(max(1, days)):
        day = (_now() + timedelta(days=d)).date().isoformat()
        nxt = (_now() + timedelta(days=d + 1)).date().isoformat()
        lo = "0000" if d == 0 else day  # count overdue into day 0
        n = store.query_one("SELECT COUNT(*) AS n FROM cards WHERE due_at>=? AND due_at<?",
                            (lo, nxt))["n"]
        out.append({"date": day, "due": n})
    return out


if __name__ == "__main__":
    mcp.run()
