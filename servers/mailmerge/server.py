"""mailmerge — personalized bulk outreach with STRICT guardrails. Renders a batch from a
template, validates/dedupes/throttles, and (by design) hands off the actual sending to `reachout`
one row at a time so caps + the campaign ledger stay authoritative. Independent: it never sends directly.
"""
from __future__ import annotations

import csv
import html
import io
import re
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path

# Cap CSV file reads so a huge/hostile file can't exhaust memory.
_MAX_CSV_BYTES = 20 * 1024 * 1024  # 20 MB

from jinja2 import Environment, meta
from jinja2 import Template
from mcp_base import data_dir, db_path, make_server

mcp = make_server(
    "mailmerge",
    instructions=("Import (import_csv) -> validate (validate_recipients) -> preview/personalize_check "
                  "-> dedupe vs your ledger -> throttle_plan -> render_batch, then send via "
                  "reachout.create_draft per row so caps + the campaign ledger stay authoritative. "
                  "Never sends directly."),
)

REACHOUT_TEMPLATES = Path(__file__).resolve().parents[1] / "reachout" / "templates"
DATA = data_dir("mailmerge")
_JINJA_ENV = Environment()


def _load_template(name: str) -> str | None:
    f = REACHOUT_TEMPLATES / f"{name}.md"
    return f.read_text(encoding="utf-8") if f.exists() else None


def _available_templates() -> list[str]:
    if not REACHOUT_TEMPLATES.exists():
        return []
    return sorted(f.stem for f in REACHOUT_TEMPLATES.glob("*.md"))


def _template_vars(raw: str) -> set[str]:
    """Undeclared (required) Jinja2 variables referenced by a template."""
    try:
        return meta.find_undeclared_variables(_JINJA_ENV.parse(raw))
    except Exception:
        return set()


def _split_subject(text: str) -> tuple[str, str]:
    if text.lower().startswith("subject:"):
        first, _, rest = text.partition("\n")
        return first.split(":", 1)[1].strip(), rest.lstrip("\n")
    return "", text


def _ledger_emails() -> list[str]:
    """Read already-contacted emails from the campaign ledger DB, read-only.

    Opens ~/.mcp-suite/campaign/store.db in ro mode and pulls lowercased addresses from
    outreach_log. Returns [] if the DB is missing, locked, or the schema differs. Never writes —
    mailmerge stays independent; this is an opt-in convenience for dedupe(use_ledger=True)."""
    cdb = db_path("campaign")
    if not Path(cdb).exists():
        return []
    conn = None
    try:
        conn = sqlite3.connect(f"file:{cdb}?mode=ro", uri=True, timeout=2.0)
        rows = conn.execute(
            "SELECT DISTINCT LOWER(TRIM(email)) e FROM outreach_log "
            "WHERE email IS NOT NULL AND TRIM(email) <> ''"
        ).fetchall()
        return [r[0] for r in rows if r and r[0]]
    except Exception:  # noqa: BLE001 — missing/locked DB or schema drift -> no ledger emails
        return []
    finally:
        if conn is not None:
            conn.close()


def _valid_email(addr: str) -> bool:
    """Syntax-only email check (offline). Uses email-validator if present, else a regex fallback."""
    addr = (addr or "").strip()
    if not addr:
        return False
    try:
        from email_validator import EmailNotValidError, validate_email
        try:
            validate_email(addr, check_deliverability=False)
            return True
        except EmailNotValidError:
            return False
    except Exception:
        return bool(re.match(r"^[^@\s]+@[^@\s]+\.[^@\s]+$", addr))


