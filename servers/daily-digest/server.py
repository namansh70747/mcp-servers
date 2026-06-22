"""daily-digest — the connective tissue of the suite.

Read-only aggregation of "what needs action today" across other servers. It opens each
server's shared SQLite DB at ~/.mcp-suite/<server>/store.db in READ-ONLY mode and degrades
gracefully when a DB file, table, or column is absent (a missing source simply contributes
nothing — it never errors the whole digest).

Sources & their "needs action" signals:
  - task-manager   tasks that are open and due/overdue
  - jobtrack       applications with a next_followup date (due/overdue)
  - reachout       sent outreach with no reply yet, follow-up window elapsed, not snoozed
  - learn-tracker  courses with a review_at / deadline that has arrived
  - habit-tracker  active habits not yet checked in today
  - interview-prep flashcards whose spaced-repetition due_at has arrived

Tools: today(), whats_due(limit=50), counts_by_source(), source(name, limit=50).
All read-only — this server writes nothing to any source DB.
"""
from __future__ import annotations

import sqlite3
from datetime import date, datetime, timezone

from mcp_base import base_data_dir, err, get_logger, make_server, not_found, ok

log = get_logger("daily-digest")

mcp = make_server(
    "daily-digest",
    instructions=(
        "Cross-server read-only digest of what needs action today. today() and whats_due() "
        "return a ranked, sourced list of items ({source, item, due, urgency}); "
        "counts_by_source() summarizes; source(name) drills into one server. Missing source "
        "DBs/tables are skipped gracefully."
    ),
)

# Source server name -> the SQLite db file lives at ~/.mcp-suite/<server>/store.db
SOURCES = (
    "task-manager",
    "jobtrack",
    "reachout",
    "learn-tracker",
    "habit-tracker",
    "interview-prep",
)

# Urgency ranking (lower sorts first / is more urgent).
_URGENCY_ORDER = {"overdue": 0, "due_today": 1, "due_soon": 2, "due": 2, "open": 3}


# ---------------- time helpers ----------------
def _now() -> datetime:
    return datetime.now(timezone.utc)


def _today_str() -> str:
    return date.today().isoformat()


def _parse_dt(val) -> datetime | None:
    """Best-effort parse of an ISO date/datetime string into an aware UTC datetime."""
    if not val or not isinstance(val, str):
        return None
    raw = val.strip()
    if not raw:
        return None
    try:
        dt = datetime.fromisoformat(raw.replace("Z", "+00:00"))
    except ValueError:
        # bare date like '2026-06-19'
        try:
            dt = datetime.fromisoformat(raw[:10])
        except ValueError:
            return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _urgency_for(due_val) -> str:
    """Classify a due timestamp relative to now: overdue / due_today / due_soon."""
    dt = _parse_dt(due_val)
    if dt is None:
        return "open"
    now = _now()
    if dt < now:
        # overdue unless it's still today's date
        if dt.date() == now.date():
            return "due_today"
        return "overdue"
    if dt.date() == now.date():
        return "due_today"
    delta = (dt.date() - now.date()).days
    return "due_soon" if delta <= 3 else "due"


# ---------------- read-only db access ----------------
def _connect_ro(server: str) -> sqlite3.Connection | None:
    """Open a source server's store.db READ-ONLY. Returns None if it doesn't exist."""
    path = base_data_dir() / server / "store.db"
    if not path.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        conn.row_factory = sqlite3.Row
        return conn
    except sqlite3.Error as exc:  # noqa: BLE001
        log.warning("daily-digest: cannot open %s read-only: %s", path, exc)
        return None


def _table_exists(conn: sqlite3.Connection, table: str) -> bool:
    try:
        row = conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table' AND name=?", (table,)
        ).fetchone()
        return row is not None
    except sqlite3.Error:
        return False


