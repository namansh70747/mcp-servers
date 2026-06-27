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

from mcp_base import BaseStore, data_dir, db_path, err, make_server, not_found

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


def _chunks(lst: list, n: int):
    """Yield n-sized chunks — keeps IN-clause params ≤ SQLite's 999-param limit (use ≤ 900)."""
    for i in range(0, len(lst), n):
        yield lst[i:i + n]


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
def bulk_record_outreach(rows: list[dict]) -> dict:
    """Record multiple outreach sends in one call. One IN-scan pre-fetches existing ledger rows;
    outreach_log inserts are batched with executemany (one DB transaction). Inputs chunked to
    ≤900 to stay within SQLite's 999-param IN limit.
    Each row: {company, domain?, contacts?, email?, contact_name?, cooldown_days?,
               gmail_message_id?, thread_id?}
    Returns {ok, recorded, updated_ledger, new_ledger}"""
    if not rows:
        return {"ok": True, "recorded": 0, "updated_ledger": 0, "new_ledger": 0}

    now = _now()
    now_iso = now.isoformat()
    last_run = store.query_one("SELECT id FROM runs ORDER BY started_at DESC LIMIT 1")
    run_id = last_run["id"] if last_run else None

    # Pre-fetch all existing ledger rows in one IN-scan
    company_vals = [_key(r.get("company") or "", "") for r in rows if r.get("company")]
    domain_vals = [_key("", r.get("domain") or "") for r in rows if r.get("domain")]
    all_keys = list(set(company_vals + domain_vals))
    existing: dict[str, dict] = {}
    for chunk in _chunks(all_keys, 900):
        qs = ",".join("?" * len(chunk))
        for row in store.query(
            f"SELECT id, company, domain, count FROM ledger "
            f"WHERE LOWER(COALESCE(domain,'')) IN ({qs}) "
            f"OR LOWER(COALESCE(company,'')) IN ({qs})",
            chunk + chunk,
        ):
            if row.get("domain"):
                existing[_key("", row["domain"])] = row
            if row.get("company"):
                existing[_key(row["company"], "")] = row

    updated_ledger = 0
    new_ledger = 0
    log_params: list[tuple] = []
    for r in rows:
        company = (r.get("company") or "").strip()
        domain = (r.get("domain") or "").strip()
        contacts = r.get("contacts") or ""
        email = (r.get("email") or "").strip().lower()
        contact_name = r.get("contact_name") or ""
        cooldown_days = int(r.get("cooldown_days") or 0)
        cooldown_until = (
            (now + timedelta(days=cooldown_days)).isoformat() if cooldown_days > 0 else None
        )
        ex = existing.get(_key("", domain)) or existing.get(_key(company, ""))
        if ex:
            store.execute(
                "UPDATE ledger SET last_contacted_at=?, count=count+1, cooldown_until=?, "
                "contacts=COALESCE(NULLIF(?,''),contacts) WHERE id=?",
                (now_iso, cooldown_until, contacts, ex["id"]),
            )
            updated_ledger += 1
        else:
            store.execute(
                "INSERT INTO ledger(company,domain,contacts,first_contacted_at,"
                "last_contacted_at,cooldown_until) VALUES(?,?,?,?,?,?)",
                (company, domain, contacts, now_iso, now_iso, cooldown_until),
            )
            new_ledger += 1
        log_params.append((company, domain, email, contact_name, now_iso, run_id,
                           r.get("gmail_message_id") or None, r.get("thread_id") or None))

    store.executemany(
        "INSERT INTO outreach_log(company,domain,email,contact_name,sent_at,run_id,"
        "gmail_message_id,thread_id) VALUES(?,?,?,?,?,?,?,?)",
        log_params,
    )
    return {"ok": True, "recorded": len(rows),
            "updated_ledger": updated_ledger, "new_ledger": new_ledger}


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
def bulk_is_contacted(keys: list[dict]) -> list[dict]:
    """Check whether multiple companies have been contacted in one call. ~3 IN-scans regardless
    of batch size (vs 3N queries for N serial is_contacted calls). Inputs chunked to ≤900.
    Each key: {company?, domain?, email?}.
    Returns a list aligned to input order: [{contacted, suppressed, cooldown_active, record?}]"""
    if not keys:
        return []

    now_iso = _now().isoformat()

    # One IN-scan on ledger for all company/domain keys
    company_vals = [_key(k.get("company") or "", "") for k in keys if k.get("company")]
    domain_vals = [_key("", k.get("domain") or "") for k in keys if k.get("domain")]
    all_ledger_keys = list(set(company_vals + domain_vals))
    ledger_rows: dict[str, dict] = {}
    for chunk in _chunks(all_ledger_keys, 900):
        qs = ",".join("?" * len(chunk))
        for row in store.query(
            f"SELECT company, domain, last_contacted_at, cooldown_until, count "
            f"FROM ledger WHERE LOWER(COALESCE(domain,'')) IN ({qs}) "
            f"OR LOWER(COALESCE(company,'')) IN ({qs})",
            chunk + chunk,
        ):
            if row.get("domain"):
                ledger_rows[_key("", row["domain"])] = row
            if row.get("company"):
                ledger_rows[_key(row["company"], "")] = row

    # One IN-scan on suppression for all values
    all_sup_vals = set()
    for k in keys:
        for v in (k.get("company", ""), k.get("domain", ""), k.get("email", "")):
            if v:
                all_sup_vals.add(v.strip().lower())
    suppressed_set: set[str] = set()
    for chunk in _chunks(list(all_sup_vals), 900):
        qs = ",".join("?" * len(chunk))
        for row in store.query(
            f"SELECT value FROM suppression WHERE LOWER(value) IN ({qs})", chunk
        ):
            suppressed_set.add(row["value"].lower())

    results = []
    for k in keys:
        company = (k.get("company") or "").strip()
        domain = (k.get("domain") or "").strip()
        email = (k.get("email") or "").strip().lower()
        row = ledger_rows.get(_key("", domain)) or ledger_rows.get(_key(company, ""))
        is_sup = bool(
            (company and company.lower() in suppressed_set) or
            (domain and domain.lower() in suppressed_set) or
            (email and email in suppressed_set)
        )
        cd = (row or {}).get("cooldown_until")
        cooldown_active = bool(cd and cd > now_iso)
        results.append({
            "contacted": bool(row),
            "suppressed": is_sup,
            "cooldown_active": cooldown_active,
            **({"record": dict(row)} if row else {}),
        })
    return results


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


