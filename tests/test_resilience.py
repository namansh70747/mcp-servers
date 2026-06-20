"""Offline resilience tests for P3 changes on bookmark-vault, news-radar, apollo, rss-reader,
learn-tracker, api-tester.

Covers (additive, shapes unchanged):
  - LIMIT clamps on list/search tools (results never exceed the server's max clamp)
  - id-existence checks before mutating tools that take an id
  - swap raw httpx -> mcp_base.http for network calls, preserving each tool's return shape and
    error behavior (verified with monkeypatched http; NO real network)

Run: VIRTUAL_ENV= /Users/namansharma/mcp-servers/.venv/bin/python tests/test_resilience.py
"""
import asyncio
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
from fastmcp import Client  # noqa: E402
from mcp_base import http  # noqa: E402


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


async def main():
    # ---------------- bookmark-vault: clamps + id checks ----------------
    async def bv(c, server):
        # seed a few bookmarks (no network: fetch_title=False)
        for i in range(5):
            await c.call_tool("add_bookmark",
                              {"url": f"https://example.com/p{i}", "title": f"T{i}",
                               "tags": "x", "fetch_title": False})
        # huge limit is clamped to _MAX_LIMIT, never explodes
        big = _data(await c.call_tool("list_bookmarks", {"limit": 10_000_000}))
        assert len(big) <= server._MAX_LIMIT, big
        # search also clamps and still returns a list
        s = _data(await c.call_tool("search", {"query": "T1", "limit": 10_000_000}))
        assert isinstance(s, list) and len(s) <= server._MAX_LIMIT
        # id-existence checks: mutating tools on missing id return {"error": ...}
        assert "error" in _data(await c.call_tool("tag", {"bookmark_id": 999999, "tags": "y"}))
        assert "error" in _data(await c.call_tool("update_bookmark",
                                                  {"bookmark_id": 999999, "title": "z"}))
        assert "error" in _data(await c.call_tool("delete_bookmark", {"bookmark_id": 999999}))
        # clamp helper direct: negative/garbage -> default; over-max -> max
        assert server._clamp_limit(0, 50) == 50
        assert server._clamp_limit(-5, 50) == 50
        assert server._clamp_limit(10**9) == server._MAX_LIMIT
        print("bookmark-vault resilience OK")
    await one("bookmark-vault", bv)

    # ---------------- news-radar: http swap shape + clamp ----------------
    async def nr(c, server):
        calls = {}

        def fake_get_json(url, **kw):
            calls["url"] = url
            return {"hits": [
                {"title": "Acme raises $10M Series A", "url": "https://x/1",
                 "created_at": "2026-01-01", "objectID": "1"},
                {"title": "Unrelated story", "url": "https://x/2",
                 "created_at": "2026-01-01", "objectID": "2"},
            ]}

        orig = http.get_json
        http.get_json = fake_get_json
        try:
            r = _data(await c.call_tool("scan_company",
                                        {"name": "Acme", "source": "hackernews", "limit": 5}))
        finally:
            http.get_json = orig
        # shape preserved: company/fetched/stored_or_seen/by_signal/signals keys
        assert set(["company", "fetched", "stored_or_seen", "by_signal", "signals"]) <= set(r), r
        assert r["company"] == "Acme"
        assert any(s["signal"] == "funding" for s in r["signals"]), r["signals"]
        assert calls["url"] == server.HN_SEARCH
        # clamp helper
        assert server._clamp_limit(10**9) == server.MAX_LIMIT
        # list_signals clamps too
        lst = _data(await c.call_tool("list_signals", {"limit": 10**9}))
        assert isinstance(lst, list) and len(lst) <= server.MAX_LIMIT
        print("news-radar resilience OK")
    await one("news-radar", nr)

    # ---------------- apollo: http swap preserves error/success shape ----------------
    async def ap(c, server):
        # No API key path is unchanged (offline-safe): returns hint dict
        # Force a key so we exercise the _api() path, then monkeypatch http.request.
        import mcp_base
        real_get_env = mcp_base.config.get_env if hasattr(mcp_base, "config") else None

        # Patch get_env used inside the server module to return a fake key.
        server.get_env = lambda k, *a, **kw: "FAKE_KEY" if k == "APOLLO_API_KEY" else ""

        def fake_request(method, url, **kw):
            return {"ok": True, "status": 200,
                    "json": {"people": [{"name": "Jane", "title": "CTO",
                                         "organization": {"name": "Acme",
                                                          "primary_domain": "acme.com"}}],
                             "pagination": {"page": 1, "per_page": 5,
                                            "total_entries": 1, "total_pages": 1}}}

        orig = http.request
        http.request = fake_request
        try:
            r = _data(await c.call_tool("find_people", {"domain": "acme.com", "limit": 5}))
        finally:
            http.request = orig
        # success shape preserved
        assert r["domain"] == "acme.com" and r["count"] == 1
        assert r["people"][0]["name"] == "Jane"
        assert "pagination" in r and "note" in r

        # error path: http returns not-ok -> tool returns {"error": ...} (old shape)
        def fail_request(method, url, **kw):
            return {"ok": False, "error": "boom", "status": None}

        http.request = fail_request
        try:
            e = _data(await c.call_tool("enrich_org", {"domain": "acme.com"}))
        finally:
            http.request = orig
        assert "error" in e and e["error"] == "boom", e
        # offline helpers still work without key
        assert "seniorities" in _data(await c.call_tool("seniorities", {}))
        print("apollo resilience OK")
    await one("apollo", ap)

    # ---------------- rss-reader: clamp + id checks ----------------
    async def rss(c, server):
        await c.call_tool("add_feed", {"url": "https://example.com/feed.xml", "title": "Ex"})
        # mark_read / remove_feed on missing id -> error
        assert "error" in _data(await c.call_tool("mark_read", {"item_id": 999999}))
        assert "error" in _data(await c.call_tool("remove_feed", {"feed_id": 999999}))
        # unread + search clamp (no items yet, but must not error and respect bound)
        u = _data(await c.call_tool("unread", {"limit": 10**9}))
        assert isinstance(u, list) and len(u) <= server._MAX_LIMIT
        sr = _data(await c.call_tool("search", {"query": "anything", "limit": 10**9}))
        assert isinstance(sr, list) and len(sr) <= server._MAX_LIMIT
        assert server._clamp_limit(10**9) == server._MAX_LIMIT
        print("rss-reader resilience OK")
    await one("rss-reader", rss)

    # ---------------- learn-tracker: clamp + id checks ----------------
    async def lt(c, server):
        await c.call_tool("add_course", {"title": "DS", "hours": 10, "status": "in_progress"})
        # mutating tools on bad id -> error
        assert "error" in _data(await c.call_tool("log_progress",
                                                  {"course_id": 999999, "progress_pct": 50}))
        assert "error" in _data(await c.call_tool("mark_for_review",
                                                  {"course_id": 999999, "days": 3}))
        assert "error" in _data(await c.call_tool("add_resource",
                                                  {"course_id": 999999, "url": "https://x",
                                                   "fetch_title": False}))
        # list_courses now bounded; whats_next/reviews_due clamp
        lc = _data(await c.call_tool("list_courses", {}))
        assert isinstance(lc, list) and len(lc) <= server._MAX_LIMIT
        wn = _data(await c.call_tool("whats_next", {"limit": 10**9}))
        assert isinstance(wn, list) and len(wn) <= server._MAX_LIMIT
        rd = _data(await c.call_tool("reviews_due", {"limit": 10**9}))
        assert isinstance(rd, list) and len(rd) <= server._MAX_LIMIT
        assert server._clamp_limit(10**9) == server._MAX_LIMIT
        print("learn-tracker resilience OK")
    await one("learn-tracker", lt)

    # ---------------- api-tester: clamp + id check + http swap (no network) ----------------
    async def at(c, server):
        r = _data(await c.call_tool("add_request",
                                    {"name": "gh", "url": "https://api.github.com", "method": "GET"}))
        rid = r["id"]
        # delete on missing id -> error (new id-existence check); valid delete keeps old shape
        assert "error" in _data(await c.call_tool("delete_request", {"request_id": 999999}))
        # history clamps
        h = _data(await c.call_tool("history", {"limit": 10**9}))
        assert isinstance(h, list) and len(h) <= server._MAX_LIMIT
        assert server._clamp_limit(10**9) == server._MAX_LIMIT
        # import_openapi via URL now uses http.request; monkeypatch to return a tiny spec (no network)
        spec = ('{"openapi":"3.0.0","info":{"title":"demo"},'
                '"servers":[{"url":"https://api.demo.test"}],'
                '"paths":{"/ping":{"get":{"operationId":"ping"}}}}')

        def fake_request(method, url, **kw):
            return {"ok": True, "status": 200, "text": spec, "headers": {}}

        orig = http.request
        http.request = fake_request
        try:
            imp = _data(await c.call_tool("import_openapi", {"spec": "https://api.demo.test/openapi.json"}))
        finally:
            http.request = orig
        assert imp.get("ok") and imp.get("imported") == 1, imp
        # error path preserved on fetch failure
        def fail_request(method, url, **kw):
            return {"ok": False, "error": "dns boom", "status": None}
        http.request = fail_request
        try:
            ef = _data(await c.call_tool("import_openapi", {"spec": "https://bad.test/openapi.json"}))
        finally:
            http.request = orig
        assert "error" in ef and "fetch failed" in ef["error"], ef
        # delete a real request still returns old success shape
        d = _data(await c.call_tool("delete_request", {"request_id": rid}))
        assert d.get("ok") is True and d.get("deleted") == rid
        print("api-tester resilience OK")
    await one("api-tester", at)

    print("\nP3 RESILIENCE (offline) OK")


asyncio.run(main())
