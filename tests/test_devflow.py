"""Offline tests for the devflow cluster via FastMCP's in-memory client.

Run: VIRTUAL_ENV= .venv/bin/python tests/test_devflow.py
Uses a temp MCP_DATA_DIR so it never touches real user data. No network, no creds.

Covers happy paths + key edge cases (including the security/validation fixes)."""
import asyncio
import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Isolate data dir BEFORE importing any server module, and wipe any prior state.
_TMP = tempfile.mkdtemp(prefix="devflow-test-")
os.environ["MCP_DATA_DIR"] = _TMP
for _name in ("snippet-vault", "jobtrack", "api-tester"):
    shutil.rmtree(Path(_TMP) / _name, ignore_errors=True)
# Also clear any real ~/.mcp-suite dirs are NOT touched because MCP_DATA_DIR overrides.

from fastmcp import Client  # noqa: E402


def load(name: str):
    path = ROOT / "servers" / name / "server.py"
    spec = importlib.util.spec_from_file_location(f"devflow_{name.replace('-', '_')}", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.mcp


async def tools(client) -> set:
    return {t.name for t in await client.list_tools()}


async def call(c, name, args):
    return (await c.call_tool(name, args)).data


async def test_scaffold():
    async with Client(load("scaffold")) as c:
        ts = await tools(c)
        assert {"list_templates", "new_project", "add_ci"} <= ts, "kept originals"
        assert {"list_licenses", "add_license", "add_makefile", "git_init",
                "add_precommit", "add_devcontainer"} <= ts, "new tools"
        tmpl = await call(c, "list_templates", {})
        for must in ("fastapi", "react-vite", "go", "rust", "cli", "mcp-server"):
            assert must in tmpl, f"missing stack {must}"

        dest = tempfile.mkdtemp(prefix="scaffold-")
        r = await call(c, "new_project", {
            "stack": "mcp-server", "name": "demo", "dest": dest,
            "license": "Apache-2.0", "makefile": True, "editorconfig": True})
        assert "error" not in r, r
        assert (Path(dest) / "demo" / "server.py").exists()
        assert (Path(dest) / "demo" / "Makefile").exists()
        assert "Apache" in (Path(dest) / "demo" / "LICENSE").read_text()

        # edge: unknown stack / license
        assert "error" in await call(c, "new_project",
                                     {"stack": "nope", "name": "x", "dest": dest})
        assert "error" in await call(c, "new_project",
                                     {"stack": "go", "name": "x", "dest": dest, "license": "WTFPL"})
        # edge: existing non-empty dir
        r2 = await call(c, "new_project", {"stack": "mcp-server", "name": "demo", "dest": dest})
        assert "error" in r2 and "exists" in r2["error"], r2

        # SECURITY: path traversal via name must be rejected, nothing written outside dest
        for bad in ("../escape", "..", "a/b", "foo\\bar", "", "  "):
            res = await call(c, "new_project", {"stack": "go", "name": bad, "dest": dest})
            assert "error" in res, f"name {bad!r} should be rejected: {res}"
        assert not (Path(dest).parent / "escape").exists(), "traversal created a dir outside dest!"
        # empty dest rejected
        assert "error" in await call(c, "new_project", {"stack": "go", "name": "ok", "dest": ""})

        # add_* on an existing dir
        proj = Path(dest) / "demo"
        assert "error" not in await call(c, "add_license", {"project_dir": str(proj), "license": "MIT"})
        assert "error" not in await call(c, "add_precommit", {"project_dir": str(proj), "stack": "python-uv"})
        print("scaffold OK:", len(ts), "tools")


async def test_devlog():
    async with Client(load("devlog")) as c:
        ts = await tools(c)
        assert {"daily_log", "weekly_summary", "standup"} <= ts
        assert {"authors", "activity", "streak", "file_churn", "multi_summary",
                "export_markdown", "contributors"} <= ts
        r = await call(c, "authors", {"repo": str(ROOT), "since": "365.days.ago"})
        assert "authors" in r, r
        r = await call(c, "export_markdown", {"repo": str(ROOT), "days": 365})
        assert "markdown" in r
        # edge: not a repo -> graceful error
        nope = tempfile.mkdtemp(prefix="notrepo-")
        assert "error" in await call(c, "authors", {"repo": nope})
        assert "error" in await call(c, "streak", {"repo": nope})
        # daily_log on non-repo returns empty, not a crash
        dl = await call(c, "daily_log", {"repo": nope, "days": 1})
        assert dl["count"] == 0
        print("devlog OK:", len(ts), "tools")


async def test_readme_changelog():
    async with Client(load("readme-changelog")) as c:
        ts = await tools(c)
        assert {"gen_changelog", "release_notes", "gen_readme"} <= ts
        assert {"suggest_bump", "lint_commits", "keep_a_changelog", "contributors", "badges"} <= ts
        r = await call(c, "suggest_bump", {"repo": str(ROOT)})
        assert r["suggested_bump"] in ("major", "minor", "patch"), r
        r = await call(c, "lint_commits", {"repo": str(ROOT)})
        assert "pass_rate_pct" in r
        r = await call(c, "keep_a_changelog", {"repo": str(ROOT), "version": "1.2.3"})
        assert "1.2.3" in r["markdown"]
        b = await call(c, "badges", {"repo": str(ROOT), "owner": "me", "name": "proj"})
        assert "shields.io" in b["markdown"]
        # edge: gen_readme on a fresh dir + missing repo
        d = tempfile.mkdtemp(prefix="rd-")
        gr = await call(c, "gen_readme", {"repo": d})
        assert "draft_outline" in gr
        assert "error" in await call(c, "gen_readme", {"repo": d + "/nope"})
        print("readme-changelog OK:", len(ts), "tools")


async def test_snippet_vault():
    async with Client(load("snippet-vault")) as c:
        ts = await tools(c)
        assert {"save_snippet", "search", "get", "list_by_lang", "delete"} <= ts
        assert {"detect_language", "update_snippet", "import_file", "import_dir",
                "export", "run_snippet", "stats", "tags"} <= ts
        r = await call(c, "save_snippet", {
            "title": "hello", "code": "def f():\n    print('hi')\n", "tags": "py,demo"})
        assert r["lang"] == "python", r
        sid = r["id"]
        s = await call(c, "search", {"query": "hello"})
        assert any(x["id"] == sid for x in s)

        # VALIDATION: empty title/code rejected
        assert "error" in await call(c, "save_snippet", {"title": "", "code": "x"})
        assert "error" in await call(c, "save_snippet", {"title": "t", "code": "   "})

        # get increments usage; missing id -> error
        g = await call(c, "get", {"snippet_id": sid})
        assert g["usage_count"] >= 1
        assert "error" in await call(c, "get", {"snippet_id": 999999})

        # update only non-empty fields; missing -> error
        u = await call(c, "update_snippet", {"snippet_id": sid, "tags": "py,updated"})
        assert "updated" in u["tags"]
        assert "error" in await call(c, "update_snippet", {"snippet_id": 999999, "title": "x"})

        run = await call(c, "save_snippet", {
            "title": "run", "code": "print('from-subprocess')", "lang": "python"})
        rr = await call(c, "run_snippet", {"snippet_id": run["id"]})
        assert rr.get("ok") and "from-subprocess" in rr.get("stdout", ""), rr
        # run unsupported lang refused
        comp = await call(c, "save_snippet", {"title": "c", "code": "int main(){}", "lang": "c"})
        assert "error" in await call(c, "run_snippet", {"snippet_id": comp["id"]})
        # run missing snippet
        assert "error" in await call(c, "run_snippet", {"snippet_id": 888888})

        # import_file happy + missing
        src = Path(tempfile.mkdtemp()) / "x.py"
        src.write_text("import os\nprint(os.getcwd())\n")
        imp = await call(c, "import_file", {"path": str(src)})
        assert imp["lang"] == "python"
        assert "error" in await call(c, "import_file", {"path": str(src) + ".missing"})

        exp = tempfile.mktemp(suffix=".md")
        e = await call(c, "export", {"path": exp, "format": "markdown"})
        assert e["count"] >= 2 and Path(exp).exists()
        st = await call(c, "stats", {})
        assert st["total"] >= 2
        # delete
        assert (await call(c, "delete", {"snippet_id": sid}))["deleted"] == sid
        print("snippet-vault OK:", len(ts), "tools")


async def test_jobtrack():
    async with Client(load("jobtrack")) as c:
        ts = await tools(c)
        assert {"add_application", "update_status", "list_pipeline", "due_followups", "stats"} <= ts
        assert {"add_interview", "list_interviews", "add_note", "set_followup",
                "get_application", "search", "export_csv", "import_csv", "analytics"} <= ts
        a = await call(c, "add_application", {"company": "Acme", "role": "Eng", "source": "linkedin"})
        aid = a["id"]

        # VALIDATION: missing company/role + bad status
        assert "error" in await call(c, "add_application", {"company": "", "role": "X"})
        assert "error" in await call(c, "add_application", {"company": "Y", "role": "Z", "status": "bogus"})
        assert "error" in await call(c, "update_status", {"application_id": aid, "status": "bogus"})
        assert "error" in await call(c, "update_status", {"application_id": 999999, "status": "applied"})

        await call(c, "update_status", {"application_id": aid, "status": "phone_screen"})
        await call(c, "add_interview", {"application_id": aid, "kind": "phone_screen", "notes": "went well"})
        assert "error" in await call(c, "add_interview", {"application_id": 999999})
        full = await call(c, "get_application", {"application_id": aid})
        assert full["interviews"] and full["history"], full
        assert "error" in await call(c, "get_application", {"application_id": 999999})

        note = await call(c, "add_note", {"application_id": aid, "note": "called recruiter"})
        assert "called recruiter" in note["notes"]
        found = await call(c, "search", {"query": "Acme"})
        assert any(x["id"] == aid for x in found)

        csv_path = tempfile.mktemp(suffix=".csv")
        ex = await call(c, "export_csv", {"path": csv_path})
        assert ex["count"] >= 1
        im = await call(c, "import_csv", {"path": csv_path})
        assert im["added"] >= 1, im
        assert "error" in await call(c, "import_csv", {"path": csv_path + ".nope"})
        an = await call(c, "analytics", {})
        assert "funnel" in an
        print("jobtrack OK:", len(ts), "tools")


async def test_api_tester():
    async with Client(load("api-tester")) as c:
        ts = await tools(c)
        assert {"add_request", "run", "run_inline", "run_collection", "list_requests"} <= ts
        assert {"set_env", "get_env", "set_auth", "set_assertions", "import_curl",
                "import_openapi", "history", "curl_for", "save_response", "delete_request"} <= ts
        await call(c, "set_env", {"env": "default", "key": "BASE", "value": "https://example.com"})
        env = await call(c, "get_env", {"env": "default"})
        assert env["vars"]["BASE"] == "https://example.com"

        ic = await call(c, "import_curl", {
            "command": "curl -X POST https://api.example.com/v1/x -H 'Authorization: Bearer t' -d '{\"a\":1}'",
            "name": "x"})
        assert ic["method"] == "POST" and ic["url"].endswith("/v1/x"), ic
        rid = ic["id"]
        await call(c, "set_assertions", {"request_id": rid,
                                         "assertions": [{"type": "status", "equals": 200}]})
        cf = await call(c, "curl_for", {"request_id": rid})
        assert "curl" in cf and "POST" in cf["curl"]

        # VALIDATION: empty url / bad method rejected at save time
        assert "error" in await call(c, "add_request", {"name": "n", "url": ""})
        assert "error" in await call(c, "add_request", {"name": "n", "url": "http://x", "method": "FETCH"})
        # run_inline validates scheme + method offline (no network reached)
        assert "error" in await call(c, "run_inline", {"url": "ftp://x"})
        assert "error" in await call(c, "run_inline", {"url": "http://x", "method": "BOGUS"})
        assert "error" in await call(c, "run_inline", {"url": ""})
        # import_curl with no URL
        assert "error" in await call(c, "import_curl", {"command": "curl -X GET -H 'A: b'"})

        # OpenAPI import from a temp JSON file (offline)
        spec = tempfile.mktemp(suffix=".json")
        Path(spec).write_text('{"openapi":"3.0.0","info":{"title":"T"},'
                              '"servers":[{"url":"https://api.t.com"}],'
                              '"paths":{"/ping":{"get":{"operationId":"ping"}}}}')
        oi = await call(c, "import_openapi", {"spec": spec})
        assert oi["imported"] == 1, oi
        # bad spec / missing file
        bad = tempfile.mktemp(suffix=".json")
        Path(bad).write_text("not json")
        assert "error" in await call(c, "import_openapi", {"spec": bad})
        assert "error" in await call(c, "import_openapi", {"spec": spec + ".nope"})

        lst = await call(c, "list_requests", {})
        assert any(r["name"] == "x" for r in lst)
        assert (await call(c, "delete_request", {"request_id": rid}))["deleted"] == rid
        print("api-tester OK:", len(ts), "tools")


async def test_repo_health():
    async with Client(load("repo-health")) as c:
        ts = await tools(c)
        assert {"health_report", "stale_branches", "large_files", "missing_files",
                "todo_census", "commit_cadence", "gitignore_check"} <= ts
        r = await call(c, "health_report", {"repo": str(ROOT)})
        assert 0 <= r["score"] <= 100 and r["grade"] in "ABCDF", r
        mf = await call(c, "missing_files", {"repo": str(ROOT)})
        assert "present" in mf
        td = await call(c, "todo_census", {"repo": str(ROOT), "max_files": 100})
        assert "by_kind" in td
        gi = await call(c, "gitignore_check", {"repo": str(ROOT)})
        assert "has_gitignore" in gi
        lf = await call(c, "large_files", {"repo": str(ROOT), "min_kb": 10000})
        assert "files" in lf
        # edge: non-existent path -> error
        assert "error" in await call(c, "health_report", {"repo": str(ROOT) + "/nope"})
        assert "error" in await call(c, "missing_files", {"repo": str(ROOT) + "/nope"})
        print("repo-health OK:", len(ts), "tools")


async def main():
    await test_scaffold()
    await test_devlog()
    await test_readme_changelog()
    await test_snippet_vault()
    await test_jobtrack()
    await test_api_tester()
    await test_repo_health()
    print("\nALL DEVFLOW TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    sys.exit(0)
