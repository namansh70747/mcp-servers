"""jobtrack — job-application pipeline tracker (SQLite). Complements reachout (which tracks
email outreach); jobtrack tracks formal applications, interview rounds, and their stages.

Adds interview notes, status history, CSV import/export, and funnel analytics."""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp_base import BaseStore, db_path, err, make_server, not_found, semantic

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
    "jd_text": "TEXT DEFAULT ''",  # optional full job-description text (powers match_score)
})

# Sidecar vector table for hybrid semantic search over applications. Degrades to keyword-only
# when no local embedding model is installed (semantic.* is a no-op in that case).
store.migrate(semantic.vec_table_sql("jobtrack_vec"))

_VEC_COLS = ("company", "role", "jd_url", "jd_text", "notes")


def _reindex(rid: int) -> None:
    """(Re)embed one application's searchable text. No-op without an embedding model."""
    r = store.query_one(
        "SELECT company,role,jd_url,jd_text,notes FROM applications WHERE id=?", (rid,))
    if r:
        semantic.index_row(store, "jobtrack_vec", rid,
                           " ".join(str(r.get(k) or "") for k in _VEC_COLS))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recent_application_ids(limit: int = 10) -> list[int]:
    """Recent application ids, newest first — handed to not_found() so a weak agent can recover."""
    return [r["id"] for r in store.query(
        "SELECT id FROM applications ORDER BY id DESC LIMIT ?", (limit,))]


def _add_application(company: str, role: str, jd_url: str = "", status: str = "applied",
                     followup_in_days: int = 7, notes: str = "", contact_name: str = "",
                     contact_email: str = "", location: str = "", salary: str = "",
                     source: str = "", contact_id: int | None = None, jd_text: str = "") -> dict:
    if not (company or "").strip() or not (role or "").strip():
        return {"error": "company and role are required"}
    if status not in STATUSES:
        return {"error": f"status must be one of {STATUSES}"}
    nf = (datetime.now(timezone.utc) + timedelta(days=followup_in_days)).isoformat()
    aid = store.execute(
        "INSERT INTO applications(company,role,jd_url,jd_text,status,applied_at,next_followup,notes,"
        "contact_name,contact_email,location,salary,source,contact_id,created_at,updated_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (company, role, jd_url, jd_text, status, _now(), nf, notes, contact_name, contact_email,
         location, salary, source, contact_id, _now(), _now()))
    store.execute("INSERT INTO status_history(application_id,status,changed_at) VALUES(?,?,?)",
                  (aid, status, _now()))
    _reindex(aid)
    return {"id": aid, "company": company, "role": role, "status": status}


@mcp.tool
def add_application(company: str, role: str, jd_url: str = "", status: str = "applied",
                    followup_in_days: int = 7, notes: str = "", contact_name: str = "",
                    contact_email: str = "", location: str = "", salary: str = "",
                    source: str = "", contact_id: int | None = None, jd_text: str = "") -> dict:
    """Log an application. Sets a follow-up reminder N days out. Optional contact/location/source.
    Optional `contact_id` links to a contacts-server record (P2 cross-server identity).
    Optional `jd_text` stores the full job-description text (improves search + match_score)."""
    return _add_application(company, role, jd_url, status, followup_in_days, notes,
                            contact_name, contact_email, location, salary, source, contact_id,
                            jd_text)


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
    if (notes or "").strip():
        _reindex(application_id)
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
    semantic.drop_row(store, "jobtrack_vec", application_id)
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
    _reindex(application_id)
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
    """Hybrid search across company / role / job description / notes: keyword (substring) fused with
    semantic vector similarity so a query like 'distributed backend in Berlin' surfaces relevant roles
    even without those exact words. Falls back to keyword-only when no embedding model is installed."""
    q = (query or "").strip()
    if not q:
        return []
    like = f"%{q}%"
    fts_ids = [r["id"] for r in store.query(
        "SELECT id FROM applications "
        "WHERE company LIKE ? OR role LIKE ? OR location LIKE ? OR notes LIKE ? "
        "OR jd_url LIKE ? OR jd_text LIKE ? "
        "ORDER BY applied_at DESC LIMIT 50", (like, like, like, like, like, like))]
    vec = [rid for rid, _ in semantic.vector_hits(store, "jobtrack_vec", q, limit=50)]
    ids = semantic.rrf(fts_ids, vec, max(1, limit)) if vec else fts_ids[:max(1, limit)]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in store.query(
        f"SELECT id,company,role,status,location FROM applications WHERE id IN ({ph})", tuple(ids))}
    return [rows[i] for i in ids if i in rows]


