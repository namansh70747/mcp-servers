"""mailbox — inbox triage + reply assist over Gmail (read/label/draft/search/filters/attachments).
Reuses your Google OAuth client (looks for credentials.json here, then falls back to reachout's).
Scope: gmail.modify (covers read, label, draft, archive, trash, settings filters)."""
from __future__ import annotations

import base64
import os
import re
from email.message import EmailMessage
from email.utils import parseaddr, parsedate_to_datetime
from pathlib import Path

from mcp_base import data_dir, get_gmail_service, make_server

# Cap how much attachment data we will decode/write to disk (defensive; avoids
# unbounded memory/disk use from a hostile or oversized attachment).
_MAX_ATTACHMENT_BYTES = 50 * 1024 * 1024  # 50 MB

mcp = make_server(
    "mailbox",
    instructions=("Triage Gmail: list_unread / search -> summarize_thread or thread_actions "
                  "-> draft_reply (drafts only). needs_reply finds what's waiting; summarize_inbox "
                  "gives an at-a-glance triage. Label/archive/star/trash + Gmail filters supported."),
)

DATA = data_dir("mailbox")
REACHOUT_DATA = data_dir("reachout")
SCOPES = ["https://www.googleapis.com/auth/gmail.modify"]


def _cred_path() -> Path:
    here = DATA / "credentials.json"
    return here if here.exists() else REACHOUT_DATA / "credentials.json"


def _gmail():
    # Route OAuth through the shared helper, keeping mailbox's behavior identical:
    # token always lives in mailbox's DATA, credentials are read from DATA if present
    # else fall back to reachout's data dir. We pick the data dir that already holds a
    # token (mailbox) or the one that holds credentials, so the shared helper finds both.
    token_path = DATA / "token.json"
    if token_path.exists() or (DATA / "credentials.json").exists():
        auth_dir = DATA
    elif (REACHOUT_DATA / "credentials.json").exists():
        auth_dir = REACHOUT_DATA
    else:
        raise RuntimeError(f"Missing credentials.json (looked in {DATA} and {REACHOUT_DATA}).")
    return get_gmail_service(auth_dir, SCOPES)


def _safe(fn):
    """Run a Gmail call, converting auth/API errors into {'error': ...} dicts."""
    try:
        return fn()
    except RuntimeError as e:
        return {"error": str(e)}
    except Exception as e:  # googleapiclient.errors.HttpError etc.
        return {"error": f"{type(e).__name__}: {e}"}


def _clamp(n: int, lo: int, hi: int) -> int:
    return max(lo, min(hi, int(n)))


def _safe_filename(name: str, fallback: str) -> str:
    """Reduce an untrusted attachment filename to a single safe basename.

    Strips any directory components (defeats path traversal like '../../x' or
    '/etc/passwd'), removes NUL bytes, and rejects empty/dot-only names."""
    base = os.path.basename((name or "").replace("\\", "/").replace("\x00", "")).strip()
    if not base or base in (".", ".."):
        return fallback
    return base


# ---------- offline parsing helpers (no network) ----------
def _parse_addr(from_header: str | None) -> dict:
    """Split a From header into {name, email} using stdlib parsing."""
    name, email = parseaddr(from_header or "")
    return {"name": name, "email": email}


def _strip_html(html: str) -> str:
    """Best-effort HTML -> text. Uses beautifulsoup4 if available, else a regex fallback."""
    if not html:
        return ""
    try:
        from bs4 import BeautifulSoup
        return BeautifulSoup(html, "html.parser").get_text(separator="\n").strip()
    except Exception:
        text = re.sub(r"(?is)<(script|style).*?>.*?</\1>", "", html)
        text = re.sub(r"(?s)<[^>]+>", " ", text)
        text = re.sub(r"&nbsp;?", " ", text)
        return re.sub(r"[ \t]+\n", "\n", re.sub(r"[ \t]{2,}", " ", text)).strip()


_ACTION_HINTS = (
    "can you", "could you", "would you", "please", "let me know", "need to",
    "by eod", "by end of", "deadline", "due ", "asap", "follow up", "action item",
    "to do", "todo", "next step", "we should", "make sure", "don't forget",
)