@mcp.tool
def preview(recipients: list[dict], template_name: str, common: dict | None = None,
            limit: int = 50) -> dict:
    """Render the batch (first `limit` rows) so you can eyeball it before anything is created.
    Each recipient is a dict of template vars (must include at least `to_email`). Each rendered row also
    reports missing_vars, valid_email, and word/char counts."""
    raw = _load_template(template_name)
    if raw is None:
        return {"error": f"no reachout template '{template_name}'", "available": _available_templates()}
    tmpl = Template(raw)
    required = _template_vars(raw)
    rendered, issues = [], []
    common = common or {}
    for i, r in enumerate(recipients[:limit]):
        ctx = {**common, **r}
        if not r.get("to_email"):
            issues.append({"row": i, "issue": "missing to_email"})
        missing_vars = sorted(v for v in required if not str(ctx.get(v, "")).strip())
        if missing_vars:
            issues.append({"row": i, "issue": f"missing vars: {', '.join(missing_vars)}"})
        text = tmpl.render(**ctx)
        subject, body = _split_subject(text)
        rendered.append({
            "to_email": r.get("to_email", ""), "subject": subject, "body": body,
            "missing_vars": missing_vars,
            "valid_email": _valid_email(r.get("to_email", "")),
            "word_count": len(body.split()), "char_count": len(body),
        })
    return {"count": len(rendered), "issues": issues, "rendered": rendered}


@mcp.tool
def dry_run(recipients: list[dict], template_name: str, daily_cap: int = 20) -> dict:
    """Validate a batch without sending: flag missing/invalid emails, duplicate addresses, and whether
    the batch exceeds the daily cap. Returns a plan the agent executes via reachout.create_draft."""
    raw = _load_template(template_name)
    if raw is None:
        return {"error": f"no reachout template '{template_name}'", "available": _available_templates()}
    seen, dupes, missing, invalid, ok = set(), [], [], [], []
    for i, r in enumerate(recipients):
        email = (r.get("to_email") or "").strip().lower()
        if not email:
            missing.append(i)
        elif not _valid_email(email):
            invalid.append(email)
        elif email in seen:
            dupes.append(email)
        else:
            seen.add(email)
            ok.append(r)
    plan = ok[:daily_cap]
    return {
        "total": len(recipients), "sendable": len(ok), "missing_email": missing,
        "invalid_email": invalid, "duplicates": dupes,
        "over_cap": max(0, len(ok) - daily_cap), "plan_count": len(plan),
        "next_action": "For each planned recipient call reachout.create_draft (drafts-first); "
                       "then campaign.record_outreach. Respect reachout's cap + cooldown.",
    }


@mcp.tool
def validate_recipients(recipients: list[dict], required_vars: list[str] | None = None) -> dict:
    """Per-row hygiene check (no template needed): valid email syntax, missing required vars, and
    duplicate detection. Returns a summary plus per-row diagnostics."""
    required = required_vars or []
    seen: dict[str, int] = {}
    rows, n_valid, n_dupe = [], 0, 0
    for i, r in enumerate(recipients):
        email = (r.get("to_email") or "").strip()
        norm = email.lower()
        valid = _valid_email(email)
        dupe = norm in seen and bool(norm)
        if norm:
            seen[norm] = seen.get(norm, 0) + 1
        miss = [v for v in required if not str(r.get(v, "")).strip()]
        if valid and not dupe:
            n_valid += 1
        if dupe:
            n_dupe += 1
        rows.append({"row": i, "to_email": email, "normalized": norm, "valid_email": valid,
                     "duplicate": dupe, "missing_vars": miss})
    return {"total": len(recipients), "valid_unique": n_valid, "duplicates": n_dupe,
            "invalid": sum(1 for r in rows if not r["valid_email"]), "rows": rows}


