"""contacts — a lightweight CRM for recruiters/CTOs/founders, linked (loosely, via company
+ email) to the outreach pipeline and jobtrack. Pure local SQLite.

CSV import/export, merge/dedupe, signature enrichment, tagging, and jobtrack hand-off — all free
and offline. No external services.
"""
from __future__ import annotations

import csv
import re
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import BaseStore, data_dir, db_path, err, make_server, not_found, semantic

mcp = make_server(
    "contacts",
    instructions=("Local CRM for outreach targets. add_contact/find/update_status; import_csv/export_csv; "
                  "find_duplicates/merge_contacts/dedupe; tag/enrich_from_signature; stats."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS contacts(
  id INTEGER PRIMARY KEY,
  name TEXT, email TEXT, company TEXT, domain TEXT, role TEXT, title TEXT,
  linkedin TEXT, github TEXT, twitter TEXT, phone TEXT, tags TEXT DEFAULT '',
  source TEXT, confidence TEXT DEFAULT 'unknown',
  status TEXT DEFAULT 'new', notes TEXT DEFAULT '', last_contacted_at TEXT,
  created_at TEXT, updated_at TEXT,
  UNIQUE(email, company)
);
CREATE INDEX IF NOT EXISTS idx_contacts_company ON contacts(company);
CREATE INDEX IF NOT EXISTS idx_contacts_email ON contacts(email);
"""
store = BaseStore(db_path("contacts"), schema=SCHEMA)
store.migrate(semantic.vec_table_sql("contacts_vec"))
OUT = data_dir("contacts")
STATUSES = {"new", "queued", "contacted", "replied", "bounced", "closed"}
MAX_IMPORT_BYTES = 25 * 1024 * 1024  # 25 MB cap on CSV import
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


_VEC_COLS = ("name", "company", "title", "role", "notes", "tags")


def _reindex(cid: int) -> None:
    """(Re)build the semantic vector for one contact from its searchable text columns. No-op without a model."""
    r = store.query_one(
        "SELECT name,company,title,role,notes,tags FROM contacts WHERE id=?", (cid,))
    if r:
        semantic.index_row(store, "contacts_vec", cid,
                           " ".join(str(r.get(k) or "") for k in _VEC_COLS))


def _recent_contact_ids(limit: int = 10) -> list[int]:
    """Recent contact ids, newest first — handed to not_found() so a weak agent can recover."""
    return [r["id"] for r in store.query(
        "SELECT id FROM contacts ORDER BY updated_at DESC LIMIT ?", (limit,))]


def _ensure_columns() -> None:
    """Additively migrate older DBs: add any missing columns without touching data."""
    have = {r["name"] for r in store.query("PRAGMA table_info(contacts)")}
    wanted = {
        "twitter": "TEXT", "phone": "TEXT", "tags": "TEXT DEFAULT ''",
        "last_contacted_at": "TEXT",
    }
    for col, decl in wanted.items():
        if col not in have:
            store.execute(f"ALTER TABLE contacts ADD COLUMN {col} {decl}")


_ensure_columns()


def _valid_email(email: str) -> bool:
    if not email:
        return True  # email is optional
    try:
        from email_validator import validate_email
        validate_email(email, check_deliverability=False)
        return True
    except ImportError:
        return bool(EMAIL_RE.fullmatch(email.strip()))
    except Exception:
        return False


def _domain_of(email: str) -> str:
    return email.split("@", 1)[1].strip().lower() if "@" in email else ""


def _merge_tags(*tagsets: str) -> str:
    seen: list[str] = []
    for ts in tagsets:
        for t in (ts or "").split(","):
            t = t.strip()
            if t and t.lower() not in {x.lower() for x in seen}:
                seen.append(t)
    return ",".join(seen)


@mcp.tool
def add_contact(name: str, company: str, email: str = "", role: str = "", title: str = "",
                domain: str = "", linkedin: str = "", github: str = "", source: str = "",
                confidence: str = "unknown", notes: str = "", phone: str = "",
                twitter: str = "", tags: str = "") -> dict:
    """Add or update a contact (unique by email+company). Validates email; derives domain from email."""
    if email and not _valid_email(email):
        return {"error": f"invalid email: {email!r}"}
    email = email.strip().lower()
    if not domain and email:
        domain = _domain_of(email)
    domain = domain.strip().lower()
    now = _now()
    cid = store.execute(
        "INSERT INTO contacts(name,email,company,domain,role,title,linkedin,github,twitter,phone,tags,"
        "source,confidence,notes,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(email,company) DO UPDATE SET name=excluded.name,role=excluded.role,"
        "title=excluded.title,domain=excluded.domain,linkedin=excluded.linkedin,"
        "github=excluded.github,twitter=excluded.twitter,phone=excluded.phone,"
        "tags=excluded.tags,source=excluded.source,confidence=excluded.confidence,"
        "updated_at=excluded.updated_at",
        (name, email, company, domain, role, title, linkedin, github, twitter, phone, tags,
         source, confidence, notes, now, now),
    )
    # On an upsert conflict SQLite returns the would-be rowid (not the updated row's id), so resolve
    # the canonical id by the unique key before indexing.
    row = store.query_one("SELECT id FROM contacts WHERE email=? AND company=?", (email, company))
    cid = row["id"] if row else cid
    _reindex(cid)
    return {"id": cid, "name": name, "company": company, "email": email}


_FIND_COLS = "id,name,email,company,role,title,status,confidence,tags"


@mcp.tool
def find(query: str = "", company: str = "", status: str = "", tag: str = "", limit: int = 25) -> list[dict]:
    """Search contacts by free text (name/company/title/role/notes/tags) and/or company/status/tag filters.

    When `query` is given this is a HYBRID search: keyword (LIKE) fused with semantic vector similarity
    via reciprocal-rank fusion, so 'infra hiring lead' surfaces a relevant contact even without those
    exact words. Falls back to keyword-only when no embedding model is installed. Any of
    company/status/tag further constrain the results. With no query at all it lists recent matches."""
    limit = max(1, min(int(limit) if isinstance(limit, (int, float)) else 25, 200))
    q = (query or "").strip()

    # Build the structured filter clause shared by both paths.
    filt, fparams = "", []
    if company:
        filt += " AND company LIKE ?"; fparams.append(f"%{company}%")
    if status:
        filt += " AND status = ?"; fparams.append(status)
    if tag:
        filt += " AND tags LIKE ?"; fparams.append(f"%{tag}%")

    if not q:
        # No free-text term: keep the original filter-only listing behavior.
        return store.query(
            f"SELECT {_FIND_COLS} FROM contacts WHERE 1=1{filt} ORDER BY updated_at DESC LIMIT ?",
            (*fparams, limit))

    like = f"%{q}%"
    kw_ids = [r["id"] for r in store.query(
        f"SELECT id FROM contacts WHERE (name LIKE ? OR email LIKE ? OR company LIKE ? "
        f"OR role LIKE ? OR title LIKE ? OR notes LIKE ? OR tags LIKE ?){filt} "
        f"ORDER BY updated_at DESC LIMIT 50",
        (like, like, like, like, like, like, like, *fparams))]
    vec_ids = [rid for rid, _ in semantic.vector_hits(store, "contacts_vec", q, limit=50)]
    ids = semantic.rrf(kw_ids, vec_ids, limit) if vec_ids else kw_ids[:limit]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in store.query(
        f"SELECT {_FIND_COLS} FROM contacts WHERE id IN ({ph}){filt}", (*ids, *fparams))}
    # Preserve fused rank order; filters above may have dropped some ids.
    return [rows[i] for i in ids if i in rows]


@mcp.tool
def find_similar(contact_id: int, limit: int = 10) -> list[dict]:
    """Find contacts semantically similar to a given one (by name/company/title/role/notes/tags).

    Useful for clustering near-duplicates or finding people in adjacent roles/companies. Returns []
    (never raises) for a bad id or when no embedding model is installed (uv sync --group embed)."""
    limit = max(1, min(int(limit) if isinstance(limit, (int, float)) else 10, 200))
    row = store.query_one(
        "SELECT name,company,title,role,notes,tags FROM contacts WHERE id=?", (contact_id,))
    if not row:
        return []
    seed = " ".join(str(row.get(k) or "") for k in _VEC_COLS).strip()
    if not seed:
        return []
    hits = semantic.vector_hits(store, "contacts_vec", seed, limit=limit + 1)
    ids = [rid for rid, _ in hits if rid != contact_id][:limit]
    if not ids:
        return []
    ph = ",".join("?" * len(ids))
    rows = {r["id"]: r for r in store.query(
        f"SELECT {_FIND_COLS} FROM contacts WHERE id IN ({ph})", tuple(ids))}
    return [rows[i] for i in ids if i in rows]


@mcp.tool
def reindex_semantic() -> dict:
    """(Re)build semantic embeddings for all contacts. Needs a local model (uv sync --group embed)."""
    if not semantic.available():
        return {"ok": False, "engine": "unavailable", "hint": "uv sync --group embed then call again"}
    n = 0
    for r in store.query("SELECT id FROM contacts"):
        _reindex(r["id"])
        n += 1
    return {"ok": True, "indexed": n}


@mcp.tool
def get(contact_id: int) -> dict:
    """Get a full contact record."""
    return store.query_one("SELECT * FROM contacts WHERE id=?", (contact_id,)) or not_found(
        "contact", contact_id, available=_recent_contact_ids(), hint="use list_contacts() or find()")


@mcp.tool
def update_status(contact_id: int, status: str, notes: str = "") -> dict:
    """Set a contact's pipeline status (new/queued/contacted/replied/bounced/closed)."""
    if status not in STATUSES:
        return {"error": f"status must be one of {sorted(STATUSES)}"}
    if not store.query_one("SELECT id FROM contacts WHERE id=?", (contact_id,)):
        return not_found("contact", contact_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    store.execute("UPDATE contacts SET status=?, notes=COALESCE(NULLIF(?,''),notes), updated_at=? WHERE id=?",
                  (status, notes, _now(), contact_id))
    return {"ok": True, "id": contact_id, "status": status}


@mcp.tool
def mark_contacted(contact_id: int) -> dict:
    """Mark a contact as contacted now (sets status + last_contacted_at). Pairs with reachout/campaign."""
    if not store.query_one("SELECT id FROM contacts WHERE id=?", (contact_id,)):
        return not_found("contact", contact_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    now = _now()
    store.execute("UPDATE contacts SET status='contacted', last_contacted_at=?, updated_at=? WHERE id=?",
                  (now, now, contact_id))
    return {"ok": True, "id": contact_id, "last_contacted_at": now}


@mcp.tool
def enrich_from_signature(contact_id: int, signature: str) -> dict:
    """Parse an email signature block for email/phone/title/linkedin/github/twitter and merge in."""
    email = EMAIL_RE.search(signature)
    linkedin = re.search(r"(https?://[^\s]*linkedin\.com/[^\s]+)", signature, re.I)
    github = re.search(r"(https?://[^\s]*github\.com/[^\s]+)", signature, re.I)
    twitter = re.search(r"(https?://(?:[^\s]*twitter\.com|x\.com)/[^\s]+)", signature, re.I)
    phone = re.search(r"(\+?\d[\d\s().-]{7,}\d)", signature)
    found = {
        "email": email.group(0).lower() if email else "",
        "linkedin": linkedin.group(1) if linkedin else "",
        "github": github.group(1) if github else "",
        "twitter": twitter.group(1) if twitter else "",
        "phone": phone.group(1).strip() if phone else "",
    }
    sets, params = [], []
    for k, v in found.items():
        if v:
            sets.append(f"{k}=?")
            params.append(v)
    if found["email"] and not _domain_of(found["email"]) == "":
        sets.append("domain=COALESCE(NULLIF(domain,''),?)")
        params.append(_domain_of(found["email"]))
    if sets:
        params += [_now(), contact_id]
        store.execute(f"UPDATE contacts SET {','.join(sets)}, updated_at=? WHERE id=?", params)
        _reindex(contact_id)
    return {"ok": True, "id": contact_id, "found": found}


@mcp.tool
def enrich_domain(contact_id: int) -> dict:
    """Fill in a contact's domain from its email if missing. Pure string derivation."""
    row = store.query_one("SELECT email, domain FROM contacts WHERE id=?", (contact_id,))
    if not row:
        return not_found("contact", contact_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    if row.get("domain"):
        return {"ok": True, "id": contact_id, "domain": row["domain"], "unchanged": True}
    dom = _domain_of(row.get("email") or "")
    if dom:
        store.execute("UPDATE contacts SET domain=?, updated_at=? WHERE id=?", (dom, _now(), contact_id))
    return {"ok": True, "id": contact_id, "domain": dom}


@mcp.tool
def tag(contact_id: int, tags: str) -> dict:
    """Add comma-separated tags to a contact (union with existing, case-insensitive)."""
    if not (tags or "").strip():
        return err("tags is required (comma-separated)", id=contact_id)
    row = store.query_one("SELECT tags FROM contacts WHERE id=?", (contact_id,))
    if not row:
        return not_found("contact", contact_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    merged = _merge_tags(row.get("tags") or "", tags)
    store.execute("UPDATE contacts SET tags=?, updated_at=? WHERE id=?", (merged, _now(), contact_id))
    _reindex(contact_id)
    return {"ok": True, "id": contact_id, "tags": merged}


@mcp.tool
def untag(contact_id: int, tags: str) -> dict:
    """Remove comma-separated tags from a contact (case-insensitive)."""
    if not (tags or "").strip():
        return err("tags is required (comma-separated)", id=contact_id)
    row = store.query_one("SELECT tags FROM contacts WHERE id=?", (contact_id,))
    if not row:
        return not_found("contact", contact_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    drop = {t.strip().lower() for t in tags.split(",") if t.strip()}
    kept = [t.strip() for t in (row.get("tags") or "").split(",")
            if t.strip() and t.strip().lower() not in drop]
    merged = ",".join(kept)
    store.execute("UPDATE contacts SET tags=?, updated_at=? WHERE id=?", (merged, _now(), contact_id))
    _reindex(contact_id)
    return {"ok": True, "id": contact_id, "tags": merged}


@mcp.tool
def list_contacts(limit: int = 50) -> list[dict]:
    """List recent contacts."""
    return store.query(
        "SELECT id,name,email,company,role,status,tags FROM contacts ORDER BY updated_at DESC LIMIT ?",
        (limit,),
    )


@mcp.tool
def import_csv(path: str, default_company: str = "") -> dict:
    """Bulk import contacts from a CSV (stdlib csv). Flexible headers: name,email,company,role,title,
    linkedin,github,twitter,phone,domain,tags,source,notes. Upserts by email+company. Returns counts."""
    p = Path(path).expanduser()
    if not p.is_file():
        return {"error": f"no file at {p}"}
    if p.stat().st_size > MAX_IMPORT_BYTES:
        return {"error": f"file too large ({p.stat().st_size} bytes > {MAX_IMPORT_BYTES})"}
    added = updated = skipped = 0
    errors: list[dict] = []
    try:
        with p.open(newline="", encoding="utf-8-sig") as fh:
            reader = csv.DictReader(fh)
            for i, raw in enumerate(reader):
                row = {(k or "").strip().lower(): (v or "").strip() for k, v in raw.items()}
                name = row.get("name") or row.get("full_name") or ""
                company = row.get("company") or default_company
                email = row.get("email") or ""
                if not (name and company):
                    skipped += 1
                    continue
                if email and not _valid_email(email):
                    errors.append({"row": i, "issue": f"invalid email {email!r}"})
                    skipped += 1
                    continue
                key = (email.strip().lower(), company)
                exists = store.query_one("SELECT 1 FROM contacts WHERE email=? AND company=?", key)
                add_contact(
                    name=name, company=company, email=email, role=row.get("role", ""),
                    title=row.get("title", ""), domain=row.get("domain", ""),
                    linkedin=row.get("linkedin", ""), github=row.get("github", ""),
                    twitter=row.get("twitter", ""), phone=row.get("phone", ""),
                    tags=row.get("tags", ""), source=row.get("source", "csv"),
                    notes=row.get("notes", ""),
                )
                if exists:
                    updated += 1
                else:
                    added += 1
    except Exception as e:  # noqa: BLE001
        return {"error": str(e), "added": added, "updated": updated, "skipped": skipped}
    return {"added": added, "updated": updated, "skipped": skipped, "errors": errors[:20]}


@mcp.tool
def export_csv(path: str = "", status: str = "", company: str = "") -> dict:
    """Export contacts to a CSV file (optionally filtered by status/company). Returns the path + count."""
    sql = "SELECT * FROM contacts WHERE 1=1"
    params: list = []
    if status:
        sql += " AND status=?"; params.append(status)
    if company:
        sql += " AND company LIKE ?"; params.append(f"%{company}%")
    sql += " ORDER BY company, name"
    rows = store.query(sql, params)
    out = Path(path).expanduser() if path else OUT / f"contacts_export_{datetime.now().date()}.csv"
    cols = ["id", "name", "email", "company", "domain", "role", "title", "linkedin", "github",
            "twitter", "phone", "tags", "source", "confidence", "status", "last_contacted_at",
            "created_at", "updated_at"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        for r in rows:
            w.writerow(r)
    return {"path": str(out), "count": len(rows)}


@mcp.tool
def find_duplicates() -> dict:
    """Find likely duplicate contacts grouped by shared email (case-insensitive) or name+company."""
    by_email: dict[str, list[dict]] = {}
    by_nc: dict[str, list[dict]] = {}
    for r in store.query("SELECT id,name,email,company FROM contacts ORDER BY id"):
        if r.get("email"):
            by_email.setdefault(r["email"].lower(), []).append(r)
        nc = f"{(r.get('name') or '').lower()}|{(r.get('company') or '').lower()}"
        if nc.strip("|"):
            by_nc.setdefault(nc, []).append(r)
    email_dups = [g for g in by_email.values() if len(g) > 1]
    name_dups = [g for g in by_nc.values() if len(g) > 1]
    return {"by_email": email_dups, "by_name_company": name_dups,
            "duplicate_groups": len(email_dups) + len(name_dups)}


@mcp.tool
def merge_contacts(keep_id: int, dup_id: int) -> dict:
    """Merge dup_id into keep_id: fill empty fields from dup, union tags+notes, then delete dup."""
    keep = store.query_one("SELECT * FROM contacts WHERE id=?", (keep_id,))
    dup = store.query_one("SELECT * FROM contacts WHERE id=?", (dup_id,))
    if not keep:
        return not_found("contact (keep_id)", keep_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    if not dup:
        return not_found("contact (dup_id)", dup_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    if keep_id == dup_id:
        return {"error": "keep_id and dup_id are the same"}
    fill_cols = ["name", "email", "company", "domain", "role", "title", "linkedin", "github",
                 "twitter", "phone", "source", "confidence", "last_contacted_at"]
    sets, params = [], []
    for c in fill_cols:
        if not (keep.get(c) or "").strip() and (dup.get(c) or "").strip():
            sets.append(f"{c}=?"); params.append(dup[c])
    merged_tags = _merge_tags(keep.get("tags") or "", dup.get("tags") or "")
    sets.append("tags=?"); params.append(merged_tags)
    notes = "\n".join(x for x in [keep.get("notes") or "", dup.get("notes") or ""] if x.strip())
    sets.append("notes=?"); params.append(notes)
    sets.append("updated_at=?"); params.append(_now())
    params.append(keep_id)
    # email+company uniqueness: drop dup first to avoid conflict if we copy its email
    store.execute("DELETE FROM contacts WHERE id=?", (dup_id,))
    semantic.drop_row(store, "contacts_vec", dup_id)
    try:
        store.execute(f"UPDATE contacts SET {','.join(sets)} WHERE id=?", params)
    except Exception:  # noqa: BLE001 — conflict; keep stays as-is
        pass
    _reindex(keep_id)
    return {"ok": True, "kept": keep_id, "removed": dup_id, "tags": merged_tags}


@mcp.tool
def dedupe(dry_run: bool = True) -> dict:
    """Auto-merge obvious duplicate contacts that share the same email. dry_run reports the plan only."""
    dups = find_duplicates()["by_email"]
    plan, merged = [], 0
    for group in dups:
        ids = sorted(c["id"] for c in group)
        keep, rest = ids[0], ids[1:]
        for d in rest:
            plan.append({"keep": keep, "remove": d})
            if not dry_run:
                merge_contacts(keep, d)
                merged += 1
    return {"dry_run": dry_run, "planned_merges": len(plan), "merged": merged, "plan": plan}


@mcp.tool
def link_jobtrack_payload(contact_id: int) -> dict:
    """Return a payload ready to pass to jobtrack.add_application (company/role). Loose cross-server link."""
    row = store.query_one("SELECT name,company,role,title FROM contacts WHERE id=?", (contact_id,))
    if not row:
        return not_found("contact", contact_id, available=_recent_contact_ids(),
                         hint="use list_contacts() or find()")
    return {"company": row.get("company") or "", "role": row.get("role") or row.get("title") or "",
            "notes": f"Contact: {row.get('name','')}", "_hint": "pass to jobtrack.add_application"}


@mcp.tool
def stats() -> dict:
    """CRM totals: by status, by confidence, by source, plus total + recently added (7d)."""
    def grp(col: str) -> dict:
        return {r[col] or "(none)": r["n"]
                for r in store.query(f"SELECT {col}, COUNT(*) AS n FROM contacts GROUP BY {col}")}
    return {
        "total": store.query_one("SELECT COUNT(*) AS n FROM contacts")["n"],
        "by_status": grp("status"),
        "by_confidence": grp("confidence"),
        "by_source": grp("source"),
        "with_email": store.query_one("SELECT COUNT(*) AS n FROM contacts WHERE email!=''")["n"],
    }


if __name__ == "__main__":
    mcp.run()