def _extract_actions(text: str, limit: int = 12) -> list[str]:
    """Heuristic action/question extraction from a message body (offline best-effort)."""
    if not text:
        return []
    out: list[str] = []
    seen: set[str] = set()
    # split into sentence-ish chunks
    chunks = re.split(r"(?<=[.!?])\s+|\n+", text)
    for raw in chunks:
        s = raw.strip()
        if len(s) < 6 or len(s) > 280:
            continue
        low = s.lower()
        if s.endswith("?") or any(h in low for h in _ACTION_HINTS):
            key = low[:60]
            if key not in seen:
                seen.add(key)
                out.append(s)
        if len(out) >= limit:
            break
    return out


def _attachment_meta(payload: dict) -> list[dict]:
    """Collect attachment metadata (filename, mimeType, size, attachment_id) from a message payload."""
    out: list[dict] = []

    def walk(part: dict):
        filename = part.get("filename") or ""
        body = part.get("body", {}) or {}
        if filename and body.get("attachmentId"):
            out.append({
                "filename": filename,
                "mimeType": part.get("mimeType"),
                "size": body.get("size", 0),
                "attachment_id": body["attachmentId"],
            })
        for p in part.get("parts", []) or []:
            walk(p)

    walk(payload or {})
    return out


def _extract_text(payload) -> str:
    """Extract readable text: prefer text/plain, fall back to stripped text/html."""
    if payload.get("body", {}).get("data") and payload.get("mimeType", "").startswith("text/plain"):
        return base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
    if payload.get("body", {}).get("data") and not payload.get("parts"):
        raw = base64.urlsafe_b64decode(payload["body"]["data"]).decode("utf-8", "ignore")
        return _strip_html(raw) if payload.get("mimeType") == "text/html" else raw
    html_fallback = ""
    for part in payload.get("parts", []) or []:
        mt = part.get("mimeType", "")
        data = part.get("body", {}).get("data")
        if mt == "text/plain" and data:
            return base64.urlsafe_b64decode(data).decode("utf-8", "ignore")
        if mt == "text/html" and data and not html_fallback:
            html_fallback = _strip_html(base64.urlsafe_b64decode(data).decode("utf-8", "ignore"))
        t = _extract_text(part)
        if t:
            return t
    return html_fallback


def _headers(msg: dict) -> dict:
    return {h["name"]: h["value"] for h in msg.get("payload", {}).get("headers", [])}


# ---------- label resolution ----------
def _label_map(svc) -> dict:
    """Return {lower_name: id} and keep system label ids usable directly."""
    res = svc.users().labels().list(userId="me").execute()
    return {lbl["name"].lower(): lbl["id"] for lbl in res.get("labels", [])}


def _resolve_label_ids(svc, names: list[str], create_missing: bool = False) -> list[str]:
    name_to_id = _label_map(svc)
    ids: list[str] = []
    for n in names:
        key = n.lower()
        if key in name_to_id:
            ids.append(name_to_id[key])
        elif n.upper() in ("INBOX", "UNREAD", "STARRED", "IMPORTANT", "SPAM", "TRASH", "SENT", "DRAFT"):
            ids.append(n.upper())
        elif create_missing:
            created = svc.users().labels().create(
                userId="me", body={"name": n, "labelListVisibility": "labelShow",
                                   "messageListVisibility": "show"}).execute()
            ids.append(created["id"])
            name_to_id[key] = created["id"]
        else:
            raise RuntimeError(f"label not found: {n}")
    return ids


# ---------- tools ----------
@mcp.tool
def auth_status() -> dict:
    """Report Gmail auth state for mailbox."""
    return {"data_dir": str(DATA), "credentials_found": str(_cred_path()) if _cred_path().exists() else None,
            "token_present": (DATA / "token.json").exists()}


@mcp.tool
def list_unread(max_results: int = 15) -> list[dict] | dict:
    """List unread messages (id, thread, from, subject, snippet)."""
    def go():
        svc = _gmail()
        res = svc.users().messages().list(userId="me", q="is:unread", maxResults=max_results).execute()
        out = []
        for m in res.get("messages", []):
            full = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["From", "Subject"]).execute()
            hdr = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            out.append({"id": m["id"], "thread_id": full["threadId"], "from": hdr.get("From"),
                        "subject": hdr.get("Subject"), "snippet": full.get("snippet", "")})
        return out
    return _safe(go)


