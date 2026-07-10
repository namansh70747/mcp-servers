"""campaign — the single source of truth for "already contacted." Owns the outreach ledger +
suppression list so the pipeline NEVER mails the same company twice. Every server stays
independent; dedup flows through this shared store.

Company-level never-again + contact-level cooldown. The scheduled agent calls
filter_uncontacted() before pitching and record_outreach() after sending. CSV import/export +
analytics for the do-not-contact list and the ledger.
"""
from __future__ import annotations

import csv
from datetime import datetime, timedelta, timezone
from pathlib import Path

from mcp_base import BaseStore, data_dir, db_path, err, make_server, not_found, normalize_email

mcp = make_server(
    "campaign",
    instructions=("Outreach dedup ledger + suppression. filter_uncontacted(companies) before "
                  "pitching; record_outreach(...) after sending; add_suppression(...) for do-not-contact. "
                  "import/export CSV; analytics; per-contact cooldown."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS ledger(
  id INTEGER PRIMARY KEY, company TEXT, domain TEXT, contacts TEXT DEFAULT '',
  first_contacted_at TEXT, last_contacted_at TEXT, cooldown_until TEXT, count INTEGER DEFAULT 1
);
CREATE INDEX IF NOT EXISTS idx_ledger_domain ON ledger(domain);
CREATE INDEX IF NOT EXISTS idx_ledger_company ON ledger(company);
CREATE TABLE IF NOT EXISTS suppression(
  id INTEGER PRIMARY KEY, value TEXT UNIQUE, kind TEXT, reason TEXT, created_at TEXT
);
CREATE TABLE IF NOT EXISTS runs(id INTEGER PRIMARY KEY, started_at TEXT, note TEXT DEFAULT '');
CREATE TABLE IF NOT EXISTS outreach_log(
  id INTEGER PRIMARY KEY, company TEXT, domain TEXT, email TEXT, contact_name TEXT,
  sent_at TEXT, run_id INTEGER
);
CREATE INDEX IF NOT EXISTS idx_log_email ON outreach_log(email);
"""
store = BaseStore(db_path("campaign"), schema=SCHEMA)
OUT = data_dir("campaign")
VALID_KINDS = {"domain", "email", "company"}
MAX_IMPORT_BYTES = 10 * 1024 * 1024  # 10 MB cap on CSV/list imports


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _ensure_columns() -> None:
    """Additively migrate older rows: ledger (first_contacted_at, cooldown_until) and the
    per-send outreach_log (gmail_message_id, thread_id) for cross-server message linkage."""
    have = {r["name"] for r in store.query("PRAGMA table_info(ledger)")}
    for col, decl in {"first_contacted_at": "TEXT", "cooldown_until": "TEXT"}.items():
        if col not in have:
            store.execute(f"ALTER TABLE ledger ADD COLUMN {col} {decl}")
    have_log = {r["name"] for r in store.query("PRAGMA table_info(outreach_log)")}
    for col, decl in {"gmail_message_id": "TEXT", "thread_id": "TEXT"}.items():
        if col not in have_log:
            store.execute(f"ALTER TABLE outreach_log ADD COLUMN {col} {decl}")


_ensure_columns()


def _key(company: str, domain: str) -> str:
    return (domain or company or "").strip().lower()


def _suppressed(company: str, domain: str, email: str = "") -> bool:
    vals = [v.strip().lower() for v in (company, domain, email) if v]
    if not vals:
        return False
    qs = ",".join("?" * len(vals))
    row = store.query_one(f"SELECT 1 FROM suppression WHERE LOWER(value) IN ({qs})", vals)
    return bool(row)


@mcp.tool
def filter_uncontacted(companies: list[dict], respect_cooldown: bool = True) -> dict:
    """Given candidate companies [{company, domain}], return only those NOT already contacted
    and NOT suppressed. This is the dedup gate — call it before pitching. When respect_cooldown,
    a company whose cooldown_until is still in the future is dropped (reason='cooldown')."""
    fresh, skipped = [], []
    now_iso = _now().isoformat()
    for c in companies:
        company = (c.get("company") or "").strip()
        domain = (c.get("domain") or "").strip()
        row = store.query_one(
            "SELECT cooldown_until FROM ledger WHERE LOWER(COALESCE(domain,''))=? OR LOWER(COALESCE(company,''))=?",
            (_key("", domain), _key(company, "")),
        )
        if row:
            cd = row.get("cooldown_until")
            if respect_cooldown and cd and cd > now_iso:
                skipped.append({"company": company, "domain": domain, "reason": "cooldown",
                                "until": cd})
            else:
                skipped.append({"company": company, "domain": domain, "reason": "already_contacted"})
        elif _suppressed(company, domain):
            skipped.append({"company": company, "domain": domain, "reason": "suppressed"})
        else:
            fresh.append(c)
    return {"fresh": fresh, "skipped": skipped, "fresh_count": len(fresh)}


@mcp.tool
def record_outreach(company: str, domain: str = "", contacts: str = "", email: str = "",
                    contact_name: str = "", cooldown_days: int = 0,
                    gmail_message_id: str = "", thread_id: str = "") -> dict:
    """Mark a company as contacted (so it's never targeted again). `contacts` = who you emailed.
    Optionally log a specific contact (email/contact_name) and set a per-company cooldown_days
    after which it may be re-targeted (0 = never again). Optional gmail_message_id/thread_id link
    the per-send log row back to the actual Gmail message (P2 cross-server identity)."""
    email = normalize_email(email)
    now = _now()
    now_iso = now.isoformat()
    cooldown_until = (now + timedelta(days=cooldown_days)).isoformat() if cooldown_days > 0 else None
    existing = store.query_one(
        "SELECT id, count FROM ledger WHERE LOWER(COALESCE(domain,''))=? OR LOWER(COALESCE(company,''))=?",
        (_key("", domain), _key(company, "")),
    )
    if existing:
        store.execute("UPDATE ledger SET last_contacted_at=?, count=count+1, cooldown_until=?, "
                      "contacts=COALESCE(NULLIF(?,''),contacts) WHERE id=?",
                      (now_iso, cooldown_until, contacts, existing["id"]))
        rid = existing["id"]
        count = existing["count"] + 1
    else:
        rid = store.execute(
            "INSERT INTO ledger(company,domain,contacts,first_contacted_at,last_contacted_at,cooldown_until) "
            "VALUES(?,?,?,?,?,?)",
            (company, domain, contacts, now_iso, now_iso, cooldown_until))
        count = 1
    last_run = store.query_one("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1")
    store.execute(
        "INSERT INTO outreach_log(company,domain,email,contact_name,sent_at,run_id,"
        "gmail_message_id,thread_id) VALUES(?,?,?,?,?,?,?,?)",
        (company, domain, email.strip().lower(), contact_name, now_iso,
         last_run["id"] if last_run else None, gmail_message_id or None, thread_id or None))
    return {"ok": True, "id": rid, "company": company, "count": count,
            "cooldown_until": cooldown_until, "gmail_message_id": gmail_message_id or None,
            "thread_id": thread_id or None}


@mcp.tool
def is_contacted(company: str = "", domain: str = "", email: str = "") -> dict:
    """Check whether a company (by name/domain) has already been contacted, or is suppressed."""
    row = store.query_one(
        "SELECT company, last_contacted_at, count, cooldown_until FROM ledger "
        "WHERE LOWER(COALESCE(domain,''))=? OR LOWER(COALESCE(company,''))=?",
        (_key("", domain), _key(company, "")),
    )
    # Surface the most recent linked Gmail message/thread for this company (P2 identity), if logged.
    last_msg = store.query_one(
        "SELECT gmail_message_id, thread_id, sent_at FROM outreach_log "
        "WHERE LOWER(COALESCE(domain,''))=? OR LOWER(COALESCE(company,''))=? "
        "ORDER BY sent_at DESC LIMIT 1",
        (_key("", domain), _key(company, "")),
    )
    return {"contacted": bool(row), "record": row,
            "suppressed": _suppressed(company, domain, email),
            "gmail_message_id": (last_msg or {}).get("gmail_message_id"),
            "thread_id": (last_msg or {}).get("thread_id")}


@mcp.tool
def contact_cooldown(email: str) -> dict:
    """When was this specific person last contacted, and how many times? From the per-contact log."""
    email = email.strip().lower()
    rows = store.query("SELECT company, sent_at FROM outreach_log WHERE email=? ORDER BY sent_at DESC",
                       (email,))
    return {"email": email, "times_contacted": len(rows),
            "last_contacted_at": rows[0]["sent_at"] if rows else None, "history": rows[:10]}


@mcp.tool
def add_suppression(value: str, reason: str = "", kind: str = "domain") -> dict:
    """Add a domain/email/company to the permanent do-not-contact list."""
    kind = kind.strip().lower()
    if kind not in VALID_KINDS:
        return not_found("suppression kind", kind, available=sorted(VALID_KINDS),
                         hint="kind must be one of domain/email/company")
    value = value.strip().lower()
    if not value:
        return err("value is required", hint="pass the domain/email/company to suppress")
    before = store.query_one("SELECT 1 FROM suppression WHERE LOWER(value)=?", (value,))
    store.execute("INSERT OR IGNORE INTO suppression(value,kind,reason,created_at) VALUES(?,?,?,?)",
                  (value, kind, reason, _now().isoformat()))
    return {"ok": True, "value": value, "kind": kind, "newly_added": not before}


@mcp.tool
def bulk_add_suppression(values: list[str], kind: str = "domain", reason: str = "") -> dict:
    """Add many values to the do-not-contact list in one call. Returns how many were newly added."""
    if kind.strip().lower() not in VALID_KINDS:
        return not_found("suppression kind", kind.strip().lower(), available=sorted(VALID_KINDS),
                         hint="kind must be one of domain/email/company")
    added = 0
    for v in values:
        r = add_suppression(v, reason=reason, kind=kind)
        if r.get("newly_added"):
            added += 1
    return {"ok": True, "submitted": len(values), "newly_added": added}


@mcp.tool
def remove_suppression(value: str) -> dict:
    """Remove a value from the do-not-contact list (un-suppress)."""
    value = value.strip().lower()
    existed = store.query_one("SELECT 1 FROM suppression WHERE LOWER(value)=?", (value,))
    store.execute("DELETE FROM suppression WHERE LOWER(value)=?", (value,))
    return {"ok": True, "value": value, "removed": bool(existed)}


@mcp.tool
def list_suppressions(limit: int = 100) -> list[dict]:
    """List the do-not-contact entries."""
    return store.query("SELECT value, kind, reason, created_at FROM suppression ORDER BY created_at DESC LIMIT ?",
                       (limit,))


@mcp.tool
def import_suppression_csv(path: str, kind: str = "domain", reason: str = "imported") -> dict:
    """Bulk-import a do-not-contact list from a file. Accepts CSV (uses 'value'/'domain'/'email'/
    'company' column if present, else first column) or a plain newline-delimited list."""
    if not (path or "").strip():
        return err("path is required", hint="pass a path to a CSV or newline-delimited list")
    p = Path(path).expanduser()
    if not p.is_file():
        return not_found("file", str(p), hint="pass a path to an existing CSV or text file")
    if p.stat().st_size > MAX_IMPORT_BYTES:
        return err(f"file too large ({p.stat().st_size} bytes > {MAX_IMPORT_BYTES})",
                   hint=f"split the file under {MAX_IMPORT_BYTES} bytes")
    values: list[str] = []
    try:
        text = p.read_text(encoding="utf-8-sig")
        if "," in text or p.suffix.lower() == ".csv":
            reader = csv.DictReader(text.splitlines())
            cols = [c.lower() for c in (reader.fieldnames or [])]
            pick = next((c for c in ("value", "domain", "email", "company") if c in cols), None)
            if pick:
                for row in reader:
                    v = (row.get(pick) or row.get(pick.title()) or "").strip()
                    if v:
                        values.append(v)
            else:  # no header match: treat first column of each line
                for line in text.splitlines():
                    first = line.split(",")[0].strip()
                    if first:
                        values.append(first)
        else:
            values = [ln.strip() for ln in text.splitlines() if ln.strip()]
    except Exception as e:  # noqa: BLE001
        return {"error": str(e)}
    res = bulk_add_suppression(values, kind=kind, reason=reason)
    return {"path": str(p), "submitted": len(values), "newly_added": res.get("newly_added", 0)}


@mcp.tool
def export_suppression_csv(path: str = "") -> dict:
    """Export the do-not-contact list to a CSV file. Returns the path + count."""
    rows = store.query("SELECT value, kind, reason, created_at FROM suppression ORDER BY created_at DESC")
    out = Path(path).expanduser() if path else OUT / f"suppression_{datetime.now().date()}.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["value", "kind", "reason", "created_at"])
        w.writeheader()
        w.writerows(rows)
    return {"path": str(out), "count": len(rows)}


@mcp.tool
def export_ledger_csv(path: str = "") -> dict:
    """Export the contacted-companies ledger to a CSV file. Returns the path + count."""
    rows = store.query("SELECT company,domain,contacts,first_contacted_at,last_contacted_at,"
                       "cooldown_until,count FROM ledger ORDER BY last_contacted_at DESC")
    out = Path(path).expanduser() if path else OUT / f"ledger_{datetime.now().date()}.csv"
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=["company", "domain", "contacts", "first_contacted_at",
                                           "last_contacted_at", "cooldown_until", "count"])
        w.writeheader()
        w.writerows(rows)
    return {"path": str(out), "count": len(rows)}


@mcp.tool
def recent_outreach(days: int = 7, limit: int = 50) -> list[dict]:
    """Recent per-contact sends from the outreach log (last N days)."""
    cutoff = (_now() - timedelta(days=days)).isoformat()
    return store.query(
        "SELECT company, email, contact_name, sent_at, gmail_message_id, thread_id "
        "FROM outreach_log WHERE sent_at >= ? "
        "ORDER BY sent_at DESC LIMIT ?", (cutoff, limit))


@mcp.tool
def next_run_due(interval_days: int = 7) -> dict:
    """Is the next scheduled outreach run due? Compares now to the last recorded run."""
    last = store.query_one("SELECT started_at FROM runs ORDER BY started_at DESC LIMIT 1")
    if not last:
        return {"due": True, "reason": "no prior run"}
    last_dt = datetime.fromisoformat(last["started_at"])
    due_at = last_dt + timedelta(days=interval_days)
    return {"due": _now() >= due_at, "last_run": last["started_at"], "due_at": due_at.isoformat()}


@mcp.tool
def record_run(note: str = "") -> dict:
    """Record that an outreach run started now (for scheduling bookkeeping)."""
    rid = store.execute("INSERT INTO runs(started_at, note) VALUES(?,?)", (_now().isoformat(), note))
    return {"ok": True, "run_id": rid}


@mcp.tool
def campaign_stats() -> dict:
    """Totals: companies contacted, suppressions, runs."""
    return {
        "companies_contacted": store.query_one("SELECT COUNT(*) AS n FROM ledger")["n"],
        "total_sends": store.query_one("SELECT COALESCE(SUM(count),0) AS n FROM ledger")["n"],
        "suppressions": store.query_one("SELECT COUNT(*) AS n FROM suppression")["n"],
        "runs": store.query_one("SELECT COUNT(*) AS n FROM runs")["n"],
    }


@mcp.tool
def analytics() -> dict:
    """Richer rollup: avg sends/company, top companies, suppression by kind, contacts logged, last run."""
    n_companies = store.query_one("SELECT COUNT(*) AS n FROM ledger")["n"]
    total_sends = store.query_one("SELECT COALESCE(SUM(count),0) AS n FROM ledger")["n"]
    top = store.query("SELECT company, count FROM ledger ORDER BY count DESC, last_contacted_at DESC LIMIT 10")
    supp_by_kind = {r["kind"] or "(none)": r["n"]
                    for r in store.query("SELECT kind, COUNT(*) AS n FROM suppression GROUP BY kind")}
    last_run = store.query_one("SELECT started_at, note FROM runs ORDER BY started_at DESC LIMIT 1")
    return {
        "companies_contacted": n_companies,
        "total_sends": total_sends,
        "avg_sends_per_company": round(total_sends / n_companies, 2) if n_companies else 0,
        "top_companies": top,
        "suppression_by_kind": supp_by_kind,
        "contacts_logged": store.query_one("SELECT COUNT(*) AS n FROM outreach_log")["n"],
        "last_run": last_run,
    }


if __name__ == "__main__":
    mcp.run()
