"""meeting-prep — assemble a one-page briefing for an upcoming meeting by pulling, READ-ONLY, from
the other suite servers' SQLite stores: contacts (by company / attendee), reachout (recent outreach),
codeindex (relevant project files) and notes (relevant notes).

It never writes to those DBs, never calls the other servers at runtime, and degrades gracefully when
any store is missing. quick_brief() works with no DBs at all."""
from __future__ import annotations

import re
import sqlite3
from pathlib import Path

from mcp_base import base_data_dir, make_server, ok

mcp = make_server(
    "meeting-prep",
    instructions=("Assemble a meeting briefing from read-only suite DBs (contacts, reachout, codeindex, "
                  "notes). build_brief(company?, attendees?, topic?) and quick_brief(text)."),
)


def _open_ro(server: str) -> sqlite3.Connection | None:
    """Open another server's store.db read-only. Returns None if absent/unreadable."""
    p = base_data_dir() / server / "store.db"
    if not p.exists():
        return None
    try:
        conn = sqlite3.connect(f"file:{p}?mode=ro", uri=True, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        return conn
    except Exception:
        return None


def _q(conn: sqlite3.Connection | None, sql: str, params: tuple = ()) -> list[dict]:
    if conn is None:
        return []
    try:
        return [dict(r) for r in conn.execute(sql, params).fetchall()]
    except Exception:
        return []


def _has_table(conn: sqlite3.Connection | None, table: str) -> bool:
    if conn is None:
        return False
    try:
        return bool(conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type IN ('table','view') AND name=?", (table,)).fetchone())
    except Exception:
        return False


def _fts_query(text: str) -> str:
    """Build a safe FTS5 OR-query from free text (alnum tokens, quoted)."""
    toks = [t for t in re.findall(r"[A-Za-z0-9]{3,}", text or "")][:12]
    return " OR ".join(f'"{t}"' for t in toks)


def _contacts_for(company: str, attendees: list[str]) -> list[dict]:
    conn = _open_ro("contacts")
    if not _has_table(conn, "contacts"):
        return []
    found: dict[int, dict] = {}
    if company.strip():
        like = f"%{company.strip()}%"
        for r in _q(conn, "SELECT id,name,email,company,role,title,linkedin,status,last_contacted_at,notes "
                          "FROM contacts WHERE company LIKE ? OR domain LIKE ? LIMIT 25", (like, like)):
            found[r["id"]] = r
    for a in (attendees or []):
        a = (a or "").strip()
        if not a:
            continue
        like = f"%{a}%"
        for r in _q(conn, "SELECT id,name,email,company,role,title,linkedin,status,last_contacted_at,notes "
                          "FROM contacts WHERE name LIKE ? OR email LIKE ? LIMIT 10", (like, like)):
            found[r["id"]] = r
    return list(found.values())


def _outreach_for(company: str, attendees: list[str]) -> list[dict]:
    conn = _open_ro("reachout")
    if not _has_table(conn, "outreach"):
        return []
    found: dict[int, dict] = {}
    cols = "id,recipient_name,recipient_email,company,role,subject,status,sent_at,replied_at,followup_count"
    if company.strip():
        for r in _q(conn, f"SELECT {cols} FROM outreach WHERE company LIKE ? "
                          "ORDER BY COALESCE(sent_at,created_at) DESC LIMIT 15", (f"%{company.strip()}%",)):
            found[r["id"]] = r
    for a in (attendees or []):
        a = (a or "").strip()
        if not a:
            continue
        like = f"%{a}%"
        for r in _q(conn, f"SELECT {cols} FROM outreach WHERE recipient_name LIKE ? OR recipient_email LIKE ? "
                          "ORDER BY COALESCE(sent_at,created_at) DESC LIMIT 10", (like, like)):
            found[r["id"]] = r
    return list(found.values())


def _notes_for(terms: str, limit: int = 8) -> list[dict]:
    conn = _open_ro("notes")
    if conn is None:
        return []
    fts = _fts_query(terms)
    if fts and _has_table(conn, "notes_fts"):
        rows = _q(conn, "SELECT n.id,n.title FROM notes_fts f JOIN notes n ON n.id=f.rowid "
                       "WHERE notes_fts MATCH ? ORDER BY rank LIMIT ?", (fts, limit))
        if rows:
            return rows
    if _has_table(conn, "notes") and terms.strip():
        like = f"%{terms.strip()}%"
        return _q(conn, "SELECT id,title FROM notes WHERE title LIKE ? OR body LIKE ? LIMIT ?",
                  (like, like, limit))
    return []


def _code_for(terms: str, limit: int = 8) -> list[dict]:
    conn = _open_ro("codeindex")
    if not _has_table(conn, "files_fts"):
        return []
    fts = _fts_query(terms)
    if not fts:
        return []
    return _q(conn, "SELECT path, project FROM files_fts WHERE files_fts MATCH ? "
                   "ORDER BY rank LIMIT ?", (fts, limit))


@mcp.tool
def build_brief(company: str = "", attendees: list[str] | None = None, topic: str = "") -> dict:
    """Assemble a structured meeting brief. Pulls READ-ONLY from contacts (by company/attendee),
    reachout (recent outreach to them), notes (relevant) and codeindex (relevant project files).

    All inputs optional. Returns the brief plus a `sources` map showing which DBs were available."""
    attendees = attendees or []
    company = company or ""
    topic = topic or ""
    search_terms = " ".join([company, topic] + attendees).strip()

    contacts = _contacts_for(company, attendees)
    outreach = _outreach_for(company, attendees)
    notes = _notes_for(search_terms)
    code = _code_for(topic or company)

    sources = {
        "contacts": (base_data_dir() / "contacts" / "store.db").exists(),
        "reachout": (base_data_dir() / "reachout" / "store.db").exists(),
        "notes": (base_data_dir() / "notes" / "store.db").exists(),
        "codeindex": (base_data_dir() / "codeindex" / "store.db").exists(),
    }

    # Light talking points derived from what we found.
    talking_points = []
    if contacts:
        talking_points.append(f"{len(contacts)} known contact(s) at/for this meeting.")
    replied = [o for o in outreach if o.get("replied_at")]
    if outreach:
        talking_points.append(
            f"{len(outreach)} prior outreach thread(s), {len(replied)} replied.")
    if notes:
        talking_points.append(f"{len(notes)} relevant note(s) to review.")
    if topic.strip():
        talking_points.append(f"Topic focus: {topic.strip()}")

    return ok(
        meeting={"company": company, "attendees": attendees, "topic": topic},
        contacts=contacts,
        recent_outreach=outreach,
        relevant_notes=notes,
        relevant_code=code,
        talking_points=talking_points,
        sources=sources,
    )


@mcp.tool
def quick_brief(text: str) -> dict:
    """Lightweight brief from a free-text blurb (e.g. a calendar invite). Extracts likely company,
    attendee names and a topic line heuristically, then runs build_brief on them. No DBs required."""
    text = text or ""
    # crude attendee extraction: emails + capitalized name pairs
    emails = re.findall(r"[\w.+-]+@[\w.-]+\.\w+", text)
    domains = {e.split("@", 1)[1].split(".")[0] for e in emails if "@" in e}
    domains.discard("gmail")
    domains.discard("outlook")
    domains.discard("yahoo")
    names = re.findall(r"\b([A-Z][a-z]+ [A-Z][a-z]+)\b", text)
    company = sorted(domains)[0] if domains else ""
    # topic: first non-empty line
    topic = ""
    for line in text.splitlines():
        if line.strip():
            topic = line.strip()[:120]
            break
    attendees = list(dict.fromkeys(emails + names))[:10]
    brief = build_brief(company=company, attendees=attendees, topic=topic)
    brief["extracted"] = {"company": company, "attendees": attendees, "topic": topic}
    return brief


if __name__ == "__main__":
    mcp.run()