@mcp.tool
def needs_reply(max_results: int = 15) -> list[dict] | dict:
    """Unread messages in the inbox not sent by you — i.e. likely waiting for a reply."""
    def go():
        svc = _gmail()
        res = svc.users().messages().list(userId="me", q="is:unread in:inbox -from:me",
                                          maxResults=max_results).execute()
        out = []
        for m in res.get("messages", []):
            full = svc.users().messages().get(userId="me", id=m["id"], format="metadata",
                                              metadataHeaders=["From", "Subject"]).execute()
            hdr = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            out.append({"id": m["id"], "thread_id": full["threadId"], "from": hdr.get("From"),
                        "subject": hdr.get("Subject"), "snippet": full.get("snippet", "")})
        return out
    return _safe(go)


@mcp.tool
def search(query: str, max_results: int = 20, include_snippet: bool = True) -> dict:
    """Search Gmail with any Gmail query operators (e.g. 'from:x newer_than:7d has:attachment is:unread').
    Returns matching messages with id/thread/from/subject/date/labels (and snippet by default)."""
    def go():
        svc = _gmail()
        n = _clamp(max_results, 1, 100)
        res = svc.users().messages().list(userId="me", q=query, maxResults=n).execute()
        out = []
        for m in res.get("messages", []):
            full = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
            hdr = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            row = {"id": m["id"], "thread_id": full["threadId"], "from": hdr.get("From"),
                   "subject": hdr.get("Subject"), "date": hdr.get("Date"),
                   "labels": full.get("labelIds", [])}
            if include_snippet:
                row["snippet"] = full.get("snippet", "")
            out.append(row)
        return {"query": query, "count": len(out), "messages": out}
    return _safe(go)


@mcp.tool
def list_threads(query: str = "in:inbox", max_results: int = 20) -> dict:
    """List threads matching a Gmail query, with message counts and the latest message's from/subject."""
    def go():
        svc = _gmail()
        n = _clamp(max_results, 1, 100)
        res = svc.users().threads().list(userId="me", q=query, maxResults=n).execute()
        out = []
        for t in res.get("threads", []):
            th = svc.users().threads().get(userId="me", id=t["id"], format="metadata",
                                           metadataHeaders=["From", "Subject"]).execute()
            msgs = th.get("messages", [])
            last = _headers(msgs[-1]) if msgs else {}
            out.append({"thread_id": t["id"], "message_count": len(msgs),
                        "last_from": last.get("From"), "subject": last.get("Subject"),
                        "snippet": t.get("snippet", "")})
        return {"query": query, "count": len(out), "threads": out}
    return _safe(go)


@mcp.tool
def summarize_thread(thread_id: str) -> dict:
    """Return a thread's messages as plain text so you (the agent) can summarize/extract actions."""
    def go():
        svc = _gmail()
        th = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        msgs = []
        for m in th.get("messages", []):
            hdr = {h["name"]: h["value"] for h in m["payload"]["headers"]}
            body = _extract_text(m["payload"])
            msgs.append({"from": hdr.get("From"), "date": hdr.get("Date"),
                         "subject": hdr.get("Subject"), "text": body[:4000]})
        return {"thread_id": thread_id, "messages": msgs}
    return _safe(go)


@mcp.tool
def thread_actions(thread_id: str) -> dict:
    """Return a thread's text plus a heuristic list of likely action items / open questions per message
    (offline keyword+question scan — a starting point for you to refine)."""
    def go():
        svc = _gmail()
        th = svc.users().threads().get(userId="me", id=thread_id, format="full").execute()
        msgs, all_actions = [], []
        for m in th.get("messages", []):
            hdr = _headers(m)
            body = _extract_text(m["payload"])
            actions = _extract_actions(body)
            all_actions.extend(actions)
            msgs.append({"from": hdr.get("From"), "date": hdr.get("Date"),
                         "subject": hdr.get("Subject"), "text": body[:3000],
                         "heuristic_actions": actions})
        return {"thread_id": thread_id, "messages": msgs,
                "all_heuristic_actions": all_actions,
                "note": "heuristic extraction — verify before acting"}
    return _safe(go)


