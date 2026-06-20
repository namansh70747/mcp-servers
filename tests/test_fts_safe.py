"""Regression: FTS5 MATCH search tools must never raise on special-character queries.

Every tool that runs a `... MATCH ?` against user input (notes.search,
bookmark-vault.search incl. content=True, snippet-vault.search, notes.link_suggestions)
must fall back gracefully (LIKE search or []) so a query like `"`, `*`, `(`, `:`,
`a AND (` returns a list rather than crashing.
"""
import asyncio
import importlib.util
import os
import sys
import tempfile

try:
    import pytest
except ImportError:  # allow running standalone without pytest installed
    pytest = None

REPO = "/Users/namansharma/mcp-servers"
sys.path.insert(0, os.path.join(REPO, "shared"))

from fastmcp import Client  # noqa: E402

# Inputs that are invalid / dangerous FTS5 query syntax and historically crashed.
SPECIALS = [
    '"', "*", "(", ")", ":", "^", "-", "+",
    'a AND (', 'a OR', 'NEAR(', 'foo"bar', '((', 'a:b:c', '* * *', 'AND',
]


def _load(name, rel):
    os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp()
    spec = importlib.util.spec_from_file_location(name, os.path.join(REPO, rel))
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


async def _call(mod, tool, args):
    async with Client(mod.mcp) as c:
        res = await c.call_tool(tool, args)
        return res.data


def test_notes_search_special_chars():
    mod = _load("notes_fts", "servers/notes/server.py")

    async def run():
        async with Client(mod.mcp) as c:
            await c.call_tool("new_note", {"title": "Alpha", "body": "hello python coding words here foo bar"})
            await c.call_tool("new_note", {"title": "Beta", "body": "another note python stuff code many words"})
            for q in SPECIALS:
                res = await c.call_tool("search", {"query": q})
                assert isinstance(res.data, list), f"notes.search({q!r}) did not return a list"
            # link_suggestions also runs MATCH over derived terms
            res = await c.call_tool("link_suggestions", {"title": "Alpha"})
            assert isinstance(res.data, list)

    asyncio.run(run())


def test_bookmark_search_special_chars():
    mod = _load("bm_fts", "servers/bookmark-vault/server.py")

    async def run():
        async with Client(mod.mcp) as c:
            await c.call_tool("add_bookmark", {"url": "https://ex.com/a", "title": "Python guide", "fetch_title": False})
            row = mod.store.query_one("SELECT id FROM bookmarks WHERE url=?", ("https://ex.com/a",))
            mod._index_content(row["id"], "long page content about python and code words here")
            for q in SPECIALS:
                for content in (False, True):
                    res = await c.call_tool("search", {"query": q, "content": content})
                    assert isinstance(res.data, list), f"bm.search({q!r}, content={content}) did not return a list"

    asyncio.run(run())


def test_snippet_search_special_chars():
    mod = _load("sn_fts", "servers/snippet-vault/server.py")

    async def run():
        async with Client(mod.mcp) as c:
            await c.call_tool("save_snippet", {"title": "Q", "code": "def f(): return 1", "lang": "python", "tags": "util"})
            for q in SPECIALS:
                res = await c.call_tool("search", {"query": q})
                assert isinstance(res.data, list), f"snippet.search({q!r}) did not return a list"

    asyncio.run(run())


if __name__ == "__main__":
    if pytest is not None:
        sys.exit(pytest.main([__file__, "-v"]))
    test_notes_search_special_chars()
    print("notes OK")
    test_bookmark_search_special_chars()
    print("bookmark OK")
    test_snippet_search_special_chars()
    print("snippet OK")
    print("ALL GREEN")