@mcp.tool
def import_csv(source: str, email_column: str = "email", is_path: bool = True) -> dict:
    """Parse a CSV into the recipients shape (list of dicts). `source` is a file path (is_path=True) or
    raw CSV text (is_path=False). The chosen email_column is mapped to `to_email`; all columns are kept
    as template vars. Returns rows + any warnings."""
    try:
        if is_path:
            p = Path(source).expanduser()
            if not p.exists():
                return {"error": f"file not found: {p}"}
            if not p.is_file():
                return {"error": f"not a file: {p}"}
            if p.stat().st_size > _MAX_CSV_BYTES:
                return {"error": f"CSV too large ({p.stat().st_size} bytes > {_MAX_CSV_BYTES})"}
            try:
                text = p.read_text(encoding="utf-8")
            except UnicodeDecodeError:
                text = p.read_text(encoding="latin-1")
        else:
            if not isinstance(source, str):
                return {"error": "source must be a string"}
            if len(source.encode("utf-8", "ignore")) > _MAX_CSV_BYTES:
                return {"error": "CSV text too large"}
            text = source
        reader = csv.DictReader(io.StringIO(text))
        if not reader.fieldnames:
            return {"error": "no header row found in CSV"}
        fields = [f.strip() for f in reader.fieldnames]
        email_key = next((f for f in fields if f.lower() == email_column.lower()), None)
        warnings = []
        if not email_key:
            warnings.append(f"no '{email_column}' column; columns: {fields}")
        rows = []
        for raw in reader:
            row = {(k.strip() if k else k): (v.strip() if isinstance(v, str) else v)
                   for k, v in raw.items()}
            if email_key and row.get(email_key):
                row["to_email"] = row[email_key]
            rows.append(row)
        no_email = sum(1 for r in rows if not r.get("to_email"))
        if no_email:
            warnings.append(f"{no_email} row(s) have no email")
        return {"count": len(rows), "columns": fields, "recipients": rows, "warnings": warnings}
    except Exception as e:
        return {"error": f"{type(e).__name__}: {e}"}


@mcp.tool
def dedupe(recipients: list[dict], against_emails: list[str] | None = None,
           use_ledger: bool = False) -> dict:
    """Collapse duplicate emails within the batch and (optionally) subtract emails already contacted.
    Pass `against_emails` from your reachout/campaign ledger to avoid re-contacting. Returns kept + removed.

    By default mailmerge stays independent (reads no other server's DB). Set use_ledger=True to also
    pull already-contacted addresses from the campaign ledger (~/.mcp-suite/campaign/store.db,
    read-only) so dedupe works without a manually-passed list; the two sources are merged. The
    return shape is unchanged (removed_already_contacted covers both sources)."""
    blocked = {e.strip().lower() for e in (against_emails or []) if e and e.strip()}
    if use_ledger:
        blocked |= set(_ledger_emails())
    seen: set[str] = set()
    kept, removed_dupe, removed_blocked = [], [], []
    for r in recipients:
        email = (r.get("to_email") or "").strip().lower()
        if not email:
            kept.append(r)  # nothing to dedupe on; surface in validation instead
            continue
        if email in blocked:
            removed_blocked.append(email)
        elif email in seen:
            removed_dupe.append(email)
        else:
            seen.add(email)
            kept.append(r)
    return {"input": len(recipients), "kept": kept, "kept_count": len(kept),
            "removed_duplicates": removed_dupe, "removed_already_contacted": removed_blocked}


@mcp.tool
def throttle_plan(count: int, daily_cap: int = 20, per_minute: int = 5,
                  start_iso: str = "", skip_weekends: bool = True) -> dict:
    """Compute a humane send schedule for `count` messages: how many days, daily batch sizes, the
    suggested delay between sends, and the per-day start dates. Pure planning — no sending."""
    count = max(0, int(count))
    daily_cap = max(1, int(daily_cap))
    per_minute = max(1, int(per_minute))
    days_needed = (count + daily_cap - 1) // daily_cap if count else 0
    try:
        start = datetime.fromisoformat(start_iso) if start_iso else datetime.now(timezone.utc)
    except ValueError:
        start = datetime.now(timezone.utc)
    schedule, remaining, cursor = [], count, start
    while remaining > 0:
        if skip_weekends and cursor.weekday() >= 5:
            cursor += timedelta(days=1)
            continue
        batch = min(daily_cap, remaining)
        schedule.append({"date": cursor.date().isoformat(), "count": batch})
        remaining -= batch
        cursor += timedelta(days=1)
    return {
        "total": count, "daily_cap": daily_cap, "days_needed": days_needed,
        "suggested_delay_seconds": round(60 / per_minute, 1),
        "per_minute": per_minute, "skip_weekends": skip_weekends,
        "schedule": schedule,
        "advice": "Ramp new domains slowly (warmup). Keep daily volume under the cap and randomize "
                  "send times within business hours to look human.",
    }


