"""jobtrack — job-application pipeline tracker (SQLite). Complements reachout (which tracks
email outreach); jobtrack tracks formal applications, interview rounds, and their stages.

Adds interview notes, status history, CSV import/export, and funnel analytics."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp_base import BaseStore, db_path, err, make_server, not_found

mcp = make_server(
    "jobtrack",
    instructions=("Track job applications: add_application, update_status, list_pipeline, "
                  "due_followups, add_interview, add_note, export_csv/import_csv, analytics."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS applications(
  id INTEGER PRIMARY KEY, company TEXT, role TEXT, jd_url TEXT DEFAULT '',
  status TEXT DEFAULT 'applied', applied_at TEXT, next_followup TEXT, notes TEXT DEFAULT '',
  created_at TEXT
);
CREATE TABLE IF NOT EXISTS interviews(
  id INTEGER PRIMARY KEY, application_id INTEGER, kind TEXT, scheduled_at TEXT,
  notes TEXT DEFAULT '', outcome TEXT DEFAULT '', created_at TEXT,
  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
);
CREATE TABLE IF NOT EXISTS status_history(
  id INTEGER PRIMARY KEY, application_id INTEGER, status TEXT, changed_at TEXT,
  FOREIGN KEY(application_id) REFERENCES applications(id) ON DELETE CASCADE
);
"""
store = BaseStore(db_path("jobtrack"), schema=SCHEMA)
STATUSES = ["wishlist", "applied", "phone_screen", "interview", "offer", "rejected", "accepted"]
_FUNNEL = ["applied", "phone_screen", "interview", "offer", "accepted"]


