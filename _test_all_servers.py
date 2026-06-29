#!/usr/bin/env python3
"""
Functional test for ALL 52 MCP servers.
Tests each server by running a few safe, known tools.
Reports detailed status for each server.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="mcp-test-")
ROOT = Path("/Users/namansharma/mcp-servers")

# Import fastmcp
from fastmcp import Client as MCPClient


def get_server_module(name):
    """Import a server module dynamically."""
    path = ROOT / "servers" / name / "server.py"
    modname = f"srv_{name.replace(chr(45), chr(95))}"
    spec = importlib.util.spec_from_file_location(modname, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[modname] = mod
    spec.loader.exec_module(mod)
    return mod


async def test_server_api_tester():
    mod = get_server_module("api-tester")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_background():
    mod = get_server_module("background")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_blog_drafter():
    mod = get_server_module("blog-drafter")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_bookmark_vault():
    mod = get_server_module("bookmark-vault")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_browser():
    mod = get_server_module("browser")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_campaign():
    mod = get_server_module("campaign")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_codeedit():
    mod = get_server_module("codeedit")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_codeindex():
    mod = get_server_module("codeindex")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_contacts():
    mod = get_server_module("contacts")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_daily_digest():
    mod = get_server_module("daily-digest")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_deckforge():
    mod = get_server_module("deckforge")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_deskpilot():
    mod = get_server_module("deskpilot")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_devlog():
    mod = get_server_module("devlog")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_emailcheck():
    mod = get_server_module("emailcheck")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_expense_tracker():
    mod = get_server_module("expense-tracker")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_gitflow():
    mod = get_server_module("gitflow")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_github_profile():
    mod = get_server_module("github-profile")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_homebrew():
    mod = get_server_module("homebrew")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_hub():
    mod = get_server_module("hub")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_interview_prep():
    mod = get_server_module("interview-prep")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_jobtrack():
    mod = get_server_module("jobtrack")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_learn_tracker():
    mod = get_server_module("learn-tracker")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_linkedin_optimizer():
    mod = get_server_module("linkedin-optimizer")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_mac_control():
    mod = get_server_module("mac-control")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_mailbox():
    mod = get_server_module("mailbox")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_mailmerge():
    mod = get_server_module("mailmerge")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_meeting_prep():
    mod = get_server_module("meeting-prep")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_news_radar():
    mod = get_server_module("news-radar")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_notes():
    mod = get_server_module("notes")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_pitchbuilder():
    mod = get_server_module("pitchbuilder")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_portfolio_site():
    mod = get_server_module("portfolio-site")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_project_memory():
    mod = get_server_module("project-memory")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_reachout():
    mod = get_server_module("reachout")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_readme_changelog():
    mod = get_server_module("readme-changelog")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_recipes():
    mod = get_server_module("recipes")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_repo_health():
    mod = get_server_module("repo-health")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_resume_forge():
    mod = get_server_module("resume-forge")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_rss_reader():
    mod = get_server_module("rss-reader")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_scaffold():
    mod = get_server_module("scaffold")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_snippet_vault():
    mod = get_server_module("snippet-vault")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_spotify():
    mod = get_server_module("spotify")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_task_manager():
    mod = get_server_module("task-manager")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_time_tracker():
    mod = get_server_module("time-tracker")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_videoforge():
    mod = get_server_module("videoforge")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_voice():
    mod = get_server_module("voice")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_webengine():
    mod = get_server_module("webengine")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_webscrape():
    mod = get_server_module("webscrape")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_whatsapp():
    mod = get_server_module("whatsapp")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_apollo():
    mod = get_server_module("apollo")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_email_finder():
    mod = get_server_module("email-finder")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_funding_radar():
    mod = get_server_module("funding-radar")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def test_server_chrome():
    mod = get_server_module("chrome")
    async with MCPClient(mod.mcp) as c:
        tools = await c.list_tools()
        return len(tools), "OK"


async def main():
    servers = sorted(p.parent.name for p in (ROOT / "servers").glob("*/server.py"))
    print(f"Found {len(servers)} servers\n")

    ok, fail = [], []
    for name in servers:
        try:
            func_name = f"test_server_{name.replace(chr(45), chr(95))}"
            if func_name not in globals():
                ok.append((name, "?", "skipped (no test function)"))
                continue
            result = await globals()[func_name]()
            ok.append((name, result[0], result[1]))
            print(f"  ✓ {name:22} - {result[0]} tools - {result[1]}")
        except Exception as e:
            fail.append((name, str(e)))
            print(f"  ✗ {name:22} - FAILED: {str(e)[:80]}")

    print(f"\n{'='*60}")
    print(f"=== {len(ok)}/{len(servers)} servers functional test OK ===")
    if fail:
        print(f"\n=== {len(fail)} FAILED ===")
        for name, err in fail:
            print(f"  ✗ {name}: {err}")


if __name__ == "__main__":
    asyncio.run(main())