@mcp.tool
def render_batch(recipients: list[dict], template_name: str, common: dict | None = None,
                 limit: int = 200) -> dict:
    """Render the full reachout-ready payload for each recipient (to_email/subject/body + recipient_name/
    company/role/template_used), with validation flags, so you can loop directly into reachout.create_draft."""
    raw = _load_template(template_name)
    if raw is None:
        return {"error": f"no reachout template '{template_name}'", "available": _available_templates()}
    tmpl = Template(raw)
    required = _template_vars(raw)
    common = common or {}
    payloads, skipped = [], []
    for i, r in enumerate(recipients[:limit]):
        ctx = {**common, **r}
        email = (r.get("to_email") or "").strip()
        if not _valid_email(email):
            skipped.append({"row": i, "to_email": email, "reason": "invalid/missing email"})
            continue
        missing_vars = sorted(v for v in required if not str(ctx.get(v, "")).strip())
        try:
            rendered = tmpl.render(**ctx)
        except Exception as render_err:
            skipped.append({"row": i, "to_email": email, "reason": f"render error: {render_err}"})
            continue
        subject, body = _split_subject(rendered)
        payloads.append({
            "to_email": email, "subject": subject, "body": body,
            "recipient_name": r.get("name") or r.get("recipient_name", ""),
            "company": r.get("company", ""), "role": r.get("role", ""),
            "template_used": template_name,
            "_diagnostics": {"missing_vars": missing_vars},
        })
    return {"ready": len(payloads), "skipped": skipped, "payloads": payloads,
            "next_action": (
                "For each payload call reachout.create_draft(**{k:v for k,v in payload.items() "
                "if not k.startswith('_')}), review, then reachout.send_draft. "
                "Respect reachout's daily cap + cooldown."
            )}


@mcp.tool
def personalize_check(recipients: list[dict], template_name: str, common: dict | None = None) -> dict:
    """Spam-quality guardrail: detect rows that render IDENTICAL bodies (sign the template isn't actually
    personalized) and rows where required merge fields fell back to empty. Offline."""
    raw = _load_template(template_name)
    if raw is None:
        return {"error": f"no reachout template '{template_name}'", "available": _available_templates()}
    tmpl = Template(raw)
    required = _template_vars(raw)
    common = common or {}
    bodies: dict[str, int] = {}
    empty_field_rows = []
    for i, r in enumerate(recipients):
        ctx = {**common, **r}
        miss = [v for v in required if not str(ctx.get(v, "")).strip()]
        if miss:
            empty_field_rows.append({"row": i, "empty": miss})
        _, body = _split_subject(tmpl.render(**ctx))
        bodies[body] = bodies.get(body, 0) + 1
    identical = {b[:60] + ("..." if len(b) > 60 else ""): n for b, n in bodies.items() if n > 1}
    distinct = len(bodies)
    return {
        "total": len(recipients), "distinct_bodies": distinct,
        "identical_groups": identical,
        "fully_personalized": distinct == len(recipients) and not empty_field_rows,
        "rows_with_empty_fields": empty_field_rows,
        "advice": "If distinct_bodies is much lower than total, your template barely personalizes — "
                  "add per-recipient detail to avoid spam filters.",
    }


