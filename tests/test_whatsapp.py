"""Offline tests for the robust whatsapp connector. No real Chrome, no WPP, no network.

The module-level `wpp_*` bridge functions are monkeypatched with stubs (a fake contact list + fake
history), so we exercise the real logic: WPP-not-ready handling, name/nickname/fuzzy resolution +
"did you mean?", auto-learning on send, persistent nickname memory, the per-contact tone engine, and
the send/react job plumbing — all without a browser.

Run:  VIRTUAL_ENV= .venv/bin/python tests/test_whatsapp.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import tempfile
from pathlib import Path

os.environ["MCP_NO_DOTENV"] = "1"
os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="wa-test-")

from fastmcp import Client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]

FAKE_CONTACTS = [
    {"id": "14155550001@c.us", "number": "14155550001", "name": "Mom", "pushname": "Mom", "isMyContact": True},
    {"id": "14155550002@c.us", "number": "14155550002", "name": "Aman Sharma", "pushname": "Aman", "isMyContact": True},
    {"id": "14155550003@c.us", "number": "14155550003", "name": "Amit Verma", "pushname": "Amit", "isMyContact": True},
]
FAKE_HISTORY = [  # how "you" message Mom: lowercase, hinglish, emoji, short
    {"body": "haan maa abhi aa raha hu", "t": 1}, {"body": "khana kha liya kya 😊", "t": 2},
    {"body": "thik hai", "t": 3}, {"body": "5 min me ghar pohunch raha", "t": 4},
    {"body": "ok ok", "t": 5}, {"body": "love you maa ❤", "t": 6},
]


def _load(_n=[0]):
    """Load a fresh server module instance (unique name) with the WPP bridge stubbed."""
    _n[0] += 1
    spec = importlib.util.spec_from_file_location(f"wa_srv_{_n[0]}", ROOT / "servers" / "whatsapp" / "server.py")
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    m.wpp_ready = lambda: (True, "")
    m.wpp_status = lambda: {"chrome_running": True, "js_from_apple_events": True, "wpp": True,
                            "wpp_ready": True, "extension": True, "ready": True}
    m.wpp_get_all_contacts = lambda: (True, list(FAKE_CONTACTS))
    m.wpp_get_all_chats = lambda: (True, [{"id": "14155550001@c.us", "name": "Mom", "isGroup": False,
                                          "unread": 2, "t": 9}])
    m.wpp_get_my_messages = lambda cid, count=80: (True, list(FAKE_HISTORY))
    m.wpp_query_exists = lambda num: (True, {"exists": True, "id": f"{num}@c.us"})
    m.wpp_send_text = lambda cid, text: (True, {"sent": True})
    m.wpp_raw = lambda expr, timeout=None: (True, {"ok": True})
    m.wpp_chat_messages = lambda ref, count=30: (True, {"found": True, "chat": ref, "msgs": []})
    m.THROTTLE = 0
    return m


def _call(m, tool, args):
    async def go():
        async with Client(m.mcp) as c:
            return (await c.call_tool(tool, args)).data
    return asyncio.run(go())


def _tools(m):
    async def go():
        async with Client(m.mcp) as c:
            return {t.name for t in await c.list_tools()}
    return asyncio.run(go())


def test_registry():
    m = _load()
    expected = {"health", "diagnose", "setup", "resolve_contact", "set_nickname", "list_nicknames",
                "forget", "contact_memory", "memory_digest", "contact_style", "compose", "send",
                "react", "read_chat", "wait_for_reply", "autopilot_brief", "list_chats", "list_unread",
                "mark_read", "send_media", "list_groups", "create_group", "add_to_group",
                "post_text_status", "start_call", "end_call", "job_status", "list_jobs", "cancel_job"}
    assert expected <= _tools(m), sorted(expected - _tools(m))
    assert sum(1 for t in _tools(m) if t == "health") == 1


def test_resolution_fuzzy_and_nickname():
    m = _load()
    assert _call(m, "resolve_contact", {"query": "Mom"})["contact"]["id"] == "14155550001@c.us"
    amb = _call(m, "resolve_contact", {"query": "Am"})
    assert amb["status"] == "ambiguous" and len(amb["candidates"]) >= 2, amb
    ph = _call(m, "resolve_contact", {"query": "+1 415 555 0009"})
    assert ph["status"] == "phone" and ph["contact"]["id"] == "14155550009@c.us"
    # learn a nickname, then it resolves instantly — and persists to a NEW module instance (same db)
    assert _call(m, "set_nickname", {"nickname": "Mamma", "target": "Mom"})["ok"]
    m2 = _load()
    r2 = _call(m2, "resolve_contact", {"query": "Mamma"})
    assert r2["status"] == "resolved" and r2["contact"]["id"] == "14155550001@c.us", r2


def test_send_learns_alias_and_ambiguity():
    m = _load()
    r = _call(m, "send", {"contact": "Mom", "message": "on my way"})
    assert r["ok"] and (r.get("result", {}).get("sent") or r.get("job_id")), r
    assert any(a["alias"] == "mom" for a in m.mem_list_aliases())
    amb = _call(m, "send", {"contact": "Am", "message": "hi"})
    assert amb.get("needs_confirmation") is True and amb.get("candidates"), amb
    assert _call(m, "send", {"contact": "Am", "message": "hi", "exact": True})["ok"]


def test_tone_style_engine():
    m = _load()
    r = _call(m, "contact_style", {"contact": "Mom"})
    assert r["ok"], r
    p = r["profile"]
    assert p["language"] == "hinglish (roman)" and p["mostly_lowercase"] is True and p["emoji_rate"] > 0, p
    assert "write the next whatsapp message" in r["instruction"].lower() and r["samples"]
    cmp = _call(m, "compose", {"contact": "Mom", "intent": "tell her I'll be late"})
    assert cmp["ok"] and cmp["intent"] and cmp["instruction"], cmp


def test_validation_and_not_ready():
    m = _load()
    assert _call(m, "send", {"contact": "", "message": "x"})["ok"] is False
    assert _call(m, "send", {"contact": "Mom", "message": ""})["ok"] is False
    assert _call(m, "job_status", {"job_id": "nope"})["ok"] is False
    m.wpp_ready = lambda: (False, "WPP engine not present — load the extension")
    nr = _call(m, "send", {"contact": "Mom", "message": "hi"})
    assert nr["ok"] is False and "extension" in (nr.get("error") or "").lower(), nr


def test_stopwords_and_chatid():
    m = _load()
    assert m.is_stopword("bye") and m.is_stopword("Ok Bye.") and m.is_stopword("TTYL")
    assert not m.is_stopword("hello") and not m.is_stopword("bye for now buying milk")
    # chat id is parsed out of a WPP message id
    assert m.chat_id_from_msg_id("true_134265475977373@lid_3EB0ABC") == "134265475977373@lid"
    assert m.chat_id_from_msg_id("false_916280852252@c.us_XYZ_out") == "916280852252@c.us"
    assert m.chat_id_from_msg_id("garbage") == ""


def test_wait_for_reply():
    m = _load()
    # stateful mock: baseline has only our own msg; next poll adds an incoming reply from the peer
    state = {"polls": 0}
    base = [{"id": "true_X@lid_OUR", "from_me": True, "type": "chat", "body": "yo", "t": 1}]

    def fake(ref, count=30):
        state["polls"] += 1
        if state["polls"] <= 1:
            return True, {"found": True, "chat": ref, "msgs": list(base)}
        return True, {"found": True, "chat": ref, "msgs": base + [
            {"id": "false_X@lid_THEIRS", "from_me": False, "type": "chat", "body": "bye", "t": 2}]}
    m.wpp_chat_messages = fake
    r = _call(m, "wait_for_reply", {"chat_id": "X@lid", "timeout": 12})
    assert r["ok"] and r.get("message") == "bye" and r.get("from_me") is False, r
    assert r.get("is_stop") is True, r  # "bye" is a stop word

    # timeout path: no new peer message ever
    m2 = _load()
    m2.wpp_chat_messages = lambda ref, count=30: (True, {"found": True, "chat": ref, "msgs": list(base)})
    rt = _call(m2, "wait_for_reply", {"chat_id": "X@lid", "timeout": 5})
    assert rt["ok"] and rt.get("timeout") is True, rt


def test_autopilot_brief():
    m = _load()
    r = _call(m, "autopilot_brief", {"contact": "Mom", "goal": "say hi"})
    assert r["ok"] and r.get("chat_id") == "14155550001@c.us", r
    assert "directive" in r and "wait_for_reply" in r["directive"] and r.get("stopwords"), r


if __name__ == "__main__":
    for fn in (test_registry, test_resolution_fuzzy_and_nickname, test_send_learns_alias_and_ambiguity,
               test_tone_style_engine, test_validation_and_not_ready, test_stopwords_and_chatid,
               test_wait_for_reply, test_autopilot_brief):
        fn()
        print(fn.__name__, "OK")
    print("ALL WHATSAPP TESTS PASSED")
