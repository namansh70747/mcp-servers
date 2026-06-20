"""reachout — send your fixed outreach template, personalized, via Gmail (drafts-first), with
tracking and anti-spam guardrails. Free Gmail API (gmail.compose scope).

Setup: place credentials.json in this server's data dir (printed by auth_status). First send opens
a browser to authorize; token is cached. Templates live in ./templates/*.md (first line may be
'Subject: ...'). Supports A/B templates, multi-step sequences, scheduled/snoozed sends, threaded
follow-ups, signature from profile.json, and reply tracking + analytics.
"""
from __future__ import annotations

import base64
import csv
import hashlib
import json
import os
import re
from datetime import datetime, timedelta, timezone
from email.message import EmailMessage
from pathlib import Path

from jinja2 import Template
from mcp_base import BaseStore, data_dir, db_path, err, make_server, not_found

mcp = make_server(
    "reachout",
    instructions=("Templated Gmail outreach, drafts-first. render_template -> create_draft (review) "
                  "-> send_draft. Tracks everything; list_followups_due for follow-ups; sequences + "
                  "scheduled sends + A/B + analytics."),
)

TEMPLATES_DIR = Path(__file__).parent / "templates"
DATA = data_dir("reachout")
ROOT = Path(__file__).resolve().parents[2]
PROFILE = ROOT / "profile.json"
SCOPES = ["https://www.googleapis.com/auth/gmail.compose"]
DAILY_CAP = int(os.environ.get("REACHOUT_DAILY_CAP", "20"))
COOLDOWN_DAYS = int(os.environ.get("REACHOUT_COOLDOWN_DAYS", "14"))
EMAIL_RE = re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+")
MAX_ATTACH_BYTES = 25 * 1024 * 1024  # 25 MB per attachment (Gmail's own limit)