def _ensure_columns(table: str, cols: dict[str, str]) -> None:
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_columns("applications", {
    "contact_name": "TEXT DEFAULT ''",
    "contact_email": "TEXT DEFAULT ''",
    "location": "TEXT DEFAULT ''",
    "salary": "TEXT DEFAULT ''",
    "source": "TEXT DEFAULT ''",
    "updated_at": "TEXT",
    "contact_id": "INTEGER",
    "archived_at": "TEXT",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recent_application_ids(limit: int = 10) -> list[int]:
    """Recent application ids, newest first — handed to not_found() so a weak agent can recover."""
    return [r["id"] for r in store.query(
        "SELECT id FROM applications ORDER BY id DESC LIMIT ?", (limit,))]


def _add_application(company: str, role: str, jd_url: str = "", status: str = "applied",
                     followup_in_days: int = 7, notes: str = "", contact_name: str = "",
                     contact_email: str = "", location: str = "", salary: str = "",
                     source: str = "", contact_id: int | None = None) -> dict:
    if not (company or "").strip() or not (role or "").strip():
        return {"error": "company and role are required"}
    if status not in STATUSES:
        return {"error": f"status must be one of {STATUSES}"}
    nf = (datetime.now(timezone.utc) + timedelta(days=followup_in_days)).isoformat()
    aid = store.execute(
        "INSERT INTO applications(company,role,jd_url,status,applied_at,next_followup,notes,"
        "contact_name,contact_email,location,salary,source,contact_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (company, role, jd_url, status, _now(), nf, notes, contact_name, contact_email,
         location, salary, source, contact_id, _now(), _now()))
    store.execute("INSERT INTO status_history(application_id,status,changed_at) VALUES(?,?,?)",
                  (aid, status, _now()))
    return {"id": aid, "company": company, "role": role, "status": status}


@mcp.tool
def add_application(company: str, role: str, jd_url: str = "", status: str = "applied",
                    followup_in_days: int = 7, notes: str = "", contact_name: str = "",
                    contact_email: str = "", location: str = "", salary: str = "",
                    source: str = "", contact_id: int | None = None) -> dict:
    """Log an application. Sets a follow-up reminder N days out. Optional contact/location/source.
    Optional `contact_id` links to a contacts-server record (P2 cross-server identity)."""
    return _add_application(company, role, jd_url, status, followup_in_days, notes,
                            contact_name, contact_email, location, salary, source, contact_id)


@mcp.tool
def update_status(application_id: int, status: str, notes: str = "") -> dict:
    """Move an application to a new stage (wishlist/applied/phone_screen/interview/offer/rejected/accepted)."""
    if status not in STATUSES:
        return {"error": f"status must be one of {STATUSES}"}
    if not store.query_one("SELECT id FROM applications WHERE id=?", (application_id,)):
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    store.execute("UPDATE applications SET status=?, notes=COALESCE(NULLIF(?,''),notes), updated_at=? WHERE id=?",
                  (status, notes, _now(), application_id))
    store.execute("INSERT INTO status_history(application_id,status,changed_at) VALUES(?,?,?)",
                  (application_id, status, _now()))
    return {"ok": True, "id": application_id, "status": status}


@mcp.tool
def list_pipeline(status: str = "", limit: int = 100) -> list[dict]:
    """List applications, optionally filtered by status."""
    if status:
        return store.query("SELECT id,company,role,status,applied_at,next_followup FROM applications "
                           "WHERE status=? ORDER BY applied_at DESC LIMIT ?", (status, limit))
    return store.query("SELECT id,company,role,status,applied_at,next_followup FROM applications "
                       "ORDER BY applied_at DESC LIMIT ?", (limit,))


@mcp.tool
def due_followups(limit: int = 20) -> list[dict]:
    """Applications whose follow-up date has passed and aren't closed."""
    return store.query(
        "SELECT id,company,role,status,next_followup FROM applications "
        "WHERE next_followup <= ? AND status NOT IN ('rejected','accepted','offer') "
        "ORDER BY next_followup ASC LIMIT ?", (_now(), limit))


@mcp.tool
def stats() -> dict:
    """Counts by status."""
    rows = store.query("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")
    return {"by_status": {r["status"]: r["n"] for r in rows},
            "total": store.query_one("SELECT COUNT(*) AS n FROM applications")["n"]}


@mcp.tool
def get_application(application_id: int) -> dict:
    """Full record for one application incl. interviews and status history."""
    app = store.query_one("SELECT * FROM applications WHERE id=?", (application_id,))
    if not app:
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    app["interviews"] = store.query(
        "SELECT id,kind,scheduled_at,notes,outcome FROM interviews WHERE application_id=? ORDER BY id",
        (application_id,))
    app["history"] = store.query(
        "SELECT status,changed_at FROM status_history WHERE application_id=? ORDER BY id",
        (application_id,))
    return app


@mcp.tool
def for_contact(contact_id: int, limit: int = 50) -> list[dict]:
    """List applications linked to a given contacts-server contact_id (P2 cross-server identity)."""
    return store.query(
        "SELECT id,company,role,status,applied_at,next_followup FROM applications "
        "WHERE contact_id=? ORDER BY applied_at DESC LIMIT ?", (contact_id, limit))


@mcp.tool
def archive_application(application_id: int) -> dict:
    """Soft-archive an application (sets archived_at; keeps the row + history). Reversible."""
    if not store.query_one("SELECT id FROM applications WHERE id=?", (application_id,)):
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    store.execute("UPDATE applications SET archived_at=?, updated_at=? WHERE id=?",
                  (_now(), _now(), application_id))
    return {"ok": True, "id": application_id, "archived_at": _now()}


@mcp.tool
def delete_application(application_id: int) -> dict:
    """Hard-delete an application and its interviews/history (cascade). Errors if it doesn't exist."""
    if not store.query_one("SELECT id FROM applications WHERE id=?", (application_id,)):
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    store.execute("DELETE FROM interviews WHERE application_id=?", (application_id,))
    store.execute("DELETE FROM status_history WHERE application_id=?", (application_id,))
    store.execute("DELETE FROM applications WHERE id=?", (application_id,))
    return {"ok": True, "id": application_id, "deleted": True}


@mcp.tool
def add_interview(application_id: int, kind: str = "phone_screen", scheduled_at: str = "",
                  notes: str = "", outcome: str = "") -> dict:
    """Log an interview round (kind, optional scheduled time, notes, outcome)."""
    if not store.query_one("SELECT id FROM applications WHERE id=?", (application_id,)):
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    iid = store.execute(
        "INSERT INTO interviews(application_id,kind,scheduled_at,notes,outcome,created_at) "
        "VALUES(?,?,?,?,?,?)", (application_id, kind, scheduled_at, notes, outcome, _now()))
    return {"id": iid, "application_id": application_id, "kind": kind}


@mcp.tool
def list_interviews(application_id: int) -> list[dict]:
    """List interview rounds for an application."""
    return store.query(
        "SELECT id,kind,scheduled_at,notes,outcome,created_at FROM interviews "
        "WHERE application_id=? ORDER BY id", (application_id,))


@mcp.tool
def add_note(application_id: int, note: str) -> dict:
    """Append a timestamped note to an application (preserves existing notes)."""
    if not (note or "").strip():
        return err("note is required", id=application_id)
    app = store.query_one("SELECT notes FROM applications WHERE id=?", (application_id,))
    if not app:
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    stamp = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    merged = (app["notes"] + "\n" if app["notes"] else "") + f"[{stamp}] {note}"
    store.execute("UPDATE applications SET notes=?, updated_at=? WHERE id=?",
                  (merged, _now(), application_id))
    return {"ok": True, "id": application_id, "notes": merged}


@mcp.tool
def set_followup(application_id: int, in_days: int = 7) -> dict:
    """Reschedule the follow-up date N days from now."""
    if not store.query_one("SELECT id FROM applications WHERE id=?", (application_id,)):
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    nf = (datetime.now(timezone.utc) + timedelta(days=in_days)).isoformat()
    store.execute("UPDATE applications SET next_followup=?, updated_at=? WHERE id=?",
                  (nf, _now(), application_id))
    return {"ok": True, "id": application_id, "next_followup": nf}


@mcp.tool
def search(query: str, limit: int = 50) -> list[dict]:
    """Search applications by company, role, location, or notes (substring)."""
    like = f"%{query}%"
    return store.query(
        "SELECT id,company,role,status,location FROM applications "
        "WHERE company LIKE ? OR role LIKE ? OR location LIKE ? OR notes LIKE ? "
        "ORDER BY applied_at DESC LIMIT ?", (like, like, like, like, limit))


_CSV_COLS = ["company", "role", "status", "jd_url", "location", "salary", "source",
             "contact_name", "contact_email", "applied_at", "next_followup", "notes"]


@mcp.tool
def export_csv(path: str, status: str = "") -> dict:
    """Export applications to a CSV file (optionally filtered by status)."""
    sql = "SELECT * FROM applications"
    params: list = []
    if status:
        sql += " WHERE status=?"
        params.append(status)
    rows = store.query(sql + " ORDER BY id", params)
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=_CSV_COLS, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow({k: r.get(k, "") for k in _CSV_COLS})
    return {"ok": True, "path": str(out), "count": len(rows)}


@mcp.tool
def import_csv(path: str) -> dict:
    """Import applications from a CSV file. Requires at least company,role columns."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"error": f"no such file: {path}"}
    added, skipped = 0, 0
    with p.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            company = (row.get("company") or "").strip()
            role = (row.get("role") or "").strip()
            if not company or not role:
                skipped += 1
                continue
            status = (row.get("status") or "applied").strip() or "applied"
            if status not in STATUSES:
                status = "applied"
            _add_application(
                company, role, jd_url=row.get("jd_url", ""), status=status, notes=row.get("notes", ""),
                contact_name=row.get("contact_name", ""), contact_email=row.get("contact_email", ""),
                location=row.get("location", ""), salary=row.get("salary", ""),
                source=row.get("source", ""))
            added += 1
    return {"ok": True, "added": added, "skipped": skipped}


@mcp.tool
def analytics() -> dict:
    """Funnel + conversion analytics: counts per stage, conversion rates, time-to-offer.

    Stage reach is computed from status history (ever reached a stage), so closed
    applications still count toward the funnel they passed through."""
    by_status = {r["status"]: r["n"] for r in
                 store.query("SELECT status, COUNT(*) AS n FROM applications GROUP BY status")}
    total = store.query_one("SELECT COUNT(*) AS n FROM applications")["n"]

    reach = {s: 0 for s in _FUNNEL}
    for r in store.query("SELECT DISTINCT application_id, status FROM status_history"):
        if r["status"] in reach:
            reach[r["status"]] += 1
    # also count current status into reach (covers rows that never recorded history)
    funnel = []
    prev = None
    for stage in _FUNNEL:
        n = reach[stage]
        conv = round(n / prev * 100, 1) if prev else None
        funnel.append({"stage": stage, "count": n, "conversion_from_prev_pct": conv})
        prev = n if n else prev

    by_source = store.query(
        "SELECT COALESCE(NULLIF(source,''),'(none)') AS source, COUNT(*) AS n "
        "FROM applications GROUP BY source ORDER BY n DESC")
    offers = reach.get("offer", 0) + by_status.get("offer", 0)
    return {
        "total": total,
        "by_status": by_status,
        "funnel": funnel,
        "response_rate_pct": round((reach["phone_screen"] / reach["applied"] * 100), 1)
            if reach.get("applied") else None,
        "offer_rate_pct": round((reach["offer"] / reach["applied"] * 100), 1)
            if reach.get("applied") else None,
        "by_source": by_source,
    }


if __name__ == "__main__":
    mcp.run()
