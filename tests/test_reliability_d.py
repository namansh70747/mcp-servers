"""Offline weak-model reliability tests for wave-D servers:
contacts, task-manager, jobtrack, reachout, interview-prep.

Asserts that not-found paths on id/name-keyed tools now return an ACTIONABLE message
(mcp_base.not_found): {"ok": False, "error": "...", "available": [...], "hint": "use list_*()"}
instead of a bare {"error": "not found"}, so a free model can recover. Also checks a couple of
required-param validations. Success shapes are unchanged (only error paths were touched).

Run: VIRTUAL_ENV= /Users/namansharma/mcp-servers/.venv/bin/python tests/test_reliability_d.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
from fastmcp import Client  # noqa: E402


async def one(name, fn):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    try:
        async with Client(server.mcp) as c:
            await fn(c, server)
    finally:
        del sys.modules["server"]


def _data(res):
    """Unwrap a FastMCP CallToolResult to its .data."""
    return res.data


def _assert_actionable(d, *, expect_id=None):
    """A not_found() envelope: ok False, a non-empty error, an `available` list, and a hint."""
    assert isinstance(d, dict), d
    assert d.get("ok") is False, d
    assert isinstance(d.get("error"), str) and d["error"], d
    assert "available" in d and isinstance(d["available"], list), d
    assert d.get("hint"), d
    if expect_id is not None:
        # the missing id appears in the actionable message
        assert str(expect_id) in d["error"], d


async def main():
    BOGUS = 999_999

    # ---------------- contacts ----------------
    async def contacts(c, server):
        seed = _data(await c.call_tool("add_contact", {"name": "Ada", "company": "Acme",
                                                       "email": "ada@acme.com"}))
        real_id = seed["id"]
        # get() on a missing id -> actionable, lists the real contact id
        g = _data(await c.call_tool("get", {"contact_id": BOGUS}))
        _assert_actionable(g, expect_id=BOGUS)
        assert real_id in g["available"], g
        # mutating tools on a missing id are actionable too (no silent ok)
        _assert_actionable(_data(await c.call_tool("update_status",
                                                   {"contact_id": BOGUS, "status": "queued"})))
        _assert_actionable(_data(await c.call_tool("mark_contacted", {"contact_id": BOGUS})))
        _assert_actionable(_data(await c.call_tool("tag", {"contact_id": BOGUS, "tags": "x"})))
        # merge distinguishes which id is missing
        m = _data(await c.call_tool("merge_contacts", {"keep_id": BOGUS, "dup_id": real_id}))
        assert m.get("ok") is False and "keep_id" in m["error"], m
        # required-param validation: empty tags is a clear err, not a crash/silent pass
        bad = _data(await c.call_tool("tag", {"contact_id": real_id, "tags": "   "}))
        assert bad.get("ok") is False and "tags" in bad["error"], bad
        # success shape preserved: get on the real id still returns the row dict
        good = _data(await c.call_tool("get", {"contact_id": real_id}))
        assert good.get("name") == "Ada" and good.get("id") == real_id, good
        print("contacts reliability OK")
    await one("contacts", contacts)

    # ---------------- task-manager ----------------
    async def tasks(c, server):
        t = _data(await c.call_tool("add_task", {"title": "Write tests"}))
        real_id = t["id"]
        g = _data(await c.call_tool("get_task", {"task_id": BOGUS}))
        _assert_actionable(g, expect_id=BOGUS)
        assert real_id in g["available"], g
        _assert_actionable(_data(await c.call_tool("complete", {"task_id": BOGUS})))
        _assert_actionable(_data(await c.call_tool("delete_task", {"task_id": BOGUS})))
        _assert_actionable(_data(await c.call_tool("add_subtask",
                                                   {"parent_id": BOGUS, "title": "sub"})))
        # success shape preserved
        good = _data(await c.call_tool("get_task", {"task_id": real_id}))
        assert good.get("title") == "Write tests", good
        print("task-manager reliability OK")
    await one("task-manager", tasks)

    # ---------------- jobtrack ----------------
    async def jobtrack(c, server):
        a = _data(await c.call_tool("add_application", {"company": "Acme", "role": "SWE"}))
        real_id = a["id"]
        g = _data(await c.call_tool("get_application", {"application_id": BOGUS}))
        _assert_actionable(g, expect_id=BOGUS)
        assert real_id in g["available"], g
        _assert_actionable(_data(await c.call_tool("update_status",
                                                   {"application_id": BOGUS, "status": "interview"})))
        _assert_actionable(_data(await c.call_tool("add_interview", {"application_id": BOGUS})))
        # required-param validation on add_note
        bad = _data(await c.call_tool("add_note", {"application_id": real_id, "note": "  "}))
        assert bad.get("ok") is False and "note" in bad["error"], bad
        # success shape preserved
        good = _data(await c.call_tool("get_application", {"application_id": real_id}))
        assert good.get("company") == "Acme" and "history" in good, good
        print("jobtrack reliability OK")
    await one("jobtrack", jobtrack)

    # ---------------- reachout ----------------
    async def reachout(c, server):
        # log an outreach without touching Gmail (no network)
        o = _data(await c.call_tool("log_outreach",
                                    {"recipient_name": "Bo", "recipient_email": "bo@x.com",
                                     "company": "Acme"}))
        real_id = o["outreach_id"]
        # silent-update tools now refuse a bogus id with an actionable envelope
        mr = _data(await c.call_tool("mark_replied", {"outreach_id": BOGUS}))
        _assert_actionable(mr, expect_id=BOGUS)
        assert real_id in mr["available"], mr
        _assert_actionable(_data(await c.call_tool("snooze", {"outreach_id": BOGUS, "days": 3})))
        _assert_actionable(_data(await c.call_tool("record_followup", {"outreach_id": BOGUS})))
        # unknown sequence name is actionable and lists defined names
        gs = _data(await c.call_tool("get_sequence", {"name": "does-not-exist"}))
        assert gs.get("ok") is False and "available" in gs and gs.get("hint"), gs
        # success shape preserved: mark_replied on the real id returns the old ok shape
        good = _data(await c.call_tool("mark_replied", {"outreach_id": real_id}))
        assert good.get("ok") is True and good.get("status") == "replied", good
        print("reachout reliability OK")
    await one("reachout", reachout)

    # ---------------- interview-prep ----------------
    async def prep(c, server):
        card = _data(await c.call_tool("add_card", {"front": "Q", "back": "A"}))
        card_id = card["id"]
        prob = _data(await c.call_tool("add_problem", {"title": "Two Sum"}))
        prob_id = prob["id"]
        # card not-found is actionable
        rv = _data(await c.call_tool("review", {"card_id": BOGUS, "grade": 4}))
        _assert_actionable(rv, expect_id=BOGUS)
        assert card_id in rv["available"], rv
        _assert_actionable(_data(await c.call_tool("review_fsrs",
                                                   {"card_id": BOGUS, "rating": "good"})))
        _assert_actionable(_data(await c.call_tool("move_card",
                                                   {"card_id": BOGUS, "deck": "d"})))
        # problem not-found is actionable
        ta = _data(await c.call_tool("track_attempt", {"problem_id": BOGUS, "solved": True}))
        _assert_actionable(ta, expect_id=BOGUS)
        assert prob_id in ta["available"], ta
        # mock not-found is actionable
        _assert_actionable(_data(await c.call_tool("complete_mock", {"mock_id": BOGUS})))
        # success shape preserved: review on real card returns next_due/interval
        good = _data(await c.call_tool("review", {"card_id": card_id, "grade": 4}))
        assert "next_due" in good and "interval_days" in good, good
        print("interview-prep reliability OK")
    await one("interview-prep", prep)

    print("\nWAVE-D RELIABILITY (offline) OK")


asyncio.run(main())
