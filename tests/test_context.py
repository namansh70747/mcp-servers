"""Comprehensive OFFLINE tests for the "context" cluster: codeindex + project-memory.

Exercises every @mcp.tool of both servers via the FastMCP in-memory Client — happy paths plus
key edge cases (path traversal, bad git refs, empty/oversized inputs, semantic graceful-degrade
with no embedding backend installed). No network, no credentials, no model required.

Run:  VIRTUAL_ENV= /Users/namansharma/mcp-servers/.venv/bin/python tests/test_context.py
"""
import asyncio
import os
import shutil
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]

# Isolate suite data into a throwaway dir and skip dotenv BEFORE importing any server,
# so we never touch real ~/.mcp-suite/<server> data.
_TMP_DATA = tempfile.mkdtemp(prefix="context-test-data-")
os.environ["MCP_DATA_DIR"] = _TMP_DATA
os.environ["MCP_NO_DOTENV"] = "1"

from fastmcp import Client  # noqa: E402


def _make_sample_project() -> str:
    """Create a tiny multi-file Python/JS project on disk to index."""
    root = Path(tempfile.mkdtemp(prefix="context-test-proj-"))
    (root / "pkg").mkdir()
    (root / ".gitignore").write_text("ignored_dir/\n*.log\n")
    (root / "ignored_dir").mkdir()
    (root / "ignored_dir" / "secret.py").write_text("SECRET = 1\n")
    (root / "skip.log").write_text("noise\n")
    (root / "main.py").write_text(
        "import os\n"
        "from pkg.util import helper\n"
        "\n"
        "def run():  # TODO: wire up CLI\n"
        "    helper()\n"
        "    return compute(2)\n"
        "\n"
        "def compute(x):\n"
        "    return x + 1\n"
        "\n"
        "class Engine:\n"
        "    def start(self):\n"
        "        return run()\n"
    )
    (root / "pkg" / "__init__.py").write_text("")
    (root / "pkg" / "util.py").write_text(
        "def helper():\n"
        "    # FIXME: handle errors\n"
        "    return duplicate_block_a()\n"
        "\n"
        "def duplicate_block_a():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    f = 6\n"
        "    return a + b + c + d + e + f\n"
        "\n"
        "def never_called_anywhere():\n"
        "    return 99\n"
    )
    # duplicate_block_b lives in a SECOND file so duplicate_code sees it across 2 paths
    (root / "pkg" / "dup.py").write_text(
        "def duplicate_block_b():\n"
        "    a = 1\n"
        "    b = 2\n"
        "    c = 3\n"
        "    d = 4\n"
        "    e = 5\n"
        "    f = 6\n"
        "    return a + b + c + d + e + f\n"
    )
    (root / "app.js").write_text(
        "import { thing } from './lib.js'\n"
        "export function widget() { return thing() }\n"
    )
    (root / "lib.js").write_text("export function thing() { return 1 }\n")
    return str(root)


