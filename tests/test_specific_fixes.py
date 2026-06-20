"""Regression tests for the specific-fixes group (offline, no pytest required).

Run: VIRTUAL_ENV= /Users/namansharma/mcp-servers/.venv/bin/python tests/test_specific_fixes.py

Covers (per assignment):
  - gitflow.open_pr        — does not raise on the _q() shell-quote path
  - api-tester JSON path   — bad JSON-path assertions FAIL gracefully (never raise)
  - devlog.streak          — consecutive-day current streak counts correctly
  - codeindex semantic     — embedding-dim drift degrades to FTS (never garbage cosine)
  - codeedit.replace_in_file — anchor+count counting stays correct (no negative decrement)
  - mac-control            — set_wallpaper(bogus path) -> err; get_brightness graceful

Mutating mac tools are NOT exercised (only the bogus-path / read paths).
"""
from __future__ import annotations

import asyncio
import datetime
import importlib.util
import os
import subprocess
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRV = ROOT / "servers"
sys.path.insert(0, str(ROOT / "shared"))
sys.path.insert(0, str(ROOT))

os.environ.setdefault("MCP_DATA_DIR", tempfile.mkdtemp(prefix="specific-fixes-"))

from fastmcp import Client  # noqa: E402


def _load(name: str, uniq: str):
    spec = importlib.util.spec_from_file_location(uniq, str(SRV / name / "server.py"))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def _call(mod, tool: str, args: dict):
    async def go():
        async with Client(mod.mcp) as c:
            r = await c.call_tool(tool, args)
            return getattr(r, "data", r)
    return asyncio.run(go())


def _git(repo, *args, env=None):
    subprocess.run(["git", "-C", repo, *args], check=False, env=env,
                   capture_output=True, text=True)


def _init_repo() -> str:
    repo = tempfile.mkdtemp()
    subprocess.run(["git", "init", "-q", repo], check=False)
    _git(repo, "config", "user.email", "a@b.c")
    _git(repo, "config", "user.name", "x")
    return repo


# --------------------------------------------------------------------------- gitflow
def test_gitflow_open_pr_does_not_raise():
    gf = _load("gitflow", "sf_gitflow")
    repo = _init_repo()
    (Path(repo) / "f.txt").write_text("hi\n")
    _git(repo, "add", ".")
    _git(repo, "commit", "-q", "-m", "init")
    r = _call(gf, "open_pr", {"repo": repo, "title": "Title", "body": "Body"})
    assert isinstance(r, dict), r
    assert "command" in r, r
    assert "gh pr create --title" in r["command"], r


def test_gitflow_q_helper_quotes():
    gf = _load("gitflow", "sf_gitflow2")
    assert gf._q("a b") == "'a b'"
    assert gf._q("plain") == "plain"


# --------------------------------------------------------------------------- api-tester
def test_api_tester_bad_json_path_fails_gracefully():
    at = _load("api-tester", "sf_api")
    resp = {"status": 200, "body_preview": '{"data":[{"id":5}]}', "elapsed_ms": 10}
    bad_specs = [
        {"type": "json", "path": "data.99.id", "equals": 5},        # IndexError
        {"type": "json", "path": "data.notanint.id", "equals": 5},  # ValueError (int())
        {"type": "json", "path": "data.0.missing", "equals": 5},    # KeyError
        {"type": "json", "path": "notexist", "equals": 5},          # KeyError
    ]
    for spec in bad_specs:
        res = at._check_assertions([spec], resp)  # must NOT raise
        assert len(res) == 1 and res[0]["ok"] is False, (spec, res)
    good = at._check_assertions(
        [{"type": "json", "path": "data.0.id", "equals": 5}], resp)
    assert good[0]["ok"] is True, good


# --------------------------------------------------------------------------- devlog
def test_devlog_streak_consecutive_days():
    dl = _load("devlog", "sf_devlog")
    repo = _init_repo()
    today = datetime.date.today()
    for i in range(5, 0, -1):
        d = today - datetime.timedelta(days=i - 1)
        ds = d.strftime("%Y-%m-%dT12:00:00")
        env = dict(os.environ, GIT_AUTHOR_DATE=ds, GIT_COMMITTER_DATE=ds)
        with open(Path(repo) / "f.txt", "a") as fh:
            fh.write(f"line {i}\n")
        _git(repo, "add", ".", env=env)
        _git(repo, "commit", "-q", "-m", f"c{i}", env=env)
    r = _call(dl, "streak", {"repo": repo})
    assert r["current_streak"] == 5, r
    assert r["longest_streak"] == 5, r


def test_devlog_streak_stale_is_zero():
    dl = _load("devlog", "sf_devlog2")
    repo = _init_repo()
    today = datetime.date.today()
    for i in (5, 4, 3):
        d = today - datetime.timedelta(days=i)
        ds = d.strftime("%Y-%m-%dT12:00:00")
        env = dict(os.environ, GIT_AUTHOR_DATE=ds, GIT_COMMITTER_DATE=ds)
        with open(Path(repo) / "f.txt", "a") as fh:
            fh.write(f"l {i}\n")
        _git(repo, "add", ".", env=env)
        _git(repo, "commit", "-q", "-m", f"c{i}", env=env)
    r = _call(dl, "streak", {"repo": repo})
    assert r["current_streak"] == 0, r
    assert r["longest_streak"] == 3, r