def _columns(conn: sqlite3.Connection, table: str) -> set[str]:
    try:
        return {r["name"] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    except sqlite3.Error:
        return set()


def _safe_rows(conn: sqlite3.Connection, sql: str, params=()) -> list[sqlite3.Row]:
    try:
        return list(conn.execute(sql, params).fetchall())
    except sqlite3.Error as exc:  # noqa: BLE001
        log.warning("daily-digest: query failed: %s", exc)
        return []


def _item(source: str, item: str, due, *, urgency: str | None = None, ref=None) -> dict:
    out = {
        "source": source,
        "item": item,
        "due": due,
        "urgency": urgency if urgency is not None else _urgency_for(due),
    }
    if ref is not None:
        out["ref_id"] = ref
    return out


# ---------------- per-source collectors ----------------
# Each returns a list of digest items, or [] if the source is absent / has nothing due.
# They never raise: a malformed/absent source contributes nothing.

def _collect_task_manager() -> list[dict]:
    conn = _connect_ro("task-manager")
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "tasks"):
            return []
        cols = _columns(conn, "tasks")
        if "title" not in cols:
            return []
        sql = "SELECT id, title, due, status FROM tasks"
        items: list[dict] = []
        for r in _safe_rows(conn, sql):
            status = (r["status"] or "").lower()
            if status in ("done", "completed", "cancelled", "archived"):
                continue
            due = r["due"]
            urg = _urgency_for(due) if due else "open"
            # Only surface tasks that are actionable today: have a due date, or are open.
            if due and urg in ("overdue", "due_today", "due_soon"):
                items.append(_item("task-manager", r["title"], due, urgency=urg, ref=r["id"]))
            elif not due and status == "open":
                items.append(_item("task-manager", r["title"], None, urgency="open", ref=r["id"]))
        return items
    finally:
        conn.close()


def _collect_jobtrack() -> list[dict]:
    conn = _connect_ro("jobtrack")
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "applications"):
            return []
        cols = _columns(conn, "applications")
        if "next_followup" not in cols:
            return []
        rows = _safe_rows(
            conn,
            "SELECT id, company, role, status, next_followup FROM applications "
            "WHERE next_followup IS NOT NULL AND next_followup != ''",
        )
        items: list[dict] = []
        today = _today_str()
        for r in rows:
            status = (r["status"] or "").lower()
            if status in ("rejected", "withdrawn", "offer", "accepted", "closed"):
                continue
            due = r["next_followup"]
            urg = _urgency_for(due)
            # only items that have actually come due
            if due[:10] <= today or urg in ("overdue", "due_today", "due_soon"):
                label = f"Follow up: {r['company'] or '?'}" + (f" — {r['role']}" if r["role"] else "")
                items.append(_item("jobtrack", label, due, urgency=urg, ref=r["id"]))
        return items
    finally:
        conn.close()


def _collect_reachout(follow_up_days: int = 7) -> list[dict]:
    conn = _connect_ro("reachout")
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "outreach"):
            return []
        cols = _columns(conn, "outreach")
        if not {"sent_at", "status"} <= cols:
            return []
        rows = _safe_rows(
            conn,
            "SELECT id, recipient_name, company, sent_at, status, "
            "snooze_until, replied_at, followup_count FROM outreach "
            "WHERE status='sent' AND sent_at IS NOT NULL AND sent_at != ''",
        )
        items: list[dict] = []
        now = _now()
        for r in rows:
            if "replied_at" in r.keys() and r["replied_at"]:
                continue
            snooze = r["snooze_until"] if "snooze_until" in r.keys() else None
            sn_dt = _parse_dt(snooze)
            if sn_dt is not None and sn_dt > now:
                continue  # still snoozed
            sent = _parse_dt(r["sent_at"])
            if sent is None:
                continue
            days_since = (now - sent).days
            if days_since < follow_up_days:
                continue
            name = r["recipient_name"] or r["company"] or "contact"
            label = f"Follow up with {name} ({days_since}d since send)"
            urg = "overdue" if days_since >= follow_up_days * 2 else "due_today"
            items.append(_item("reachout", label, r["sent_at"], urgency=urg, ref=r["id"]))
        return items
    finally:
        conn.close()


