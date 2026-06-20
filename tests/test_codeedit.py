"""Smoke test for codeedit: exercises write_file / replace_lines / multi_edit, a unified-diff
apply, and proves a DELIBERATELY BROKEN python edit with validate=True AUTO-ROLLS BACK (the file
on disk is restored to its prior, valid state). Runs offline; no network/credentials."""
import asyncio
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "servers" / "codeedit"))

import server  # noqa: E402
from fastmcp import Client  # noqa: E402


async def main():
    tmp = Path(tempfile.mkdtemp(prefix="codeedit-test-"))
    repo = str(tmp)

    async with Client(server.mcp) as c:
        tools = sorted(t.name for t in await c.list_tools())
        print("tools:", tools)
        for expected in ("preview_patch", "apply_patch", "replace_in_file", "insert_lines",
                         "delete_lines", "replace_lines", "write_file", "multi_edit", "undo",
                         "list_backups", "validate", "syntax_check", "format_code", "lint",
                         "run_tests"):
            assert expected in tools, f"missing contract tool: {expected}"

        # --- write_file ----------------------------------------------------- #
        r = await c.call_tool("write_file",
                              {"path": "a.py", "content": "x = 1\ny = 2\nz = 3\n", "repo": repo})
        print("write_file:", r.data["ok"], r.data.get("syntax_check", {}).get("checker"))
        assert r.data["ok"] is True
        assert (tmp / "a.py").read_text() == "x = 1\ny = 2\nz = 3\n"
        assert r.data["diff"], "write_file should return a unified diff"

        # --- replace_lines -------------------------------------------------- #
        r = await c.call_tool("replace_lines",
                              {"path": "a.py", "start": 2, "end": 2, "text": "y = 22",
                               "repo": repo})
        print("replace_lines:", r.data["ok"])
        assert r.data["ok"] is True
        assert (tmp / "a.py").read_text() == "x = 1\ny = 22\nz = 3\n"

        # --- replace_in_file ------------------------------------------------ #
        r = await c.call_tool("replace_in_file",
                              {"path": "a.py", "find": "z = 3", "replace": "z = 33",
                               "repo": repo})
        print("replace_in_file:", r.data["ok"], "replaced=", r.data.get("replaced"))
        assert r.data["ok"] and r.data["replaced"] == 1
        assert "z = 33" in (tmp / "a.py").read_text()

        # --- insert_lines + delete_lines ------------------------------------ #
        r = await c.call_tool("insert_lines",
                              {"path": "a.py", "lineno": 1, "text": "# header", "repo": repo})
        assert r.data["ok"]
        assert (tmp / "a.py").read_text().startswith("# header\n")
        r = await c.call_tool("delete_lines",
                              {"path": "a.py", "start": 1, "end": 1, "repo": repo})
        assert r.data["ok"]
        assert not (tmp / "a.py").read_text().startswith("# header")

        # --- multi_edit (atomic, all succeed) ------------------------------- #
        r = await c.call_tool("multi_edit", {
            "repo": repo,
            "edits": [
                {"op": "write_file", "path": "b.py", "content": "def f():\n    return 1\n"},
                {"op": "replace_lines", "path": "a.py", "start": 1, "end": 1, "text": "x = 111"},
            ],
        })
        print("multi_edit ok:", r.data["ok"], "applied=", len(r.data.get("applied", [])))
        assert r.data["ok"] is True
        assert (tmp / "b.py").exists()
        assert (tmp / "a.py").read_text().startswith("x = 111\n")

        # --- unified-diff apply --------------------------------------------- #
        diff = (
            "--- a/b.py\n"
            "+++ b/b.py\n"
            "@@ -1,2 +1,2 @@\n"
            " def f():\n"
            "-    return 1\n"
            "+    return 2\n"
        )
        r = await c.call_tool("preview_patch", {"path_or_repo": repo, "unified_diff": diff})
        print("preview_patch applies:", r.data.get("applies"), "backend-free dry run")
        assert r.data["ok"] and r.data["applies"] is True
        assert "return 1" in (tmp / "b.py").read_text(), "preview must NOT write"

        r = await c.call_tool("apply_patch", {"unified_diff": diff, "repo": repo})
        print("apply_patch:", r.data["ok"], "backend=", r.data.get("backend"))
        assert r.data["ok"] is True
        assert (tmp / "b.py").read_text() == "def f():\n    return 2\n"

        # --- DELIBERATELY BROKEN edit must AUTO-ROLL BACK ------------------- #
        good = (tmp / "a.py").read_text()
        n_backups_before = len((await c.call_tool(
            "list_backups", {"path": "a.py", "repo": repo})).data["backups"])

        r = await c.call_tool("write_file", {
            "path": "a.py",
            "content": "def broken(:\n    pass\n",   # invalid python syntax
            "repo": repo,
            "validate": True,
        })
        print("broken write_file ok:", r.data["ok"], "rolled_back:", r.data.get("rolled_back"))
        assert r.data["ok"] is False, "broken edit must fail"
        assert r.data["rolled_back"] is True, "must auto-roll back"
        assert r.data["syntax_check"]["ok"] is False
        restored = (tmp / "a.py").read_text()
        assert restored == good, f"file must be restored to prior valid state; got:\n{restored}"
        print("auto-rollback restored file intact ✅")

        # --- broken multi_edit rolls back EVERYTHING ------------------------ #
        a_before = (tmp / "a.py").read_text()
        b_before = (tmp / "b.py").read_text()
        r = await c.call_tool("multi_edit", {
            "repo": repo,
            "edits": [
                {"op": "replace_lines", "path": "a.py", "start": 1, "end": 1, "text": "x = 999"},
                {"op": "write_file", "path": "b.py", "content": "def nope(:\n    pass\n"},
            ],
        })
        print("broken multi_edit ok:", r.data["ok"])
        assert r.data["ok"] is False
        assert (tmp / "a.py").read_text() == a_before, "a.py must be rolled back too"
        assert (tmp / "b.py").read_text() == b_before, "b.py must be rolled back"
        print("atomic multi_edit rollback intact ✅")

        # --- undo ----------------------------------------------------------- #
        r = await c.call_tool("undo", {"path": "a.py", "repo": repo})
        print("undo:", r.data["ok"], "restored_from set:", bool(r.data.get("restored_from")))
        assert r.data["ok"] is True

        # --- syntax_check / validate ---------------------------------------- #
        r = await c.call_tool("syntax_check", {"path": "b.py", "repo": repo})
        print("syntax_check:", r.data["valid"], r.data["checker"])
        assert r.data["ok"] and r.data["valid"] is True
        r = await c.call_tool("validate", {"path": "b.py", "repo": repo})
        assert r.data["ok"] and r.data["valid"] is True

        # unknown extension -> skipped, never crash
        await c.call_tool("write_file", {"path": "notes.xyz", "content": "hi\n", "repo": repo})
        r = await c.call_tool("syntax_check", {"path": "notes.xyz", "repo": repo})
        print("syntax_check unknown ext skipped:", r.data["skipped"])
        assert r.data["ok"] and r.data["skipped"] is True

        # --- format_code (degrades gracefully) ------------------------------ #
        r = await c.call_tool("format_code", {"path": "b.py", "write": False, "repo": repo})
        print("format_code:", {k: r.data.get(k) for k in ("formatter", "skipped", "changed")})
        assert r.data["ok"] is True  # works whether or not black is installed

        # --- lint (degrades gracefully) ------------------------------------- #
        r = await c.call_tool("lint", {"path": "b.py", "repo": repo})
        print("lint:", {k: r.data.get(k) for k in ("linter", "skipped", "clean")})
        assert r.data["ok"] is True

        # --- run_tests (degrades gracefully on an empty repo) --------------- #
        r = await c.call_tool("run_tests", {"repo": repo})
        print("run_tests:", {k: r.data.get(k) for k in ("runner", "skipped", "passed")})
        assert r.data["ok"] is True  # skipped or ran; must not crash

        # --- path safety: traversal rejected -------------------------------- #
        r = await c.call_tool("write_file",
                              {"path": "../escape.py", "content": "x=1\n", "repo": repo})
        print("traversal rejected:", r.data["ok"] is False)
        assert r.data["ok"] is False and "escapes root" in r.data["error"]

        print("\nCODEEDIT OK ✅")


asyncio.run(main())
