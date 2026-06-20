"""Comprehensive OFFLINE tests for the outreach-discovery cluster:
funding-radar, email-finder, apollo, news-radar.

Network is never required: tools that need network are exercised only on their
offline logic (validation, no-key paths, bad/empty inputs) and their registration.

Run with the suite venv (no network, no credentials):
    VIRTUAL_ENV= .venv/bin/python tests/test_outreach-discovery.py
"""
from __future__ import annotations

import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Force a clean, isolated, offline environment BEFORE importing any server.
os.environ["MCP_NO_DOTENV"] = "1"
os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="outreach-disc-test-")
# Make sure no real API keys leak in from the host environment.
for _k in ("APOLLO_API_KEY", "HUNTER_API_KEY", "REOON_API_KEY", "TOMBA_API_KEY",
           "TOMBA_SECRET", "GITHUB_PERSONAL_ACCESS_TOKEN", "SEC_USER_AGENT"):
    os.environ.pop(_k, None)

# Also clean the default per-server dirs in case MCP_DATA_DIR is ignored anywhere.
for _name in ("funding-radar", "email-finder", "apollo", "news-radar"):
    shutil.rmtree(Path.home() / ".mcp-suite" / _name, ignore_errors=True)
    shutil.rmtree(Path(os.environ["MCP_DATA_DIR"]) / _name, ignore_errors=True)

from fastmcp import Client  # noqa: E402


async def one(name, fn):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    async with Client(server.mcp) as c:
        await fn(c, server)
    del sys.modules["server"]
    sys.path.pop(0)


async def tool_names(c):
    return {t.name for t in await c.list_tools()}