def _collect_learn_tracker() -> list[dict]:
    conn = _connect_ro("learn-tracker")
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "courses"):
            return []
        cols = _columns(conn, "courses")
        date_cols = [c for c in ("review_at", "deadline") if c in cols]
        if not date_cols or "title" not in cols:
            return []
        select_cols = ["id", "title", "status"] + date_cols
        rows = _safe_rows(conn, f"SELECT {', '.join(select_cols)} FROM courses")
        items: list[dict] = []
        today = _today_str()
        for r in rows:
            status = (r["status"] or "").lower()
            if status in ("done", "completed", "dropped", "archived"):
                continue
            # pick the most urgent of the available date columns that has come due
            best_due = None
            best_urg = None
            for c in date_cols:
                val = r[c]
                if not val:
                    continue
                if val[:10] > today and _urgency_for(val) not in ("overdue", "due_today", "due_soon"):
                    continue
                urg = _urgency_for(val)
                if best_urg is None or _URGENCY_ORDER.get(urg, 9) < _URGENCY_ORDER.get(best_urg, 9):
                    best_urg, best_due = urg, val
            if best_due is None:
                continue
            kind = "Review" if "review_at" in date_cols and best_due == r["review_at"] else "Deadline"
            items.append(_item("learn-tracker", f"{kind}: {r['title']}", best_due,
                               urgency=best_urg, ref=r["id"]))
        return items
    finally:
        conn.close()


def _collect_habit_tracker() -> list[dict]:
    conn = _connect_ro("habit-tracker")
    if conn is None:
        return []
    try:
        if not (_table_exists(conn, "habits") and _table_exists(conn, "checkins")):
            return []
        hcols = _columns(conn, "habits")
        if "name" not in hcols:
            return []
        rows = _safe_rows(conn, "SELECT id, name, status FROM habits")
        today = _today_str()
        items: list[dict] = []
        for r in rows:
            status = (r["status"] or "active").lower()
            if status not in ("active", ""):
                continue
            checked = _safe_rows(
                conn, "SELECT 1 FROM checkins WHERE habit_id=? AND day=? LIMIT 1",
                (r["id"], today),
            )
            if checked:
                continue
            items.append(_item("habit-tracker", f"Check in: {r['name']}", today,
                               urgency="due_today", ref=r["id"]))
        return items
    finally:
        conn.close()


def _collect_interview_prep() -> list[dict]:
    conn = _connect_ro("interview-prep")
    if conn is None:
        return []
    try:
        if not _table_exists(conn, "cards"):
            return []
        cols = _columns(conn, "cards")
        if "due_at" not in cols or "front" not in cols:
            return []
        rows = _safe_rows(
            conn,
            "SELECT id, front, topic, due_at FROM cards "
            "WHERE due_at IS NOT NULL AND due_at != ''",
        )
        items: list[dict] = []
        now = _now()
        for r in rows:
            due = _parse_dt(r["due_at"])
            if due is None or due > now:
                continue
            front = (r["front"] or "").strip()
            label = front if len(front) <= 60 else front[:57] + "..."
            topic = r["topic"] if "topic" in r.keys() else ""
            if topic:
                label = f"[{topic}] {label}"
            items.append(_item("interview-prep", f"Review card: {label}", r["due_at"],
                               ref=r["id"]))
        return items
    finally:
        conn.close()


_COLLECTORS = {
    "task-manager": _collect_task_manager,
    "jobtrack": _collect_jobtrack,
    "reachout": _collect_reachout,
    "learn-tracker": _collect_learn_tracker,
    "habit-tracker": _collect_habit_tracker,
    "interview-prep": _collect_interview_prep,
}


def _gather(sources=None) -> list[dict]:
    """Run every (or selected) collector, swallowing per-source failures."""
    names = sources if sources is not None else SOURCES
    out: list[dict] = []
    for name in names:
        fn = _COLLECTORS.get(name)
        if fn is None:
            continue
        try:
            out.extend(fn())
        except Exception as exc:  # noqa: BLE001 - one bad source must not break the digest
            log.warning("daily-digest: collector %s failed: %s", name, exc)
    return out


def _rank(items: list[dict]) -> list[dict]:
    """Sort by urgency (overdue first), then by due date ascending (None last)."""
    def key(it):
        urg = _URGENCY_ORDER.get(it.get("urgency", "open"), 9)
        dt = _parse_dt(it.get("due"))
        # Put dated items before undated within the same urgency bucket.
        return (urg, 0, dt.timestamp()) if dt else (urg, 1, 0.0)

    return sorted(items, key=key)