async def test_codeindex():
    sys.path.insert(0, str(ROOT / "servers" / "codeindex"))
    import server as ci  # noqa: E402
    proj = _make_sample_project()

    async with Client(ci.mcp) as c:
        tools = sorted(t.name for t in await c.list_tools())
        expected = [
            "index_project", "reindex", "project_map", "get_file", "get_lines", "set_summary",
            "file_summary", "symbols", "outline", "definition", "references", "callers",
            "call_graph", "imports", "imported_by", "import_graph", "search", "grep",
            "todo_scan", "duplicate_code", "dead_code_hints", "changed_files", "diff_since",
            "embed_index", "semantic_search", "relevant_context", "export_context_file", "status",
        ]
        for t in expected:
            assert t in tools, f"codeindex missing tool {t}"

        # ---- index_project: happy path + gitignore + binary skip ----
        r = await c.call_tool("index_project", {"path": proj})
        assert r.data["indexed"] >= 5, r.data
        gf = await c.call_tool("get_file", {"path": "ignored_dir/secret.py"})
        assert "error" in gf.data, gf.data  # gitignored
        sk = await c.call_tool("get_file", {"path": "skip.log"})
        assert "error" in sk.data  # *.log gitignored

        # ---- index_project edge cases ----
        bad = await c.call_tool("index_project", {"path": ""})
        assert "error" in bad.data
        nodir = await c.call_tool("index_project", {"path": proj + "/main.py"})
        assert "error" in nodir.data, nodir.data

        # ---- project_map / status ----
        pm = await c.call_tool("project_map", {})
        assert pm.data["total_files"] >= 5
        st = await c.call_tool("status", {})
        assert st.data["files"] >= 5 and st.data["symbols"] >= 5
        assert st.data["semantic_ready"] is False

        # ---- get_file / get_lines happy + edges ----
        gf = await c.call_tool("get_file", {"path": "main.py"})
        assert "def run()" in gf.data["content"]
        gl = await c.call_tool("get_lines", {"path": "main.py", "start": 1, "end": 2})
        assert "import os" in gl.data["content"]
        br = await c.call_tool("get_lines", {"path": "main.py", "start": 5, "end": 2})
        assert "error" in br.data
        gl0 = await c.call_tool("get_lines", {"path": "main.py", "start": -3, "end": 1})
        assert gl0.data["start"] == 1
        missing = await c.call_tool("get_lines", {"path": "nope.py", "start": 1, "end": 1})
        assert "error" in missing.data

        # ---- summaries ----
        ss = await c.call_tool("set_summary", {"path": "main.py", "summary": "entry point"})
        assert ss.data["ok"]
        fs = await c.call_tool("file_summary", {"path": "main.py"})
        assert fs.data["summary"] == "entry point" and fs.data["agent_written"]
        assert fs.data["stale"] is False
        fs2 = await c.call_tool("file_summary", {"path": "pkg/util.py"})
        assert fs2.data["agent_written"] is False
        ssm = await c.call_tool("set_summary", {"path": "ghost.py", "summary": "x"})
        assert "error" in ssm.data

        # ---- symbols / outline / definition ----
        syms = await c.call_tool("symbols", {"path": "main.py"})
        names = {s["name"] for s in syms.data}
        assert {"run", "compute", "Engine", "start"} <= names, names
        outl = await c.call_tool("outline", {"path": "main.py"})
        eng = [n for n in outl.data["outline"] if n["name"] == "Engine"]
        assert eng and any(ch["name"] == "start" for ch in eng[0]["children"]), outl.data
        d = await c.call_tool("definition", {"name": "compute"})
        assert any(x["path"] == "main.py" for x in d.data)

        # ---- references / callers / call_graph ----
        refs = await c.call_tool("references", {"name": "helper"})
        assert any(x["path"] == "main.py" for x in refs.data)
        cl = await c.call_tool("callers", {"name": "compute"})
        assert any("compute(" in x["line"] for x in cl.data)
        cg = await c.call_tool("call_graph", {"name": "run"})
        assert cg.data["symbol"] == "run"
        assert "compute" in cg.data["callees"] or "helper" in cg.data["callees"], cg.data

        # ---- imports / imported_by / import_graph ----
        imp = await c.call_tool("imports", {"path": "main.py"})
        assert "pkg/util.py" in imp.data["internal"], imp.data
        assert "os" in imp.data["external"]
        ib = await c.call_tool("imported_by", {"path": "pkg/util.py"})
        assert any(rw["importer"] == "main.py" for rw in ib.data)
        ig = await c.call_tool("import_graph", {})
        assert any(e["dst"] == "pkg/util.py" for e in ig.data["edges"]), ig.data

        # ---- search / grep ----
        srch = await c.call_tool("search", {"query": "helper"})
        assert any(x["path"] in ("main.py", "pkg/util.py") for x in srch.data)
        gp = await c.call_tool("grep", {"pattern": r"def \w+", "regex": True})
        assert len(gp.data) >= 4
        gp_bad = await c.call_tool("grep", {"pattern": "(", "regex": True})
        assert "error" in gp_bad.data[0]
        gp_empty = await c.call_tool("grep", {"pattern": ""})
        assert "error" in gp_empty.data[0]

        # ---- todo_scan ----
        td = await c.call_tool("todo_scan", {})
        assert td.data["total"] >= 2
        assert "TODO" in td.data["by_marker"] and "FIXME" in td.data["by_marker"]

        # ---- duplicate_code ----
        dup = await c.call_tool("duplicate_code", {"min_lines": 5})
        assert dup.data["clusters"], dup.data

        # ---- dead_code_hints ----
        dead = await c.call_tool("dead_code_hints", {})
        assert any(h["name"] == "never_called_anywhere" for h in dead.data["hints"]), dead.data

        # ---- git tools: not a git repo + bad-ref guard ----
        cf = await c.call_tool("changed_files", {})
        assert "error" in cf.data  # sample project is not a git repo
        badref = await c.call_tool("changed_files", {"ref": "--output=/tmp/x"})
        assert "invalid git ref" in badref.data["error"], badref.data
        badref2 = await c.call_tool("diff_since", {"ref": "; rm -rf /"})
        assert "invalid git ref" in badref2.data["error"], badref2.data

        # ---- semantic: graceful degrade (no model installed) ----
        emb = await c.call_tool("embed_index", {})
        if emb.data.get("engine") == "unavailable":
            assert "hint" in emb.data
            sem = await c.call_tool("semantic_search", {"query": "compute numbers"})
            assert sem.data["engine"] == "unavailable" and "hint" in sem.data
        else:
            sem = await c.call_tool("semantic_search", {"query": "compute numbers"})
            assert "results" in sem.data
        sem_empty = await c.call_tool("semantic_search", {"query": "  "})
        assert "error" in sem_empty.data

        # ---- relevant_context: fts + bad mode falls back + empty task ----
        rc = await c.call_tool("relevant_context", {"task": "where is compute defined"})
        assert rc.data["mode"] == "fts" and isinstance(rc.data["files"], list)
        rc_bad = await c.call_tool("relevant_context", {"task": "x", "mode": "bogus"})
        assert rc_bad.data["mode"] == "fts"
        rc_empty = await c.call_tool("relevant_context", {"task": ""})
        assert "error" in rc_empty.data

        # ---- reindex: traversal guard + single file + project re-scan ----
        trav = await c.call_tool("reindex", {"path": "/etc/passwd"})
        assert "error" in trav.data, trav.data
        (Path(proj) / "main.py").write_text((Path(proj) / "main.py").read_text() + "\n# touched\n")
        ri = await c.call_tool("reindex", {"path": str(Path(proj) / "main.py")})
        assert ri.data.get("reindexed") is True, ri.data
        ri_all = await c.call_tool("reindex", {})
        assert "changed" in ri_all.data

        # ---- export_context_file: write into the (temp) project ----
        ex = await c.call_tool("export_context_file", {})
        assert (Path(proj) / "CLAUDE.md").exists()
        assert ex.data["bytes"] > 0

    del sys.modules["server"]
    sys.path.pop(0)
    shutil.rmtree(proj, ignore_errors=True)
    print("codeindex OK —", len(expected), "tools; index/nav/search/git-guard/semantic-degrade")