async def main():
    # ---------------------------------------------------------------- funding-radar
    async def fr(c, mod):
        names = await tool_names(c)
        assert {"scan", "scan_rss", "scan_hackernews", "list_leads", "filter_leads",
                "guess_domain", "as_targets", "enrich_targets", "dedupe", "sources",
                "stats"} <= names, names

        # headline parsing happy path
        p = mod.parse_headline("Exclusive: Acme AI raises $12.5M Series B to expand fintech platform")
        assert p["company"] == "Acme AI", p
        assert p["amount_usd"] == 12_500_000.0, p
        assert p["round"] == "Series B", p
        assert p["sector"] in ("ai", "fintech"), p

        # parse edge cases
        empty = mod.parse_headline("")
        assert empty["company"] == "" and empty["amount_usd"] is None, empty
        assert mod._amount_to_usd("$1.2 billion") == 1_200_000_000.0
        assert mod._amount_to_usd("garbage") is None

        # guess_domain happy + empty
        gd = await c.call_tool("guess_domain", {"company": "Acme AI", "check_dns": False})
        assert gd.data["best"].startswith("acmeai."), gd.data
        gd2 = await c.call_tool("guess_domain", {"company": "!!!", "check_dns": False})
        assert gd2.data["best"] == "" and gd2.data["candidates"] == [], gd2.data

        # store leads directly then filter / list / stats
        mod._store_lead("ZetaCorp", "$5M", "Seed", "saas", "test", "http://x", "ZetaCorp raises $5M",
                        5_000_000.0, "zetacorp.com", "")
        mod._store_lead("TinyCo", "$100K", "Pre-Seed", "ai", "test", "http://y", "TinyCo raises $100K",
                        100_000.0, "", "")
        rows = await c.call_tool("filter_leads", {"min_amount_usd": 1_000_000, "limit": 5})
        assert any(r["company"] == "ZetaCorp" for r in rows.data), rows.data
        assert all(r["amount_usd"] >= 1_000_000 for r in rows.data), rows.data
        bysec = await c.call_tool("filter_leads", {"sector": "ai", "limit": 5})
        assert any(r["company"] == "TinyCo" for r in bysec.data), bysec.data

        ll = await c.call_tool("list_leads", {"limit": 10})
        assert len(ll.data) >= 2, ll.data
        ll_src = await c.call_tool("list_leads", {"source": "test", "limit": 10})
        assert all(r["source"] == "test" for r in ll_src.data), ll_src.data

        # limit clamping: negative / absurd limits don't explode and stay bounded
        clamped = await c.call_tool("list_leads", {"limit": -5})
        assert isinstance(clamped.data, list)
        big = await c.call_tool("filter_leads", {"limit": 10_000})
        assert len(big.data) <= mod.MAX_LIMIT

        # as_targets / enrich_targets
        tg = await c.call_tool("as_targets", {"limit": 5})
        assert all("company" in t and "domain" in t for t in tg.data), tg.data
        en = await c.call_tool("enrich_targets", {"limit": 5, "check_dns": False})
        # TinyCo had no stored domain -> should be guessed
        tiny = [t for t in en.data if t["company"] == "TinyCo"]
        assert tiny and tiny[0]["domain"].startswith("tinyco."), en.data

        src = await c.call_tool("sources", {})
        assert "hackernews" in src.data["sources"]
        assert src.data["sec_user_agent_set"] is False, src.data

        st = await c.call_tool("stats", {})
        assert st.data["total"] >= 2, st.data

        # SECURITY: file:// and other non-http feeds must be rejected (SSRF/local-file read)
        assert mod._safe_http_url("https://example.com/feed") is True
        assert mod._safe_http_url("file:///etc/passwd") is False
        assert mod._safe_http_url("ftp://x/y") is False
        assert mod._safe_http_url("") is False
        r = await c.call_tool("scan_rss", {"feeds": ["file:///etc/passwd",
                                                     "http://127.0.0.1:9/nope.xml"], "limit": 2})
        assert r.data["fetched"] == 0, r.data  # file:// dropped, localhost feed yields nothing offline

        # wrong-type feeds arg is rejected by the schema (defense in depth in code too)
        try:
            await c.call_tool("scan_rss", {"feeds": "not-a-list", "limit": 2})
            raise AssertionError("expected schema rejection for non-list feeds")
        except Exception as e:
            assert "list" in str(e).lower(), e

        # dedupe collapses same normalized name across sources
        assert mod._norm_company("NeuralWave") == mod._norm_company("Neural Wave") == "neuralwave"
        mod._store_lead("NeuralWave", "$6M", "Seed", "ai", "test", "http://nw1", "x",
                        6_000_000.0, "", "")
        mod._store_lead("Neural Wave", "$7M", "Seed", "ai", "test2", "http://nw2", "x",
                        7_000_000.0, "", "")
        dd = await c.call_tool("dedupe", {})
        assert dd.data["removed"] >= 1, dd.data
        print("funding-radar OK — parse/guess/filter/limits/SSRF-guard/dedupe")

    await one("funding-radar", fr)

    # ---------------------------------------------------------------- email-finder
    async def ef(c, mod):
        names = await tool_names(c)
        assert {"guess", "mx", "verify", "confidence_breakdown", "from_github", "scrape_site",
                "hunter_domain_search", "find", "bulk_find", "cache_stats", "clear_cache"} <= names, names

        # guess happy + dedup + role-free locals
        g = await c.call_tool("guess", {"name": "Jane Q Doe", "domain": "acme.com"})
        assert "jane.doe@acme.com" in g.data["candidates"], g.data
        assert len(g.data["candidates"]) == len(set(g.data["candidates"])), g.data
        # guess with no last name / empty
        g2 = await c.call_tool("guess", {"name": "", "domain": "acme.com"})
        assert g2.data["candidates"] == [], g2.data

        # mx of an invalid domain -> error or empty (offline, never raises)
        m = await c.call_tool("mx", {"domain": "no-such-domain-xyz.invalid"})
        assert m.data.get("error") or m.data["mx"] == [], m.data

        # verify: clearly bad syntax -> deliverable False, high confidence
        v = await c.call_tool("verify", {"email": "not-an-email", "check_smtp": False})
        assert v.data["deliverable"] is False, v.data
        # verify: empty / wrong shape guarded
        v2 = await c.call_tool("verify", {"email": "", "check_smtp": False})
        assert v2.data["deliverable"] is False, v2.data
        # verify: disposable domain -> rejected, no SMTP needed
        vd = await c.call_tool("verify", {"email": "x@mailinator.com", "check_smtp": False})
        assert vd.data["deliverable"] is False and vd.data["checks"].get("disposable") is True, vd.data

        cb = await c.call_tool("confidence_breakdown", {"email": "info@acme.com"})
        assert cb.data["signals"]["role_account"] is True, cb.data
        cb2 = await c.call_tool("confidence_breakdown", {"email": "garbage"})
        assert "bad" in cb2.data["signals"]["syntax"], cb2.data

        # from_github: invalid usernames rejected before any network call
        bad_users = ["../../etc/passwd", "a b", "name/slashes", "-leadinghyphen", ""]
        for u in bad_users:
            r = await c.call_tool("from_github", {"username": u})
            assert r.data.get("error") == "invalid github username", (u, r.data)
        assert mod.GITHUB_USER_RE.match("torvalds")
        assert mod.GITHUB_USER_RE.match("a-b-c")

        # scrape_site SSRF guards (offline; must not attempt internal hosts)
        for host in ("localhost", "http://127.0.0.1", "http://169.254.169.254",
                     "http://10.0.0.5", "http://192.168.1.1", "http://[::1]"):
            r = await c.call_tool("scrape_site", {"domain": host})
            assert "error" in r.data, (host, r.data)
        assert mod._is_internal_host("127.0.0.1") is True
        assert mod._is_internal_host("169.254.169.254") is True
        assert mod._is_internal_host("10.1.2.3") is True
        assert mod._is_internal_host("example.com") is False
        # empty domain rejected
        r = await c.call_tool("scrape_site", {"domain": ""})
        assert "error" in r.data, r.data

        # find with no domain -> none
        f = await c.call_tool("find", {"name": "Jane Doe", "scrape": False})
        assert f.data["best"] is None and f.data["confidence"] == "none", f.data
        # find pattern-only path (domain, no network verify success offline)
        f2 = await c.call_tool("find", {"name": "Jane Doe", "domain": "acme.com", "scrape": False})
        assert f2.data["best"], f2.data  # at least a best-guess

        # bulk_find: mixed valid/invalid, wrong type guarded
        bf = await c.call_tool("bulk_find", {"people": [{"name": "A", "domain": "acme.com"},
                                                        {"no_name": 1}]})
        assert bf.data["count"] == 2 and any("error" in r for r in bf.data["results"]), bf.data
        try:
            await c.call_tool("bulk_find", {"people": "nope"})
            raise AssertionError("expected schema rejection for non-list people")
        except Exception as e:
            assert "list" in str(e).lower(), e

        # hunter no key -> graceful hint (never returns the key)
        hd = await c.call_tool("hunter_domain_search", {"domain": "acme.com"})
        assert hd.data.get("error") == "no HUNTER_API_KEY", hd.data

        cs = await c.call_tool("cache_stats", {})
        assert "verify_cache_entries" in cs.data, cs.data
        cc = await c.call_tool("clear_cache", {})
        assert "cleared" in cc.data, cc.data
        print("email-finder OK — guess/verify/SSRF-guard/gh-validation/find/bulk/cache")

    await one("email-finder", ef)

    # ---------------------------------------------------------------- apollo (no key)
    async def ap(c, mod):
        names = await tool_names(c)
        assert {"find_people", "find_people_paged", "find_company", "enrich_org",
                "enrich_person", "seniorities", "titles_catalog", "has_key"} <= names, names

        # no-key paths return a hint, never raise, never leak a key
        fp = await c.call_tool("find_people", {"domain": "acme.com"})
        assert fp.data.get("error") == "no APOLLO_API_KEY", fp.data
        assert "hint" in fp.data, fp.data

        # input validation happens before key check
        empty = await c.call_tool("find_people", {"domain": ""})
        assert empty.data.get("error") == "domain is required", empty.data
        empty2 = await c.call_tool("enrich_org", {"domain": "   "})
        assert empty2.data.get("error") == "domain is required", empty2.data
        empty3 = await c.call_tool("find_company", {})
        assert "error" in empty3.data, empty3.data
        empty4 = await c.call_tool("enrich_person", {})
        # enrich_person requires a key first OR name/linkedin; with no key -> hint
        assert "error" in empty4.data, empty4.data

        # offline helpers work with no key
        sen = await c.call_tool("seniorities", {})
        assert "c_suite" in sen.data["seniorities"], sen.data
        tc = await c.call_tool("titles_catalog", {})
        assert "founders" in tc.data["presets"], tc.data
        hk = await c.call_tool("has_key", {})
        assert hk.data["configured"] is False, hk.data

        # _clamp helper bounds
        assert mod._clamp(-1, 5) == 5
        assert mod._clamp(99999, 5) == mod.MAX_PER_PAGE
        assert mod._clamp("bad", 7) == 7
        print("apollo OK — no-key hints + validation + offline helpers")

    await one("apollo", ap)

    # ---------------------------------------------------------------- news-radar
    async def nr(c, mod):
        names = await tool_names(c)
        assert {"scan_company", "list_signals", "top_signals", "signal_types", "stats"} <= names, names

        # classification
        assert mod._classify("Acme hires 50 engineers") == "hiring"
        assert mod._classify("Acme CEO steps down after a decade") == "exec_change"
        assert mod._classify("Acme launches new product") == "product_launch"
        assert mod._classify("Acme partners with BigCo") == "partnership"
        assert mod._classify("Acme raises $10M Series A") == "funding"
        assert mod._classify("Acme says hello") == "news"

        stp = await c.call_tool("signal_types", {})
        assert "funding" in stp.data["signals"], stp.data

        # scan_company input validation (empty name) without network
        empty = await c.call_tool("scan_company", {"name": ""})
        assert empty.data.get("error") == "name is required", empty.data

        # store signals directly, then browse
        mod._store_signal("Acme", "Acme raises $10M", "funding", "test", "http://n/1", "")
        mod._store_signal("Acme", "Acme hires CTO", "hiring", "test", "http://n/2", "")
        mod._store_signal("Beta", "Beta says hello", "news", "test", "http://n/3", "")

        top = await c.call_tool("top_signals", {"limit": 5})
        assert any(s["company"] == "Acme" for s in top.data), top.data
        # excludes generic 'news' by default
        assert all(s["signal"] != "news" for s in top.data), top.data
        top_all = await c.call_tool("top_signals", {"limit": 10, "exclude_news": False})
        assert any(s["signal"] == "news" for s in top_all.data), top_all.data

        ls = await c.call_tool("list_signals", {"company": "Acme"})
        assert len(ls.data) >= 2, ls.data
        ls_sig = await c.call_tool("list_signals", {"signal": "funding"})
        assert all(s["signal"] == "funding" for s in ls_sig.data), ls_sig.data

        # limit clamp
        assert mod._clamp_limit(-3, 20) == 20
        assert mod._clamp_limit(10_000) == mod.MAX_LIMIT

        st = await c.call_tool("stats", {})
        assert st.data["total"] >= 3, st.data
        print("news-radar OK — classify/validation/signals/top/limits")

    await one("news-radar", nr)

    print("\nOUTREACH-DISCOVERY OK ✅")


asyncio.run(main())