@mcp.tool
def export_preview(recipients: list[dict], template_name: str, common: dict | None = None,
                   fmt: str = "md", dest: str = "", limit: int = 200) -> dict:
    """Write a human-reviewable preview file (fmt: md | csv | html) of the rendered batch to disk
    (defaults to the mailmerge data dir). Returns the file path. No sending."""
    raw = _load_template(template_name)
    if raw is None:
        return {"error": f"no reachout template '{template_name}'", "available": _available_templates()}
    tmpl = Template(raw)
    common = common or {}
    rows = []
    for r in recipients[:limit]:
        ctx = {**common, **r}
        subject, body = _split_subject(tmpl.render(**ctx))
        rows.append({"to_email": r.get("to_email", ""), "subject": subject, "body": body})

    fmt = fmt.lower()
    out_dir = Path(dest).expanduser() if dest else (DATA / "previews")
    out_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    if fmt == "csv":
        path = out_dir / f"preview-{template_name}-{stamp}.csv"
        with path.open("w", newline="", encoding="utf-8") as fh:
            w = csv.DictWriter(fh, fieldnames=["to_email", "subject", "body"])
            w.writeheader()
            w.writerows(rows)
    elif fmt == "html":
        path = out_dir / f"preview-{template_name}-{stamp}.html"
        cells = "".join(
            f"<tr><td>{i+1}</td><td>{html.escape(str(r['to_email']))}</td>"
            f"<td>{html.escape(str(r['subject']))}</td>"
            f"<td><pre style='white-space:pre-wrap'>{html.escape(str(r['body']))}</pre></td></tr>"
            for i, r in enumerate(rows))
        path.write_text(
            "<table border=1 cellpadding=6><tr><th>#</th><th>To</th><th>Subject</th>"
            f"<th>Body</th></tr>{cells}</table>", encoding="utf-8")
    else:
        fmt = "md"
        path = out_dir / f"preview-{template_name}-{stamp}.md"
        parts = [f"# Preview: {template_name} ({len(rows)} rows)\n"]
        for i, r in enumerate(rows):
            parts.append(f"## {i+1}. {r['to_email']}\n\n**Subject:** {r['subject']}\n\n{r['body']}\n")
        path.write_text("\n".join(parts), encoding="utf-8")
    return {"ok": True, "path": str(path), "format": fmt, "rows": len(rows)}


@mcp.tool
def campaign_advice(sendable: int, daily_cap: int = 20, cooldown_days: int = 14) -> dict:
    """Deterministic guidance on running this batch safely: dedupe-vs-ledger, cooldown, warmup ramp,
    and the drafts-first handoff to reachout. Offline."""
    days = (max(0, sendable) + daily_cap - 1) // daily_cap if sendable else 0
    return {
        "sendable": sendable, "daily_cap": daily_cap, "estimated_days": days,
        "cooldown_days": cooldown_days,
        "checklist": [
            "Dedupe within the batch and against your campaign ledger (pass already-contacted "
            "emails to mailmerge.dedupe).",
            f"Stay under reachout's daily cap (~{daily_cap}) and let its cooldown ({cooldown_days}d) "
            "block recent re-contacts.",
            "Drafts-first: reachout.create_draft -> review -> reachout.send_draft.",
            "Warm up new sending domains gradually; personalize every row (personalize_check).",
            "Include a clear opt-out and a real reply-to.",
        ],
    }


def _strip_subject_prefix(s: str) -> str:
    """Drop a leading 'Subject:' label if present; return the bare subject line."""
    s = (s or "").strip()
    if s.lower().startswith("subject:"):
        s = s.split(":", 1)[1].strip()
    # collapse to a single line + squeeze internal whitespace
    return re.sub(r"\s+", " ", s.splitlines()[0].strip()) if s else ""


@mcp.tool
def ab_subjects(subject: str, n: int = 3) -> dict:
    """Generate labeled A/B subject-line variants from a base subject (analysis/draft only — never sends).

    Returns up to `n` deterministically-derived variants (A, B, C, ...), each with a short rationale and
    a heuristic length flag, so you can pick the strongest line for reachout. Offline; never raises."""
    base = _strip_subject_prefix(subject)
    if not base:
        return {"error": "subject is required", "variants": []}
    try:
        n = int(n)
    except (TypeError, ValueError):
        n = 3
    n = max(1, min(n, 6))

    words = base.split()
    first_word = words[0] if words else base

    def _flags(text: str) -> dict:
        chars = len(text)
        return {
            "char_count": chars,
            "word_count": len(text.split()),
            # ~ <50 chars renders without truncation in most inbox previews
            "length_ok": chars <= 50,
            "too_long": chars > 70,
        }

    # Deterministic, no-LLM heuristics. Each is a distinct, defensible angle.
    candidates: list[tuple[str, str]] = [
        (base, "control / original"),
        (base.lower() if base != base.lower() else base,
         "lowercase, casual tone (often feels more personal, less 'marketing')"),
        (f"Quick question: {first_word.lower()}..." if first_word else base,
         "curiosity-gap opener (drives opens; pair with a relevant body)"),
        (re.sub(r"[.!?…]+$", "", base),
         "punctuation-trimmed, plain statement"),
        (f"{base} ({datetime.now(timezone.utc).strftime('%b')})" if len(base) <= 55 else base,
         "timeliness cue with current month"),
        (" ".join(words[:6]) + ("…" if len(words) > 6 else ""),
         "shortened to first 6 words for mobile previews"),
    ]

    variants, seen = [], set()
    labels = "ABCDEF"
    for text, why in candidates:
        text = _strip_subject_prefix(text)
        if not text:
            continue
        key = text.lower()
        if key in seen:
            continue
        seen.add(key)
        variants.append({
            "label": labels[len(variants)],
            "subject": text,
            "rationale": why,
            **_flags(text),
        })
        if len(variants) >= n:
            break

    return {
        "base": base,
        "count": len(variants),
        "variants": variants,
        "advice": "Send variant A to a holdout and one alternative to a matched group; keep the body "
                  "identical so the subject is the only changed variable. Pick the winner by open rate, "
                  "then standardize. Aim for <= 50 chars so the line isn't truncated in inbox previews.",
        "note": "Draft/analysis only — mailmerge never sends. Hand the chosen subject to reachout.",
    }