async def test_project_memory():
    sys.path.insert(0, str(ROOT / "servers" / "project-memory"))
    import server as pm  # noqa: E402
    proj = "/tmp/context-test-memproj"

    async with Client(pm.mcp) as c:
        tools = sorted(t.name for t in await c.list_tools())
        expected = [
            "remember", "recall", "list_memories", "update_memory", "pin", "set_importance",
            "auto_tag", "link", "unlink", "links_of", "graph", "forget", "forget_where",
            "find_duplicates", "merge", "checkpoint", "resume", "timeline", "export_digest", "stats",
        ]
        for t in expected:
            assert t in tools, f"project-memory missing tool {t}"

        # ---- remember happy + validation ----
        m1 = await c.call_tool("remember", {
            "note": "Use SQLite FTS5 for search instead of postgres",
            "kind": "decision", "project": proj, "importance": 4, "auto_tag": True})
        assert m1.data["id"] and "sqlite" in m1.data["tags"], m1.data
        m2 = await c.call_tool("remember", {
            "note": "Always parameterize SQL queries in the api layer",
            "kind": "convention", "project": proj, "importance": 5, "pinned": True})
        assert m2.data["pinned"]
        m3 = await c.call_tool("remember", {
            "note": "TODO wire up the embedding cache", "kind": "todo", "project": proj})
        m4 = await c.call_tool("remember", {
            "note": "Use SQLite FTS5 for search rather than postgres database",
            "kind": "decision", "project": proj})
        bad = await c.call_tool("remember", {"note": "   ", "project": proj})
        assert "error" in bad.data
        ctx = await c.call_tool("remember", {"note": "loose note", "kind": "weird", "project": proj})
        assert ctx.data["kind"] == "context"

        # ---- recall: ranked, kind filter, empty guard ----
        rec = await c.call_tool("recall", {"query": "sqlite search", "project": proj})
        assert rec.data and all("score" in rw for rw in rec.data)
        rec_empty = await c.call_tool("recall", {"query": "  ", "project": proj})
        assert rec_empty.data == []
        rec_kind = await c.call_tool("recall", {"query": "sqlite", "project": proj, "kind": "decision"})
        assert all(rw["kind"] == "decision" for rw in rec_kind.data)

        # ---- list_memories / pinned_only ----
        lm = await c.call_tool("list_memories", {"project": proj})
        assert len(lm.data) >= 5
        lp = await c.call_tool("list_memories", {"project": proj, "pinned_only": True})
        assert lp.data and all(rw["pinned"] for rw in lp.data)

        # ---- update_memory / set_importance / pin / auto_tag ----
        up = await c.call_tool("update_memory", {"memory_id": m3.data["id"], "note": "updated todo text"})
        assert up.data["ok"]
        up_bad = await c.call_tool("update_memory", {"memory_id": 999999, "note": "x"})
        assert "error" in up_bad.data
        si = await c.call_tool("set_importance", {"memory_id": m3.data["id"], "importance": 9})
        assert si.data["importance"] == 5  # clamped
        pn = await c.call_tool("pin", {"memory_id": m3.data["id"], "pinned": True})
        assert pn.data["pinned"]
        at = await c.call_tool("auto_tag", {"memory_id": m1.data["id"]})
        assert at.data["ok"]

        # ---- link / links_of / graph / unlink ----
        lk = await c.call_tool("link", {"a_id": m1.data["id"], "b_id": m2.data["id"],
                                        "rel": "depends_on", "project": proj})
        assert lk.data["rel"] == "depends_on"
        lo = await c.call_tool("links_of", {"memory_id": m1.data["id"], "project": proj})
        assert any(rw["id"] == m2.data["id"] for rw in lo.data)
        g = await c.call_tool("graph", {"memory_id": m1.data["id"], "depth": 2, "project": proj})
        node_ids = {n["id"] for n in g.data["nodes"]}
        assert m1.data["id"] in node_ids and m2.data["id"] in node_ids
        assert g.data["edges"]
        ul = await c.call_tool("unlink", {"a_id": m1.data["id"], "b_id": m2.data["id"], "project": proj})
        assert ul.data["ok"]

        # ---- find_duplicates / merge ----
        dups = await c.call_tool("find_duplicates", {"project": proj, "threshold": 0.3})
        assert dups.data, dups.data
        pair = dups.data[0]
        mg = await c.call_tool("merge", {"keep_id": pair["a_id"], "drop_ids": [pair["b_id"]],
                                         "project": proj})
        assert mg.data["ok"] and mg.data["kept"] == pair["a_id"]
        gone = await c.call_tool("update_memory", {"memory_id": pair["b_id"], "note": "x"})
        assert "error" in gone.data

        # ---- checkpoint / resume / timeline ----
        ck = await c.call_tool("checkpoint", {"summary": "wired up context cluster",
                                              "open_items": "- audit tests\n- ship digest",
                                              "project": proj})
        assert ck.data["id"]
        rs = await c.call_tool("resume", {"project": proj})
        assert rs.data["last_checkpoint"]["summary"] == "wired up context cluster"
        assert rs.data["last_checkpoint"]["open_items_list"] == ["audit tests", "ship digest"]
        assert rs.data["pinned"]
        tl = await c.call_tool("timeline", {"project": proj})
        assert any(x["type"] == "checkpoint" for x in tl.data)
        assert any(x["type"] == "memory" for x in tl.data)

        # ---- stats ----
        stt = await c.call_tool("stats", {"project": proj})
        assert stt.data["checkpoints"] >= 1 and stt.data["pinned"] >= 1
        assert "by_kind" in stt.data

        # ---- export_digest: write inside project + traversal guard ----
        wp = Path(tempfile.mkdtemp(prefix="context-mem-write-"))
        ed = await c.call_tool("export_digest", {"project": str(wp), "write": False})
        assert ed.data["bytes"] > 0
        await c.call_tool("remember", {"note": "ship it", "kind": "decision", "project": str(wp)})
        edw = await c.call_tool("export_digest", {"project": str(wp), "write": True,
                                                  "target": "MEMORY.md"})
        assert (wp / "MEMORY.md").exists(), edw.data
        trav = await c.call_tool("export_digest", {"project": str(wp), "write": True,
                                                   "target": "../escape.md"})
        assert "unsafe target path" in trav.data["error"], trav.data
        abs_t = await c.call_tool("export_digest", {"project": str(wp), "write": True,
                                                    "target": "/tmp/abs-escape.md"})
        assert "unsafe target path" in abs_t.data["error"], abs_t.data
        shutil.rmtree(wp, ignore_errors=True)

        # ---- forget / forget_where guard ----
        fr = await c.call_tool("forget", {"memory_id": m3.data["id"]})
        assert fr.data["deleted"] == m3.data["id"]
        fw_noargs = await c.call_tool("forget_where", {"project": proj})
        assert "error" in fw_noargs.data  # must require a filter
        fw = await c.call_tool("forget_where", {"project": proj, "kind": "context"})
        assert fw.data["ok"]

    del sys.modules["server"]
    sys.path.pop(0)
    print("project-memory OK —", len(expected),
          "tools; remember/recall/graph/merge/resume/digest-traversal-guard")


async def main():
    await test_codeindex()
    await test_project_memory()
    print("\nALL CONTEXT CLUSTER TESTS PASSED")


if __name__ == "__main__":
    asyncio.run(main())
    shutil.rmtree(_TMP_DATA, ignore_errors=True)