# ---------------- impact/effort/urgency prioritization ----------------
# Relative "impact" weight per source: how consequential it is to act on an item
# from this server *today*. Higher = more impactful. Outreach/jobtrack follow-ups
# and overdue tasks tend to be time-sensitive and career-consequential; habit
# check-ins and flashcard reviews are valuable but lower-stakes if slipped a day.
_SOURCE_WEIGHT = {
    "jobtrack": 5.0,        # a missed follow-up can cost an opportunity
    "reachout": 4.5,        # outreach windows close fast
    "task-manager": 4.0,    # explicit user-chosen commitments
    "learn-tracker": 3.0,   # deadlines/reviews matter but are reschedulable
    "interview-prep": 2.5,  # SRS reviews compound, low single-day cost
    "habit-tracker": 2.0,   # streaks matter, but one slip is recoverable
}
_DEFAULT_SOURCE_WEIGHT = 3.0

# Urgency multiplier from the item's classified bucket (overdue dominates).
_URGENCY_WEIGHT = {
    "overdue": 3.0,
    "due_today": 2.0,
    "due_soon": 1.3,
    "due": 1.0,
    "open": 0.7,
}

# A rough "effort" proxy per source (1 = quick, higher = heavier). We reward
# low-effort wins slightly so quick high-impact items float up (impact/effort).
_SOURCE_EFFORT = {
    "habit-tracker": 1.0,    # a single check-in
    "interview-prep": 1.0,   # one card review
    "reachout": 1.5,         # send/personalize a follow-up
    "jobtrack": 1.5,         # draft a follow-up note
    "learn-tracker": 2.0,    # a study/review session
    "task-manager": 2.0,     # arbitrary, treat as medium
}
_DEFAULT_EFFORT = 2.0


def _overdue_days(due_val) -> float:
    """How many days an item is past due (0 if not overdue / undated)."""
    dt = _parse_dt(due_val)
    if dt is None:
        return 0.0
    delta_days = (_now() - dt).total_seconds() / 86400.0
    return delta_days if delta_days > 0 else 0.0


def _days_until(due_val) -> float | None:
    """Calendar days from today until the due date (negative if past). None if undated."""
    dt = _parse_dt(due_val)
    if dt is None:
        return None
    return float((dt.date() - _now().date()).days)


def _priority_score(it: dict) -> float:
    """Heuristic impact/effort/urgency score for one digest item (higher = do first).

    score = (source_impact * urgency_multiplier * proximity_boost) / effort
    plus an overdue accelerator so the longest-overdue items rise to the top.
    Pure function over the item dict — never raises on malformed fields.
    """
    src = it.get("source", "")
    urg = it.get("urgency", "open")
    impact = _SOURCE_WEIGHT.get(src, _DEFAULT_SOURCE_WEIGHT)
    urg_mult = _URGENCY_WEIGHT.get(urg, _URGENCY_WEIGHT["open"])
    effort = _SOURCE_EFFORT.get(src, _DEFAULT_EFFORT) or _DEFAULT_EFFORT

    # Proximity boost: the closer (or further past) a due date, the more pressing.
    due = it.get("due")
    days = _days_until(due)
    if days is None:
        proximity = 1.0  # undated / open items: neutral
    elif days <= 0:
        proximity = 1.0  # due today or past — overdue accelerator handles the past
    else:
        # within ~2 weeks ramps from ~1.5 down toward 1.0
        proximity = 1.0 + max(0.0, (14.0 - min(days, 14.0)) / 28.0)

    # Overdue accelerator: each overdue day adds urgency, capped so a single
    # ancient item can't completely starve everything else.
    overdue = _overdue_days(due)
    overdue_boost = min(overdue, 30.0) * 0.15

    base = (impact * urg_mult * proximity) / effort
    return round(base + overdue_boost, 4)


def _prioritize(items: list[dict]) -> list[dict]:
    """Return items annotated with `score` and sorted by it (desc), stable on ties.

    Ties break by the existing urgency/due ranking so output is deterministic.
    """
    ranked = _rank(items)  # deterministic baseline ordering
    scored = []
    for idx, it in enumerate(ranked):
        out = dict(it)
        out["score"] = _priority_score(it)
        scored.append((idx, out))
    # Sort by score desc; idx (the _rank order) breaks ties deterministically.
    scored.sort(key=lambda pair: (-pair[1]["score"], pair[0]))
    return [out for _, out in scored]


