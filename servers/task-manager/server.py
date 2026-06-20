"""task-manager — todos with priority, due dates, subtasks, projects/tags, recurrence, Eisenhower
quadrants, natural-language due parsing, and ICS export (SQLite). calendar_payload() emits an event dict
to hand to the google_workspace server (kept independent — no cross-server calls)."""
from __future__ import annotations

import re
from datetime import date, datetime, time, timedelta, timezone

from mcp_base import BaseStore, data_dir, db_path, err, make_server, not_found

mcp = make_server(
    "task-manager",
    instructions=("Todos: add_task (natural-language due ok), complete, list_due/list_tasks, subtasks, "
                  "projects/tags, recurring tasks, eisenhower(), export_ics. calendar_payload(id) -> "
                  "google_workspace.create_event."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS tasks(
  id INTEGER PRIMARY KEY, title TEXT, priority TEXT DEFAULT 'med', due TEXT,
  status TEXT DEFAULT 'open', notes TEXT DEFAULT '', created_at TEXT
);
"""
store = BaseStore(db_path("task-manager"), schema=SCHEMA)
PRIOS = {"low", "med", "high"}


def _now():
    return datetime.now(timezone.utc).isoformat()


def _recent_task_ids(limit: int = 10) -> list[int]:
    """Recent task ids, newest first — handed to not_found() so a weak agent can recover."""
    return [r["id"] for r in store.query(
        "SELECT id FROM tasks ORDER BY id DESC LIMIT ?", (limit,))]


def _ensure_cols(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_cols("tasks", {
    "parent_id": "INTEGER",
    "project": "TEXT DEFAULT ''",
    "tags": "TEXT DEFAULT ''",
    "recurrence": "TEXT DEFAULT ''",
    "importance": "INTEGER DEFAULT 0",
    "completed_at": "TEXT",
})


# ---------------- Natural-language due parsing (stdlib only) ----------------
_WEEKDAYS = {"monday": 0, "mon": 0, "tuesday": 1, "tue": 1, "tues": 1, "wednesday": 2, "wed": 2,
             "thursday": 3, "thu": 3, "thurs": 3, "friday": 4, "fri": 4, "saturday": 5, "sat": 5,
             "sunday": 6, "sun": 6}


def _parse_time(text: str) -> time | None:
    m = re.search(r"\b(\d{1,2})(?::(\d{2}))?\s*(am|pm)\b", text)
    if m:
        h = int(m.group(1)) % 12
        if m.group(3) == "pm":
            h += 12
        return time(h, int(m.group(2) or 0))
    m = re.search(r"\b(\d{1,2}):(\d{2})\b", text)
    if m:
        return time(int(m.group(1)) % 24, int(m.group(2)))
    if "noon" in text:
        return time(12, 0)
    if "midnight" in text:
        return time(0, 0)
    return None


def _parse_due(text: str) -> str | None:
    """Parse natural-language or ISO due text into an ISO string. Returns None if unparseable."""
    if not text:
        return None
    raw = text.strip()
    # Already ISO?
    try:
        return datetime.fromisoformat(raw.replace("Z", "+00:00")).isoformat()
    except ValueError:
        pass
    t = raw.lower()
    today = datetime.now().date()
    tod = _parse_time(t)
    target: date | None = None

    if "today" in t or "tonight" in t:
        target = today
        if "tonight" in t and not tod:
            tod = time(20, 0)
    elif "tomorrow" in t or "tmrw" in t:
        target = today + timedelta(days=1)
    elif "yesterday" in t:
        target = today - timedelta(days=1)
    else:
        m = re.search(r"in\s+(\d+)\s+(day|days|week|weeks|hour|hours|month|months)", t)
        if m:
            n = int(m.group(1)); unit = m.group(2)
            if unit.startswith("day"):
                target = today + timedelta(days=n)
            elif unit.startswith("week"):
                target = today + timedelta(weeks=n)
            elif unit.startswith("month"):
                target = today + timedelta(days=30 * n)
            elif unit.startswith("hour"):
                return (datetime.now(timezone.utc) + timedelta(hours=n)).replace(microsecond=0).isoformat()
        else:
            m = re.search(r"(next\s+)?(" + "|".join(_WEEKDAYS) + r")\b", t)
            if m:
                wd = _WEEKDAYS[m.group(2)]
                ahead = (wd - today.weekday()) % 7
                if ahead == 0:
                    ahead = 7  # "monday" => next monday, not today
                if m.group(1) and ahead <= 7:  # "next" pushes a week if needed
                    pass
                target = today + timedelta(days=ahead)
            else:
                # bare date like "july 1" or "1 jul" or "7/1"
                m = re.search(r"\b(\d{1,2})/(\d{1,2})(?:/(\d{2,4}))?\b", t)
                if m:
                    mo, dy = int(m.group(1)), int(m.group(2))
                    yr = int(m.group(3)) if m.group(3) else today.year
                    if yr < 100:
                        yr += 2000
                    try:
                        target = date(yr, mo, dy)
                    except ValueError:
                        target = None
    if target is None:
        return None
    dt = datetime.combine(target, tod or time(9, 0)).astimezone(timezone.utc)
    return dt.replace(microsecond=0).isoformat()


@mcp.tool
def parse_due(text: str) -> dict:
    """Parse natural-language due text ('tomorrow 5pm', 'next mon', 'in 3 days', '7/1') into ISO."""
    iso = _parse_due(text)
    return {"input": text, "iso": iso, "parsed": iso is not None}


# ---------------- Core CRUD (preserved + deepened) ----------------
def _create_task(title: str, priority: str, due: str, notes: str, *, parent_id=None,
                 project="", tags="", recurrence="", importance=0) -> int:
    if priority not in PRIOS:
        priority = "med"
    due_iso = _parse_due(due) if due else ""
    return store.execute(
        "INSERT INTO tasks(title,priority,due,notes,parent_id,project,tags,recurrence,importance,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)",
        (title, priority, due_iso or "", notes, parent_id, project, tags, recurrence, 1 if importance else 0, _now()))


@mcp.tool
def add_task(title: str, priority: str = "med", due: str = "", notes: str = "",
             project: str = "", tags: str = "", recurrence: str = "", importance: bool = False) -> dict:
    """Add a task. `due` accepts ISO OR natural language ('tomorrow 5pm', 'next fri', 'in 3 days').
    `recurrence`: daily / weekly / monthly / 'every:N:days'. `importance` marks it Eisenhower-important."""
    title = (title or "").strip()
    if not title:
        return {"error": "title is required"}
    if priority not in PRIOS:
        priority = "med"
    tid = _create_task(title, priority, due, notes, project=project, tags=tags,
                       recurrence=recurrence.strip(), importance=importance)
    row = store.query_one("SELECT due FROM tasks WHERE id=?", (tid,))
    return {"id": tid, "title": title, "priority": priority, "due": row["due"]}


@mcp.tool
def add_subtask(parent_id: int, title: str, priority: str = "med", due: str = "", notes: str = "") -> dict:
    """Add a subtask under a parent task."""
    if not store.query_one("SELECT id FROM tasks WHERE id=?", (parent_id,)):
        return not_found("parent task", parent_id, available=_recent_task_ids(),
                         hint="use list_tasks()")
    title = (title or "").strip()
    if not title:
        return {"error": "title is required"}
    tid = _create_task(title, priority if priority in PRIOS else "med", due, notes, parent_id=parent_id)
    return {"id": tid, "parent_id": parent_id, "title": title}


@mcp.tool
def subtasks(task_id: int) -> list[dict]:
    """List subtasks of a task."""
    return store.query("SELECT id,title,priority,due,status FROM tasks WHERE parent_id=? ORDER BY id", (task_id,))


@mcp.tool
def get_task(task_id: int) -> dict:
    """Fetch a single task by id with all of its fields (returns an error if not found)."""
    row = store.query_one(
        "SELECT id,title,priority,due,status,notes,parent_id,project,tags,recurrence,"
        "importance,created_at,completed_at FROM tasks WHERE id=?", (task_id,))
    if not row:
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    return row


def _next_due(due_iso: str, recurrence: str) -> str | None:
    if not recurrence:
        return None
    try:
        base = datetime.fromisoformat(due_iso) if due_iso else datetime.now()
    except ValueError:
        base = datetime.now()
    r = recurrence.strip().lower()
    if r == "daily":
        nxt = base + timedelta(days=1)
    elif r == "weekly":
        nxt = base + timedelta(weeks=1)
    elif r == "monthly":
        nxt = base + timedelta(days=30)
    else:
        m = re.match(r"every:(\d+):(day|days|week|weeks)", r)
        if not m:
            return None
        n = int(m.group(1))
        nxt = base + (timedelta(weeks=n) if m.group(2).startswith("week") else timedelta(days=n))
    return nxt.replace(microsecond=0).isoformat()


@mcp.tool
def complete(task_id: int) -> dict:
    """Mark a task done. If it has open subtasks they are also completed. If recurring, spawns the next
    occurrence and returns its id."""
    t = store.query_one("SELECT id,title,priority,due,notes,project,tags,recurrence,importance FROM tasks WHERE id=?",
                        (task_id,))
    if not t:
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    store.execute("UPDATE tasks SET status='done', completed_at=? WHERE id=? OR (parent_id=? AND status='open')",
                  (_now(), task_id, task_id))
    result = {"ok": True, "id": task_id}
    if t["recurrence"]:
        nxt = _next_due(t["due"], t["recurrence"])
        if nxt:
            nid = store.execute(
                "INSERT INTO tasks(title,priority,due,notes,project,tags,recurrence,importance,created_at) "
                "VALUES(?,?,?,?,?,?,?,?,?)",
                (t["title"], t["priority"], nxt, t["notes"], t["project"], t["tags"],
                 t["recurrence"], t["importance"], _now()))
            result["next_occurrence"] = {"id": nid, "due": nxt}
    return result


@mcp.tool
def reopen(task_id: int) -> dict:
    """Reopen a completed task."""
    if not store.query_one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    store.execute("UPDATE tasks SET status='open', completed_at=NULL WHERE id=?", (task_id,))
    return {"ok": True, "id": task_id}


@mcp.tool
def update_task(task_id: int, title: str = "", priority: str = "", due: str = "", notes: str = "",
                project: str = "", tags: str = "", recurrence: str = "") -> dict:
    """Update fields on a task (only non-empty args are applied). `due` accepts natural language."""
    if not store.query_one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    sets, params = [], []
    if title:
        sets.append("title=?"); params.append(title.strip())
    if priority and priority in PRIOS:
        sets.append("priority=?"); params.append(priority)
    if due:
        sets.append("due=?"); params.append(_parse_due(due) or due)
    if notes:
        sets.append("notes=?"); params.append(notes)
    if project:
        sets.append("project=?"); params.append(project)
    if tags:
        sets.append("tags=?"); params.append(tags)
    if recurrence:
        sets.append("recurrence=?"); params.append(recurrence.strip())
    if not sets:
        return {"error": "nothing to update"}
    params.append(task_id)
    store.execute(f"UPDATE tasks SET {','.join(sets)} WHERE id=?", params)
    return {"ok": True, "id": task_id}


@mcp.tool
def snooze(task_id: int, until: str) -> dict:
    """Push a task's due date out. `until` accepts natural language or ISO."""
    if not store.query_one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    iso = _parse_due(until)
    if not iso:
        return {"error": f"could not parse '{until}'"}
    store.execute("UPDATE tasks SET due=? WHERE id=?", (iso, task_id))
    return {"ok": True, "id": task_id, "due": iso}


@mcp.tool
def delete_task(task_id: int) -> dict:
    """Delete a task and its subtasks."""
    if not store.query_one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    store.execute("DELETE FROM tasks WHERE id=? OR parent_id=?", (task_id, task_id))
    return {"ok": True, "id": task_id}


@mcp.tool
def list_due(limit: int = 50) -> list[dict]:
    """Open tasks with a due date that has passed or is today, soonest first."""
    return store.query("SELECT id,title,priority,due FROM tasks WHERE status='open' AND due!='' AND due<=? "
                       "ORDER BY due ASC LIMIT ?", (_now(), max(1, limit)))


@mcp.tool
def list_tasks(status: str = "open", limit: int = 100, project: str = "", tag: str = "",
               parent_id: int = -1) -> list[dict]:
    """List tasks by status (open/done), optionally filtered by project, tag, or parent_id (-1 = any)."""
    sql = ("SELECT id,title,priority,due,status,project,tags,parent_id FROM tasks WHERE status=?")
    params: list = [status]
    if project:
        sql += " AND project=?"; params.append(project)
    if tag:
        sql += " AND tags LIKE ?"; params.append(f"%{tag}%")
    if parent_id >= 0:
        sql += " AND parent_id=?"; params.append(parent_id)
    sql += (" ORDER BY CASE priority WHEN 'high' THEN 0 WHEN 'med' THEN 1 ELSE 2 END, "
            "CASE WHEN due='' THEN 1 ELSE 0 END, due LIMIT ?")
    params.append(max(1, limit))
    return store.query(sql, params)


@mcp.tool
def list_projects() -> list[dict]:
    """List projects with open/total task counts."""
    return store.query(
        "SELECT COALESCE(NULLIF(project,''),'(none)') AS project, "
        "SUM(CASE WHEN status='open' THEN 1 ELSE 0 END) AS open, COUNT(*) AS total "
        "FROM tasks GROUP BY COALESCE(NULLIF(project,''),'(none)') ORDER BY open DESC")


@mcp.tool
def agenda(days: int = 7) -> dict:
    """Open tasks grouped by due day for the next N days, plus an overdue bucket."""
    now = datetime.now(timezone.utc)
    horizon = (now + timedelta(days=max(1, days))).isoformat()
    rows = store.query("SELECT id,title,priority,due FROM tasks WHERE status='open' AND due!='' AND due<=? "
                       "ORDER BY due ASC", (horizon,))
    out: dict[str, list] = {"overdue": []}
    for r in rows:
        d = _to_aware(r["due"])
        if d is None:
            continue
        if d < now:
            out["overdue"].append(r)
        else:
            day = d.date().isoformat()
            out.setdefault(day, []).append(r)
    return out


# ---------------- Eisenhower matrix ----------------
@mcp.tool
def set_importance(task_id: int, important: bool) -> dict:
    """Mark a task important (Eisenhower) or not."""
    if not store.query_one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    store.execute("UPDATE tasks SET importance=? WHERE id=?", (1 if important else 0, task_id))
    return {"ok": True, "id": task_id, "important": important}


@mcp.tool
def eisenhower(urgent_within_days: int = 2) -> dict:
    """Open tasks split into the 4 Eisenhower quadrants. Urgent = due within N days (or high priority);
    important = the `importance` flag (high priority counts as important too)."""
    now = datetime.now(timezone.utc)
    soon = now + timedelta(days=max(0, urgent_within_days))
    rows = store.query("SELECT id,title,priority,due,importance FROM tasks WHERE status='open'")
    q = {"do_now": [], "schedule": [], "delegate": [], "eliminate": []}
    for r in rows:
        urgent = r["priority"] == "high"
        if r["due"]:
            d = _to_aware(r["due"])
            if d is not None:
                urgent = urgent or d <= soon
        important = bool(r["importance"]) or r["priority"] == "high"
        item = {"id": r["id"], "title": r["title"], "due": r["due"], "priority": r["priority"]}
        if important and urgent:
            q["do_now"].append(item)
        elif important and not urgent:
            q["schedule"].append(item)
        elif not important and urgent:
            q["delegate"].append(item)
        else:
            q["eliminate"].append(item)
    return q


# ---------------- Calendar / ICS ----------------
@mcp.tool
def calendar_payload(task_id: int, duration_min: int = 30) -> dict:
    """Return a Google Calendar event payload for a task (pass to google_workspace.create_event)."""
    t = store.query_one("SELECT title, due FROM tasks WHERE id=?", (task_id,))
    if not t:
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    return {"summary": t["title"], "start": t["due"] or _now(), "duration_min": duration_min,
            "note": "Pass to google_workspace.create_event (this server stays independent)."}


def _to_aware(s: str) -> datetime | None:
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    return dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else dt


def _ics_dt(due: str) -> str | None:
    try:
        dt = datetime.fromisoformat(due)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _escape_ics(s: str) -> str:
    return (s or "").replace("\\", "\\\\").replace(";", "\\;").replace(",", "\\,").replace("\n", "\\n")


def _build_ics(status: str = "open", project: str = "") -> str:
    sql = "SELECT id,title,due,notes,priority FROM tasks WHERE due!=''"
    params: list = []
    if status:
        sql += " AND status=?"; params.append(status)
    if project:
        sql += " AND project=?"; params.append(project)
    rows = store.query(sql, params)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    lines = ["BEGIN:VCALENDAR", "VERSION:2.0", "PRODID:-//mcp-suite//task-manager//EN", "CALSCALE:GREGORIAN"]
    for r in rows:
        dt = _ics_dt(r["due"])
        if not dt:
            continue
        end = _ics_dt((datetime.fromisoformat(r["due"]) + timedelta(minutes=30)).isoformat()) or dt
        lines += [
            "BEGIN:VEVENT",
            f"UID:task-{r['id']}@mcp-suite",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{dt}",
            f"DTEND:{end}",
            f"SUMMARY:{_escape_ics(r['title'])}",
        ]
        if r["notes"]:
            lines.append(f"DESCRIPTION:{_escape_ics(r['notes'])}")
        lines.append("END:VEVENT")
    lines.append("END:VCALENDAR")
    return "\r\n".join(lines) + "\r\n"


@mcp.tool
def export_ics(status: str = "open", project: str = "") -> dict:
    """Export tasks with due dates as an RFC5545 ICS calendar (returns the text)."""
    return {"ics": _build_ics(status, project)}


@mcp.tool
def write_ics(path: str = "", status: str = "open", project: str = "") -> dict:
    """Write tasks to a .ics file. Default path: <data_dir>/exports/tasks.ics."""
    from pathlib import Path
    target = Path(path) if path else (data_dir("task-manager") / "exports" / "tasks.ics")
    try:
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(_build_ics(status, project), encoding="utf-8")
    except OSError as e:
        return {"ok": False, "error": f"could not write {target}: {e}"}
    return {"ok": True, "path": str(target)}


@mcp.tool
def bulk_complete(task_ids: list[int]) -> dict:
    """Complete several tasks at once. Returns per-id results (recurring tasks spawn next occurrences)."""
    results = []
    for tid in task_ids:
        results.append({"id": tid, **complete(tid)})
    done = sum(1 for r in results if r.get("ok"))
    return {"completed": done, "requested": len(task_ids), "results": results}


@mcp.tool
def move_to_project(task_id: int, project: str) -> dict:
    """Move a task (and its subtasks) into a project."""
    if not store.query_one("SELECT id FROM tasks WHERE id=?", (task_id,)):
        return not_found("task", task_id, available=_recent_task_ids(), hint="use list_tasks()")
    store.execute("UPDATE tasks SET project=? WHERE id=? OR parent_id=?", (project, task_id, task_id))
    return {"ok": True, "id": task_id, "project": project}


@mcp.tool
def search_tasks(query: str, status: str = "", limit: int = 50) -> list[dict]:
    """Search tasks by substring in title or notes. Empty status = any status."""
    q = (query or "").strip()
    if not q:
        return []
    sql = "SELECT id,title,priority,due,status,project FROM tasks WHERE (title LIKE ? OR notes LIKE ?)"
    params: list = [f"%{q}%", f"%{q}%"]
    if status:
        sql += " AND status=?"; params.append(status)
    sql += " ORDER BY status, due LIMIT ?"; params.append(max(1, limit))
    return store.query(sql, params)


@mcp.tool
def overdue(limit: int = 50) -> list[dict]:
    """Open tasks whose due date is strictly in the past, oldest-overdue first, with days overdue."""
    now = datetime.now(timezone.utc)
    rows = store.query("SELECT id,title,priority,due,project FROM tasks WHERE status='open' AND due!='' AND due<? "
                       "ORDER BY due ASC LIMIT ?", (now.isoformat(), max(1, limit)))
    for r in rows:
        d = _to_aware(r["due"])
        r["days_overdue"] = (now - d).days if d else None
    return rows


@mcp.tool
def completion_report(days: int = 7) -> dict:
    """Throughput over the last N days: tasks completed, completions by day, and by project."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=max(1, days))).isoformat()
    rows = store.query("SELECT project, completed_at FROM tasks WHERE status='done' AND completed_at IS NOT NULL "
                       "AND completed_at>=?", (cutoff,))
    by_day: dict[str, int] = {}
    by_project: dict[str, int] = {}
    for r in rows:
        d = _to_aware(r["completed_at"])
        if d:
            by_day[d.date().isoformat()] = by_day.get(d.date().isoformat(), 0) + 1
        p = r["project"] or "(none)"
        by_project[p] = by_project.get(p, 0) + 1
    return {"days": days, "completed": len(rows),
            "by_day": dict(sorted(by_day.items())),
            "by_project": dict(sorted(by_project.items(), key=lambda x: -x[1]))}


@mcp.tool
def stats() -> dict:
    """Counts: open/done/overdue, by priority, and projects."""
    one = lambda q, p=(): store.query_one(q, p)["n"]
    now = _now()
    by_prio = {r["priority"]: r["n"] for r in store.query(
        "SELECT priority, COUNT(*) AS n FROM tasks WHERE status='open' GROUP BY priority")}
    return {
        "open": one("SELECT COUNT(*) AS n FROM tasks WHERE status='open'"),
        "done": one("SELECT COUNT(*) AS n FROM tasks WHERE status='done'"),
        "overdue": one("SELECT COUNT(*) AS n FROM tasks WHERE status='open' AND due!='' AND due<?", (now,)),
        "open_by_priority": by_prio,
        "projects": one("SELECT COUNT(DISTINCT NULLIF(project,'')) AS n FROM tasks"),
    }


if __name__ == "__main__":
    mcp.run()
