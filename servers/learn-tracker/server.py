"""learn-tracker — track courses/tutorials, progress, study sessions, streaks, goals, resource links,
spaced review, and generate a study plan (SQLite). All offline; resource-title fetch is lazy + safe."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from mcp_base import BaseStore, db_path, err, make_server, not_found

mcp = make_server(
    "learn-tracker",
    instructions=("Track learning: add_course, log_progress, log_study (streaks), set_goal/goal_progress, "
                  "add_resource, mark_for_review/reviews_due, whats_next, generate_plan, report."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS courses(
  id INTEGER PRIMARY KEY, title TEXT, provider TEXT DEFAULT '', url TEXT DEFAULT '',
  status TEXT DEFAULT 'wishlist', progress_pct INTEGER DEFAULT 0, hours INTEGER DEFAULT 0,
  notes TEXT DEFAULT '', updated_at TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS sessions(
  id INTEGER PRIMARY KEY, course_id INTEGER, minutes INTEGER, note TEXT DEFAULT '', at TEXT
);
CREATE TABLE IF NOT EXISTS goals(
  id INTEGER PRIMARY KEY, kind TEXT, target INTEGER, period TEXT DEFAULT 'week', created_at TEXT
);
CREATE TABLE IF NOT EXISTS resources(
  id INTEGER PRIMARY KEY, course_id INTEGER, url TEXT, title TEXT DEFAULT '', kind TEXT DEFAULT 'link',
  created_at TEXT
);
"""
store = BaseStore(db_path("learn-tracker"), schema=SCHEMA)


def _now():
    return datetime.now(timezone.utc).isoformat()


_MAX_LIMIT = 500  # clamp for unbounded list queries


def _clamp_limit(limit: int, default: int = 50) -> int:
    try:
        n = int(limit)
    except (TypeError, ValueError):
        return default
    if n <= 0:
        return default
    return min(n, _MAX_LIMIT)


def _course_ids(limit: int = 10) -> list[int]:
    """Recent course ids, to suggest valid targets in not-found errors."""
    return [r["id"] for r in store.query(
        "SELECT id FROM courses ORDER BY updated_at DESC LIMIT ?", (limit,))]


def _no_course(course_id) -> dict:
    return not_found("course", course_id, available=_course_ids(),
                     hint="use list_courses() to see valid course ids")