@mcp.tool
def get_message(message_id: str, include_body: bool = True) -> dict:
    """Full single-message view: parsed from/to/subject/date, body text, and attachment metadata."""
    def go():
        svc = _gmail()
        m = svc.users().messages().get(userId="me", id=message_id, format="full").execute()
        hdr = _headers(m)
        res = {"id": message_id, "thread_id": m.get("threadId"),
               "from": _parse_addr(hdr.get("From")), "to": hdr.get("To"),
               "subject": hdr.get("Subject"), "date": hdr.get("Date"),
               "labels": m.get("labelIds", []), "snippet": m.get("snippet", ""),
               "attachments": _attachment_meta(m["payload"])}
        if include_body:
            res["body"] = _extract_text(m["payload"])[:8000]
        return res
    return _safe(go)


@mcp.tool
def download_attachment(message_id: str, attachment_id: str, dest_dir: str = "") -> dict:
    """Download a message attachment to disk (defaults to the mailbox data dir). Returns path + bytes."""
    if not (message_id or "").strip() or not (attachment_id or "").strip():
        return {"error": "message_id and attachment_id are required"}

    def go():
        svc = _gmail()
        att = svc.users().messages().attachments().get(
            userId="me", messageId=message_id, id=attachment_id).execute()
        data = base64.urlsafe_b64decode(att["data"])
        if len(data) > _MAX_ATTACHMENT_BYTES:
            return {"error": f"attachment too large ({len(data)} bytes > {_MAX_ATTACHMENT_BYTES})"}
        meta = _attachment_meta(
            svc.users().messages().get(userId="me", id=message_id, format="full").execute()["payload"])
        raw_name = next((a["filename"] for a in meta if a["attachment_id"] == attachment_id), "")
        fname = _safe_filename(raw_name, f"{message_id}_{attachment_id[:8]}.bin")
        out_dir = (Path(dest_dir).expanduser() if dest_dir else (DATA / "attachments")).resolve()
        out_dir.mkdir(parents=True, exist_ok=True)
        # Confine the final path to out_dir even if the sanitized name still
        # somehow resolves elsewhere (belt-and-suspenders).
        path = (out_dir / fname).resolve()
        if out_dir not in path.parents and path != out_dir:
            return {"error": "refusing to write outside the destination directory"}
        path.write_bytes(data)
        return {"path": str(path), "bytes": len(data), "filename": fname}
    return _safe(go)


@mcp.tool
def draft_reply(thread_id: str, to_email: str, subject: str, body: str) -> dict:
    """Create a DRAFT reply within a thread (nothing sent)."""
    if not (thread_id or "").strip() or not (to_email or "").strip():
        return {"error": "thread_id and to_email are required"}
    # Guard against header injection via embedded newlines in user-supplied fields.
    to_email = to_email.replace("\r", " ").replace("\n", " ").strip()
    subject = (subject or "").replace("\r", " ").replace("\n", " ")

    def go():
        svc = _gmail()
        msg = EmailMessage()
        msg["To"] = to_email
        msg["Subject"] = subject if subject.lower().startswith("re:") else f"Re: {subject}"
        msg.set_content(body)
        raw = base64.urlsafe_b64encode(msg.as_bytes()).decode()
        draft = svc.users().drafts().create(
            userId="me", body={"message": {"raw": raw, "threadId": thread_id}}).execute()
        return {"draft_id": draft["id"], "thread_id": thread_id}
    return _safe(go)


@mcp.tool
def categorize(message_id: str, add_labels: list[str], remove_labels: list[str] | None = None) -> dict:
    """Apply/remove Gmail label IDs on a message (e.g. mark read by removing UNREAD)."""
    if not (message_id or "").strip():
        return {"error": "message_id is required"}

    def go():
        svc = _gmail()
        # id-existence check: confirm the message exists before modifying it.
        svc.users().messages().get(userId="me", id=message_id, format="minimal").execute()
        svc.users().messages().modify(userId="me", id=message_id,
                                      body={"addLabelIds": add_labels,
                                            "removeLabelIds": remove_labels or []}).execute()
        return {"ok": True, "id": message_id}
    return _safe(go)