# Heuristic best-send windows (local time at the recipient). Tuesday-Thursday mid-morning and
# early afternoon are the broadly-cited cold-outreach sweet spots; Monday AM and Fri PM are weak.
_SEND_WINDOWS = [
    {"day": "Tue", "start": "09:30", "end": "11:00", "tier": "best",
     "why": "Mid-morning Tue/Wed/Thu is the classic cold-email peak"},
    {"day": "Wed", "start": "09:30", "end": "11:00", "tier": "best",
     "why": "Mid-morning Tue/Wed/Thu is the classic cold-email peak"},
    {"day": "Thu", "start": "09:30", "end": "11:00", "tier": "best",
     "why": "Mid-morning Tue/Wed/Thu is the classic cold-email peak"},
    {"day": "Tue", "start": "13:30", "end": "15:00", "tier": "good",
     "why": "Early afternoon, post-lunch inbox triage"},
    {"day": "Wed", "start": "13:30", "end": "15:00", "tier": "good",
     "why": "Early afternoon, post-lunch inbox triage"},
    {"day": "Thu", "start": "13:30", "end": "15:00", "tier": "good",
     "why": "Early afternoon, post-lunch inbox triage"},
    {"day": "Mon", "start": "10:00", "end": "11:30", "tier": "ok",
     "why": "After the Monday backlog clears; avoid the early-AM flood"},
]

_AVOID_WINDOWS = [
    "Before 08:00 and after 18:00 local (off-hours look automated)",
    "Monday before 10:00 (buried under weekend backlog)",
    "Friday after 14:00 and all weekend (low engagement)",
    "Lunch hour ~12:00-13:00 local",
]


@mcp.tool
def send_time_suggestion(timezone: str = "") -> dict:
    """Heuristic best send-time windows for cold outreach (analysis only — never schedules or sends).

    Returns ranked day/time windows in the recipient's LOCAL time, plus windows to avoid. `timezone` is
    a free-text label (e.g. 'America/New_York', 'PT', 'recipient local') echoed back for context; no
    network, no tz math — pure guidance. Never raises."""
    tz = (timezone or "").strip() or "recipient local"
    best = [w for w in _SEND_WINDOWS if w["tier"] == "best"]
    return {
        "timezone": tz,
        "basis": "local time at the recipient",
        "windows": list(_SEND_WINDOWS),
        "top_pick": {
            "day": "Tue/Wed/Thu",
            "window": "09:30-11:00",
            "why": best[0]["why"] if best else "mid-morning midweek",
        },
        "avoid": list(_AVOID_WINDOWS),
        "advice": "Schedule in the recipient's local timezone, not yours. Randomize the exact minute "
                  "within a window so sends don't look batched, and keep daily volume under your cap "
                  "(see throttle_plan). These are heuristics, not guarantees — test against your own "
                  "open-rate data.",
        "note": "Draft/analysis only — mailmerge never schedules or sends. Use reachout to act on this.",
    }


if __name__ == "__main__":
    mcp.run()