def _parse_iso(s: str | None) -> datetime | None:
    """Tolerant ISO parse — returns None on anything unparseable (never raises)."""
    if not s or not isinstance(s, str):
        return None
    try:
        dt = datetime.fromisoformat(s)
    except (ValueError, TypeError):
        return None
    # Normalize naive timestamps to UTC so comparisons against _now() are safe.
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt


def _score_row(row: dict, now: datetime, replies: int = 0) -> dict:
    """Heuristic reply-likelihood for one ledger row. Returns a 0-1 score + factor breakdown.

    Pure function over already-fetched data; never touches the network and never raises.
    Higher score => better candidate to contact next. Factors:
      - recency:  longer since last touch => warmer to re-approach (decays in over ~120d)
      - touches:  more prior sends => fatigue => lower score
      - replies:  prior replies are a strong positive signal of engagement
      - cooldown: an active cooldown_until in the future zeroes the score (do-not-contact yet)
    """
    last_dt = _parse_iso(row.get("last_contacted_at")) or _parse_iso(row.get("first_contacted_at"))
    try:
        touches = int(row.get("count") or 0)
    except (TypeError, ValueError):
        touches = 0
    try:
        replies = max(0, int(replies or 0))
    except (TypeError, ValueError):
        replies = 0

    # Recency: 0 days since last touch -> ~0.1 (too soon), ramping up to ~1.0 around 120 days.
    if last_dt is None:
        recency = 1.0  # never contacted (or unknown) -> maximally fresh
        days_since = None
    else:
        days_since = max(0.0, (now - last_dt).total_seconds() / 86400.0)
        recency = round(min(1.0, 0.1 + 0.9 * min(days_since, 120.0) / 120.0), 4)

    # Fatigue: each prior touch past the first shaves the score; floor at 0.2.
    fatigue = round(max(0.2, 1.0 - 0.18 * max(0, touches - 1)), 4)

    # Engagement: prior replies are a strong positive (each reply +0.25, capped).
    engagement = round(min(1.0, replies * 0.25), 4)

    # Active cooldown gate.
    cd = _parse_iso(row.get("cooldown_until"))
    cooldown_active = bool(cd and cd > now)

    # Weighted blend, then boosted by engagement, then gated by cooldown.
    base = 0.55 * recency + 0.45 * fatigue
    score = base + (1.0 - base) * engagement  # replies pull the score toward 1.0
    if cooldown_active:
        score = 0.0
    score = round(max(0.0, min(1.0, score)), 4)

    return {
        "company": row.get("company"),
        "domain": row.get("domain"),
        "score": score,
        "touches": touches,
        "replies": replies,
        "days_since_last": round(days_since, 1) if days_since is not None else None,
        "last_contacted_at": row.get("last_contacted_at"),
        "cooldown_until": row.get("cooldown_until"),
        "cooldown_active": cooldown_active,
        "factors": {"recency": recency, "fatigue": fatigue, "engagement": engagement},
    }