@mcp.tool
def batch_modify(message_ids: list[str], add_labels: list[str] | None = None,
                 remove_labels: list[str] | None = None) -> dict:
    """Bulk add/remove label IDs across many messages in one call (great for mass triage)."""
    def go():
        svc = _gmail()
        svc.users().messages().batchModify(
            userId="me", body={"ids": message_ids, "addLabelIds": add_labels or [],
                               "removeLabelIds": remove_labels or []}).execute()
        return {"ok": True, "count": len(message_ids)}
    return _safe(go)


@mcp.tool
def list_labels() -> dict:
    """List all Gmail labels (id, name, type, message/thread totals)."""
    def go():
        svc = _gmail()
        res = svc.users().labels().list(userId="me").execute()
        out = []
        for lbl in res.get("labels", []):
            full = svc.users().labels().get(userId="me", id=lbl["id"]).execute()
            out.append({"id": lbl["id"], "name": lbl["name"], "type": lbl.get("type"),
                        "messages_total": full.get("messagesTotal"),
                        "messages_unread": full.get("messagesUnread"),
                        "threads_total": full.get("threadsTotal")})
        return {"count": len(out), "labels": out}
    return _safe(go)


@mcp.tool
def create_label(name: str) -> dict:
    """Create a Gmail label (idempotent: returns the existing one if the name already exists)."""
    def go():
        svc = _gmail()
        existing = _label_map(svc)
        if name.lower() in existing:
            return {"ok": True, "id": existing[name.lower()], "name": name, "created": False}
        created = svc.users().labels().create(
            userId="me", body={"name": name, "labelListVisibility": "labelShow",
                               "messageListVisibility": "show"}).execute()
        return {"ok": True, "id": created["id"], "name": name, "created": True}
    return _safe(go)


@mcp.tool
def apply_label(message_id: str, label_names: list[str], create_missing: bool = True) -> dict:
    """Apply labels to a message BY NAME (resolves to IDs; auto-creates missing labels by default)."""
    def go():
        svc = _gmail()
        ids = _resolve_label_ids(svc, label_names, create_missing=create_missing)
        svc.users().messages().modify(userId="me", id=message_id, body={"addLabelIds": ids}).execute()
        return {"ok": True, "id": message_id, "applied": label_names}
    return _safe(go)


@mcp.tool
def remove_label(message_id: str, label_names: list[str]) -> dict:
    """Remove labels from a message BY NAME (resolves to IDs; ignores names that don't exist)."""
    def go():
        svc = _gmail()
        ids = _resolve_label_ids(svc, [n for n in label_names], create_missing=False)
        svc.users().messages().modify(userId="me", id=message_id, body={"removeLabelIds": ids}).execute()
        return {"ok": True, "id": message_id, "removed": label_names}
    return _safe(go)


@mcp.tool
def mark_read(message_id: str) -> dict:
    """Mark a message read (removes the UNREAD label)."""
    return _safe(lambda: (_gmail().users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["UNREAD"]}).execute(),
        {"ok": True, "id": message_id, "read": True})[1])


@mcp.tool
def mark_unread(message_id: str) -> dict:
    """Mark a message unread (adds the UNREAD label)."""
    return _safe(lambda: (_gmail().users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": ["UNREAD"]}).execute(),
        {"ok": True, "id": message_id, "read": False})[1])


@mcp.tool
def archive(message_id: str) -> dict:
    """Archive a message (removes it from the INBOX)."""
    return _safe(lambda: (_gmail().users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["INBOX"]}).execute(),
        {"ok": True, "id": message_id, "archived": True})[1])


@mcp.tool
def star(message_id: str) -> dict:
    """Star a message."""
    return _safe(lambda: (_gmail().users().messages().modify(
        userId="me", id=message_id, body={"addLabelIds": ["STARRED"]}).execute(),
        {"ok": True, "id": message_id, "starred": True})[1])


@mcp.tool
def unstar(message_id: str) -> dict:
    """Unstar a message."""
    return _safe(lambda: (_gmail().users().messages().modify(
        userId="me", id=message_id, body={"removeLabelIds": ["STARRED"]}).execute(),
        {"ok": True, "id": message_id, "starred": False})[1])