SCHEMA = """
CREATE TABLE IF NOT EXISTS outreach(
  id INTEGER PRIMARY KEY, recipient_name TEXT, recipient_email TEXT, company TEXT, role TEXT,
  template_used TEXT, subject TEXT, gmail_message_id TEXT, thread_id TEXT, status TEXT DEFAULT 'drafted',
  sent_at TEXT, last_followup_at TEXT, followup_count INTEGER DEFAULT 0,
  scheduled_for TEXT, snooze_until TEXT, sequence_id INTEGER, step INTEGER DEFAULT 0,
  variant TEXT DEFAULT '', opened_at TEXT, replied_at TEXT,
  notes TEXT DEFAULT '', created_at TEXT
);
CREATE TABLE IF NOT EXISTS sequences(
  id INTEGER PRIMARY KEY, name TEXT UNIQUE, steps_json TEXT NOT NULL, created_at TEXT
);
CREATE TABLE IF NOT EXISTS scheduled(
  id INTEGER PRIMARY KEY, outreach_id INTEGER, action TEXT, run_at TEXT,
  payload_json TEXT DEFAULT '', status TEXT DEFAULT 'pending', created_at TEXT
);
CREATE TABLE IF NOT EXISTS events(
  id INTEGER PRIMARY KEY, outreach_id INTEGER, kind TEXT, at TEXT, meta TEXT DEFAULT ''
);
"""
store = BaseStore(db_path("reachout"), schema=SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _recent_outreach_ids(limit: int = 10) -> list[int]:
    """Recent outreach ids, newest first — handed to not_found() so a weak agent can recover."""
    return [r["id"] for r in store.query(
        "SELECT id FROM outreach ORDER BY created_at DESC LIMIT ?", (limit,))]


def _sequence_names(limit: int = 10) -> list[str]:
    """Defined sequence names — handed to not_found() so a weak agent can recover."""
    return [r["name"] for r in store.query(
        "SELECT name FROM sequences ORDER BY name LIMIT ?", (limit,))]


def _ensure_columns() -> None:
    """Additively migrate older outreach rows (thread_id, scheduling, sequence, variant, tracking)."""
    have = {r["name"] for r in store.query("PRAGMA table_info(outreach)")}
    wanted = {
        "thread_id": "TEXT", "scheduled_for": "TEXT", "snooze_until": "TEXT",
        "sequence_id": "INTEGER", "step": "INTEGER DEFAULT 0", "variant": "TEXT DEFAULT ''",
        "opened_at": "TEXT", "replied_at": "TEXT", "contact_id": "INTEGER",
    }
    for col, decl in wanted.items():
        if col not in have:
            store.execute(f"ALTER TABLE outreach ADD COLUMN {col} {decl}")


_ensure_columns()


def _profile() -> dict:
    if PROFILE.exists():
        try:
            return json.loads(PROFILE.read_text())
        except Exception:  # noqa: BLE001
            return {}
    return {}


def _valid_email(email: str) -> bool:
    if not email:
        return False
    try:
        from email_validator import validate_email
        validate_email(email, check_deliverability=False)
        return True
    except ImportError:
        return bool(EMAIL_RE.fullmatch(email.strip()))
    except Exception:
        return False


def _log_event(outreach_id: int | None, kind: str, meta: str = "") -> None:
    if outreach_id is None:
        return
    store.execute("INSERT INTO events(outreach_id,kind,at,meta) VALUES(?,?,?,?)",
                  (outreach_id, kind, _now(), meta))


# ---------- Gmail (lazy; only when a send/draft tool is called) ----------
def _gmail():
    """Build the Gmail service via the shared mcp_base helper (handles OAuth + token cache)."""
    from mcp_base import get_gmail_service
    return get_gmail_service(DATA, SCOPES)


def _gmail_or_err() -> tuple[object | None, dict | None]:
    """Return (service, None) when authorized, else (None, err(...)) with an auth hint.

    Converts the missing-credentials RuntimeError (and any transient Gmail/API error
    raised while building the client) into a clean err() so tools degrade instead of
    crashing when credentials.json/token.json are absent."""
    try:
        return _gmail(), None
    except RuntimeError as e:
        return None, err(str(e), code="gmail_auth",
                         hint="Run auth_status() to see where to put credentials.json.")
    except Exception as e:  # noqa: BLE001 — googleapiclient.errors.HttpError etc.
        return None, err(f"{type(e).__name__}: {e}", code="gmail_error")


def _build_message(to_email, subject, body, attachments=None) -> dict:
    msg = EmailMessage()
    msg["To"] = to_email
    msg["Subject"] = subject
    msg.set_content(body)
    for path in attachments or []:
        if not path:
            continue
        p = Path(path).expanduser()
        if p.is_file():
            try:
                if p.stat().st_size > MAX_ATTACH_BYTES:
                    raise ValueError(
                        f"attachment too large ({p.stat().st_size} bytes > {MAX_ATTACH_BYTES}): {p.name}")
                msg.add_attachment(p.read_bytes(), maintype="application", subtype="octet-stream",
                                   filename=p.name)
            except OSError as e:  # noqa: BLE001 — unreadable file: skip silently like a missing path
                raise ValueError(f"cannot read attachment {p.name}: {e}") from e
    return {"raw": base64.urlsafe_b64encode(msg.as_bytes()).decode()}


def _sent_today() -> int:
    today = datetime.now(timezone.utc).date().isoformat()
    row = store.query_one("SELECT COUNT(*) AS n FROM outreach WHERE status='sent' AND sent_at LIKE ?",
                          (today + "%",))
    return row["n"]


def _recent_to(email: str) -> dict | None:
    cutoff = (datetime.now(timezone.utc) - timedelta(days=COOLDOWN_DAYS)).isoformat()
    return store.query_one(
        "SELECT id, sent_at FROM outreach WHERE recipient_email=? AND sent_at >= ? "
        "ORDER BY sent_at DESC LIMIT 1", (email, cutoff))


# ---------- tools ----------
@mcp.tool
def auth_status() -> dict:
    """Report Gmail auth state + where to put credentials.json."""
    return {
        "data_dir": str(DATA),
        "credentials_present": (DATA / "credentials.json").exists(),
        "token_present": (DATA / "token.json").exists(),
        "hint": "Put your Desktop OAuth client JSON at credentials.json above; first send authorizes.",
    }


@mcp.tool
def signature_block() -> dict:
    """Build a signature from profile.json (name, headline, links). Used as a default when callers
    don't supply one. Returns {sender, signature}."""
    p = _profile()
    name = p.get("name", "")
    links = p.get("links", {}) or {}
    lines = []
    if p.get("headline"):
        lines.append(p["headline"])
    for label, key in (("GitHub", "github"), ("LinkedIn", "linkedin"), ("Web", "website")):
        if links.get(key):
            lines.append(f"{label}: {links[key]}")
    if p.get("email"):
        lines.append(p["email"])
    return {"sender": name, "signature": "\n".join(lines)}


@mcp.tool
def list_templates() -> list[dict]:
    """List available outreach templates (from ./templates/*.md)."""
    out = []
    TEMPLATES_DIR.mkdir(exist_ok=True)
    for f in sorted(TEMPLATES_DIR.glob("*.md")):
        text = f.read_text(encoding="utf-8")
        subject = ""
        if text.lower().startswith("subject:"):
            subject = text.split("\n", 1)[0].split(":", 1)[1].strip()
        out.append({"name": f.stem, "subject": subject, "path": str(f)})
    return out


def _render(template_name: str, variables: dict) -> dict:
    if not template_name or not isinstance(template_name, str):
        return {"error": "template_name must be a non-empty string"}
    # Prevent path traversal / nested-dir access via the template name.
    if "/" in template_name or "\\" in template_name or ".." in template_name:
        return {"error": f"invalid template name: {template_name!r}"}
    variables = variables if isinstance(variables, dict) else {}
    f = TEMPLATES_DIR / f"{template_name}.md"
    if not f.exists():
        return {"error": f"no template '{template_name}'. Have: {[t['name'] for t in list_templates()]}"}
    raw = f.read_text(encoding="utf-8")
    rendered = Template(raw).render(**variables)
    subject, body = "", rendered
    if rendered.lower().startswith("subject:"):
        first, _, rest = rendered.partition("\n")
        subject = first.split(":", 1)[1].strip()
        body = rest.lstrip("\n")
    missing = [k for k in ("name", "company") if not variables.get(k)]
    return {"subject": subject, "body": body, "missing": missing}


@mcp.tool
def render_template(template_name: str, variables: dict) -> dict:
    """Fill a template (Jinja2) with variables; returns {subject, body, missing}."""
    return _render(template_name, variables)


@mcp.tool
def render_with_profile(template_name: str, variables: dict) -> dict:
    """Like render_template but pre-fills sender + signature (+ default link) from profile.json,
    then merges caller variables on top. Returns {subject, body, missing}."""
    sig = signature_block()
    p = _profile()
    links = p.get("links", {}) or {}
    base = {"sender": sig["sender"], "signature": sig["signature"],
            "link": links.get("website") or links.get("github") or ""}
    base.update({k: v for k, v in (variables or {}).items() if v not in (None, "")})
    return _render(template_name, base)


@mcp.tool
def create_draft(to_email: str, subject: str, body: str, recipient_name: str = "",
                 company: str = "", role: str = "", template_used: str = "",
                 attachments: list[str] | None = None, attach_onepager: str = "",
                 variant: str = "", sequence_id: int | None = None, step: int = 0,
                 contact_id: int | None = None) -> dict:
    """Create a Gmail DRAFT (nothing sent) and log it. The safe default path — review, then send_draft.
    `attach_onepager` is a convenience path appended to attachments. Records an event + variant.
    Optional `contact_id` links this outreach to a contacts-server record (P2 cross-server identity)."""
    if not _valid_email(to_email):
        return {"error": f"invalid recipient email: {to_email!r}"}
    atts = list(attachments or [])
    if attach_onepager:
        atts.append(attach_onepager)
    # Build (and validate attachments) before touching Gmail so attachment errors
    # surface exactly as before; only the network call is wrapped for graceful degrade.
    message = _build_message(to_email, subject, body, atts)
    service, gerr = _gmail_or_err()
    if gerr:
        return gerr
    try:
        draft = service.users().drafts().create(
            userId="me", body={"message": message}).execute()
    except Exception as e:  # noqa: BLE001 — transient Gmail/API error
        return err(f"{type(e).__name__}: {e}", code="gmail_error")
    oid = store.execute(
        "INSERT INTO outreach(recipient_name,recipient_email,company,role,template_used,subject,"
        "gmail_message_id,thread_id,status,variant,sequence_id,step,contact_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (recipient_name, to_email, company, role, template_used, subject,
         draft["message"]["id"], draft["message"].get("threadId", ""), "drafted",
         variant, sequence_id, step, contact_id, _now()),
    )
    _log_event(oid, "drafted", template_used)
    return {"draft_id": draft["id"], "gmail_message_id": draft["message"]["id"], "outreach_id": oid}