@mcp.tool
def reply_likelihood(company: str = "", domain: str = "", replies: int = 0) -> dict:
    """Heuristic 0-1 score for how worth-it it is to (re)contact one company next, read-only over
    the outreach ledger. Blends recency-since-last-touch, send fatigue (#touches), prior `replies`
    (pass the count if you track engagement elsewhere, e.g. reachout), and an active cooldown gate
    (cooldown_until in the future -> score 0). Higher = better candidate. Never sends; never raises."""
    company = (company or "").strip()
    domain = (domain or "").strip()
    if not company and not domain:
        return err("company or domain is required",
                   hint="pass a company name and/or domain to score")
    row = store.query_one(
        "SELECT company, domain, first_contacted_at, last_contacted_at, cooldown_until, count "
        "FROM ledger WHERE LOWER(COALESCE(domain,''))=? OR LOWER(COALESCE(company,''))=?",
        (_key("", domain), _key(company, "")),
    )
    if not row:
        # Not in the ledger => never contacted => a maximally-fresh candidate.
        row = {"company": company or None, "domain": domain or None,
               "first_contacted_at": None, "last_contacted_at": None,
               "cooldown_until": None, "count": 0}
        scored = _score_row(row, _now(), replies=replies)
        scored["in_ledger"] = False
        return {"ok": True, **scored}
    scored = _score_row(row, _now(), replies=replies)
    scored["in_ledger"] = True
    return {"ok": True, **scored}


@mcp.tool
def score_contacts(limit: int = 50, include_cooldown: bool = False,
                   replies_by_company: dict | None = None) -> dict:
    """Rank every company in the ledger by reply_likelihood so you know who to contact next.
    Read-only; never raises. By default companies under an active cooldown are excluded (they
    score 0); pass include_cooldown=True to keep them in the list. Optionally pass
    replies_by_company={company_or_domain(lowercased): n} to fold in prior-reply counts you
    track elsewhere. Returns rows sorted high-to-low score."""
    try:
        limit = max(1, min(int(limit), 500))
    except (TypeError, ValueError):
        limit = 50
    rmap: dict = {}
    if isinstance(replies_by_company, dict):
        for k, v in replies_by_company.items():
            try:
                rmap[str(k).strip().lower()] = max(0, int(v))
            except (TypeError, ValueError):
                continue
    rows = store.query(
        "SELECT company, domain, first_contacted_at, last_contacted_at, cooldown_until, count "
        "FROM ledger ORDER BY last_contacted_at DESC")
    now = _now()
    scored = []
    for r in rows:
        rep = rmap.get((r.get("domain") or "").strip().lower()) \
            or rmap.get((r.get("company") or "").strip().lower()) or 0
        s = _score_row(r, now, replies=rep)
        if s["cooldown_active"] and not include_cooldown:
            continue
        scored.append(s)
    scored.sort(key=lambda x: x["score"], reverse=True)
    return {"ok": True, "count": len(scored), "ranked": scored[:limit]}


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
