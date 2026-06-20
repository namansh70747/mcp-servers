"""Offline tests for the career cluster: deckforge, resume-forge, github-profile,
portfolio-site, blog-drafter, linkedin-optimizer.

Exercises each tool's happy path plus key edge cases (including the security
guards: path-traversal-safe filenames, input validation). Fully offline — no
network, no credentials. Network-backed tools (GitHub) are tested only for
registration + input validation, never live calls.

Run with the suite venv:
    VIRTUAL_ENV= .venv/bin/python tests/test_career.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import tempfile
from pathlib import Path

# Isolate all runtime data in a throwaway dir BEFORE importing any server.
os.environ["MCP_NO_DOTENV"] = "1"
_TMP = tempfile.mkdtemp(prefix="career-test-")
os.environ["MCP_DATA_DIR"] = _TMP

from fastmcp import Client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _clean():
    """Clean any ~/.mcp-suite/<server> dirs used by these servers (belt + suspenders;
    MCP_DATA_DIR already points at a temp dir)."""
    base = Path(_TMP)
    for name in ("deckforge", "resume-forge", "github-profile", "portfolio-site",
                 "blog-drafter", "linkedin-optimizer"):
        d = base / name
        if d.exists():
            shutil.rmtree(d, ignore_errors=True)


def _load(name: str, fname: str):
    path = ROOT / "servers" / name / "server.py"
    spec = importlib.util.spec_from_file_location(fname, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.mcp


async def _tools(mcp):
    async with Client(mcp) as c:
        return {t.name for t in await c.list_tools()}


async def _call(mcp, tool, args):
    async with Client(mcp) as c:
        return (await c.call_tool(tool, args)).data


def _under(path: str, *names: str) -> bool:
    """True if path is inside the temp data dir (i.e. not written outside via traversal)."""
    rp = Path(path).resolve()
    return str(rp).startswith(str(Path(_TMP).resolve()))


# --------------------------------------------------------------------------- deckforge
def test_deckforge():
    mcp = _load("deckforge", "deckforge_t")
    tools = asyncio.run(_tools(mcp))
    assert {"create_presentation", "add_title_slide", "add_bullet_slide", "add_two_column_slide",
            "add_comparison_slide", "add_chart_slide", "add_quote_slide", "add_code_slide",
            "add_agenda_slide", "add_timeline_slide", "add_metrics_slide", "add_section",
            "add_image_slide", "set_speaker_notes", "build_from_outline", "deck_info",
            "list_decks", "delete_deck", "save_presentation", "export_pdf"} <= tools

    # happy: create + add several slide kinds + save
    c = asyncio.run(_call(mcp, "create_presentation", {"template": "career", "theme": "midnight"}))
    did = c["deck_id"]
    assert c["theme"] == "midnight" and c["slides"] >= 1
    asyncio.run(_call(mcp, "add_bullet_slide", {"deck_id": did, "title": "B", "bullets": ["x", "y"]}))
    asyncio.run(_call(mcp, "add_chart_slide", {"deck_id": did, "title": "C", "categories": ["a", "b"],
                                               "series": [{"name": "s", "values": [1, 2]}]}))
    info = asyncio.run(_call(mcp, "deck_info", {"deck_id": did}))
    assert info["slides"] >= 3

    # edge: unknown theme falls back, not error
    c2 = asyncio.run(_call(mcp, "create_presentation", {"theme": "bogus"}))
    assert c2["theme"] == "dev_dark"

    # edge: chart with non-numeric values returns error (no raise)
    bad = asyncio.run(_call(mcp, "add_chart_slide", {"deck_id": did, "title": "C", "categories": ["a"],
                                                     "series": [{"name": "s", "values": ["oops"]}]}))
    assert "error" in bad
    # edge: unknown chart_type
    bad2 = asyncio.run(_call(mcp, "add_chart_slide", {"deck_id": did, "title": "C", "categories": ["a"],
                                                      "series": [{"name": "s", "values": [1]}],
                                                      "chart_type": "nope"}))
    assert "error" in bad2
    # edge: empty categories
    bad3 = asyncio.run(_call(mcp, "add_chart_slide", {"deck_id": did, "title": "C", "categories": [],
                                                      "series": [{"name": "s", "values": [1]}]}))
    assert "error" in bad3
    # edge: timeline/metrics require items
    assert "error" in asyncio.run(_call(mcp, "add_timeline_slide", {"deck_id": did, "title": "T", "milestones": []}))
    assert "error" in asyncio.run(_call(mcp, "add_metrics_slide", {"deck_id": did, "title": "M", "metrics": []}))
    # edge: missing image
    assert "error" in asyncio.run(_call(mcp, "add_image_slide", {"deck_id": did, "image_path": "/no/such.png"}))

    # one-shot build + save
    out = asyncio.run(_call(mcp, "build_from_outline", {"outline": [
        {"type": "title", "title": "T"},
        {"type": "metrics", "title": "M", "metrics": [{"value": "9", "label": "x"}]},
        {"type": "unknown_kind"},
    ], "filename": "_t.pptx"}))
    assert os.path.exists(out["path"]) and out["slides"] >= 2
    assert any("error" in r for r in out["results"])  # unknown kind recorded as error
    assert _under(out["path"])

    # SECURITY: filename traversal is neutralized to a basename inside OUT
    trav = asyncio.run(_call(mcp, "save_presentation", {"deck_id": did, "filename": "../../../../etc/pwn.pptx"}))
    assert _under(trav["path"]) and Path(trav["path"]).name == "pwn.pptx"
    abs_try = asyncio.run(_call(mcp, "save_presentation", {"deck_id": did, "filename": "/tmp/evil.pptx"}))
    assert _under(abs_try["path"])
    # suffix forced
    nosuf = asyncio.run(_call(mcp, "save_presentation", {"deck_id": did, "filename": "noext"}))
    assert nosuf["path"].endswith(".pptx")

    # export_pdf input validation (no LibreOffice / bad path -> ok False, never raises)
    assert asyncio.run(_call(mcp, "export_pdf", {}))["ok"] is False
    assert asyncio.run(_call(mcp, "export_pdf", {"pptx_path": "/no/such.pptx"}))["ok"] is False
    assert asyncio.run(_call(mcp, "export_pdf", {"pptx_path": "/etc/passwd"}))["ok"] is False  # wrong suffix

    # unknown deck_id raises (documented), delete works
    asyncio.run(_call(mcp, "delete_deck", {"deck_id": did}))
    decks = asyncio.run(_call(mcp, "list_decks", {}))
    assert all(d["deck_id"] != did for d in decks)


# --------------------------------------------------------------------------- resume-forge
def test_resume_forge():
    mcp = _load("resume-forge", "resume_t")
    tools = asyncio.run(_tools(mcp))
    assert {"read_profile", "keyword_gaps", "ats_score", "bullet_strength", "list_templates",
            "build_resume", "cover_letter", "export_markdown", "export_json_resume",
            "import_json_resume", "export_pdf", "list_versions"} <= tools

    s = asyncio.run(_call(mcp, "ats_score", {"jd": "Python Django Docker AWS testing",
                                             "resume_text": "Built Python Django apps in Docker"}))
    assert 0 <= s["score"] <= 100 and "matched_keywords" in s
    # edge: empty JD -> graceful
    s0 = asyncio.run(_call(mcp, "ats_score", {"jd": "", "resume_text": "x"}))
    assert s0["score"] == 0

    kg = asyncio.run(_call(mcp, "keyword_gaps", {"jd": "Kubernetes Terraform Go microservices"}))
    assert "possible_gaps" in kg and "jd_terms_ranked" in kg

    bs = asyncio.run(_call(mcp, "bullet_strength", {"bullets": [
        "Responsible for various things", "Built API reducing latency by 40%"]}))
    assert bs["bullets"][0]["score"] < bs["bullets"][1]["score"]

    r = asyncio.run(_call(mcp, "build_resume", {"filename": "_t.docx", "template": "modern"}))
    assert os.path.exists(r["path"]) and _under(r["path"])
    # unknown template falls back
    r2 = asyncio.run(_call(mcp, "build_resume", {"filename": "_t2.docx", "template": "bogus"}))
    assert r2["template"] == "classic"

    cl = asyncio.run(_call(mcp, "cover_letter", {"company": "Acme", "role": "Eng",
                                                 "body": "Para one.\n\nPara two."}))
    assert os.path.exists(cl["path"]) and _under(cl["path"])

    md = asyncio.run(_call(mcp, "export_markdown", {"filename": "_t.md"}))
    assert os.path.exists(md["path"]) and md["markdown"].startswith("#")

    jr = asyncio.run(_call(mcp, "export_json_resume", {"filename": "_t.json"}))
    assert os.path.exists(jr["path"]) and "basics" in jr["resume"]

    # round-trip: import the just-exported json resume (raw string)
    import json as _json
    raw = _json.dumps(jr["resume"])
    imp = asyncio.run(_call(mcp, "import_json_resume", {"path_or_json": raw}))
    assert "profile" in imp and "name" in imp["profile"]
    # edge: invalid JSON
    assert "error" in asyncio.run(_call(mcp, "import_json_resume", {"path_or_json": "{bad"}))
    # edge: empty
    assert "error" in asyncio.run(_call(mcp, "import_json_resume", {"path_or_json": "   "}))
    # edge: JSON that isn't an object
    assert "error" in asyncio.run(_call(mcp, "import_json_resume", {"path_or_json": "[1,2,3]"}))

    # SECURITY: filename traversal neutralized to basename inside OUT
    trav = asyncio.run(_call(mcp, "build_resume", {"filename": "../../../../tmp/pwn.docx"}))
    assert _under(trav["path"]) and Path(trav["path"]).name == "pwn.docx"

    # export_pdf validation (no LibreOffice/bad path -> ok False, never raises)
    assert asyncio.run(_call(mcp, "export_pdf", {"docx_path": "/no/such.docx"}))["ok"] is False
    assert asyncio.run(_call(mcp, "export_pdf", {"docx_path": "/etc/passwd"}))["ok"] is False

    versions = asyncio.run(_call(mcp, "list_versions", {}))
    assert any(p.endswith("_t.docx") for p in versions)


# --------------------------------------------------------------------------- github-profile
def test_github_profile():
    mcp = _load("github-profile", "ghp_t")
    tools = asyncio.run(_tools(mcp))
    assert {"build_readme", "suggest_pins", "describe_repo", "language_stats",
            "contribution_summary", "profile_stats", "repo_readme"} <= tools

    # SECURITY: input validation rejects malformed usernames/repos WITHOUT a network call.
    for tool, args, key in (
        ("suggest_pins", {"username": "../etc"}, "__error__"),
        ("language_stats", {"username": "bad user name"}, "__error__"),
        ("contribution_summary", {"username": "a/b"}, "__error__"),
        ("profile_stats", {"username": "x" * 40}, "__error__"),
        ("describe_repo", {"full_name": "no-slash"}, "__error__"),
        ("repo_readme", {"full_name": "../../etc/passwd"}, "__error__"),
        ("build_readme", {"username": "bad name", "write": False}, "__error__"),
    ):
        res = asyncio.run(_call(mcp, tool, args))
        assert key in res, f"{tool} should reject invalid input offline, got {res}"


# --------------------------------------------------------------------------- portfolio-site
def test_portfolio_site():
    mcp = _load("portfolio-site", "ps_t")
    tools = asyncio.run(_tools(mcp))
    assert {"build_site", "list_themes", "deploy_instructions", "projects_from_github"} <= tools

    themes = asyncio.run(_call(mcp, "list_themes", {}))
    assert "nord" in themes and "dark" in themes

    r = asyncio.run(_call(mcp, "build_site", {"theme": "nord"}))
    html = Path(r["path"]).read_text()
    assert "og:title" in html and "application/ld+json" in html
    assert _under(r["path"])
    # unknown theme falls back to dark (no error)
    r2 = asyncio.run(_call(mcp, "build_site", {"theme": "bogus"}))
    assert os.path.exists(r2["path"])

    # SECURITY: autoescape on — a profile field with HTML would be escaped, but at minimum
    # the rendered page must be well-formed and contain no unescaped <script> beyond the
    # single JSON-LD block we explicitly mark safe.
    assert html.count("<script") == html.count("application/ld+json")

    # projects_from_github input validation (offline, must not hit network)
    bad = asyncio.run(_call(mcp, "projects_from_github", {"username": "bad name!!"}))
    assert "error" in bad

    dep = asyncio.run(_call(mcp, "deploy_instructions", {"repo": "https://github.com/me/site.git"}))
    assert dep["steps"] and any("git" in s for s in dep["steps"])


# --------------------------------------------------------------------------- blog-drafter
def test_blog_drafter():
    mcp = _load("blog-drafter", "bd_t")
    tools = asyncio.run(_tools(mcp))
    assert {"outline", "outline_to_draft", "new_draft", "save_draft", "set_meta", "list_drafts",
            "get", "export_md", "export_platform", "seo_check", "list_series",
            "export_md_all"} <= tools

    o = asyncio.run(_call(mcp, "outline", {"topic": "x", "kind": "tutorial"}))
    assert o["outline"] and "target_words" in o

    d = asyncio.run(_call(mcp, "outline_to_draft", {"topic": "my topic", "kind": "blog"}))
    did = d["id"]
    assert did and "## TL;DR" in d["body"]

    asyncio.run(_call(mcp, "save_draft", {"draft_id": did, "body": "# Heading\n\nSome body text "
                                          "with enough words to be interesting and meaningful here. "
                                          "```code```", "status": "ready"}))
    asyncio.run(_call(mcp, "set_meta", {"draft_id": did, "tags": ["a", "b"], "series": "S",
                                        "series_part": 1, "canonical_url": "https://x/y"}))
    full = asyncio.run(_call(mcp, "get", {"draft_id": did}))
    assert full["status"] == "ready" and full["series"] == "S"

    # set_meta on missing draft
    assert "error" in asyncio.run(_call(mcp, "set_meta", {"draft_id": 999999, "tags": ["z"]}))

    md = asyncio.run(_call(mcp, "export_md", {"draft_id": did, "with_frontmatter": True}))
    assert os.path.exists(md["path"]) and _under(md["path"])

    for plat in ("devto", "hashnode", "medium", "linkedin"):
        r = asyncio.run(_call(mcp, "export_platform", {"draft_id": did, "platform": plat}))
        assert os.path.exists(r["path"]) and _under(r["path"])
    # unknown platform
    assert "error" in asyncio.run(_call(mcp, "export_platform", {"draft_id": did, "platform": "tiktok"}))
    # missing draft
    assert "error" in asyncio.run(_call(mcp, "export_platform", {"draft_id": 999999, "platform": "medium"}))

    # SECURITY: a title with path separators must NOT escape the output dir.
    nd = asyncio.run(_call(mcp, "new_draft", {"title": "../../../etc/evil", "body": "hi"}))
    em = asyncio.run(_call(mcp, "export_md", {"draft_id": nd["id"]}))
    assert _under(em["path"]) and "/etc/" not in str(Path(em["path"]).name)

    seo = asyncio.run(_call(mcp, "seo_check", {"draft_id": did}))
    assert "score" in seo and 0 <= seo["score"] <= 100

    series = asyncio.run(_call(mcp, "list_series", {}))
    assert any(g["series"] == "S" for g in series)

    allmd = asyncio.run(_call(mcp, "export_md_all", {}))
    assert allmd["count"] >= 2


# --------------------------------------------------------------------------- linkedin-optimizer
def test_linkedin_optimizer():
    mcp = _load("linkedin-optimizer", "lo_t")
    tools = asyncio.run(_tools(mcp))
    assert {"headline_variants", "about_section", "experience_bullets", "optimize_text",
            "keyword_audit", "list_target_roles"} <= tools

    hv = asyncio.run(_call(mcp, "headline_variants", {"role": "Backend Engineer",
                                                      "skills": ["Go", "Postgres"]}))
    assert hv["variants"] and all("within_limit" in v for v in hv["variants"])

    ab = asyncio.run(_call(mcp, "about_section", {}))
    assert "about" in ab and ab["chars"] <= ab["limit"]

    eb = asyncio.run(_call(mcp, "experience_bullets", {"role": "Eng", "company": "Acme",
                          "raw": "responsible for backend\nbuilt API serving 2M requests"}))
    assert len(eb["bullets"]) == 2 and eb["bullets"][1]["has_metric"]

    opt = asyncio.run(_call(mcp, "optimize_text", {"text": "I am a passionate rockstar ninja who "
                            "leverages synergy.", "kind": "about"}))
    assert opt["buzzwords"] and opt["score"] < 100

    a = asyncio.run(_call(mcp, "keyword_audit", {"text": "Python REST Docker", "target_role": "backend"}))
    assert 0 <= a["coverage_pct"] <= 100 and a["matched"]
    # fuzzy match path: "backend developer" -> backend
    a2 = asyncio.run(_call(mcp, "keyword_audit", {"text": "Python", "target_role": "backend developer"}))
    assert a2.get("target_role") == "backend"
    # unknown role -> error with available list
    a3 = asyncio.run(_call(mcp, "keyword_audit", {"text": "x", "target_role": "astronaut"}))
    assert "error" in a3 and "available_roles" in a3

    roles = asyncio.run(_call(mcp, "list_target_roles", {}))
    assert "backend" in roles


if __name__ == "__main__":
    _clean()
    for fn in (test_deckforge, test_resume_forge, test_github_profile, test_portfolio_site,
               test_blog_drafter, test_linkedin_optimizer):
        fn()
        print(fn.__name__, "OK")
    print("ALL CAREER TESTS PASSED")
