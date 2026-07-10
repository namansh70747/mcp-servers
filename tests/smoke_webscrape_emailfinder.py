"""Live smoke test: webscrape + email-finder against real public URLs.
Uses repo .env (HUNTER/REOON keys if set). Run:
  uv run python tests/smoke_webscrape_emailfinder.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

from fastmcp import Client  # noqa: E402


def load_server(name: str):
    path = ROOT / "servers" / name / "server.py"
    spec = importlib.util.spec_from_file_location(f"smoke_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod.mcp


async def call(c, tool: str, args: dict) -> dict:
    return (await c.call_tool(tool, args)).data


async def test_webscrape() -> list[str]:
    issues: list[str] = []
    async with Client(load_server("webscrape")) as c:
        tools = {t.name for t in await c.list_tools()}
        for t in ("fetch", "extract", "links", "contacts", "crawl", "profile"):
            if t not in tools:
                issues.append(f"webscrape missing tool: {t}")

        # SSRF guard
        bad = await call(c, "fetch", {"url": "http://127.0.0.1"})
        if not bad.get("ok") is False and "error" not in bad and "refusing" not in str(bad).lower():
            issues.append(f"webscrape SSRF guard weak: {bad}")

        # Live fetch
        r = await call(c, "fetch", {"url": "https://example.com"})
        if not r.get("ok"):
            issues.append(f"webscrape fetch example.com failed: {r}")
        elif not (r.get("content") or "").strip():
            issues.append(f"webscrape fetch empty content: chars={r.get('chars')}")

        contacts = await call(c, "contacts", {"url": "https://github.com/namansh70747"})
        if contacts.get("ok") is False and "error" in contacts:
            issues.append(f"webscrape contacts failed: {contacts.get('error')}")
        elif contacts.get("ok"):
            emails = contacts.get("emails") or []
            if not emails and not contacts.get("socials"):
                issues.append("webscrape contacts returned no emails/socials for github profile")

    return issues


async def test_email_finder() -> list[str]:
    issues: list[str] = []
    async with Client(load_server("email-finder")) as c:
        tools = {t.name for t in await c.list_tools()}
        if "find" not in tools:
            issues.append("email-finder missing find tool")

        # Pattern guess offline path
        g = await call(c, "guess", {"name": "Jane Doe", "domain": "example.com"})
        if not g.get("candidates"):
            issues.append(f"email-finder guess empty: {g}")

        # Verify syntax
        v = await call(c, "verify", {"email": "not-an-email", "check_smtp": False})
        if v.get("deliverable") is not False:
            issues.append(f"email-finder verify should reject bad email: {v}")

        # Live scrape public about page
        s = await call(c, "scrape_site", {"domain": "stripe.com", "max_pages": 3})
        if s.get("error"):
            issues.append(f"email-finder scrape_site stripe.com: {s['error']}")
        elif not s.get("pages_scanned"):
            issues.append(f"email-finder scrape_site fetched 0 pages: {s}")

        # find with real domain (may use Hunter key from .env)
        f = await call(c, "find", {
            "name": "Naman Sharma",
            "company": "GitHub",
            "domain": "github.com",
            "scrape": True,
        })
        if f.get("error"):
            issues.append(f"email-finder find error: {f['error']}")
        # best may be None for github.com - that's OK; check structure
        if "best" not in f and "candidates" not in f:
            issues.append(f"email-finder find bad shape: {f}")

        # Reoon/Hunter key presence
        from mcp_base import get_env
        if get_env("HUNTER_API_KEY"):
            hd = await call(c, "hunter_domain_search", {"domain": "stripe.com"})
            if hd.get("error") and "HUNTER" in str(hd.get("error", "")):
                issues.append(f"hunter_domain_search failed despite key: {hd}")
        if get_env("REOON_API_KEY"):
            # verify a known format email via API path inside verify
            vr = await call(c, "verify", {"email": "test@example.com", "check_smtp": False})
            if "checks" not in vr:
                issues.append(f"email-finder verify bad response: {vr}")

    return issues


async def test_apollo() -> list[str]:
    issues: list[str] = []
    async with Client(load_server("apollo")) as c:
        tools = {t.name for t in await c.list_tools()}
        for t in ("find_people", "find_people_web", "has_key", "has_browser", "check_session"):
            if t not in tools:
                issues.append(f"apollo missing tool: {t}")

        hk = await call(c, "has_key", {})
        hb = await call(c, "has_browser", {})
        cs = await call(c, "check_session", {})
        print(f"  mode={hb.get('mode')}, playwright={hb.get('playwright_installed')}, "
              f"logged_in={cs.get('logged_in')}")
        if "playwright_installed" not in hb:
            issues.append(f"apollo has_browser bad shape: {hb}")

        fp = await call(c, "find_people", {"domain": "stripe.com", "limit": 2})
        if fp.get("people"):
            print(f"  find_people: {fp['count']} via {fp.get('source')}")
        elif fp.get("error") in ("apollo not logged in", "playwright is not installed"):
            print(f"  find_people: skip ({fp.get('error')}) — run setup-apollo-web.ps1")
        elif fp.get("error"):
            issues.append(f"apollo find_people: {fp.get('error')}")
        elif not fp.get("people"):
            issues.append(f"apollo find_people unexpected: {fp}")

    return issues


async def main():
    print("=== Live smoke: webscrape + email-finder + apollo ===\n")
    ws_issues = await test_webscrape()
    ef_issues = await test_email_finder()
    ap_issues = await test_apollo()

    if ws_issues:
        print("WEBSCRAPE issues:")
        for i in ws_issues:
            print(f"  - {i}")
    else:
        print("WEBSCRAPE OK")

    print()
    if ef_issues:
        print("EMAIL-FINDER issues:")
        for i in ef_issues:
            print(f"  - {i}")
    else:
        print("EMAIL-FINDER OK")

    print()
    if ap_issues:
        print("APOLLO issues:")
        for i in ap_issues:
            print(f"  - {i}")
    else:
        print("APOLLO OK")

    if ws_issues or ef_issues or ap_issues:
        sys.exit(1)
    print("\nALL SMOKE CHECKS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
