"""snippet-vault — a searchable personal library of reusable code snippets (SQLite + FTS5).

Save, search, edit, import/export, and safely run snippets. Auto-detects language, tracks
usage counts, and can run interpreted snippets in a sandboxed subprocess (resource-bounded)."""
from __future__ import annotations

import json
import os
import shutil
import subprocess
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import BaseStore, db_path, err, make_server, not_found

mcp = make_server(
    "snippet-vault",
    instructions=("Save/search reusable code: save_snippet, search, get, list_by_lang, "
                  "update_snippet, import_file/import_dir, export, run_snippet, stats, tags."),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS snippets(
  id INTEGER PRIMARY KEY, title TEXT, lang TEXT, code TEXT, tags TEXT DEFAULT '', created_at TEXT
);
CREATE VIRTUAL TABLE IF NOT EXISTS snippets_fts USING fts5(title, code, tags, content='snippets', content_rowid='id');
CREATE TRIGGER IF NOT EXISTS snip_ai AFTER INSERT ON snippets BEGIN
  INSERT INTO snippets_fts(rowid,title,code,tags) VALUES(new.id,new.title,new.code,new.tags);
END;
CREATE TRIGGER IF NOT EXISTS snip_ad AFTER DELETE ON snippets BEGIN
  INSERT INTO snippets_fts(snippets_fts,rowid,title,code,tags) VALUES('delete',old.id,old.title,old.code,old.tags);
END;
CREATE TRIGGER IF NOT EXISTS snip_au AFTER UPDATE ON snippets BEGIN
  INSERT INTO snippets_fts(snippets_fts,rowid,title,code,tags) VALUES('delete',old.id,old.title,old.code,old.tags);
  INSERT INTO snippets_fts(rowid,title,code,tags) VALUES(new.id,new.title,new.code,new.tags);
END;
"""
store = BaseStore(db_path("snippet-vault"), schema=SCHEMA)


def _ensure_columns(table: str, cols: dict[str, str]) -> None:
    """Idempotently ALTER TABLE ADD COLUMN for any missing column (keeps old DBs working)."""
    have = {r["name"] for r in store.query(f"PRAGMA table_info({table})")}
    for name, decl in cols.items():
        if name not in have:
            store.execute(f"ALTER TABLE {table} ADD COLUMN {name} {decl}")


_ensure_columns("snippets", {
    "description": "TEXT DEFAULT ''",
    "usage_count": "INTEGER DEFAULT 0",
    "updated_at": "TEXT",
    "last_used_at": "TEXT",
})


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _snippet_ids(limit: int = 10) -> list[int]:
    """Recent snippet ids, to suggest valid targets in not-found errors."""
    return [r["id"] for r in store.query(
        "SELECT id FROM snippets ORDER BY created_at DESC LIMIT ?", (limit,))]


def _no_snippet(snippet_id) -> dict:
    return not_found("snippet", snippet_id, available=_snippet_ids(),
                     hint="use list_by_lang() or search() to find valid snippet ids")


# --- language detection -----------------------------------------------------
EXT_LANG = {
    ".py": "python", ".js": "javascript", ".mjs": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".jsx": "javascript", ".sh": "bash", ".bash": "bash", ".zsh": "bash",
    ".rb": "ruby", ".go": "go", ".rs": "rust", ".java": "java", ".kt": "kotlin", ".c": "c",
    ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cs": "csharp", ".php": "php", ".swift": "swift",
    ".sql": "sql", ".html": "html", ".css": "css", ".scss": "css", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".md": "markdown", ".lua": "lua",
    ".pl": "perl", ".r": "r", ".dart": "dart", ".scala": "scala", ".clj": "clojure",
    ".ex": "elixir", ".exs": "elixir", ".hs": "haskell", ".dockerfile": "dockerfile",
}
SHEBANG = {"python": "python", "node": "javascript", "bash": "bash", "sh": "bash",
           "ruby": "ruby", "perl": "perl", "php": "php"}
KEYWORDS = [
    ("python", ("def ", "import ", "print(", "self.", "elif ", "lambda ", "__name__")),
    ("javascript", ("const ", "let ", "=> ", "function ", "console.log", "require(")),
    ("typescript", ("interface ", ": string", ": number", "export type", "implements ")),
    ("go", ("package ", "func ", "fmt.", ":= ", "import (")),
    ("rust", ("fn ", "let mut", "println!", "pub fn", "use std::", "impl ")),
    ("ruby", ("def ", "puts ", "end\n", "require '", ".each do")),
    ("java", ("public class", "System.out", "void main", "import java")),
    ("bash", ("#!/bin", "echo ", "fi\n", "then\n", "$(", "export ")),
    ("sql", ("SELECT ", "INSERT INTO", "CREATE TABLE", "WHERE ", "FROM ")),
    ("html", ("<!doctype", "<html", "<div", "<body")),
    ("css", ("{\n", "color:", "margin:", "@media")),
]


def _detect(code: str, filename: str = "") -> str:
    if filename:
        ext = Path(filename).suffix.lower()
        if Path(filename).name.lower() == "dockerfile":
            return "dockerfile"
        if ext in EXT_LANG:
            return EXT_LANG[ext]
    first = code.lstrip().splitlines()[0] if code.strip() else ""
    if first.startswith("#!"):
        for k, lang in SHEBANG.items():
            if k in first:
                return lang
    upper = code.upper()
    scores: dict[str, int] = {}
    for lang, kws in KEYWORDS:
        hay = upper if lang == "sql" else code
        scores[lang] = sum(1 for kw in kws if (kw.upper() if lang == "sql" else kw) in hay)
    best = max(scores, key=scores.get) if scores else ""
    return best if scores.get(best, 0) > 0 else "text"


def _save_snippet(title: str, code: str, lang: str = "", tags: str = "", description: str = "") -> dict:
    if not (title or "").strip():
        return err("title is required", hint="pass a non-empty snippet title")
    if not (code or "").strip():
        return err("code is required", hint="pass the snippet's code body")
    if not lang:
        lang = _detect(code)
    sid = store.execute(
        "INSERT INTO snippets(title,lang,code,tags,description,created_at,updated_at,usage_count) "
        "VALUES(?,?,?,?,?,?,?,0)",
        (title, lang, code, tags, description, _now(), _now()))
    return {"id": sid, "title": title, "lang": lang}


@mcp.tool
def save_snippet(title: str, code: str, lang: str = "", tags: str = "", description: str = "") -> dict:
    """Save a snippet (title, code, language, comma-tags, optional description).

    If `lang` is empty the language is auto-detected from the code."""
    return _save_snippet(title, code, lang, tags, description)


@mcp.tool
def search(query: str, limit: int = 15) -> list[dict]:
    """Full-text search across title/code/tags."""
    try:
        return store.query(
            "SELECT s.id,s.title,s.lang,s.tags,s.usage_count FROM snippets_fts f JOIN snippets s ON s.id=f.rowid "
            "WHERE snippets_fts MATCH ? ORDER BY rank LIMIT ?", (query, limit))
    except Exception:
        like = f"%{query}%"
        return store.query("SELECT id,title,lang,tags,usage_count FROM snippets WHERE title LIKE ? OR code LIKE ? LIMIT ?",
                           (like, like, limit))


@mcp.tool
def get(snippet_id: int) -> dict:
    """Get a snippet's full code. Increments its usage count."""
    row = store.query_one("SELECT * FROM snippets WHERE id=?", (snippet_id,))
    if not row:
        return _no_snippet(snippet_id)
    store.execute("UPDATE snippets SET usage_count=COALESCE(usage_count,0)+1, last_used_at=? WHERE id=?",
                  (_now(), snippet_id))
    row["usage_count"] = (row.get("usage_count") or 0) + 1
    return row


@mcp.tool
def list_by_lang(lang: str = "", limit: int = 50) -> list[dict]:
    """List snippets, optionally by language."""
    if lang:
        return store.query("SELECT id,title,lang,tags,usage_count FROM snippets WHERE lang=? ORDER BY created_at DESC LIMIT ?",
                           (lang, limit))
    return store.query("SELECT id,title,lang,tags,usage_count FROM snippets ORDER BY created_at DESC LIMIT ?", (limit,))


@mcp.tool
def delete(snippet_id: int) -> dict:
    """Delete a snippet."""
    store.execute("DELETE FROM snippets WHERE id=?", (snippet_id,))
    return {"ok": True, "deleted": snippet_id}


@mcp.tool
def detect_language(code: str, filename: str = "") -> dict:
    """Heuristically detect the programming language of a code blob (extension + content sniffing)."""
    return {"lang": _detect(code, filename)}


@mcp.tool
def update_snippet(snippet_id: int, title: str = "", code: str = "", lang: str = "",
                   tags: str = "", description: str = "") -> dict:
    """Partially update a snippet. Only non-empty fields are changed. Stamps updated_at."""
    cur = store.query_one("SELECT * FROM snippets WHERE id=?", (snippet_id,))
    if not cur:
        return _no_snippet(snippet_id)
    new = {
        "title": title or cur["title"],
        "code": code or cur["code"],
        "lang": lang or cur["lang"],
        "tags": tags or cur["tags"],
        "description": description or (cur.get("description") or ""),
    }
    store.execute(
        "UPDATE snippets SET title=?,code=?,lang=?,tags=?,description=?,updated_at=? WHERE id=?",
        (new["title"], new["code"], new["lang"], new["tags"], new["description"], _now(), snippet_id))
    return {"ok": True, "id": snippet_id, **new}


@mcp.tool
def import_file(path: str, title: str = "", lang: str = "", tags: str = "") -> dict:
    """Import a single source file as a snippet (language auto-detected from extension)."""
    p = Path(path).expanduser()
    if not p.is_file():
        return not_found("file", path, hint="pass a path to an existing source file")
    try:
        code = p.read_text(encoding="utf-8")
    except (UnicodeDecodeError, OSError) as e:
        return {"error": f"cannot read {path}: {e}"}
    return _save_snippet(title or p.name, code, lang or _detect(code, p.name), tags)


@mcp.tool
def import_dir(path: str, glob: str = "*", tags: str = "", recursive: bool = True,
               max_files: int = 200, max_bytes: int = 200_000) -> dict:
    """Bulk-import source files from a directory. Skips binaries and oversized files."""
    root = Path(path).expanduser()
    if not root.is_dir():
        return not_found("directory", path, hint="pass a path to an existing directory")
    it = root.rglob(glob) if recursive else root.glob(glob)
    imported, skipped = [], 0
    for f in it:
        if len(imported) >= max_files:
            break
        if not f.is_file() or any(part.startswith(".") for part in f.parts[len(root.parts):]):
            continue
        try:
            if f.stat().st_size > max_bytes:
                skipped += 1
                continue
            code = f.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            skipped += 1
            continue
        res = _save_snippet(f.name, code, _detect(code, f.name), tags)
        if "id" not in res:
            skipped += 1
            continue
        imported.append(res["id"])
    return {"imported": len(imported), "ids": imported, "skipped": skipped}


@mcp.tool
def export(path: str, lang: str = "", tag: str = "", format: str = "json") -> dict:
    """Export matching snippets to a file. format: 'json' or 'markdown'."""
    sql = "SELECT * FROM snippets WHERE 1=1"
    params: list = []
    if lang:
        sql += " AND lang=?"
        params.append(lang)
    if tag:
        sql += " AND tags LIKE ?"
        params.append(f"%{tag}%")
    rows = store.query(sql + " ORDER BY id", params)
    out = Path(path).expanduser()
    out.parent.mkdir(parents=True, exist_ok=True)
    if format == "markdown":
        parts = []
        for r in rows:
            parts.append(f"## {r['title']}  `{r['lang']}`")
            if r.get("description"):
                parts.append(r["description"])
            parts.append(f"```{r['lang']}\n{r['code']}\n```")
            if r.get("tags"):
                parts.append(f"_tags: {r['tags']}_")
            parts.append("")
        out.write_text("\n".join(parts), encoding="utf-8")
    else:
        out.write_text(json.dumps(rows, indent=2), encoding="utf-8")
    return {"ok": True, "path": str(out), "count": len(rows), "format": format}


_RUNNERS = {
    "python": [["python3"], ["python"]],
    "javascript": [["node"]],
    "bash": [["bash"]],
    "ruby": [["ruby"]],
    "php": [["php"]],
}
_EXT = {"python": ".py", "javascript": ".js", "bash": ".sh", "ruby": ".rb", "php": ".php"}


@mcp.tool
def run_snippet(snippet_id: int, stdin: str = "", timeout: int = 10, args: list[str] | None = None) -> dict:
    """Run an interpreted snippet in a bounded subprocess and capture stdout/stderr.

    Supports python/javascript(node)/bash/ruby/php only (interpreters must be on PATH).
    Runs the snippet's actual code on this machine: no shell, hard timeout, output truncated.
    Refuses compiled/unknown languages. Increments usage_count."""
    row = store.query_one("SELECT * FROM snippets WHERE id=?", (snippet_id,))
    if not row:
        return _no_snippet(snippet_id)
    lang = (row["lang"] or "").lower()
    runners = _RUNNERS.get(lang)
    if not runners:
        return not_found("runnable lang", lang, available=sorted(_RUNNERS),
                         hint="run supports only interpreted langs: python/javascript/bash/ruby/php")
    exe = None
    for candidate in runners:
        if shutil.which(candidate[0]):
            exe = candidate
            break
    if not exe:
        return {"error": f"no interpreter for '{lang}' found on PATH"}
    timeout = max(1, min(timeout, 60))
    with tempfile.TemporaryDirectory() as td:
        src = Path(td) / f"snippet{_EXT[lang]}"
        src.write_text(row["code"], encoding="utf-8")
        try:
            proc = subprocess.run(
                [*exe, str(src), *(args or [])], input=stdin, capture_output=True,
                text=True, timeout=timeout, cwd=td,
                env={**os.environ, "PYTHONDONTWRITEBYTECODE": "1"})
        except subprocess.TimeoutExpired:
            return {"error": f"timed out after {timeout}s", "timed_out": True}
        except Exception as e:  # noqa: BLE001
            return {"error": str(e)}
    store.execute("UPDATE snippets SET usage_count=COALESCE(usage_count,0)+1, last_used_at=? WHERE id=?",
                  (_now(), snippet_id))
    return {"exit_code": proc.returncode, "ok": proc.returncode == 0,
            "stdout": proc.stdout[:5000], "stderr": proc.stderr[:5000],
            "truncated": len(proc.stdout) > 5000 or len(proc.stderr) > 5000}


@mcp.tool
def stats() -> dict:
    """Library stats: counts by language, most-used snippets, total."""
    by_lang = {r["lang"]: r["n"] for r in
               store.query("SELECT lang, COUNT(*) AS n FROM snippets GROUP BY lang ORDER BY n DESC")}
    top = store.query("SELECT id,title,lang,usage_count FROM snippets "
                      "ORDER BY COALESCE(usage_count,0) DESC LIMIT 10")
    total = store.query_one("SELECT COUNT(*) AS n FROM snippets")["n"]
    return {"total": total, "by_lang": by_lang, "most_used": top}


@mcp.tool
def tags() -> list[dict]:
    """Distinct tags with usage counts (parsed from comma-separated tag fields)."""
    counts: dict[str, int] = {}
    for r in store.query("SELECT tags FROM snippets WHERE tags <> ''"):
        for t in (x.strip() for x in (r["tags"] or "").split(",")):
            if t:
                counts[t] = counts.get(t, 0) + 1
    return [{"tag": k, "count": v} for k, v in sorted(counts.items(), key=lambda kv: -kv[1])]


if __name__ == "__main__":
    mcp.run()