# --------------------------------------------------------------------------- codeindex
def test_codeindex_dim_drift_degrades_to_fts():
    ci = _load("codeindex", "sf_codeindex")
    if ci._embedder() is None:
        print("  skip: no local embedder installed (dim-drift)")
        return
    proj = tempfile.mkdtemp()
    (Path(proj) / "a.py").write_text(
        "def alpha():\n    return 'wallpaper brightness streak'\n")
    _call(ci, "index_project", {"path": proj})
    emb = _call(ci, "embed_index", {"project": proj})
    real_dim = emb["dim"]
    assert real_dim and emb["chunks"] >= 1, emb

    # Chunks/meta are keyed by the resolved project path, so manipulate via _abs(proj).
    rproj = ci._abs(proj)
    bogus = ci._pack([0.1] * (real_dim + 5))
    ci.store.execute("UPDATE chunks SET embedding=? WHERE project=?", (bogus, rproj))
    ci.store.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (ci._embed_meta_key(rproj),
         '{"model": "stale-model", "dim": %d}' % (real_dim + 5)),
    )
    r = _call(ci, "semantic_search", {"query": "alpha", "project": proj})
    assert r.get("degraded") == "fts", r
    assert "reason" in r, r
    assert all(item["score"] is None for item in r["results"]), r


def test_codeindex_semantic_search_happy_path():
    ci = _load("codeindex", "sf_codeindex2")
    if ci._embedder() is None:
        print("  skip: no local embedder installed (happy-path)")
        return
    proj = tempfile.mkdtemp()
    (Path(proj) / "a.py").write_text(
        "def parse_config():\n    return load_yaml_settings()\n")
    _call(ci, "index_project", {"path": proj})
    _call(ci, "embed_index", {"project": proj})
    r = _call(ci, "semantic_search", {"query": "configuration parsing", "project": proj})
    assert "degraded" not in r, r
    assert r["results"], r
    assert all(isinstance(item["score"], float) for item in r["results"]), r


# --------------------------------------------------------------------------- codeedit
def test_codeedit_replace_in_file_anchor_count():
    ce = _load("codeedit", "sf_codeedit")
    d = tempfile.mkdtemp()
    p = Path(d) / "t.txt"
    p.write_text("X foo foo foo\nX foo foo foo\nX foo foo foo\n")
    r = _call(ce, "replace_in_file",
              {"path": str(p), "find": "foo", "replace": "bar", "count": 2,
               "anchor": "X", "repo": d, "validate": False})
    assert r.get("ok") is True, r
    assert r["replaced"] == 2, r
    assert p.read_text() == "X bar bar foo\nX foo foo foo\nX foo foo foo\n", p.read_text()


def test_codeedit_replace_in_file_anchor_spans_lines():
    ce = _load("codeedit", "sf_codeedit2")
    d = tempfile.mkdtemp()
    p = Path(d) / "t.txt"
    p.write_text("A foo foo\nA foo foo\nB foo\nA foo foo\n")
    r = _call(ce, "replace_in_file",
              {"path": str(p), "find": "foo", "replace": "X", "count": 3,
               "anchor": "A", "repo": d, "validate": False})
    assert r.get("ok") is True, r
    assert r["replaced"] == 3, r
    assert p.read_text() == "A X X\nA X foo\nB foo\nA foo foo\n", p.read_text()


# --------------------------------------------------------------------------- mac-control
def test_mac_set_wallpaper_bogus_path_returns_err():
    mc = _load("mac-control", "sf_mac")
    r = _call(mc, "set_wallpaper",
              {"path": "/no/such/file-12345.png", "confirm": True})
    assert isinstance(r, dict) and r.get("ok") is False, r
    assert "not found" in (r.get("err") or ""), r


def test_mac_set_wallpaper_quoted_path_no_crash():
    mc = _load("mac-control", "sf_mac2")
    r = _call(mc, "set_wallpaper",
              {"path": '/tmp/a"b\\c.png', "confirm": True})
    assert isinstance(r, dict) and r.get("ok") is False, r


def test_mac_get_brightness_graceful():
    mc = _load("mac-control", "sf_mac3")
    r = _call(mc, "get_brightness", {})
    assert isinstance(r, dict), r
    assert ("brightness" in r) or (r.get("ok") is False), r


def main() -> int:
    tests = [v for k, v in sorted(globals().items())
             if k.startswith("test_") and callable(v)]
    failed = 0
    for t in tests:
        try:
            t()
            print(f"  ok  {t.__name__}")
        except Exception as e:  # noqa: BLE001
            failed += 1
            import traceback
            print(f"  FAIL {t.__name__}: {e}")
            traceback.print_exc()
    print(f"\n{len(tests) - failed}/{len(tests)} passed")
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