@mcp.tool
def send_email(to_email: str, subject: str, body: str, recipient_name: str = "", company: str = "",
               role: str = "", template_used: str = "", attachments: list[str] | None = None,
               force: bool = False, dry_run: bool = False, variant: str = "",
               contact_id: int | None = None) -> dict:
    """Send immediately (enforces daily cap + dedupe cooldown). Prefer create_draft + send_draft.
    dry_run returns the would-send payload without contacting Gmail.
    Optional `contact_id` links this outreach to a contacts-server record (P2 cross-server identity)."""
    if not _valid_email(to_email):
        return {"error": f"invalid recipient email: {to_email!r}"}
    if not force:
        if _sent_today() >= DAILY_CAP:
            return {"blocked": "daily_cap", "cap": DAILY_CAP, "hint": "use force=True to override"}
        recent = _recent_to(to_email)
        if recent:
            return {"blocked": "cooldown", "previous": recent, "cooldown_days": COOLDOWN_DAYS}
    if dry_run:
        return {"dry_run": True, "to": to_email, "subject": subject, "body": body,
                "attachments": attachments or []}
    # Build (and validate attachments) before touching Gmail so attachment errors
    # surface exactly as before; only the network call is wrapped for graceful degrade.
    message = _build_message(to_email, subject, body, attachments)
    service, gerr = _gmail_or_err()
    if gerr:
        return gerr
    try:
        sent = service.users().messages().send(userId="me", body=message).execute()
    except Exception as e:  # noqa: BLE001 — transient Gmail/API error
        return err(f"{type(e).__name__}: {e}", code="gmail_error")
    oid = store.execute(
        "INSERT INTO outreach(recipient_name,recipient_email,company,role,template_used,subject,"
        "gmail_message_id,thread_id,status,sent_at,variant,contact_id,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (recipient_name, to_email, company, role, template_used, subject, sent["id"],
         sent.get("threadId", ""), "sent", _now(), variant, contact_id, _now()),
    )
    _log_event(oid, "sent", template_used)
    return {"gmail_message_id": sent["id"], "outreach_id": oid, "status": "sent"}


