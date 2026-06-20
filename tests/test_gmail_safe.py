"""Regression: Gmail-backed tools must DEGRADE (clean err()) when credentials/token are
missing — never raise RuntimeError / crash with a stack trace.

Covers the four assigned servers' Gmail tools. No network: we use a fresh, empty
MCP_DATA_DIR so no credentials.json / token.json exists, which forces the missing-
credential path in get_gmail_service(). Every Gmail tool is expected to return a dict
carrying an error (either {"error": ...} or {"ok": False, "error": ...}) rather than
raising.

Run:
    VIRTUAL_ENV= .venv/bin/python tests/test_gmail_safe.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import uuid
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Fresh, empty data dir => no credentials anywhere => unauthenticated.
os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="gmail-safe-test-")
os.environ["MCP_NO_DOTENV"] = "1"
sys.path.insert(0, str(ROOT / "shared"))

from fastmcp import Client  # noqa: E402
from fastmcp.exceptions import ToolError  # noqa: E402


def _load(server_name):
    path = ROOT / "servers" / server_name / "server.py"
    spec = importlib.util.spec_from_file_location(f"{server_name}_{uuid.uuid4().hex}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _is_err(data) -> bool:
    """An acceptable degraded result: a dict with an error message, not a crash."""
    return isinstance(data, dict) and ("error" in data or data.get("ok") is False)


async def _assert_err(c, name, args):
    try:
        r = await c.call_tool(name, args)
    except ToolError as e:  # a raised RuntimeError surfaces here -> regression
        raise AssertionError(f"{name} RAISED instead of returning err(): {e}") from e
    data = r.data
    assert _is_err(data), f"{name} should return err() when unauthenticated, got: {data!r}"
    return data


async def main():
    # ---------- mailbox: every tool that calls _gmail() ----------
    mb = _load("mailbox")
    mailbox_calls = [
        ("list_unread", {}),
        ("needs_reply", {}),
        ("summarize_thread", {"thread_id": "t1"}),
        ("draft_reply", {"thread_id": "t1", "to_email": "a@b.com",
                         "subject": "s", "body": "b"}),
        ("search", {"query": "is:unread"}),
        ("list_threads", {}),
        ("thread_actions", {"thread_id": "t1"}),
        ("get_message", {"message_id": "m1"}),
        ("download_attachment", {"message_id": "m1", "attachment_id": "a1"}),
        ("categorize", {"message_id": "m1", "add_labels": ["X"]}),
        ("batch_modify", {"message_ids": ["m1"]}),
        ("list_labels", {}),
        ("create_label", {"name": "X"}),
        ("apply_label", {"message_id": "m1", "label_names": ["X"]}),
        ("remove_label", {"message_id": "m1", "label_names": ["X"]}),
        ("mark_read", {"message_id": "m1"}),
        ("mark_unread", {"message_id": "m1"}),
        ("archive", {"message_id": "m1"}),
        ("star", {"message_id": "m1"}),
        ("unstar", {"message_id": "m1"}),
        ("trash", {"message_id": "m1"}),
        ("untrash", {"message_id": "m1"}),
        ("summarize_inbox", {}),
        ("create_filter", {"from_query": "x@y.com"}),
        ("list_filters", {}),
        ("delete_filter", {"filter_id": "f1"}),
    ]
    async with Client(mb.mcp) as c:
        for name, args in mailbox_calls:
            await _assert_err(c, name, args)
    print(f"mailbox OK — {len(mailbox_calls)} Gmail tools degrade to err() when unauthenticated")

    # ---------- reachout: every tool that calls _gmail() ----------
    ro = _load("reachout")
    reachout_calls = [
        ("create_draft", {"to_email": "a@b.com", "subject": "s", "body": "b"}),
        ("send_email", {"to_email": "a@b.com", "subject": "s", "body": "b", "force": True}),
        ("send_draft", {"draft_id": "d1"}),
        ("delete_draft", {"draft_id": "d1"}),
    ]
    async with Client(ro.mcp) as c:
        for name, args in reachout_calls:
            await _assert_err(c, name, args)
        # thread_followup needs a real tracked row before it reaches _gmail();
        # seed one directly, then assert it degrades rather than crashing.
        oid = ro.store.execute(
            "INSERT INTO outreach(recipient_name,recipient_email,company,role,template_used,"
            "subject,status,created_at) VALUES(?,?,?,?,?,?,?,?)",
            ("Pat", "pat@hooli.com", "Hooli", "", "internship_cto", "Hi", "sent",
             "2020-01-01T00:00:00+00:00"))
        await _assert_err(c, "thread_followup",
                          {"outreach_id": oid, "template_name": "followup_generic"})
    print(f"reachout OK — {len(reachout_calls) + 1} Gmail tools degrade to err() when unauthenticated")

    # ---------- mailmerge + emailcheck: no Gmail dependency (sanity import) ----------
    for nm in ("mailmerge", "emailcheck"):
        mod = _load(nm)
        async with Client(mod.mcp) as c:
            await c.list_tools()  # imports + starts cleanly without any credentials
    print("mailmerge + emailcheck OK — start cleanly with no credentials (no Gmail path)")

    print("\nALL GMAIL-SAFE TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
