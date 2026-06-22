"""Offline tests for deskpilot — the macOS computer-use server.

Runs on ANY platform: deskpilot degrades gracefully when PyObjC is absent, so this exercises
tool registration, the health/capability report, pure input-validation + helper logic, the
confirm-gate, and the no-crash invariant — WITHOUT performing any real clicks/keystrokes
(only rejection paths of the act tools are hit; read-only perceive tools are tolerated either
way). On a configured Mac (uv sync --group desktop + permissions) the perceive tools light up.

    VIRTUAL_ENV= .venv/bin/python tests/test_deskpilot.py
"""
import asyncio
import importlib.util
import math
import os
import sys
import tempfile
from pathlib import Path

os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="deskpilot-test-")
os.environ.pop("DESKPILOT_REQUIRE_CONFIRM", None)
ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402

EXPECTED = [
    "health", "request_permissions",
    "screenshot", "screenshot_path", "ui_elements", "ui_tree", "find_element", "element_at",
    "read_text_field", "get_frontmost", "list_windows",
    "move", "click", "double_click", "right_click", "drag", "scroll", "click_element",
    "type_text", "press_keys", "key", "press_element", "focus_element", "set_field",
    "set_clipboard_paste", "open_app_and_wait", "recipe",
]


def load():
    spec = importlib.util.spec_from_file_location("dpz_server", ROOT / "servers" / "deskpilot" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_pure_helpers(s):
    # keycode mapping (no PyObjC needed)
    assert s._keycode_for("return") == (36, False)
    assert s._keycode_for("c") == (8, False)
    assert s._keycode_for("C") == (8, True)        # uppercase -> shift
    assert s._keycode_for("!") == (18, True)       # shifted symbol -> shift + base key
    assert s._keycode_for("nope") is None
    assert s._keycode_for("f5") == (96, False)

    # string helpers
    assert s._s(None) is None and s._s("  ") is None and s._s(" x ") == "x"
    assert s._trunc("abc", 10) == "abc"
    assert s._trunc("a" * 50, 10).endswith("…") and len(s._trunc("a" * 50, 10)) == 11

    # clamp: works without PyObjC (default bounds), rejects NaN / absurd
    c = s._clamp(100, 100)
    assert c == (100.0, 100.0)
    assert s._clamp(math.nan, 0) is None
    assert s._clamp(1e9, 1e9) is None
    big = s._clamp(10 ** 5, 10 ** 5)               # within absurd-cap but outside displays -> clamped
    assert big is not None and big[0] < 10 ** 5

    # confirm gate logic (independent of engine)
    assert s._require_confirm() is False
    assert s._gate(False) is None and s._gate(True) is None
    os.environ["DESKPILOT_REQUIRE_CONFIRM"] = "1"
    try:
        assert s._require_confirm() is True
        g = s._gate(False)
        assert isinstance(g, dict) and g.get("blocked") == "confirm required"
        assert s._gate(True) is None
    finally:
        os.environ.pop("DESKPILOT_REQUIRE_CONFIRM", None)

    # combo parsing only when the engine (flag constants) is present
    if s._PYOBJC:
        assert s._parse_combo("cmd+c") is not None
        assert s._parse_combo("not a key !!") is None
    print("pure helpers OK (keycodes, clamp, trunc, gate, combo)")


async def main():
    s = load()
    py = s._PYOBJC
    async with Client(s.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        for t in EXPECTED:
            assert t in tools, f"missing tool {t}"
        assert len(tools) == len(EXPECTED), f"unexpected tool set: {sorted(tools ^ set(EXPECTED))}"

        # --- health: well-formed on any platform ---
        h = (await c.call_tool("health", {})).data
        assert h.get("ok") is True
        for k in ("server", "platform", "pyobjc", "accessibility", "screen_recording",
                  "ready", "require_confirm", "displays", "host_process", "cliclick"):
            assert k in h, f"health missing {k}"
        assert h["server"] == "deskpilot"
        assert h["platform"] == sys.platform
        assert h["pyobjc"] == py
        assert isinstance(h["displays"], list)
        assert h["require_confirm"] is False
        if not py:
            assert h["ready"] is False and h.get("hints"), "should hint how to install when no engine"

        # --- recipe: pure, no engine ---
        rl = (await c.call_tool("recipe", {})).data
        assert "send_message" in rl.get("recipes", [])
        rs = (await c.call_tool("recipe", {"name": "send_message"})).data
        assert isinstance(rs.get("steps"), list) and rs["steps"]
        assert (await c.call_tool("recipe", {"name": "bogus"})).data.get("ok") is False

        # --- act tools: ONLY rejection paths (never perform a real action) ---
        # empty / oversized text validated (returns err before any keystroke even if engine is live)
        assert (await c.call_tool("type_text", {"text": ""})).data.get("ok") is False
        assert (await c.call_tool("type_text", {"text": "a" * 20000})).data.get("ok") is False
        # absurd coords clamp to None -> err (never clicks)
        assert (await c.call_tool("click", {"x": 1e9, "y": 1e9})).data.get("ok") is False
        # bad button rejected
        bb = (await c.call_tool("click", {"x": 10, "y": 10, "button": "sideways"})).data
        if py:
            assert bb.get("ok") is False  # engine live: button validated
        # unparseable combo rejected (never posts a key)
        assert (await c.call_tool("press_keys", {"combo": "totally nonsense !!"})).data.get("ok") is False
        # missing selectors rejected
        assert (await c.call_tool("set_field", {"text": "x"})).data.get("ok") is False
        assert (await c.call_tool("read_text_field", {})).data.get("ok") is False
        assert (await c.call_tool("find_element", {})).data.get("ok") is False
        # stale / unknown element id -> not_found (never acts)
        assert (await c.call_tool("click_element", {"id": "e999999"})).data.get("ok") is False
        assert (await c.call_tool("press_element", {"id": "e999999"})).data.get("ok") is False

        # --- engine-absent degradation: every engine tool returns a clean err with a hint ---
        if not py:
            for tool, args in [("screenshot", {}), ("ui_elements", {}), ("get_frontmost", {}),
                               ("move", {"x": 10, "y": 10}), ("open_app_and_wait", {"name": "Finder"})]:
                r = (await c.call_tool(tool, args)).data
                assert r.get("ok") is False and r.get("hint"), f"{tool} should degrade with a hint"
        else:
            # engine present: read-only perceive tools must not raise; ok True (perms granted) or
            # ok False with a permission hint (perms not yet granted). Either is acceptable.
            for tool in ("get_frontmost", "ui_elements", "list_windows"):
                r = (await c.call_tool(tool, {})).data
                assert isinstance(r, dict) and ("ok" in r)
            print(f"engine LIVE — accessibility={h['accessibility']} screen_recording={h['screen_recording']}")

    test_pure_helpers(s)
    print(f"\nDESKPILOT OK — {len(tools)} tools; pyobjc={py}; health, validation, gate, no-crash")


asyncio.run(main())
