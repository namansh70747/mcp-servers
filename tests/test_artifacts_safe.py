"""Offline artifact-safety tests for the profile-driven generator servers:
portfolio-site, github-profile, resume-forge, deckforge, blog-drafter.

These tools all read the suite's shared profile.json and/or hit network/python-pptx. A missing,
empty, or malformed profile.json (skills None/list, highlights/description non-list/non-string),
a non-JSON GitHub response, or a chart whose values don't align with its categories must NOT crash
the tool — it should return a clean dict (a result or an {"error": ...}/{"__error__": ...}) the
agent can recover from.

Run: VIRTUAL_ENV= /Users/namansharma/mcp-servers/.venv/bin/python tests/test_artifacts_safe.py
"""
import asyncio
import importlib.util
import json
import os
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "shared"))
# Keep real user data safe — point the data dir at a throwaway location.
os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp()

from fastmcp import Client  # noqa: E402


def _load(name, modname):
    """Import a server.py under a unique module name (so re-imports don't collide)."""
    sys.path.insert(0, str(ROOT / "servers" / name))
    spec = importlib.util.spec_from_file_location(modname, ROOT / "servers" / name / "server.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _write_profile(content) -> Path:
    """Write a profile.json (dict -> json, str -> raw) into a temp dir; return the file path."""
    d = Path(tempfile.mkdtemp())
    pf = d / "profile.json"
    pf.write_text(content if isinstance(content, str) else json.dumps(content))
    return pf


async def _call(mod, name, args):
    """Call a tool; return its .data, raising on any crash so the test fails loudly."""
    async with Client(mod.mcp) as c:
        return (await c.call_tool(name, args)).data


# Malformed/partial profiles that previously crashed these tools.
BAD_PROFILES = [
    "{ this is not valid json",                       # JSONDecodeError
    [],                                               # not a dict
    {"name": 123, "skills": None, "links": None},     # skills None, name non-str
    {"name": "X", "skills": ["flat", "list"]},        # skills a list (no .values())
    {"name": "X", "skills": {"langs": "py"}},         # skill group is a string, not a list
    {"name": "X", "summary": 42, "headline": 7},      # non-string scalars
    {"name": "X", "experience": "nope", "projects": 5},  # sections not lists
    {"name": "X",
     "skills": {"l": ["py"]},
     "experience": [{"company": "C", "role": "R", "highlights": "not-a-list"}],
     "projects": [{"name": "P", "description": 123, "tech": 9}],
     "education": ["not-a-dict"]},
]


async def test_resume_forge():
    rf = _load("resume-forge", "rf_safe")
    for prof in BAD_PROFILES:
        rf.PROFILE = _write_profile(prof)
        r = await _call(rf, "build_resume", {})
        assert isinstance(r, dict) and r.get("path"), ("build_resume", prof, r)
        r = await _call(rf, "cover_letter", {"company": "Acme", "role": "Eng", "body": "Hi.\n\nBye."})
        assert isinstance(r, dict) and r.get("path"), ("cover_letter", prof, r)
        r = await _call(rf, "export_markdown", {})
        assert isinstance(r, dict) and r.get("path"), ("export_markdown", prof, r)
        r = await _call(rf, "export_json_resume", {})
        assert isinstance(r, dict) and r.get("path"), ("export_json_resume", prof, r)
        r = await _call(rf, "ats_score", {"jd": "Python engineer with cloud experience"})
        assert isinstance(r, dict) and "score" in r, ("ats_score", prof, r)
    # missing file entirely
    rf.PROFILE = Path(tempfile.mkdtemp()) / "absent.json"
    r = await _call(rf, "build_resume", {})
    assert r.get("path"), r
    print("resume-forge: OK")


async def test_github_profile():
    gp = _load("github-profile", "gp_safe")
    # No network needed when include_stats/include_badges are off and pins error out gracefully.
    # Point at a username whose repo fetch will fail offline; build_readme must still produce md.
    for prof in BAD_PROFILES:
        gp.ROOT = _write_profile(prof).parent
        r = await _call(gp, "build_readme",
                        {"username": "octocat", "write": False,
                         "include_stats": False, "include_badges": False})
        assert isinstance(r, dict) and isinstance(r.get("markdown"), str), ("build_readme", prof, r)
        assert r.get("markdown"), ("empty markdown", prof, r)
    # missing profile.json directory
    gp.ROOT = Path(tempfile.mkdtemp())
    r = await _call(gp, "build_readme",
                    {"username": "octocat", "write": False,
                     "include_stats": False, "include_badges": False})
    assert isinstance(r.get("markdown"), str) and r["markdown"], r
    print("github-profile: OK")


async def test_github_profile_non_json():
    """A non-JSON GitHub response must surface a clean error, not crash."""
    gp = _load("github-profile", "gp_safe_njson")

    def _fake_get(url, **params):
        return {"ok": True, "status": 200, "headers": {}, "text": "<html>not json</html>"}

    gp.http.request = lambda method, url, **kw: _fake_get(url)
    r = await _call(gp, "suggest_pins", {"username": "octocat"})
    assert isinstance(r, dict) and r.get("__error__") == "invalid_json", r
    print("github-profile non-json: OK")


async def test_portfolio_site():
    ps = _load("portfolio-site", "ps_safe")
    for prof in BAD_PROFILES:
        ps.ROOT = _write_profile(prof).parent
        r = await _call(ps, "build_site", {"theme": "dark"})
        assert isinstance(r, dict) and r.get("path"), ("build_site", prof, r)

    # projects_from_github: a 200 response with non-JSON body must NOT raise JSONDecodeError.
    class _Resp:
        status_code = 200
        content = b"<html>not json</html>"

        def json(self):
            raise ValueError("not json")

    orig = ps.httpx.get
    ps.httpx.get = lambda *a, **k: _Resp()
    try:
        r = await _call(ps, "projects_from_github", {"username": "octocat"})
        assert isinstance(r, dict) and "error" in r, ("projects_from_github non-json", r)
    finally:
        ps.httpx.get = orig
    print("portfolio-site: OK")


async def test_deckforge_chart():
    dk = _load("deckforge", "dk_safe")
    async with Client(dk.mcp) as c:
        did = (await c.call_tool("create_presentation", {})).data["deck_id"]
        # mismatched: fewer values than categories
        r = (await c.call_tool("add_chart_slide",
                               {"deck_id": did, "title": "t", "categories": ["a", "b", "c"],
                                "series": [{"name": "s", "values": [1, 2]}]})).data
        assert isinstance(r, dict) and "error" in r, ("fewer values", r)
        # mismatched: more values than categories
        r = (await c.call_tool("add_chart_slide",
                               {"deck_id": did, "title": "t", "categories": ["a", "b"],
                                "series": [{"name": "s", "values": [1, 2, 3]}]})).data
        assert isinstance(r, dict) and "error" in r, ("more values", r)
        # values not a list
        r = (await c.call_tool("add_chart_slide",
                               {"deck_id": did, "title": "t", "categories": ["a"],
                                "series": [{"name": "s", "values": 5}]})).data
        assert isinstance(r, dict) and "error" in r, ("non-list values", r)
        # aligned lengths still succeed (success shape unchanged)
        r = (await c.call_tool("add_chart_slide",
                               {"deck_id": did, "title": "t", "categories": ["a", "b"],
                                "series": [{"name": "s", "values": [1, 2]}]})).data
        assert isinstance(r, dict) and "slide_index" in r and "error" not in r, ("aligned", r)
    print("deckforge: OK")


async def test_blog_drafter():
    bd = _load("blog-drafter", "bd_safe")
    for prof in BAD_PROFILES:
        bd.PROFILE = _write_profile(prof)
        r = await _call(bd, "outline_to_draft", {"topic": "x"})
        assert isinstance(r, dict) and "author" in r and r.get("id"), ("outline_to_draft", prof, r)
        r = await _call(bd, "new_draft", {"title": "T"})
        assert isinstance(r, dict) and r.get("id"), ("new_draft", prof, r)
    # missing profile.json
    bd.PROFILE = Path(tempfile.mkdtemp()) / "absent.json"
    r = await _call(bd, "outline_to_draft", {"topic": "x"})
    assert r.get("id"), r
    print("blog-drafter: OK")


async def main():
    await test_resume_forge()
    await test_github_profile()
    await test_github_profile_non_json()
    await test_portfolio_site()
    await test_deckforge_chart()
    await test_blog_drafter()
    print("\nALL ARTIFACT-SAFETY TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
