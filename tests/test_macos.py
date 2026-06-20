"""Offline smoke + security tests for the macOS cluster (mac-control, homebrew).

Network/credential free. Exercises: tool registration, clipboard roundtrip, read-only system
queries, confirm-gates, and the input-validation / argument-injection guards added in the audit.
Mutating/destructive brew + osascript paths are NOT triggered (only their pre-flight guards)."""
import asyncio
import shutil
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402

# Clean any per-server data dirs this test may touch.
for srv in ("mac-control", "homebrew"):
    d = Path.home() / ".mcp-suite" / srv
    if d.exists():
        shutil.rmtree(d, ignore_errors=True)


async def one(name, fn):
    sys.path.insert(0, str(ROOT / "servers" / name))
    import server  # noqa
    async with Client(server.mcp) as c:
        await fn(c)
    del sys.modules["server"]


async def main():
    async def mc(c):
        server = sys.modules["server"]
        tools = {t.name for t in await c.list_tools()}
        for t in ["set_volume", "get_volume", "set_dark_mode", "set_brightness", "battery",
                  "get_clipboard", "set_clipboard", "notify", "open_app", "open_url", "open_file",
                  "toggle_wifi", "toggle_bluetooth", "sleep_display"]:
            assert t in tools, f"missing preserved tool {t}"
        for t in ["screenshot", "get_system_info", "running_apps", "frontmost_app", "type_text",
                  "key_stroke", "set_wallpaper", "caffeinate", "stop_caffeinate", "lock_screen",
                  "eject", "spotlight_search", "set_focus", "list_shortcuts", "battery_detailed",
                  "network_info", "say", "empty_trash", "list_volumes", "disk_usage",
                  "clear_clipboard", "hide_app", "activate_app", "quit_app", "list_windows"]:
            assert t in tools, f"missing tool {t}"

        # clipboard roundtrip (offline)
        await c.call_tool("set_clipboard", {"text": "macos-cluster-test"})
        cb = await c.call_tool("get_clipboard", {})
        assert cb.data["text"] == "macos-cluster-test"
        cl = await c.call_tool("clear_clipboard", {})
        assert cl.data.get("ok")

        # confirm gates (no side effects)
        q = await c.call_tool("quit_app", {"name": "Calculator"})
        assert q.data.get("blocked") == "confirm required"
        for tool in ("caffeinate", "sleep", "empty_trash"):
            r = await c.call_tool(tool, {})
            assert r.data.get("blocked") == "confirm required", f"{tool} not gated"

        # missing-file guards before confirm
        wp = await c.call_tool("set_wallpaper", {"path": "/nope/missing.png"})
        assert wp.data.get("ok") is False

        # --- input validation guards (these return errors, never shell out) ---
        # empty/blank strings rejected
        for tool, args in [("notify", {"title": "", "message": "x"}),
                           ("notify", {"title": "x", "message": "  "}),
                           ("say", {"text": ""}),
                           ("open_app", {"name": ""}),
                           ("open_file", {"path": ""}),
                           ("activate_app", {"name": " "}),
                           ("hide_app", {"name": ""}),
                           ("type_text", {"text": ""}),
                           ("key_stroke", {"key": ""}),
                           ("spotlight_search", {"query": ""}),
                           ("run_shortcut", {"name": ""}),
                           ("set_focus", {"mode": ""})]:
            r = await c.call_tool(tool, args)
            assert r.data.get("ok") is False, f"{tool} {args} should be rejected"

        # quit_app empty name rejected before confirm gate
        qe = await c.call_tool("quit_app", {"name": "", "confirm": True})
        assert qe.data.get("ok") is False

        # url scheme validation
        bu = await c.call_tool("open_url", {"url": "not-a-url"})
        assert bu.data.get("ok") is False
        fu = await c.call_tool("open_url", {"url": "file:///etc/passwd"})
        assert fu.data.get("ok") is False

        # oversized text rejected
        big = await c.call_tool("say", {"text": "a" * 20000})
        assert big.data.get("ok") is False
        bigt = await c.call_tool("type_text", {"text": "a" * 20000})
        assert bigt.data.get("ok") is False

        # eject name traversal rejected (before any diskutil call) — confirm so we pass the gate
        ej = await c.call_tool("eject", {"name": "../etc", "confirm": True})
        assert ej.data.get("ok") is False

        # _q escaping: newlines/quotes/backslashes normalised, never crashes
        assert server._q('he said "hi"\n; rm -rf') == 'he said \\"hi\\" ; rm -rf'
        assert "\n" not in server._q("a\nb") and "\t" not in server._q("a\tb")

        # read-only system info (works offline on a real Mac; tolerate failure elsewhere)
        si = await c.call_tool("get_system_info", {})
        assert si.data.get("ok")
        ra = await c.call_tool("running_apps", {})
        assert isinstance(ra.data.get("apps"), list)
        sp = await c.call_tool("spotlight_search", {"query": "Safari", "kind": "app", "limit": 3})
        assert isinstance(sp.data.get("results"), list)
        du = await c.call_tool("disk_usage", {})
        assert du.data.get("ok")
        lv = await c.call_tool("list_volumes", {})
        assert isinstance(lv.data.get("volumes"), list)
        print("mac-control OK —", len(tools), "tools; clipboard; confirm gates; input validation")

    await one("mac-control", mc)

    async def hb(c):
        server = sys.modules["server"]
        tools = {t.name for t in await c.list_tools()}
        for t in ["search", "info", "list_installed", "outdated", "install", "upgrade"]:
            assert t in tools, f"missing preserved tool {t}"
        for t in ["info_json", "list_casks", "leaves", "deps", "uses", "doctor", "config",
                  "list_taps", "services_list", "services_run", "uninstall", "cleanup", "autoremove",
                  "pin", "unpin", "tap", "untap", "update", "bundle_dump", "bundle_check",
                  "bundle_install", "analytics"]:
            assert t in tools, f"missing tool {t}"

        # confirm-gate logic (pure, no brew call)
        for tool, args in [("uninstall", {"name": "x"}), ("cleanup", {"dry_run": False}),
                           ("services_run", {"action": "stop", "name": "x"}),
                           ("update", {}), ("bundle_install", {}),
                           ("install", {"name": "x"}), ("tap", {"name": "user/repo"}),
                           ("untap", {"name": "user/repo"}), ("upgrade", {})]:
            r = await c.call_tool(tool, args)
            assert r.data.get("blocked") == "confirm required", f"{tool} not gated"

        # bad action rejected before confirm
        bad = await c.call_tool("services_run", {"action": "nope"})
        assert bad.data.get("ok") is False

        # --- argument-injection / name validation guards (rejected BEFORE confirm + brew) ---
        # leading-dash names rejected even with confirm=True (would otherwise be parsed as flags)
        for tool, args in [("install", {"name": "--cask", "confirm": True}),
                           ("uninstall", {"name": "-rf", "confirm": True}),
                           ("pin", {"name": "-x"}),
                           ("unpin", {"name": "-x"}),
                           ("info", {"name": "-x"}),
                           ("info_json", {"name": "-x"}),
                           ("deps", {"name": "-x"}),
                           ("uses", {"name": "-x"}),
                           ("tap", {"name": "-x", "confirm": True}),
                           ("untap", {"name": "-x", "confirm": True})]:
            r = await c.call_tool(tool, args)
            assert r.data.get("ok") is False, f"{tool} {args} should reject dash-name"

        # shell metachar names rejected
        for tool in ("info", "deps", "uses", "pin"):
            r = await c.call_tool(tool, {"name": "wget; rm -rf /"})
            assert r.data.get("ok") is False, f"{tool} should reject metachars"

        # empty names rejected
        for tool in ("info", "info_json", "deps", "uses", "pin", "unpin"):
            r = await c.call_tool(tool, {"name": ""})
            assert r.data.get("ok") is False, f"{tool} should reject empty"
        es = await c.call_tool("search", {"query": ""})
        assert es.data.get("ok") is False

        # tap names with '/' are allowed by the validator (not flagged as illegal)
        assert server._bad_name("homebrew/cask-fonts", "tap") is None
        assert server._bad_name("-evil") is not None
        assert server._bad_name("a b") is not None
        assert server._bad_name("") is not None
        assert server._bad_name("wget") is None

        # tap with empty name -> read-only list (dict, not blocked)
        tl = await c.call_tool("tap", {})
        assert isinstance(tl.data, dict)
        print("homebrew OK —", len(tools), "tools; confirm gates; name validation")

    await one("homebrew", hb)

    print("\nMACOS CLUSTER OK")


asyncio.run(main())
