"""habit-tracker — build habits with daily/weekly cadence, check-ins, streaks, and reports (SQLite).
Complements learn-tracker/time-tracker: the recurring-behavior primitive. All offline."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mcp_base import BaseStore, db_path, err, make_server, not_found

mcp = make_server(
    "habit-tracker",
    instructions=("Habits: add_habit, check_in, streak, today (what's due), habit_report, list_habits."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS habits(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, cadence TEXT DEFAULT 'daily', target INTEGER DEFAULT 1,
  status TEXT DEFAULT 'active', created_at TEXT
);
CREATE TABLE IF NOT EXISTS checkins(
  id INTEGER PRIMARY KEY, habit_id INTEGER, day TEXT, count INTEGER DEFAULT 1, note TEXT DEFAULT '',
  UNIQUE(habit_id, day)
);
"""
store = BaseStore(db_path("habit-tracker"), schema=SCHEMA)
CADENCES = {"daily", "weekly"}


def _today():
    return datetime.now(timezone.utc).date()


def _now():
    return datetime.now(timezone.utc).isoformat()


@mcp.tool
def add_habit(name: str, cadence: str = "daily", target: int = 1) -> dict:
    """Add a habit. cadence: daily or weekly. target = times per period (default 1)."""
    name = (name or "").strip()
    if not name:
        return err("name is required", hint="pass a non-empty habit name")
    cadence = cadence.strip().lower()
    if cadence not in CADENCES:
        cadence = "daily"
    hid = store.execute("INSERT INTO habits(name,cadence,target,created_at) VALUES(?,?,?,?) "
                        "ON CONFLICT(name) DO UPDATE SET cadence=excluded.cadence, target=excluded.target",
                        (name, cadence, max(1, target), _now()))
    return {"id": hid, "name": name, "cadence": cadence, "target": max(1, target)}


def _resolve(habit: str | int) -> dict | None:
    if isinstance(habit, int) or (isinstance(habit, str) and habit.isdigit()):
        return store.query_one("SELECT * FROM habits WHERE id=?", (int(habit),))
    return store.query_one("SELECT * FROM habits WHERE name=?", (habit,))


def _habit_names(limit: int = 10) -> list[str]:
    """Active habit names, to suggest valid targets in not-found errors."""
    return [r["name"] for r in store.query(
        "SELECT name FROM habits ORDER BY status='active' DESC, name LIMIT ?", (limit,))]


def _no_habit(habit) -> dict:
    return not_found("habit", habit, available=_habit_names(),
                     hint="use list_habits() to see valid habit names/ids")


@mcp.tool
def check_in(habit: str, date: str = "", count: int = 1, note: str = "") -> dict:
    """Record a check-in for a habit (by name or id). `date`=YYYY-MM-DD (default today)."""
    h = _resolve(habit)
    if not h:
        return _no_habit(habit)
    day = (date or _today().isoformat()).strip()
    store.execute("INSERT INTO checkins(habit_id,day,count,note) VALUES(?,?,?,?) "
                  "ON CONFLICT(habit_id,day) DO UPDATE SET count=count+excluded.count, "
                  "note=COALESCE(NULLIF(excluded.note,''),note)",
                  (h["id"], day, max(1, count), note))
    return {"ok": True, "habit": h["name"], "day": day, "streak": _streak(h["id"], h["cadence"])}


def _checkin_days(habit_id: int) -> set:
    days = set()
    for r in store.query("SELECT day FROM checkins WHERE habit_id=?", (habit_id,)):
        try:
            days.add(datetime.fromisoformat(r["day"]).date() if "T" in r["day"]
                     else datetime.strptime(r["day"], "%Y-%m-%d").date())
        except ValueError:
            continue
    return days


def _streak(habit_id: int, cadence: str) -> int:
    days = _checkin_days(habit_id)
    if not days:
        return 0
    today = _today()
    if cadence == "weekly":
        weeks = {d.isocalendar()[:2] for d in days}
        streak, ref = 0, today
        while ref.isocalendar()[:2] in weeks:
            streak += 1
            ref -= timedelta(weeks=1)
        return streak
    start = today if today in days else today - timedelta(days=1)
    if start not in days:
        return 0
    streak, d = 0, start
    while d in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


@mcp.tool
def streak(habit: str) -> dict:
    """Current streak for a habit (consecutive days, or weeks for weekly habits)."""
    h = _resolve(habit)
    if not h:
        return _no_habit(habit)
    days = _checkin_days(h["id"])
    return {"habit": h["name"], "cadence": h["cadence"], "streak": _streak(h["id"], h["cadence"]),
            "total_checkins": len(days)}