# --- keyword extraction + JD/résumé matching --------------------------------
_STOP = {
    "a", "an", "the", "and", "or", "of", "to", "in", "on", "for", "with", "as", "at", "by", "is",
    "are", "be", "will", "we", "you", "our", "your", "their", "they", "this", "that", "these",
    "those", "it", "its", "from", "into", "over", "per", "via", "etc", "e", "g", "ie", "eg",
    "able", "have", "has", "had", "who", "what", "when", "where", "which", "such", "than", "then",
    "also", "but", "not", "all", "any", "can", "may", "should", "must", "would", "could", "do",
    "does", "job", "role", "work", "working", "team", "company", "experience", "years", "year",
    "strong", "good", "great", "excellent", "ability", "including", "include", "includes", "plus",
    "preferred", "required", "requirements", "responsibilities", "skills", "looking", "candidate",
    "candidates", "ideal", "across", "within", "using", "use", "used", "help", "build", "building",
}


def _keywords(text: str) -> set[str]:
    """Lowercased significant tokens (length >= 3, not a stopword). Splits on non-alphanumerics but
    keeps intra-word + and # so 'c++' / 'c#' / 'node.js' survive reasonably."""
    if not (text or "").strip():
        return set()
    out: set[str] = set()
    token = ""
    for ch in text.lower():
        if ch.isalnum() or ch in "+#.":
            token += ch
        else:
            if token:
                out.add(token)
            token = ""
    if token:
        out.add(token)
    cleaned: set[str] = set()
    for t in out:
        t = t.strip(".")
        if len(t) >= 3 and t not in _STOP and not t.isdigit():
            cleaned.add(t)
    return cleaned


def _profile_terms() -> set[str]:
    """Best-effort keyword set drawn from profile.json (skills/experience/projects/headline)."""
    try:
        import json
        from mcp_base import repo_root
        p = repo_root() / "profile.json"
        if not p.is_file():
            return set()
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return set()
    parts: list[str] = []

    def _collect(v) -> None:
        if isinstance(v, str):
            parts.append(v)
        elif isinstance(v, list):
            for x in v:
                _collect(x)
        elif isinstance(v, dict):
            for x in v.values():
                _collect(x)

    for key in ("headline", "summary", "skills", "experience", "projects", "education"):
        _collect(data.get(key))
    return _keywords(" ".join(parts))


@mcp.tool
def match_score(application_id: int, resume_text: str = "") -> dict:
    """Score how well a résumé/profile matches an application's job description (0..1) and list the
    missing JD keywords to address.

    Compares the significant terms in the application's company/role/jd_url/jd_text/notes against the
    terms in `resume_text` (if given) or, when empty, your profile.json. Returns overlap ratio plus the
    matched and missing keywords. Purely lexical — never raises, no model required."""
    app = store.query_one(
        "SELECT company,role,jd_url,jd_text,notes FROM applications WHERE id=?", (application_id,))
    if not app:
        return not_found("application", application_id, available=_recent_application_ids(),
                         hint="use list_pipeline()")
    jd_terms = _keywords(" ".join(str(app.get(k) or "") for k in _VEC_COLS))
    if not jd_terms:
        return err("no job-description text to score against",
                   id=application_id,
                   hint="add jd_text via add_application(..., jd_text=...) or import_csv, "
                        "or append role detail with add_note()")
    source = "resume_text"
    cand_terms = _keywords(resume_text)
    if not cand_terms:
        cand_terms = _profile_terms()
        source = "profile.json"
    matched = sorted(jd_terms & cand_terms)
    missing = sorted(jd_terms - cand_terms)
    score = round(len(matched) / len(jd_terms), 3) if jd_terms else 0.0
    return {
        "ok": True,
        "id": application_id,
        "compared_against": source,
        "score": score,
        "matched_count": len(matched),
        "jd_keyword_count": len(jd_terms),
        "matched": matched[:50],
        "missing": missing[:50],
    }


@mcp.tool
def reindex_semantic() -> dict:
    """(Re)build semantic embeddings for all applications. Needs a local model (uv sync --group embed)."""
    if not semantic.available():
        return {"ok": False, "engine": "unavailable", "hint": "uv sync --group embed then call again"}
    n = 0
    for r in store.query("SELECT id FROM applications"):
        _reindex(r["id"])
        n += 1
    return {"ok": True, "indexed": n}


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
                source=row.get("source", ""), jd_text=row.get("jd_text", ""))
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