@mcp.tool
def send_draft(draft_id: str) -> dict:
    """Send a previously created draft and mark it sent."""
    if not draft_id:
        return {"error": "draft_id is required"}
    service, gerr = _gmail_or_err()
    if gerr:
        return gerr
    try:
        sent = service.users().drafts().send(userId="me", body={"id": draft_id}).execute()
    except Exception as e:  # noqa: BLE001 — transient Gmail/API error
        return err(f"{type(e).__name__}: {e}", code="gmail_error")
    store.execute("UPDATE outreach SET status='sent', sent_at=? WHERE gmail_message_id=?",
                  (_now(), sent["id"]))
    row = store.query_one("SELECT id FROM outreach WHERE gmail_message_id=?", (sent["id"],))
    _log_event(row["id"] if row else None, "sent", "from_draft")
    return {"gmail_message_id": sent["id"], "status": "sent"}


@mcp.tool
def delete_draft(draft_id: str, outreach_id: int | None = None) -> dict:
    """Delete a previously created Gmail draft. Optionally pass the outreach_id (from create_draft)
    to mark that tracked row 'deleted'. Errors out (no-op) if draft_id is empty."""
    if not draft_id:
        return {"error": "draft_id is required"}
    service, gerr = _gmail_or_err()
    if gerr:
        return gerr
    try:
        service.users().drafts().delete(userId="me", id=draft_id).execute()
    except Exception as e:  # noqa: BLE001 — transient Gmail/API error
        return err(f"{type(e).__name__}: {e}", code="gmail_error")
    if outreach_id is not None:
        store.execute("UPDATE outreach SET status='deleted' WHERE id=? AND status='drafted'",
                      (outreach_id,))
        _log_event(outreach_id, "deleted", "delete_draft")
    return {"ok": True, "draft_id": draft_id, "status": "deleted"}


@mcp.tool
def log_outreach(recipient_name: str, recipient_email: str, company: str, role: str = "",
                 status: str = "sent", template_used: str = "", notes: str = "",
                 contact_id: int | None = None) -> dict:
    """Manually record an outreach (e.g. sent outside the tool) for tracking.
    Optional `contact_id` links this outreach to a contacts-server record (P2 cross-server identity)."""
    oid = store.execute(
        "INSERT INTO outreach(recipient_name,recipient_email,company,role,template_used,status,"
        "sent_at,notes,contact_id,created_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (recipient_name, recipient_email, company, role, template_used, status,
         _now() if status == "sent" else None, notes, contact_id, _now()),
    )
    _log_event(oid, status, template_used)
    return {"outreach_id": oid}


@mcp.tool
def history_for_contact(contact_id: int, limit: int = 50) -> list[dict]:
    """All outreach rows linked to a given contacts-server contact_id (P2 cross-server identity)."""
    return store.query(
        "SELECT id,recipient_name,recipient_email,company,role,status,subject,sent_at,"
        "followup_count,variant,sequence_id,step,created_at FROM outreach "
        "WHERE contact_id=? ORDER BY created_at DESC LIMIT ?", (contact_id, limit))