@mcp.tool
def today() -> list[dict]:
    """Active habits and whether they're satisfied for the current period (today / this week)."""
    out = []
    today_d = _today()
    week = today_d.isocalendar()[:2]
    for h in store.query("SELECT * FROM habits WHERE status='active' ORDER BY name"):
        if h["cadence"] == "weekly":
            n = sum(r["count"] for r in store.query(
                "SELECT count,day FROM checkins WHERE habit_id=?", (h["id"],)) if _in_week(r["day"], week))
        else:
            row = store.query_one("SELECT count FROM checkins WHERE habit_id=? AND day=?",
                                 (h["id"], today_d.isoformat()))
            n = row["count"] if row else 0
        out.append({"habit": h["name"], "cadence": h["cadence"], "target": h["target"],
                    "done": n, "satisfied": n >= h["target"], "streak": _streak(h["id"], h["cadence"])})
    return out


def _in_week(day: str, week) -> bool:
    try:
        d = datetime.strptime(day[:10], "%Y-%m-%d").date()
    except ValueError:
        return False
    return d.isocalendar()[:2] == week


@mcp.tool
def habit_report(days: int = 30) -> dict:
    """Per-habit completion over the last N days: check-in count and best streak."""
    cutoff = (_today() - timedelta(days=max(1, days)))
    out = []
    for h in store.query("SELECT * FROM habits ORDER BY name"):
        days_set = {d for d in _checkin_days(h["id"]) if d >= cutoff}
        out.append({"habit": h["name"], "cadence": h["cadence"], "status": h["status"],
                    "checkins": len(days_set), "current_streak": _streak(h["id"], h["cadence"]),
                    "best_streak": _best_streak(h["id"])})
    return {"days": days, "habits": out}


def _best_streak(habit_id: int) -> int:
    days = sorted(_checkin_days(habit_id))
    if not days:
        return 0
    best = run = 1
    for i in range(1, len(days)):
        if (days[i] - days[i - 1]).days == 1:
            run += 1; best = max(best, run)
        else:
            run = 1
    return best


@mcp.tool
def list_habits(status: str = "active") -> list[dict]:
    """List habits by status (active/archived)."""
    return store.query("SELECT id,name,cadence,target,status FROM habits WHERE status=? ORDER BY name", (status,))


@mcp.tool
def archive_habit(habit: str) -> dict:
    """Archive a habit (keeps history; hides from `today`)."""
    h = _resolve(habit)
    if not h:
        return _no_habit(habit)
    store.execute("UPDATE habits SET status='archived' WHERE id=?", (h["id"],))
    return {"ok": True, "habit": h["name"]}


@mcp.tool
def delete_habit(habit: str) -> dict:
    """Delete a habit and all its check-ins."""
    h = _resolve(habit)
    if not h:
        return _no_habit(habit)
    store.execute("DELETE FROM checkins WHERE habit_id=?", (h["id"],))
    store.execute("DELETE FROM habits WHERE id=?", (h["id"],))
    return {"ok": True, "habit": h["name"]}


@mcp.tool
def correlations(days: int = 90, min_overlap: int = 3) -> dict:
    """Which habits tend to be done on the same days. Read-only.

    Looks at the last N days of check-ins and reports, for each pair of habits,
    how often they were both done on the same day (Jaccard overlap). Useful for
    spotting habit-stacking opportunities. Never raises.
    """
    try:
        days = max(1, int(days))
    except (TypeError, ValueError):
        days = 90
    try:
        min_overlap = max(1, int(min_overlap))
    except (TypeError, ValueError):
        min_overlap = 3
    cutoff = _today() - timedelta(days=days)
    habits = store.query("SELECT id,name,cadence,status FROM habits ORDER BY name")
    # day-sets within window, per habit
    sets = {}
    for h in habits:
        ds = {d for d in _checkin_days(h["id"]) if d >= cutoff}
        if ds:
            sets[h["id"]] = ds
    by_id = {h["id"]: h for h in habits}
    pairs = []
    ids = sorted(sets)
    for i in range(len(ids)):
        for j in range(i + 1, len(ids)):
            a, b = ids[i], ids[j]
            sa, sb = sets[a], sets[b]
            both = sa & sb
            union = sa | sb
            if len(both) < min_overlap:
                continue
            jaccard = round(len(both) / len(union), 3) if union else 0.0
            pairs.append({
                "habit_a": by_id[a]["name"], "habit_b": by_id[b]["name"],
                "both_days": len(both), "a_days": len(sa), "b_days": len(sb),
                "jaccard": jaccard,
                "lift": round(len(both) / min(len(sa), len(sb)), 3) if min(len(sa), len(sb)) else 0.0,
            })
    pairs.sort(key=lambda p: (p["jaccard"], p["both_days"]), reverse=True)
    return {"ok": True, "days": days, "min_overlap": min_overlap,
            "habits_considered": len(sets), "pairs": pairs}