def _ensure_cols(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_cols("courses", {"deadline": "TEXT", "review_at": "TEXT", "tags": "TEXT DEFAULT ''"})


_MAX_FETCH_BYTES = 2_000_000  # cap downloaded page size (~2MB) to bound memory


def _fetch_title(url: str) -> str:
    url = (url or "").strip()
    if not url.lower().startswith(("http://", "https://")):
        return ""
    try:
        import httpx
        from bs4 import BeautifulSoup
        with httpx.stream("GET", url, timeout=10, follow_redirects=True,
                          headers={"User-Agent": "Mozilla/5.0"}) as r:
            chunks, total = [], 0
            for chunk in r.iter_bytes():
                chunks.append(chunk)
                total += len(chunk)
                if total >= _MAX_FETCH_BYTES:
                    break
            html = b"".join(chunks).decode(r.encoding or "utf-8", errors="ignore")
        soup = BeautifulSoup(html, "html.parser")
        return (soup.title.string or "").strip()[:200] if soup.title else ""
    except Exception:
        return ""


# ---------------- Courses ----------------
@mcp.tool
def add_course(title: str, provider: str = "", url: str = "", hours: int = 0, status: str = "wishlist",
               deadline: str = "", tags: str = "") -> dict:
    """Add a course/tutorial (status: wishlist/in_progress/done). Optional deadline (ISO) and tags."""
    title = (title or "").strip()
    if not title:
        return err("title is required", hint="pass a non-empty course title")
    cid = store.execute(
        "INSERT INTO courses(title,provider,url,hours,status,deadline,tags,updated_at,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?)", (title, provider, url, max(0, hours), status, deadline, tags, _now(), _now()))
    return {"id": cid, "title": title}


@mcp.tool
def log_progress(course_id: int, progress_pct: int, note: str = "") -> dict:
    """Update a course's progress %. Auto-sets status from the percentage."""
    if not store.query_one("SELECT id FROM courses WHERE id=?", (course_id,)):
        return _no_course(course_id)
    pct = max(0, min(100, progress_pct))
    status = "done" if pct >= 100 else ("in_progress" if pct > 0 else "wishlist")
    store.execute("UPDATE courses SET progress_pct=?, status=?, notes=COALESCE(NULLIF(?,''),notes), updated_at=? WHERE id=?",
                  (pct, status, note, _now(), course_id))
    return {"ok": True, "id": course_id, "progress_pct": pct, "status": status}


@mcp.tool
def list_courses(status: str = "", tag: str = "") -> list[dict]:
    """List courses, optionally by status and/or tag."""
    sql = "SELECT id,title,provider,status,progress_pct,deadline,tags FROM courses WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"; params.append(status)
    if tag:
        sql += " AND tags LIKE ?"; params.append(f"%{tag}%")
    sql += " ORDER BY updated_at DESC LIMIT ?"
    params.append(_MAX_LIMIT)
    return store.query(sql, params)


@mcp.tool
def whats_next(limit: int = 5) -> list[dict]:
    """In-progress courses first (closest to done), then wishlist — what to work on next.
    Courses with a nearer deadline are prioritized within each group."""
    return store.query(
        "SELECT id,title,provider,status,progress_pct,deadline FROM courses WHERE status!='done' "
        "ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, "
        "CASE WHEN deadline IS NULL OR deadline='' THEN 1 ELSE 0 END, deadline ASC, progress_pct DESC LIMIT ?",
        (_clamp_limit(limit, 5),))


# ---------------- Study sessions & streaks ----------------
@mcp.tool
def log_study(minutes: int, course_id: int = 0, note: str = "") -> dict:
    """Log a study session of N minutes (optionally tied to a course). Powers streaks & reports."""
    if minutes <= 0:
        return err("minutes must be positive", hint="log a positive number of study minutes")
    cid = course_id or None
    if cid and not store.query_one("SELECT id FROM courses WHERE id=?", (cid,)):
        return _no_course(cid)
    sid = store.execute("INSERT INTO sessions(course_id,minutes,note,at) VALUES(?,?,?,?)",
                        (cid, minutes, note, _now()))
    if cid:
        store.execute("UPDATE courses SET updated_at=? WHERE id=?", (_now(), cid))
    return {"id": sid, "minutes": minutes, "streak": _current_streak()}


def _study_days() -> set[str]:
    rows = store.query("SELECT at FROM sessions")
    days = set()
    for r in rows:
        try:
            days.add(datetime.fromisoformat(r["at"]).date().isoformat())
        except (ValueError, TypeError):
            continue
    return days


def _current_streak() -> int:
    days = _study_days()
    if not days:
        return 0
    today = datetime.now(timezone.utc).date()
    # Streak counts back from today; allow it to also count from yesterday if today not yet logged.
    start = today if today.isoformat() in days else today - timedelta(days=1)
    if start.isoformat() not in days:
        return 0
    streak, d = 0, start
    while d.isoformat() in days:
        streak += 1
        d -= timedelta(days=1)
    return streak


@mcp.tool
def current_streak() -> dict:
    """Consecutive days (ending today/yesterday) with at least one study session."""
    return {"streak_days": _current_streak(), "total_study_days": len(_study_days())}


@mcp.tool
def study_calendar(days: int = 30) -> list[dict]:
    """Per-day study minutes for the last N days (heatmap-friendly)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=max(1, days))
    rows = store.query("SELECT minutes, at FROM sessions")
    by_day: dict[str, int] = {}
    for r in rows:
        try:
            dt = datetime.fromisoformat(r["at"])
        except (ValueError, TypeError):
            continue
        if dt < cutoff:
            continue
        d = dt.date().isoformat()
        by_day[d] = by_day.get(d, 0) + (r["minutes"] or 0)
    return [{"date": d, "minutes": m} for d, m in sorted(by_day.items())]


# ---------------- Goals ----------------
@mcp.tool
def set_goal(kind: str, target: int, period: str = "week") -> dict:
    """Set a learning goal. kind: 'minutes' (study time) or 'courses' (completed). period: day/week/month."""
    kind = (kind or "").strip().lower()
    if kind not in ("minutes", "courses"):
        return not_found("goal kind", kind, available=["minutes", "courses"],
                         hint="kind must be 'minutes' (study time) or 'courses' (completed)")
    if period not in ("day", "week", "month"):
        period = "week"
    gid = store.execute("INSERT INTO goals(kind,target,period,created_at) VALUES(?,?,?,?)",
                        (kind, max(1, target), period, _now()))
    return {"id": gid, "kind": kind, "target": target, "period": period}


def _period_cutoff(period: str) -> datetime:
    now = datetime.now(timezone.utc)
    return now - timedelta(days={"day": 1, "week": 7, "month": 30}.get(period, 7))


@mcp.tool
def goal_progress() -> list[dict]:
    """Progress toward each active goal in its period."""
    out = []
    for g in store.query("SELECT id,kind,target,period FROM goals ORDER BY created_at DESC"):
        cutoff = _period_cutoff(g["period"]).isoformat()
        if g["kind"] == "minutes":
            cur = store.query_one("SELECT COALESCE(SUM(minutes),0) AS s FROM sessions WHERE at>=?", (cutoff,))["s"]
        else:
            cur = store.query_one("SELECT COUNT(*) AS s FROM courses WHERE status='done' AND updated_at>=?", (cutoff,))["s"]
        target = g["target"] or 0
        pct = round(100 * min(1.0, cur / target), 1) if target > 0 else 100.0
        out.append({"id": g["id"], "kind": g["kind"], "period": g["period"], "target": g["target"],
                    "current": cur, "met": cur >= target,
                    "pct": pct})
    return out


# ---------------- Resources ----------------
@mcp.tool
def add_resource(course_id: int, url: str, kind: str = "link", title: str = "", fetch_title: bool = True) -> dict:
    """Attach a resource link to a course (kind: link/video/book/article). Fetches page title if absent."""
    if not store.query_one("SELECT id FROM courses WHERE id=?", (course_id,)):
        return _no_course(course_id)
    if not title and fetch_title:
        title = _fetch_title(url)
    rid = store.execute("INSERT INTO resources(course_id,url,title,kind,created_at) VALUES(?,?,?,?,?)",
                        (course_id, url, title, kind, _now()))
    return {"id": rid, "url": url, "title": title}


@mcp.tool
def list_resources(course_id: int) -> list[dict]:
    """List resources attached to a course."""
    return store.query("SELECT id,url,title,kind FROM resources WHERE course_id=? ORDER BY created_at LIMIT ?",
                       (course_id, _MAX_LIMIT))


# ---------------- Spaced review of finished material ----------------
@mcp.tool
def mark_for_review(course_id: int, days: int = 7) -> dict:
    """Schedule a learned course to resurface for review in N days."""
    if not store.query_one("SELECT id FROM courses WHERE id=?", (course_id,)):
        return _no_course(course_id)
    review_at = (datetime.now(timezone.utc) + timedelta(days=max(1, days))).isoformat()
    store.execute("UPDATE courses SET review_at=? WHERE id=?", (review_at, course_id))
    return {"ok": True, "id": course_id, "review_at": review_at}


@mcp.tool
def reviews_due(limit: int = 20) -> list[dict]:
    """Courses whose spaced-review date has arrived."""
    return store.query("SELECT id,title,review_at FROM courses WHERE review_at IS NOT NULL AND review_at<=? "
                       "ORDER BY review_at ASC LIMIT ?", (_now(), _clamp_limit(limit, 20)))


# ---------------- Plan & report ----------------
@mcp.tool
def generate_plan(hours_per_week: int = 5, max_weeks: int = 0) -> dict:
    """A week-by-week plan: order unfinished courses (in_progress + nearest deadline first) and bucket by
    weekly hours. Optional max_weeks cap."""
    courses = store.query(
        "SELECT title,hours,status,progress_pct,deadline FROM courses WHERE status!='done' "
        "ORDER BY CASE status WHEN 'in_progress' THEN 0 ELSE 1 END, "
        "CASE WHEN deadline IS NULL OR deadline='' THEN 1 ELSE 0 END, deadline ASC")
    hpw = max(1, hours_per_week)
    weeks, bucket, used = [], [], 0
    for c in courses:
        remaining = max(1, round((c["hours"] or 4) * (100 - c["progress_pct"]) / 100))
        if used + remaining > hpw and bucket:
            weeks.append(bucket); bucket, used = [], 0
        bucket.append({"course": c["title"], "est_hours": remaining}); used += remaining
    if bucket:
        weeks.append(bucket)
    if max_weeks > 0:
        weeks = weeks[:max_weeks]
    return {"hours_per_week": hpw, "weeks": [{"week": i + 1, "items": w} for i, w in enumerate(weeks)]}


@mcp.tool
def report(days: int = 7) -> dict:
    """Activity report over the last N days: hours studied, sessions, courses completed, streak."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    mins = store.query_one("SELECT COALESCE(SUM(minutes),0) AS s, COUNT(*) AS n FROM sessions WHERE at>=?", (cutoff,))
    done = store.query_one("SELECT COUNT(*) AS n FROM courses WHERE status='done' AND updated_at>=?", (cutoff,))["n"]
    per_course = store.query(
        "SELECT c.title, SUM(s.minutes) AS minutes FROM sessions s JOIN courses c ON c.id=s.course_id "
        "WHERE s.at>=? GROUP BY s.course_id ORDER BY minutes DESC", (cutoff,))
    return {"days": days, "hours_studied": round((mins["s"] or 0) / 60, 2), "sessions": mins["n"],
            "courses_completed": done, "streak_days": _current_streak(),
            "by_course": [{"course": r["title"], "hours": round((r["minutes"] or 0) / 60, 2)} for r in per_course]}


if __name__ == "__main__":
    mcp.run()