# ---------------- tools ----------------
@mcp.tool
def today() -> dict:
    """Aggregate everything that needs action today across all suite servers.

    Reads each source's SQLite DB read-only and returns a ranked, sourced list
    ({source, item, due, urgency}) plus per-source counts and the date. Absent
    source DBs/tables are skipped silently.
    """
    items = _rank(_gather())
    counts: dict[str, int] = {}
    for it in items:
        counts[it["source"]] = counts.get(it["source"], 0) + 1
    return ok(
        date=_today_str(),
        total=len(items),
        counts=counts,
        items=items,
    )


@mcp.tool
def whats_due(limit: int = 50) -> dict:
    """Ranked, sourced list of action items due now, capped at `limit`.

    Same items as today() (overdue first), trimmed to the top `limit`.
    """
    items = _rank(_gather())
    limit = max(1, min(int(limit), 500))
    trimmed = items[:limit]
    return ok(
        date=_today_str(),
        total=len(items),
        returned=len(trimmed),
        items=trimmed,
    )


@mcp.tool
def counts_by_source() -> dict:
    """Per-source counts of how many items currently need action (read-only).

    Always lists every known source; `available` reflects whether that source's
    DB was found and readable.
    """
    items = _gather()
    counts: dict[str, int] = {s: 0 for s in SOURCES}
    for it in items:
        counts[it["source"]] = counts.get(it["source"], 0) + 1
    available = {s: (base_data_dir() / s / "store.db").exists() for s in SOURCES}
    return ok(
        date=_today_str(),
        total=len(items),
        counts=counts,
        available=available,
    )


@mcp.tool
def source(name: str, limit: int = 50) -> dict:
    """Drill into a single source server's due items (read-only).

    `name` must be one of: task-manager, jobtrack, reachout, learn-tracker,
    habit-tracker, interview-prep.
    """
    if name not in _COLLECTORS:
        # Actionable not-found: name what's missing AND the valid sources, plus a recovery hint.
        # Keep the legacy `sources` key for back-compat; add not_found's `available`/`hint`.
        nf = not_found("source", name, available=list(SOURCES),
                       hint="name must be one of: " + ", ".join(SOURCES))
        nf["sources"] = list(SOURCES)
        return nf
    available = (base_data_dir() / name / "store.db").exists()
    items = _rank(_gather([name]))
    limit = max(1, min(int(limit), 500))
    trimmed = items[:limit]
    return ok(
        date=_today_str(),
        source=name,
        available=available,
        total=len(items),
        returned=len(trimmed),
        items=trimmed,
    )


@mcp.tool
def prioritize(limit: int = 15) -> dict:
    """Rank today's cross-server items by an impact/effort/urgency heuristic.

    Unlike whats_due()/today() (which sort by urgency then due-date order), this
    scores each item by source impact, urgency, due-date proximity and how far it
    is overdue, divided by a rough per-source effort proxy — so the most
    consequential, time-sensitive, quick-to-act items surface first.

    Read-only across every source DB; missing DBs/tables contribute nothing and
    never error. Returns items annotated with a numeric `score` (higher = do first)
    plus a short `weights` legend describing the heuristic.
    """
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return err("limit must be an integer", hint="e.g. prioritize(limit=15)")
    n = max(1, min(n, 500))

    items = _prioritize(_gather())
    trimmed = items[:n]
    counts: dict[str, int] = {}
    for it in trimmed:
        counts[it["source"]] = counts.get(it["source"], 0) + 1
    return ok(
        date=_today_str(),
        total=len(items),
        returned=len(trimmed),
        items=trimmed,
        counts=counts,
        weights={
            "source_impact": _SOURCE_WEIGHT,
            "urgency_multiplier": _URGENCY_WEIGHT,
            "source_effort": _SOURCE_EFFORT,
            "note": (
                "score = source_impact * urgency_multiplier * due_proximity / effort "
                "+ overdue_accelerator; higher score = act first"
            ),
        },
    )


if __name__ == "__main__":
    mcp.run()
