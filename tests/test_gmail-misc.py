"""Offline smoke tests for mailbox, emailcheck, and task-manager.

Never hits Gmail/DNS/network or uses credentials: only exercises the offline
logic (parsing/scoring/heuristics) and that every tool registers. Gmail-backed
mailbox tools are not invoked here (they need OAuth); we only assert they exist
and that the offline helpers + non-network branches behave.
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402


async def _load(name):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    mod = sys.modules.pop("server")
    return mod


async def main():
    # ---------- mailbox ----------
    mailbox = await _load("mailbox")
    async with Client(mailbox.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        for expected in ("categorize", "list_unread", "search", "draft_reply",
                         "summarize_thread", "auth_status", "apply_label"):
            assert expected in tools, f"mailbox missing tool {expected}"
        # categorize id-existence guard: empty id returns an error without touching Gmail.
        r = await c.call_tool("categorize", {"message_id": "", "add_labels": ["INBOX"]})
        assert r.data.get("error"), "empty message_id should error"
        # offline parsing helpers (no network)
        assert mailbox._parse_addr("Ada <ada@x.io>") == {"name": "Ada", "email": "ada@x.io"}
        assert "hello" in mailbox._strip_html("<p>hello</p>").lower()
        acts = mailbox._extract_actions("Can you send the report? Random line. Please review by EOD.")
        assert any("report" in a.lower() for a in acts)
        assert mailbox._safe_filename("../../etc/passwd", "fb.bin") == "passwd"
        assert mailbox._safe_filename("", "fb.bin") == "fb.bin"
        # _gmail must route through the shared helper, not inline OAuth.
        import inspect
        src = inspect.getsource(mailbox._gmail)
        assert "get_gmail_service" in src, "mailbox._gmail should use shared get_gmail_service"
    print("mailbox OK — tools register, categorize guard, offline helpers, shared OAuth")

    # ---------- emailcheck ----------
    emailcheck = await _load("emailcheck")
    async with Client(emailcheck.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        for expected in ("validate_email", "extract_emails", "spam_score",
                         "parse_unsubscribe", "check_mx", "domain_report"):
            assert expected in tools, f"emailcheck missing tool {expected}"
        v = await c.call_tool("validate_email", {"email": "ada@example.com"})
        assert v.data["valid"] is True and v.data["domain"] == "example.com"
        bad = await c.call_tool("validate_email", {"email": "not-an-email"})
        assert bad.data["valid"] is False
        ex = await c.call_tool("extract_emails", {"text": "ping a@x.io and a@x.io, b@y.io"})
        assert ex.data["count"] == 2
        sp = await c.call_tool("spam_score", {"subject": "FREE WINNER ACT NOW!!!",
                                              "body": "Click here to claim your CASH 100% guaranteed!!!"})
        assert sp.data["score"] > 0 and sp.data["risk"] in ("low", "medium", "high")
        un = await c.call_tool("parse_unsubscribe",
                               {"content": 'List-Unsubscribe: <mailto:stop@x.io>\n<a href="http://x.io/unsubscribe">x</a>'})
        assert un.data["has_unsubscribe"] is True
    print("emailcheck OK — tools register, validate/extract/spam/unsubscribe offline")

    # ---------- task-manager ----------
    tm = await _load("task-manager")
    async with Client(tm.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        for expected in ("add_task", "complete", "delete_task", "get_task", "list_due"):
            assert expected in tools, f"task-manager missing tool {expected}"
        added = await c.call_tool("add_task", {"title": "Write tests", "priority": "high",
                                               "due": "2020-01-01"})
        tid = added.data["id"]
        # get_task returns the full row
        got = await c.call_tool("get_task", {"task_id": tid})
        assert got.data["id"] == tid and got.data["title"] == "Write tests"
        # get_task on a missing id errors
        miss = await c.call_tool("get_task", {"task_id": 999999})
        assert miss.data.get("error"), "get_task missing id should error"
        # complete id-existence check
        cmiss = await c.call_tool("complete", {"task_id": 999999})
        assert cmiss.data.get("error"), "complete missing id should error"
        ok = await c.call_tool("complete", {"task_id": tid})
        assert ok.data.get("ok") is True
        # delete_task id-existence check + delete
        added2 = await c.call_tool("add_task", {"title": "Throwaway"})
        tid2 = added2.data["id"]
        dmiss = await c.call_tool("delete_task", {"task_id": 999999})
        assert dmiss.data.get("error"), "delete_task missing id should error"
        dok = await c.call_tool("delete_task", {"task_id": tid2})
        assert dok.data.get("ok") is True
        gone = await c.call_tool("get_task", {"task_id": tid2})
        assert gone.data.get("error"), "deleted task should be gone"
    print("task-manager OK — get_task, delete_task guard, complete guard")

    print("\nGMAIL-MISC (offline) OK")


asyncio.run(main())