@mcp.tool
def trash(message_id: str) -> dict:
    """Move a message to Trash (recoverable for 30 days)."""
    return _safe(lambda: (_gmail().users().messages().trash(userId="me", id=message_id).execute(),
                          {"ok": True, "id": message_id, "trashed": True})[1])


@mcp.tool
def untrash(message_id: str) -> dict:
    """Restore a message from Trash."""
    return _safe(lambda: (_gmail().users().messages().untrash(userId="me", id=message_id).execute(),
                          {"ok": True, "id": message_id, "trashed": False})[1])


@mcp.tool
def summarize_inbox(max_results: int = 50) -> dict:
    """At-a-glance triage: counts of unread / needs-reply, top senders, and the oldest unread message."""
    def go():
        svc = _gmail()
        n = _clamp(max_results, 1, 200)
        res = svc.users().messages().list(userId="me", q="is:unread in:inbox", maxResults=n).execute()
        rows = []
        for m in res.get("messages", []):
            full = svc.users().messages().get(
                userId="me", id=m["id"], format="metadata",
                metadataHeaders=["From", "Subject", "Date"]).execute()
            hdr = {h["name"]: h["value"] for h in full["payload"]["headers"]}
            addr = _parse_addr(hdr.get("From"))
            dt = None
            try:
                dt = parsedate_to_datetime(hdr["Date"]) if hdr.get("Date") else None
            except Exception:
                dt = None
            rows.append({"id": m["id"], "from": addr["email"] or addr["name"],
                         "subject": hdr.get("Subject"), "labels": full.get("labelIds", []),
                         "from_me": "SENT" in full.get("labelIds", []), "dt": dt})
        by_sender: dict[str, int] = {}
        for r in rows:
            by_sender[r["from"]] = by_sender.get(r["from"], 0) + 1
        top = sorted(by_sender.items(), key=lambda kv: kv[1], reverse=True)[:10]
        dated = [r for r in rows if r["dt"]]
        oldest = min(dated, key=lambda r: r["dt"]) if dated else None
        return {
            "unread_inbox": len(rows),
            "needs_reply": sum(1 for r in rows if not r["from_me"]),
            "top_senders": [{"from": k, "count": v} for k, v in top],
            "oldest_unread": ({"id": oldest["id"], "from": oldest["from"],
                               "subject": oldest["subject"],
                               "date": oldest["dt"].isoformat()} if oldest else None),
            "scanned": len(rows),
        }
    return _safe(go)


@mcp.tool
def create_filter(from_query: str = "", to_query: str = "", subject: str = "",
                  has_words: str = "", add_label_names: list[str] | None = None,
                  archive: bool = False, mark_read: bool = False) -> dict:
    """Create a Gmail filter. Provide criteria (from/to/subject/has_words) and actions: apply labels
    (by name, auto-created), archive (skip inbox), and/or mark read. At least one criterion required."""
    def go():
        criteria = {}
        if from_query:
            criteria["from"] = from_query
        if to_query:
            criteria["to"] = to_query
        if subject:
            criteria["subject"] = subject
        if has_words:
            criteria["query"] = has_words
        if not criteria:
            return {"error": "provide at least one criterion (from/to/subject/has_words)"}
        svc = _gmail()
        action = {"addLabelIds": [], "removeLabelIds": []}
        if add_label_names:
            action["addLabelIds"] = _resolve_label_ids(svc, add_label_names, create_missing=True)
        if archive:
            action["removeLabelIds"].append("INBOX")
        if mark_read:
            action["removeLabelIds"].append("UNREAD")
        created = svc.users().settings().filters().create(
            userId="me", body={"criteria": criteria, "action": action}).execute()
        return {"ok": True, "filter_id": created.get("id"), "criteria": criteria}
    return _safe(go)


@mcp.tool
def list_filters() -> dict:
    """List existing Gmail filters (id, criteria, action)."""
    def go():
        svc = _gmail()
        res = svc.users().settings().filters().list(userId="me").execute()
        return {"filters": res.get("filter", [])}
    return _safe(go)


@mcp.tool
def delete_filter(filter_id: str) -> dict:
    """Delete a Gmail filter by id."""
    return _safe(lambda: (_gmail().users().settings().filters().delete(
        userId="me", id=filter_id).execute(), {"ok": True, "id": filter_id})[1])


if __name__ == "__main__":
    mcp.run()