@mcp.tool
def list_outreach(status: str = "", company: str = "", limit: int = 50) -> list[dict]:
    """Browse the outreach tracker, optionally filtered by status/company."""
    sql = ("SELECT id,recipient_name,recipient_email,company,role,status,sent_at,followup_count,"
           "variant,sequence_id,step FROM outreach WHERE 1=1")
    params: list = []
    if status:
        sql += " AND status=?"; params.append(status)
    if company:
        sql += " AND company LIKE ?"; params.append(f"%{company}%")
    sql += " ORDER BY created_at DESC LIMIT ?"; params.append(limit)
    return store.query(sql, params)


@mcp.tool
def list_followups_due(follow_up_days: int = 7, limit: int = 10) -> list[dict]:
    """Contacts sent >= N days ago, no reply yet, not snoozed — ready for a follow-up. Each row
    includes days_since to help prioritize."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=follow_up_days)).isoformat()
    now_iso = _now()
    rows = store.query(
        "SELECT id,recipient_name,recipient_email,company,sent_at,followup_count,template_used "
        "FROM outreach WHERE status='sent' AND sent_at <= ? "
        "AND (snooze_until IS NULL OR snooze_until <= ?) "
        "ORDER BY sent_at ASC LIMIT ?", (cutoff, now_iso, limit))
    for r in rows:
        try:
            sent = datetime.fromisoformat(r["sent_at"])
            r["days_since"] = (datetime.now(timezone.utc) - sent).days
        except Exception:  # noqa: BLE001
            r["days_since"] = None
    return rows


@mcp.tool
def mark_replied(outreach_id: int) -> dict:
    """Mark a contact as replied (drops them from follow-ups)."""
    if not store.query_one("SELECT id FROM outreach WHERE id=?", (outreach_id,)):
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    store.execute("UPDATE outreach SET status='replied', replied_at=? WHERE id=?", (_now(), outreach_id))
    _log_event(outreach_id, "replied")
    return {"ok": True, "id": outreach_id, "status": "replied"}


@mcp.tool
def record_reply(outreach_id: int, snippet: str = "") -> dict:
    """Record an inbound reply with an optional snippet (marks replied + logs event)."""
    if not store.query_one("SELECT id FROM outreach WHERE id=?", (outreach_id,)):
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    store.execute("UPDATE outreach SET status='replied', replied_at=? WHERE id=?", (_now(), outreach_id))
    _log_event(outreach_id, "replied", snippet[:200])
    return {"ok": True, "id": outreach_id, "status": "replied"}


@mcp.tool
def mark_status(outreach_id: int, status: str, notes: str = "") -> dict:
    """Set any status (sent/replied/bounced/closed) + optional note."""
    if not (status or "").strip():
        return err("status is required", id=outreach_id)
    if not store.query_one("SELECT id FROM outreach WHERE id=?", (outreach_id,)):
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    store.execute("UPDATE outreach SET status=?, notes=COALESCE(NULLIF(?,''),notes) WHERE id=?",
                  (status, notes, outreach_id))
    _log_event(outreach_id, status, notes[:200])
    return {"ok": True, "id": outreach_id, "status": status}


@mcp.tool
def record_followup(outreach_id: int) -> dict:
    """Bump the follow-up counter + timestamp after sending a follow-up."""
    if not store.query_one("SELECT id FROM outreach WHERE id=?", (outreach_id,)):
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    store.execute("UPDATE outreach SET followup_count=followup_count+1, last_followup_at=? WHERE id=?",
                  (_now(), outreach_id))
    _log_event(outreach_id, "followup")
    row = store.query_one("SELECT followup_count FROM outreach WHERE id=?", (outreach_id,))
    return {"ok": True, "id": outreach_id, "followup_count": row["followup_count"] if row else None}


@mcp.tool
def snooze(outreach_id: int, days: int) -> dict:
    """Snooze a contact for N days — excluded from list_followups_due until then."""
    if not store.query_one("SELECT id FROM outreach WHERE id=?", (outreach_id,)):
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    until = (datetime.now(timezone.utc) + timedelta(days=days)).isoformat()
    store.execute("UPDATE outreach SET snooze_until=? WHERE id=?", (until, outreach_id))
    return {"ok": True, "id": outreach_id, "snooze_until": until}


# ---------- A/B testing ----------
@mcp.tool
def ab_pick(template_a: str, template_b: str, key: str) -> dict:
    """Deterministically pick template A or B for a given key (e.g. recipient email) via a stable
    hash — same key always lands in the same bucket. Returns {template, variant}."""
    h = int(hashlib.sha256(key.encode("utf-8")).hexdigest(), 16)
    if h % 2 == 0:
        return {"template": template_a, "variant": "A"}
    return {"template": template_b, "variant": "B"}


@mcp.tool
def ab_report() -> dict:
    """Per-variant sent / replied / reply-rate from the tracker."""
    rows = store.query(
        "SELECT COALESCE(NULLIF(variant,''),'(none)') AS variant, "
        "SUM(CASE WHEN status IN ('sent','replied') THEN 1 ELSE 0 END) AS sent, "
        "SUM(CASE WHEN status='replied' THEN 1 ELSE 0 END) AS replied "
        "FROM outreach GROUP BY COALESCE(NULLIF(variant,''),'(none)')")
    for r in rows:
        r["reply_rate"] = round(r["replied"] / r["sent"], 3) if r["sent"] else 0.0
    return {"variants": rows}


# ---------- Sequences ----------
@mcp.tool
def define_sequence(name: str, steps: list[dict]) -> dict:
    """Define a multi-step sequence. steps = [{template, wait_days, condition?}]; condition defaults
    to 'no_reply'. Stored as JSON, keyed by name."""
    sid = store.execute(
        "INSERT INTO sequences(name,steps_json,created_at) VALUES(?,?,?) "
        "ON CONFLICT(name) DO UPDATE SET steps_json=excluded.steps_json",
        (name, json.dumps(steps), _now()))
    return {"id": sid, "name": name, "steps": len(steps)}


@mcp.tool
def list_sequences() -> list[dict]:
    """List defined sequences with their step counts."""
    rows = store.query("SELECT id, name, steps_json, created_at FROM sequences ORDER BY name")
    out = []
    for r in rows:
        try:
            steps = json.loads(r["steps_json"])
        except Exception:  # noqa: BLE001
            steps = []
        out.append({"id": r["id"], "name": r["name"], "steps": len(steps), "created_at": r["created_at"]})
    return out


@mcp.tool
def get_sequence(name: str) -> dict:
    """Get a sequence definition (decoded steps)."""
    row = store.query_one("SELECT * FROM sequences WHERE name=?", (name,))
    if not row:
        return not_found("sequence", name, available=_sequence_names(),
                         hint="use list_sequences() or define_sequence()")
    try:
        steps = json.loads(row["steps_json"])
    except Exception:  # noqa: BLE001
        steps = []
    return {"id": row["id"], "name": row["name"], "steps": steps, "created_at": row["created_at"]}


@mcp.tool
def start_sequence(name: str, to_email: str, recipient_name: str = "", company: str = "",
                   role: str = "", variables: dict | None = None) -> dict:
    """Start a sequence for a contact: drafts the first step (drafts-first) and schedules the next.
    Returns the draft + the scheduled next-step row."""
    seq = get_sequence(name)
    if "error" in seq:
        return seq
    steps = seq["steps"]
    if not steps:
        return {"error": "sequence has no steps"}
    s0 = steps[0]
    if not isinstance(s0, dict) or not s0.get("template"):
        return {"error": "sequence step 1 missing a 'template'"}
    rendered = render_with_profile(s0["template"], {**(variables or {}), "name": recipient_name,
                                                    "company": company, "role": role})
    if "error" in rendered:
        return rendered
    draft = create_draft(to_email, rendered["subject"], rendered["body"], recipient_name, company,
                         role, template_used=s0["template"], variant=f"{name}:1",
                         sequence_id=seq["id"], step=1)
    if "error" in draft:
        return draft
    next_at = None
    if len(steps) > 1:
        wait = int(steps[0].get("wait_days", 3))
        next_at = (datetime.now(timezone.utc) + timedelta(days=wait)).isoformat()
        store.execute(
            "INSERT INTO scheduled(outreach_id,action,run_at,payload_json,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (draft["outreach_id"], "advance_sequence", next_at,
             json.dumps({"variables": variables or {}}), "pending", _now()))
    return {"ok": True, "sequence": name, "draft": draft, "next_step_at": next_at}


@mcp.tool
def advance_sequence(outreach_id: int, variables: dict | None = None) -> dict:
    """Advance a contact to the next sequence step if they haven't replied: drafts the next step,
    bumps step, and schedules the following one. Returns the new draft or a stop reason."""
    row = store.query_one("SELECT * FROM outreach WHERE id=?", (outreach_id,))
    if not row:
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    if row.get("status") == "replied":
        return {"stopped": "replied", "id": outreach_id}
    if not row.get("sequence_id"):
        return {"error": "not part of a sequence"}
    seq = store.query_one("SELECT name, steps_json FROM sequences WHERE id=?", (row["sequence_id"],))
    if not seq:
        return {"error": "sequence definition missing"}
    steps = json.loads(seq["steps_json"])
    cur = row.get("step") or 1
    if cur >= len(steps):
        return {"stopped": "sequence_complete", "id": outreach_id, "step": cur}
    nxt = steps[cur]  # zero-indexed list; cur is 1-based so this is the next step
    if not isinstance(nxt, dict) or not nxt.get("template"):
        return {"error": f"sequence step {cur + 1} missing a 'template'"}
    rendered = render_with_profile(nxt["template"], {**(variables or {}),
                                                     "name": row.get("recipient_name", ""),
                                                     "company": row.get("company", ""),
                                                     "role": row.get("role", "")})
    if "error" in rendered:
        return rendered
    draft = create_draft(row["recipient_email"], rendered["subject"], rendered["body"],
                         row.get("recipient_name", ""), row.get("company", ""), row.get("role", ""),
                         template_used=nxt["template"], variant=f"{seq['name']}:{cur+1}",
                         sequence_id=row["sequence_id"], step=cur + 1)
    if "error" in draft:
        return draft
    next_at = None
    if cur + 1 < len(steps):
        wait = int(steps[cur].get("wait_days", 3))
        next_at = (datetime.now(timezone.utc) + timedelta(days=wait)).isoformat()
        store.execute(
            "INSERT INTO scheduled(outreach_id,action,run_at,payload_json,status,created_at) "
            "VALUES(?,?,?,?,?,?)",
            (draft["outreach_id"], "advance_sequence", next_at,
             json.dumps({"variables": variables or {}}), "pending", _now()))
    return {"ok": True, "advanced_to_step": cur + 1, "draft": draft, "next_step_at": next_at}


@mcp.tool
def sequence_status(outreach_id: int) -> dict:
    """Where a contact is in its sequence (step, total steps, status)."""
    row = store.query_one("SELECT sequence_id, step, status, recipient_email FROM outreach WHERE id=?",
                          (outreach_id,))
    if not row:
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    if not row.get("sequence_id"):
        return {"id": outreach_id, "in_sequence": False}
    seq = store.query_one("SELECT name, steps_json FROM sequences WHERE id=?", (row["sequence_id"],))
    total = len(json.loads(seq["steps_json"])) if seq else 0
    return {"id": outreach_id, "in_sequence": True, "sequence": seq["name"] if seq else None,
            "step": row.get("step"), "total_steps": total, "status": row.get("status")}


# ---------- Scheduling / snooze queue ----------
@mcp.tool
def schedule_send(outreach_id: int, run_at: str, action: str = "send_draft", payload: dict | None = None) -> dict:
    """Queue an action (e.g. send_draft / advance_sequence) for a contact at run_at (ISO time). This is
    a passive queue — drain it with due_scheduled() from your agent/cron; no daemon runs here."""
    sid = store.execute(
        "INSERT INTO scheduled(outreach_id,action,run_at,payload_json,status,created_at) "
        "VALUES(?,?,?,?,?,?)",
        (outreach_id, action, run_at, json.dumps(payload or {}), "pending", _now()))
    store.execute("UPDATE outreach SET scheduled_for=? WHERE id=?", (run_at, outreach_id))
    return {"ok": True, "scheduled_id": sid, "run_at": run_at, "action": action}


@mcp.tool
def due_scheduled(limit: int = 20) -> list[dict]:
    """Pending scheduled actions whose run_at <= now. The agent calls the named action for each."""
    now_iso = _now()
    return store.query(
        "SELECT id, outreach_id, action, run_at, payload_json FROM scheduled "
        "WHERE status='pending' AND run_at <= ? ORDER BY run_at ASC LIMIT ?", (now_iso, limit))


@mcp.tool
def mark_scheduled_done(scheduled_id: int) -> dict:
    """Mark a scheduled action as completed (after the agent has run it)."""
    store.execute("UPDATE scheduled SET status='done' WHERE id=?", (scheduled_id,))
    return {"ok": True, "scheduled_id": scheduled_id}


@mcp.tool
def cancel_scheduled(scheduled_id: int) -> dict:
    """Cancel a pending scheduled action."""
    store.execute("UPDATE scheduled SET status='cancelled' WHERE id=?", (scheduled_id,))
    return {"ok": True, "scheduled_id": scheduled_id, "status": "cancelled"}


# ---------- Threaded follow-up ----------
@mcp.tool
def thread_followup(outreach_id: int, template_name: str, variables: dict | None = None) -> dict:
    """Draft a follow-up that keeps the same subject (prefixed 'Re:') and threads onto the original
    Gmail conversation when a thread_id is known. Drafts-first; review then send_draft."""
    row = store.query_one("SELECT * FROM outreach WHERE id=?", (outreach_id,))
    if not row:
        return not_found("outreach", outreach_id, available=_recent_outreach_ids(),
                         hint="use list_outreach()")
    rendered = render_with_profile(template_name, {**(variables or {}),
                                                   "name": row.get("recipient_name", ""),
                                                   "company": row.get("company", ""),
                                                   "role": row.get("role", "")})
    if "error" in rendered:
        return rendered
    orig_subject = row.get("subject") or ""
    subject = orig_subject if orig_subject.lower().startswith("re:") else f"Re: {orig_subject}"
    if rendered.get("subject"):
        subject = rendered["subject"]
    service, gerr = _gmail_or_err()
    if gerr:
        return gerr
    message = _build_message(row["recipient_email"], subject, rendered["body"])
    if row.get("thread_id"):
        message["threadId"] = row["thread_id"]
    try:
        draft = service.users().drafts().create(userId="me", body={"message": message}).execute()
    except Exception as e:  # noqa: BLE001 — transient Gmail/API error
        return err(f"{type(e).__name__}: {e}", code="gmail_error")
    new_id = store.execute(
        "INSERT INTO outreach(recipient_name,recipient_email,company,role,template_used,subject,"
        "gmail_message_id,thread_id,status,sequence_id,step,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (row.get("recipient_name", ""), row["recipient_email"], row.get("company", ""),
         row.get("role", ""), template_name, subject, draft["message"]["id"],
         row.get("thread_id") or draft["message"].get("threadId", ""), "drafted",
         row.get("sequence_id"), (row.get("step") or 0) + 1, _now()))
    record_followup(outreach_id)
    _log_event(new_id, "drafted", "thread_followup")
    return {"draft_id": draft["id"], "outreach_id": new_id, "subject": subject}


# ---------- Analytics / export ----------
@mcp.tool
def analytics(days: int = 90) -> dict:
    """Outreach funnel over the last N days: drafted/sent/replied counts, reply rate, avg followups,
    and breakdowns by company / template / variant."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    base = "FROM outreach WHERE created_at >= ?"
    drafted = store.query_one(f"SELECT COUNT(*) AS n {base}", (cutoff,))["n"]
    sent = store.query_one(f"SELECT COUNT(*) AS n {base} AND status IN ('sent','replied')", (cutoff,))["n"]
    replied = store.query_one(f"SELECT COUNT(*) AS n {base} AND status='replied'", (cutoff,))["n"]
    avg_fu = store.query_one(f"SELECT COALESCE(AVG(followup_count),0) AS a {base}", (cutoff,))["a"]

    def grp(col: str) -> list[dict]:
        return store.query(
            f"SELECT COALESCE(NULLIF({col},''),'(none)') AS k, COUNT(*) AS n, "
            f"SUM(CASE WHEN status='replied' THEN 1 ELSE 0 END) AS replied "
            f"{base} GROUP BY COALESCE(NULLIF({col},''),'(none)') ORDER BY n DESC LIMIT 15", (cutoff,))

    return {
        "days": days,
        "drafted": drafted, "sent": sent, "replied": replied,
        "reply_rate": round(replied / sent, 3) if sent else 0.0,
        "avg_followups": round(avg_fu, 2),
        "by_company": grp("company"),
        "by_template": grp("template_used"),
        "by_variant": grp("variant"),
    }


@mcp.tool
def export_csv(path: str = "") -> dict:
    """Export the outreach tracker to a CSV file (for backup/analysis). Returns path + count."""
    rows = store.query(
        "SELECT id,recipient_name,recipient_email,company,role,template_used,subject,status,"
        "sent_at,followup_count,variant,sequence_id,step,replied_at,created_at FROM outreach "
        "ORDER BY created_at DESC")
    out = Path(path).expanduser() if path else DATA / f"outreach_export_{datetime.now().date()}.csv"
    cols = ["id", "recipient_name", "recipient_email", "company", "role", "template_used", "subject",
            "status", "sent_at", "followup_count", "variant", "sequence_id", "step", "replied_at",
            "created_at"]
    with out.open("w", newline="", encoding="utf-8") as fh:
        w = csv.DictWriter(fh, fieldnames=cols, extrasaction="ignore")
        w.writeheader()
        w.writerows(rows)
    return {"path": str(out), "count": len(rows)}


if __name__ == "__main__":
    mcp.run()
