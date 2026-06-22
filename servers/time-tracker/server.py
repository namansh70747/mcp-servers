"""time-tracker — time-blocking, pomodoro, goals, and per-day/range reports (SQLite). start/stop a
timer, log a block, run pomodoros, export CSV."""
from __future__ import annotations

import csv
import io
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp_base import BaseStore, data_dir, db_path, make_server

mcp = make_server(
    "time-tracker",
    instructions=("Track time: start(label)/stop(), log_block, pomodoro, set_time_goal/goal_status, "
                  "daily_report/report/weekly_report, export_csv."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS blocks(
  id INTEGER PRIMARY KEY, label TEXT, category TEXT DEFAULT '', start TEXT, end TEXT, seconds INTEGER
);
CREATE TABLE IF NOT EXISTS goals(
  id INTEGER PRIMARY KEY, category TEXT DEFAULT '', minutes INTEGER, period TEXT DEFAULT 'day', created_at TEXT
);
"""
store = BaseStore(db_path("time-tracker"), schema=SCHEMA)


def _now():
    return datetime.now(timezone.utc)


def _ensure_cols(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_cols("blocks", {"note": "TEXT DEFAULT ''", "kind": "TEXT DEFAULT 'work'"})


def _stop_running() -> dict:
    row = store.query_one("SELECT id, start FROM blocks WHERE end IS NULL ORDER BY id DESC LIMIT 1")
    if not row:
        return {"error": "no running timer"}
    end = _now()
    try:
        secs = int((end - datetime.fromisoformat(row["start"])).total_seconds())
    except (ValueError, TypeError):
        secs = 0
    store.execute("UPDATE blocks SET end=?, seconds=? WHERE id=?", (end.isoformat(), max(0, secs), row["id"]))
    return {"id": row["id"], "seconds": max(0, secs), "minutes": round(max(0, secs) / 60, 1)}


# ---------------- Timer (preserved) ----------------
@mcp.tool
def start(label: str, category: str = "", note: str = "") -> dict:
    """Start a timer (closes any already-running one first)."""
    label = (label or "").strip()
    if not label:
        return {"error": "label is required"}
    if store.query_one("SELECT id FROM blocks WHERE end IS NULL"):
        _stop_running()
    bid = store.execute("INSERT INTO blocks(label,category,note,kind,start) VALUES(?,?,?,'work',?)",
                        (label, category, note, _now().isoformat()))
    return {"id": bid, "label": label, "started": _now().isoformat()}


@mcp.tool
def stop() -> dict:
    """Stop the running timer and record its duration."""
    return _stop_running()


@mcp.tool
def running() -> dict:
    """Show the currently running timer (if any) and its elapsed time."""
    row = store.query_one("SELECT id,label,category,start FROM blocks WHERE end IS NULL ORDER BY id DESC LIMIT 1")
    if not row:
        return {"running": False}
    try:
        elapsed = int((_now() - datetime.fromisoformat(row["start"])).total_seconds())
    except (ValueError, TypeError):
        elapsed = 0
    return {"running": True, "id": row["id"], "label": row["label"], "category": row["category"],
            "elapsed_min": round(elapsed / 60, 1)}


@mcp.tool
def log_block(label: str, minutes: int, category: str = "", note: str = "") -> dict:
    """Log a completed block of N minutes (when you forgot to start a timer)."""
    if minutes <= 0:
        return {"error": "minutes must be positive"}
    n = _now().isoformat()
    bid = store.execute("INSERT INTO blocks(label,category,note,kind,start,end,seconds) VALUES(?,?,?,'work',?,?,?)",
                        (label, category, note, n, n, minutes * 60))
    return {"id": bid, "minutes": minutes}


@mcp.tool
def edit_block(block_id: int, label: str = "", category: str = "", minutes: int = -1, note: str = "") -> dict:
    """Edit a logged block (only non-empty/positive args applied)."""
    if not store.query_one("SELECT id FROM blocks WHERE id=?", (block_id,)):
        return {"error": "no such block"}
    sets, params = [], []
    if label:
        sets.append("label=?"); params.append(label)
    if category:
        sets.append("category=?"); params.append(category)
    if note:
        sets.append("note=?"); params.append(note)
    if minutes >= 0:
        sets.append("seconds=?"); params.append(minutes * 60)
    if not sets:
        return {"error": "nothing to update"}
    params.append(block_id)
    store.execute(f"UPDATE blocks SET {','.join(sets)} WHERE id=?", params)
    return {"ok": True, "id": block_id}


@mcp.tool
def delete_block(block_id: int) -> dict:
    """Delete a block."""
    if not store.query_one("SELECT id FROM blocks WHERE id=?", (block_id,)):
        return {"error": "no such block"}
    store.execute("DELETE FROM blocks WHERE id=?", (block_id,))
    return {"ok": True, "id": block_id}


@mcp.tool
def recent_blocks(limit: int = 20) -> list[dict]:
    """Most recent completed blocks."""
    return store.query("SELECT id,label,category,kind,seconds,start FROM blocks WHERE seconds IS NOT NULL "
                       "ORDER BY id DESC LIMIT ?", (max(1, limit),))


# ---------------- Pomodoro ----------------
@mcp.tool
def start_pomodoro(label: str, work_min: int = 25, break_min: int = 5, category: str = "") -> dict:
    """Start a pomodoro focus session (closes any running timer). pomodoro_status() shows time left."""
    label = (label or "").strip()
    if not label:
        return {"error": "label is required"}
    if store.query_one("SELECT id FROM blocks WHERE end IS NULL"):
        _stop_running()
    bid = store.execute("INSERT INTO blocks(label,category,kind,note,start) VALUES(?,?,'pomodoro',?,?)",
                        (label, category, f"work:{work_min};break:{break_min}", _now().isoformat()))
    return {"id": bid, "label": label, "work_min": work_min, "break_min": break_min,
            "ends_at": (_now() + timedelta(minutes=work_min)).isoformat()}


@mcp.tool
def pomodoro_status() -> dict:
    """Status of the active pomodoro: elapsed, remaining, and whether the work interval is up."""
    row = store.query_one("SELECT id,label,note,start FROM blocks WHERE end IS NULL AND kind='pomodoro' "
                          "ORDER BY id DESC LIMIT 1")
    if not row:
        return {"active": False}
    work_min = 25
    for part in (row["note"] or "").split(";"):
        if part.startswith("work:"):
            try:
                work_min = int(part.split(":")[1])
            except (ValueError, IndexError):
                pass
    try:
        elapsed = (_now() - datetime.fromisoformat(row["start"])).total_seconds() / 60
    except (ValueError, TypeError):
        elapsed = 0
    return {"active": True, "id": row["id"], "label": row["label"], "elapsed_min": round(elapsed, 1),
            "remaining_min": round(max(0, work_min - elapsed), 1), "work_done": elapsed >= work_min}


@mcp.tool
def pomodoro_done() -> dict:
    """Finish the active pomodoro, recording the focus block; suggests a break length."""
    row = store.query_one("SELECT id,note FROM blocks WHERE end IS NULL AND kind='pomodoro' ORDER BY id DESC LIMIT 1")
    if not row:
        return {"error": "no active pomodoro"}
    res = _stop_running()
    break_min = 5
    for part in (row["note"] or "").split(";"):
        if part.startswith("break:"):
            try:
                break_min = int(part.split(":")[1])
            except (ValueError, IndexError):
                pass
    res["take_break_min"] = break_min
    return res


@mcp.tool
def pomodoro_count(days: int = 1) -> dict:
    """How many pomodoros completed in the last N days."""
    cutoff = (_now() - timedelta(days=max(1, days))).isoformat()
    n = store.query_one("SELECT COUNT(*) AS n FROM blocks WHERE kind='pomodoro' AND end IS NOT NULL AND start>=?",
                       (cutoff,))["n"]
    return {"days": days, "pomodoros": n}


# ---------------- Goals ----------------
@mcp.tool
def set_time_goal(minutes: int, category: str = "", period: str = "day") -> dict:
    """Set a time goal (minutes per period). period: day/week. Empty category = total time."""
    if period not in ("day", "week"):
        period = "day"
    gid = store.execute("INSERT INTO goals(category,minutes,period,created_at) VALUES(?,?,?,?)",
                        (category, max(1, minutes), period, _now().isoformat()))
    return {"id": gid, "category": category or "(all)", "minutes": minutes, "period": period}


@mcp.tool
def goal_status() -> list[dict]:
    """Progress toward each time goal in its current period."""
    out = []
    for g in store.query("SELECT id,category,minutes,period FROM goals ORDER BY created_at DESC"):
        cutoff = (_now() - timedelta(days=1 if g["period"] == "day" else 7)).isoformat()
        sql = "SELECT COALESCE(SUM(seconds),0) AS s FROM blocks WHERE seconds IS NOT NULL AND start>=?"
        params: list = [cutoff]
        if g["category"]:
            sql += " AND category=?"; params.append(g["category"])
        secs = store.query_one(sql, params)["s"]
        cur_min = round(secs / 60)
        target = g["minutes"] or 0
        pct = round(100 * min(1.0, cur_min / target), 1) if target > 0 else 100.0
        out.append({"id": g["id"], "category": g["category"] or "(all)", "period": g["period"],
                    "target_min": g["minutes"], "current_min": cur_min, "met": cur_min >= target,
                    "pct": pct})
    return out


# ---------------- Reports ----------------
def _report(days: int) -> dict:
    cutoff = _now().timestamp() - days * 86400
    rows = store.query("SELECT label, category, seconds, start FROM blocks WHERE seconds IS NOT NULL")
    by_cat: dict[str, int] = {}
    by_label: dict[str, int] = {}
    total = sessions = 0
    for r in rows:
        try:
            if datetime.fromisoformat(r["start"]).timestamp() < cutoff:
                continue
        except (ValueError, TypeError):
            continue
        total += r["seconds"]; sessions += 1
        c = r["category"] or "uncategorized"
        by_cat[c] = by_cat.get(c, 0) + r["seconds"]
        by_label[r["label"]] = by_label.get(r["label"], 0) + r["seconds"]
    fmt = lambda d: {k: round(v / 3600, 2) for k, v in sorted(d.items(), key=lambda x: -x[1])}
    return {"total_hours": round(total / 3600, 2), "sessions": sessions,
            "avg_session_min": round((total / 60 / sessions), 1) if sessions else 0,
            "by_category": fmt(by_cat), "by_label": fmt(by_label)}


@mcp.tool
def weekly_report() -> dict:
    """Total time in the last 7 days, broken down by category and label."""
    return _report(7)


@mcp.tool
def report(days: int = 7) -> dict:
    """Time report over the last N days (generalizes weekly_report): totals, sessions, breakdowns."""
    return _report(max(1, days))


@mcp.tool
def daily_report(date: str = "") -> dict:
    """Time logged on a specific day (YYYY-MM-DD, default today), by category and label."""
    day = (date or _now().date().isoformat()).strip()
    rows = store.query("SELECT label, category, seconds, start FROM blocks WHERE seconds IS NOT NULL")
    by_cat: dict[str, int] = {}
    by_label: dict[str, int] = {}
    total = 0
    for r in rows:
        try:
            if datetime.fromisoformat(r["start"]).date().isoformat() != day:
                continue
        except (ValueError, TypeError):
            continue
        total += r["seconds"]
        c = r["category"] or "uncategorized"
        by_cat[c] = by_cat.get(c, 0) + r["seconds"]
        by_label[r["label"]] = by_label.get(r["label"], 0) + r["seconds"]
    fmt = lambda d: {k: round(v / 3600, 2) for k, v in sorted(d.items(), key=lambda x: -x[1])}
    return {"date": day, "total_hours": round(total / 3600, 2), "by_category": fmt(by_cat), "by_label": fmt(by_label)}


@mcp.tool
def category_breakdown(days: int = 30) -> list[dict]:
    """Hours per category over the last N days, descending."""
    rep = _report(max(1, days))
    return [{"category": k, "hours": v} for k, v in rep["by_category"].items()]


# ---------------- CSV export ----------------
def _csv(days: int) -> str:
    cutoff = _now().timestamp() - days * 86400 if days > 0 else 0
    buf = io.StringIO()
    w = csv.writer(buf)
    w.writerow(["id", "label", "category", "kind", "start", "end", "minutes"])
    for r in store.query("SELECT id,label,category,kind,start,end,seconds FROM blocks "
                         "WHERE seconds IS NOT NULL ORDER BY start"):
        try:
            if days > 0 and datetime.fromisoformat(r["start"]).timestamp() < cutoff:
                continue
        except (ValueError, TypeError):
            pass
        w.writerow([r["id"], r["label"], r["category"], r["kind"], r["start"], r["end"],
                    round((r["seconds"] or 0) / 60, 1)])
    return buf.getvalue()


@mcp.tool
def export_csv(days: int = 0) -> dict:
    """Return blocks as CSV text (days=0 = all)."""
    return {"csv": _csv(days)}


@mcp.tool
def write_csv(path: str = "", days: int = 0) -> dict:
    """Write blocks to a CSV file. Default: <data_dir>/exports/time.csv."""
    target = Path(path) if path else (data_dir("time-tracker") / "exports" / "time.csv")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_csv(days), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"could not write {target}: {e}"}
    return {"ok": True, "path": str(target)}


@mcp.tool
def today() -> dict:
    """Quick overview of today: total hours so far, running timer (if any), and category breakdown."""
    rep = daily_report()
    run = running()
    return {"date": rep["date"], "total_hours": rep["total_hours"], "by_category": rep["by_category"],
            "running": run.get("running", False),
            "running_label": run.get("label") if run.get("running") else None}


@mcp.tool
def top_labels(days: int = 30, limit: int = 10) -> list[dict]:
    """Most time-consuming labels over the last N days, with hours and share of total."""
    rep = _report(max(1, days))
    total = rep["total_hours"] or 1
    items = [{"label": k, "hours": v, "pct": round(100 * v / total, 1)}
             for k, v in rep["by_label"].items()]
    return items[:max(1, limit)]


@mcp.tool
def productivity_by_hour(days: int = 30) -> dict:
    """Hours logged bucketed by hour-of-day (0-23) over the last N days — find your peak focus hours."""
    cutoff = _now().timestamp() - max(1, days) * 86400
    buckets = {h: 0.0 for h in range(24)}
    for r in store.query("SELECT seconds,start FROM blocks WHERE seconds IS NOT NULL"):
        try:
            dt = datetime.fromisoformat(r["start"])
        except (ValueError, TypeError):
            continue
        if dt.timestamp() < cutoff:
            continue
        buckets[dt.hour] += (r["seconds"] or 0) / 3600
    rounded = {h: round(v, 2) for h, v in buckets.items()}
    peak = max(rounded.items(), key=lambda x: x[1]) if any(rounded.values()) else (None, 0)
    return {"days": days, "by_hour": rounded, "peak_hour": peak[0], "peak_hours": peak[1]}


@mcp.tool
def focus_streak() -> dict:
    """Longest and current streak of consecutive days with at least one logged block."""
    days = set()
    for r in store.query("SELECT start FROM blocks WHERE seconds IS NOT NULL"):
        try:
            days.add(datetime.fromisoformat(r["start"]).date())
        except (ValueError, TypeError):
            continue
    if not days:
        return {"current": 0, "longest": 0, "active_days": 0}
    ordered = sorted(days)
    longest = run = 1
    for i in range(1, len(ordered)):
        if (ordered[i] - ordered[i - 1]).days == 1:
            run += 1; longest = max(longest, run)
        else:
            run = 1
    today = _now().date()
    cur, d = 0, today if today in days else today - timedelta(days=1)
    while d in days:
        cur += 1; d -= timedelta(days=1)
    return {"current": cur, "longest": longest, "active_days": len(days)}


# ---------------- Focus & cognitive load (read-only, never raises) ----------------
def _blocks_on(day: str) -> list[dict]:
    """All completed blocks whose start date matches `day` (YYYY-MM-DD). Never raises."""
    out: list[dict] = []
    try:
        rows = store.query("SELECT label,category,kind,seconds,start FROM blocks WHERE seconds IS NOT NULL")
    except Exception:
        return out
    for r in rows:
        try:
            if datetime.fromisoformat(r["start"]).date().isoformat() != day:
                continue
        except (ValueError, TypeError):
            continue
        out.append(r)
    return out


def _focus_for_day(day: str) -> dict:
    """Compute a 0-100 focus score for one day from session length, fragmentation, and
    pomodoro completion. Returns a detail dict (also used by cognitive_load). Never raises."""
    blocks = _blocks_on(day)
    sessions = len(blocks)
    total_sec = sum(int(b["seconds"] or 0) for b in blocks)
    if sessions == 0 or total_sec == 0:
        return {"date": day, "score": 0, "sessions": 0, "total_minutes": 0.0,
                "avg_session_min": 0.0, "fragmentation": 0.0,
                "pomodoros": 0, "pomodoro_minutes": 0.0,
                "components": {"length": 0, "focus": 0, "pomodoro": 0}, "band": "none"}

    avg_min = (total_sec / sessions) / 60.0
    pomo = [b for b in blocks if (b["kind"] or "") == "pomodoro"]
    pomo_count = len(pomo)
    pomo_sec = sum(int(b["seconds"] or 0) for b in pomo)

    # Component 1 — session length (max 40). Reward ~25-50 min sessions; gentle taper for
    # very short (shallow) and very long (no breaks) blocks. Triangular around 35 min.
    if avg_min <= 0:
        length = 0.0
    elif avg_min <= 35:
        length = 40.0 * (avg_min / 35.0)
    else:
        # taper toward 20 by 120 min; long unbroken sessions are not pure focus
        length = max(20.0, 40.0 - (avg_min - 35.0) * (20.0 / 85.0))
    length = max(0.0, min(40.0, length))

    # Component 2 — focus / low fragmentation (max 35). Sessions-per-hour of logged time:
    # fewer switches per focused hour scores higher. ~<=2 switches/hr is ideal.
    hours = max(total_sec / 3600.0, 0.01)
    sessions_per_hour = sessions / hours
    fragmentation = round(sessions_per_hour, 2)
    if sessions_per_hour <= 2.0:
        focus = 35.0
    else:
        focus = max(0.0, 35.0 - (sessions_per_hour - 2.0) * 7.0)
    focus = max(0.0, min(35.0, focus))

    # Component 3 — pomodoro completion (max 25). Each completed pomodoro is worth ~5 pts,
    # plus a small bonus for pomodoro time as a share of the day. Caps at 25.
    pomo_share = (pomo_sec / total_sec) if total_sec else 0.0
    pomodoro = min(25.0, pomo_count * 5.0 + pomo_share * 10.0)
    pomodoro = max(0.0, pomodoro)

    score = int(round(length + focus + pomodoro))
    score = max(0, min(100, score))
    band = ("deep" if score >= 80 else "strong" if score >= 60 else
            "moderate" if score >= 40 else "shallow" if score > 0 else "none")
    return {"date": day, "score": score, "sessions": sessions,
            "total_minutes": round(total_sec / 60.0, 1), "avg_session_min": round(avg_min, 1),
            "fragmentation": fragmentation, "pomodoros": pomo_count,
            "pomodoro_minutes": round(pomo_sec / 60.0, 1),
            "components": {"length": round(length, 1), "focus": round(focus, 1),
                           "pomodoro": round(pomodoro, 1)},
            "band": band}


@mcp.tool
def focus_score(date: str = "") -> dict:
    """A 0-100 focus score for a single day (YYYY-MM-DD, default today), blended from average
    session length, fragmentation (context-switching), and completed pomodoros. Read-only;
    never raises. Returns the score, its three components, and a quality band."""
    day = (date or "").strip() or _now().date().isoformat()
    try:
        datetime.fromisoformat(day)  # validate, but accept date-only too
    except (ValueError, TypeError):
        # accept plain YYYY-MM-DD; reject anything unparseable
        try:
            datetime.strptime(day, "%Y-%m-%d")
        except (ValueError, TypeError):
            return {"error": f"invalid date '{date}' — use YYYY-MM-DD"}
    try:
        return _focus_for_day(day[:10])
    except Exception as e:  # defensive: this tool must never raise
        return {"error": f"could not compute focus score: {e}", "date": day}


_RANGE_DAYS = {"day": 1, "week": 7, "month": 30}
_seq = range  # capture builtin: the cognitive_load param `range` shadows it locally


@mcp.tool
def cognitive_load(range: str = "week") -> dict:
    """Cognitive-load summary over a range (day/week/month, default week). Aggregates per-day
    focus scores plus total focused time and context-switching to gauge sustained mental load.
    Read-only over existing blocks; never raises. Returns avg/peak/low focus, total hours,
    switches per hour, an overall load band, and a per-day series."""
    key = (range or "").strip().lower()
    days = _RANGE_DAYS.get(key)
    if days is None:
        return {"error": f"invalid range '{range}' — use one of: day, week, month",
                "valid": list(_RANGE_DAYS)}
    try:
        today_d = _now().date()
        series: list[dict] = []
        for i in _seq(days):
            d = (today_d - timedelta(days=days - 1 - i)).isoformat()
            series.append(_focus_for_day(d))

        active = [s for s in series if s["sessions"] > 0]
        total_min = round(sum(s["total_minutes"] for s in series), 1)
        total_sessions = sum(s["sessions"] for s in series)
        total_pomo = sum(s["pomodoros"] for s in series)
        active_days = len(active)
        scores = [s["score"] for s in active]
        avg_focus = round(sum(scores) / len(scores), 1) if scores else 0.0
        peak = max(active, key=lambda s: s["score"]) if active else None
        low = min(active, key=lambda s: s["score"]) if active else None

        total_hours = max(total_min / 60.0, 0.01)
        switches_per_hour = round(total_sessions / total_hours, 2) if total_sessions else 0.0
        avg_session_min = round(total_min / total_sessions, 1) if total_sessions else 0.0

        # Load band: high focus over many active days with low switching = sustainable "high"
        # load; lots of switching or thin days = "scattered"/"light".
        if active_days == 0:
            load = "idle"
        elif avg_focus >= 65 and switches_per_hour <= 2.5:
            load = "high"
        elif avg_focus >= 45:
            load = "moderate"
        elif switches_per_hour > 4:
            load = "scattered"
        else:
            load = "light"

        return {"range": key, "days": days, "active_days": active_days,
                "total_hours": round(total_min / 60.0, 2), "total_minutes": total_min,
                "total_sessions": total_sessions, "total_pomodoros": total_pomo,
                "avg_session_min": avg_session_min, "switches_per_hour": switches_per_hour,
                "avg_focus": avg_focus,
                "peak_day": {"date": peak["date"], "score": peak["score"]} if peak else None,
                "lowest_day": {"date": low["date"], "score": low["score"]} if low else None,
                "load": load, "series": series}
    except Exception as e:  # defensive: this tool must never raise
        return {"error": f"could not compute cognitive load: {e}", "range": key}


if __name__ == "__main__":
    mcp.run()