def _cadence_misses(habit_id: int, cadence: str, window_days: int) -> dict:
    """Within the last `window_days`, count completed vs missed periods.

    For weekly habits a "period" is a week, so the window is converted to weeks.
    """
    days = _checkin_days(habit_id)
    today = _today()
    if cadence == "weekly":
        periods = max(1, window_days // 7)
        weeks_done = {d.isocalendar()[:2] for d in days}
        ref = today
        hit = miss = 0
        for _ in range(periods):
            if ref.isocalendar()[:2] in weeks_done:
                hit += 1
            else:
                miss += 1
            ref -= timedelta(weeks=1)
    else:
        periods = max(1, window_days)
        hit = miss = 0
        ref = today
        for _ in range(periods):
            if ref in days:
                hit += 1
            else:
                miss += 1
            ref -= timedelta(days=1)
    return {"hit": hit, "miss": miss, "periods": periods, "unit": "week" if cadence == "weekly" else "day",
            "rate": round(hit / periods, 3) if periods else 0.0}


@mcp.tool
def recommend(habit: str = "", window: int = 30) -> dict:
    """Suggest a habit cadence based on streak/miss patterns. Read-only.

    For one habit (by name/id) or all active habits when `habit` is empty,
    inspects recent completion rate, current streak, and best streak to advise
    keeping, easing (daily -> weekly), or tightening (weekly -> daily) the cadence.
    Purely advisory; changes nothing. Never raises.
    """
    try:
        window = max(7, int(window))
    except (TypeError, ValueError):
        window = 30

    def _advise(h: dict) -> dict:
        cad = h["cadence"]
        cur = _streak(h["id"], cad)
        best = _best_streak(h["id"])
        m = _cadence_misses(h["id"], cad, window)
        rate = m["rate"]
        total = len(_checkin_days(h["id"]))
        suggestion, reason = cad, ""
        if total == 0:
            suggestion, reason = cad, "no check-ins yet — start small and build consistency"
        elif cad == "daily":
            if rate >= 0.85:
                suggestion, reason = "daily", f"strong daily adherence ({int(rate*100)}%) — keep it daily"
            elif rate >= 0.4:
                suggestion = "daily"
                reason = (f"moderate adherence ({int(rate*100)}%) — keep daily but lower the bar; "
                          "aim for a minimum viable version on busy days")
            else:
                suggestion = "weekly"
                reason = (f"low daily adherence ({int(rate*100)}%) with frequent misses — "
                          "ease to a weekly cadence to rebuild momentum without breaking streaks")
        elif cad == "weekly":
            if rate >= 0.85 and best >= 4:
                suggestion = "daily"
                reason = (f"hitting it nearly every week ({int(rate*100)}%) with a {best}-week best — "
                          "you may be ready to tighten to daily")
            elif rate >= 0.4:
                suggestion, reason = "weekly", f"steady weekly cadence ({int(rate*100)}%) — keep it weekly"
            else:
                suggestion, reason = "weekly", (
                    f"weekly adherence is low ({int(rate*100)}%) — keep weekly and pick a fixed day "
                    "to anchor the habit")
        return {"habit": h["name"], "current_cadence": cad,
                "current_streak": cur, "best_streak": best,
                "window_days": window, "periods": m["periods"], "period_unit": m["unit"],
                "completion_rate": rate, "hits": m["hit"], "misses": m["miss"],
                "suggested_cadence": suggestion, "advice": reason,
                "change": suggestion != cad}

    sel = (habit or "").strip()
    if sel:
        h = _resolve(sel)
        if not h:
            return _no_habit(sel)
        return {"ok": True, "recommendation": _advise(h)}
    rows = store.query("SELECT * FROM habits WHERE status='active' ORDER BY name")
    return {"ok": True, "window_days": window,
            "recommendations": [_advise(h) for h in rows]}


if __name__ == "__main__":
    mcp.run()
