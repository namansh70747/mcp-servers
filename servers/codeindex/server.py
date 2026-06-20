"""codeindex — deep, always-fresh understanding of a whole project so any agent can recall
it precisely and cheaply.

Stores every file and every line (retrievable), a cached per-file summary, an AST/tree-sitter
symbol graph, an import & call graph, and FTS search. `relevant_context(task)` returns a curated
bundle sized to the window (FTS, optional LOCAL semantic, or hybrid). `export_context_file()`
writes a digest into each client's auto-load file (Claude, Copilot, Cursor, Windsurf, Zed) so
every agent starts a session already familiar with the project.

Optional FREE accelerators are lazy-imported with graceful fallbacks:
  - pathspec            → correct .gitignore handling (else a stdlib glob matcher)
  - tree_sitter(+langs) → multi-language symbols (else per-language regex)
  - model2vec (light, no torch) OR sentence-transformers → local semantic search (else FTS only)
Summaries are produced by the calling agent (set_summary) and cached → zero extra API cost.
"""
from __future__ import annotations

import ast
import hashlib
import math
import os
import re
import struct
import subprocess
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import BaseStore, db_path, make_server

mcp = make_server(
    "codeindex",
    instructions=(
        "Project code index. Call index_project(path) once, then relevant_context(task) to pull "
        "the right files/symbols, get_lines for exact content, references/definition/outline/imports "
        "for navigation. todo_scan/duplicate_code/dead_code_hints surface issues; diff_since(ref) shows "
        "what changed. Run export_context_file() to prime every client. Re-run reindex() after edits."
    ),
)

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
  project TEXT NOT NULL, path TEXT NOT NULL, lang TEXT, size INTEGER, mtime REAL,
  hash TEXT, lines INTEGER, content TEXT,
  summary TEXT DEFAULT '', summary_for_hash TEXT DEFAULT '', indexed_at TEXT,
  PRIMARY KEY (project, path)
);
CREATE TABLE IF NOT EXISTS symbols(
  project TEXT NOT NULL, path TEXT NOT NULL, name TEXT, kind TEXT, lineno INTEGER,
  end_lineno INTEGER
);
CREATE INDEX IF NOT EXISTS idx_sym_name ON symbols(project, name);
CREATE INDEX IF NOT EXISTS idx_sym_path ON symbols(project, path);
CREATE TABLE IF NOT EXISTS imports(
  project TEXT NOT NULL, path TEXT NOT NULL, target TEXT, resolved TEXT,
  kind TEXT, lineno INTEGER
);
CREATE INDEX IF NOT EXISTS idx_imp_path ON imports(project, path);
CREATE INDEX IF NOT EXISTS idx_imp_res ON imports(project, resolved);
CREATE TABLE IF NOT EXISTS chunks(
  project TEXT NOT NULL, path TEXT NOT NULL, start INTEGER, end INTEGER,
  text TEXT, embedding BLOB
);
CREATE INDEX IF NOT EXISTS idx_chunk_path ON chunks(project, path);
CREATE VIRTUAL TABLE IF NOT EXISTS files_fts
  USING fts5(content, summary, path UNINDEXED, project UNINDEXED);
CREATE TABLE IF NOT EXISTS meta(key TEXT PRIMARY KEY, value TEXT);
"""

store = BaseStore(db_path("codeindex"), schema=SCHEMA)

# Defensive migration for indexes upgrading from the older schema (no end_lineno column).
for _alter in ("ALTER TABLE symbols ADD COLUMN end_lineno INTEGER",
               "ALTER TABLE imports ADD COLUMN resolved TEXT"):
    try:
        store.execute(_alter)
    except Exception:
        pass

IGNORE_DIRS = {
    ".git", "node_modules", ".venv", "venv", "__pycache__", "dist", "build", ".next",
    "target", ".idea", ".pytest_cache", ".mypy_cache", ".ruff_cache", ".cache", "out",
}
BINARY_EXT = {
    ".png", ".jpg", ".jpeg", ".gif", ".webp", ".ico", ".pdf", ".zip", ".gz", ".tar",
    ".woff", ".woff2", ".ttf", ".otf", ".mp4", ".mov", ".mp3", ".wav", ".so", ".dylib",
    ".o", ".a", ".bin", ".db", ".sqlite", ".pyc", ".jar", ".class", ".exe", ".wasm",
}
LANGS = {
    ".py": "python", ".js": "javascript", ".jsx": "javascript", ".ts": "typescript",
    ".tsx": "typescript", ".go": "go", ".rs": "rust", ".java": "java", ".rb": "ruby",
    ".c": "c", ".h": "c", ".cpp": "cpp", ".cc": "cpp", ".cs": "csharp", ".php": "php",
    ".swift": "swift", ".kt": "kotlin", ".sh": "shell", ".md": "markdown", ".json": "json",
    ".yaml": "yaml", ".yml": "yaml", ".toml": "toml", ".html": "html", ".css": "css", ".sql": "sql",
}
GENERIC_SYM = re.compile(
    r"^\s*(?:export\s+)?(?:async\s+)?(?:function|class|def|func|interface|type|struct|enum)\s+([A-Za-z_]\w*)"
    r"|^\s*(?:export\s+)?(?:const|let|var)\s+([A-Za-z_]\w*)\s*=",
)
TODO_MARKERS = ("TODO", "FIXME", "HACK", "XXX", "BUG", "OPTIMIZE", "REFACTOR")
EMBED_MODEL = "all-MiniLM-L6-v2"


def _abs(p: str) -> str:
    return str(Path(p).expanduser().resolve())


def _safe_rel(proj: str, rel: str) -> str | None:
    """Resolve a project-relative path and confirm it stays inside the project root.

    Returns the normalized relative path, or None if it escapes (traversal) or is absolute.
    Used before any on-disk read keyed by a caller-supplied path.
    """
    if not isinstance(rel, str) or not rel.strip():
        return None
    root = Path(proj).resolve()
    try:
        full = (root / rel).resolve()
        full.relative_to(root)
    except (ValueError, OSError):
        return None
    return str(full.relative_to(root))


_VALID_REF = re.compile(r"^[\w][\w./@~^{}-]*$")


def _safe_ref(ref: str) -> str | None:
    """Validate a git ref/revision so it can never be read as an option flag or shell payload.

    Rejects refs starting with '-' (argument injection), whitespace, and anything outside a
    conservative revision character set. Returns the ref or None.
    """
    if not isinstance(ref, str):
        return None
    ref = ref.strip()
    if not ref or ref.startswith("-") or len(ref) > 200:
        return None
    return ref if _VALID_REF.match(ref) else None


def _last_project() -> str | None:
    row = store.query_one("SELECT value FROM meta WHERE key='last_project'")
    return row["value"] if row else None


def _proj(p: str | None) -> str:
    proj = _abs(p) if p else _last_project()
    if not proj:
        raise RuntimeError("No project indexed yet — call index_project(path) first.")
    return proj


def _is_ignored(rel_parts: tuple[str, ...]) -> bool:
    return any(part in IGNORE_DIRS for part in rel_parts)


# ---------------------------------------------------------------- gitignore

def _load_ignore_spec(root: str):
    """Return a callable(rel_path, is_dir) -> bool using .gitignore rules.

    Prefers `pathspec` (correct gitwildmatch); falls back to a small stdlib matcher
    that understands the common subset (comments, negation, trailing/leading slash, *, **).
    Returns None if there is no .gitignore.
    """
    patterns: list[str] = []
    gi = Path(root) / ".gitignore"
    if gi.exists():
        try:
            patterns = gi.read_text(encoding="utf-8", errors="ignore").splitlines()
        except OSError:
            patterns = []
    if not patterns:
        return None
    try:
        import pathspec  # type: ignore
        spec = pathspec.PathSpec.from_lines("gitwildmatch", patterns)
        return lambda rel, is_dir=False: spec.match_file(rel + ("/" if is_dir else ""))
    except Exception:
        return _StdlibIgnore(patterns)


class _StdlibIgnore:
    """Minimal gitignore matcher (fallback when pathspec is unavailable)."""

    def __init__(self, lines: list[str]):
        self.rules: list[tuple[bool, re.Pattern]] = []
        for raw in lines:
            line = raw.rstrip("\n")
            if not line.strip() or line.lstrip().startswith("#"):
                continue
            neg = line.startswith("!")
            if neg:
                line = line[1:]
            line = line.strip()
            if not line:
                continue
            self.rules.append((neg, re.compile(self._to_regex(line))))

    @staticmethod
    def _to_regex(pat: str) -> str:
        anchored = pat.startswith("/")
        pat = pat.lstrip("/").rstrip("/")
        out = "^" if anchored else r"(?:^|/)"
        i = 0
        while i < len(pat):
            c = pat[i]
            if c == "*":
                if pat[i:i + 2] == "**":
                    out += ".*"
                    i += 2
                    continue
                out += "[^/]*"
            elif c == "?":
                out += "[^/]"
            elif c == ".":
                out += r"\."
            else:
                out += re.escape(c)
            i += 1
        out += r"(?:/.*)?$"
        return out

    def __call__(self, rel: str, is_dir: bool = False) -> bool:
        matched = False
        for neg, pat in self.rules:
            if pat.search(rel):
                matched = not neg
        return matched


# ---------------------------------------------------------------- symbols

def _ts_parser(lang: str):
    """Lazy tree-sitter parser for a language, or None."""
    try:
        # Prefer tree-sitter-language-pack (cp313 wheels); fall back to tree-sitter-languages.
        try:
            from tree_sitter_language_pack import get_parser  # type: ignore
        except Exception:
            from tree_sitter_languages import get_parser  # type: ignore
    except Exception:
        return None
    ts_lang = {"javascript": "javascript", "typescript": "typescript", "tsx": "tsx",
               "go": "go", "rust": "rust", "java": "java", "ruby": "ruby",
               "c": "c", "cpp": "cpp", "csharp": "c_sharp", "php": "php"}.get(lang)
    if not ts_lang:
        return None
    try:
        return get_parser(ts_lang)
    except Exception:
        return None


_TS_KINDS = {
    "function_declaration": "function", "function_definition": "function",
    "method_declaration": "method", "method_definition": "method",
    "class_declaration": "class", "class_definition": "class", "class_specifier": "class",
    "interface_declaration": "interface", "type_alias_declaration": "type",
    "struct_item": "struct", "struct_specifier": "struct", "enum_item": "enum",
    "enum_declaration": "enum", "enum_specifier": "enum", "trait_item": "trait",
    "function_item": "function", "impl_item": "impl", "type_declaration": "type",
    "type_spec": "type", "module": "module",
}


def _extract_symbols_ts(lang: str, content: str) -> list[tuple[str, str, int, int | None]]:
    parser = _ts_parser(lang)
    if not parser:
        return []
    try:
        tree = parser.parse(content.encode("utf-8"))
    except Exception:
        return []
    out: list[tuple[str, str, int, int | None]] = []

    def name_of(node):
        for child in node.children:
            if child.type in ("identifier", "type_identifier", "field_identifier",
                              "constant", "name"):
                return content[child.start_byte:child.end_byte]
        n = node.child_by_field_name("name") if hasattr(node, "child_by_field_name") else None
        if n is not None:
            return content[n.start_byte:n.end_byte]
        return None

    def walk(node):
        kind = _TS_KINDS.get(node.type)
        if kind:
            nm = name_of(node)
            if nm:
                out.append((nm, kind, node.start_point[0] + 1, node.end_point[0] + 1))
        for c in node.children:
            walk(c)

    walk(tree.root_node)
    return out


def _extract_symbols(path: str, lang: str, content: str) -> list[tuple[str, str, int, int | None]]:
    if lang == "python":
        try:
            tree = ast.parse(content)
            out: list[tuple[str, str, int, int | None]] = []
            for node in ast.walk(tree):
                if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                    out.append((node.name, "function", node.lineno,
                                getattr(node, "end_lineno", None)))
                elif isinstance(node, ast.ClassDef):
                    out.append((node.name, "class", node.lineno,
                                getattr(node, "end_lineno", None)))
            return out
        except SyntaxError:
            pass
    ts = _extract_symbols_ts(lang, content)
    if ts:
        return ts
    out2: list[tuple[str, str, int, int | None]] = []
    for i, line in enumerate(content.splitlines(), 1):
        m = GENERIC_SYM.match(line)
        if m:
            out2.append((m.group(1) or m.group(2), "symbol", i, None))
    return out2


# ---------------------------------------------------------------- imports

_JS_IMPORT = re.compile(r"""(?:import\s[^'"]*from\s*['"]([^'"]+)['"]|require\(\s*['"]([^'"]+)['"]\s*\)|import\(\s*['"]([^'"]+)['"]\s*\))""")
_GO_IMPORT = re.compile(r'^\s*(?:import\s+)?"([^"]+)"')
_RUST_USE = re.compile(r"^\s*use\s+([A-Za-z_][\w:]+)")
_JAVA_IMPORT = re.compile(r"^\s*import\s+(?:static\s+)?([\w.]+)")


def _resolve_import(proj: str, src_rel: str, target: str, lang: str,
                    indexed: set[str]) -> str | None:
    """Best-effort map an import string to an indexed file path."""
    src_dir = str(Path(src_rel).parent)
    if lang == "python":
        if target.startswith("."):  # relative
            base = Path(src_rel).parent
            up = len(target) - len(target.lstrip("."))
            for _ in range(max(0, up - 1)):
                base = base.parent
            mod = target.lstrip(".").replace(".", "/")
            cand = str((base / mod)) if mod else str(base)
        else:
            cand = target.replace(".", "/")
        for suffix in (".py", "/__init__.py"):
            p = (cand + suffix).lstrip("./")
            if p in indexed:
                return p
        return None
    if lang in ("javascript", "typescript"):
        if not target.startswith("."):
            return None
        base = Path(src_dir) / target
        for suffix in (".ts", ".tsx", ".js", ".jsx", "/index.ts", "/index.js"):
            p = str(base).lstrip("./") + suffix if not str(base).endswith(suffix) else str(base)
            p = os.path.normpath(str(Path(src_dir) / (target + suffix))).lstrip("./")
            if p in indexed:
                return p
        return None
    return None


def _extract_imports(rel: str, lang: str, content: str) -> list[tuple[str, str, int]]:
    """Return (target, kind, lineno)."""
    out: list[tuple[str, str, int]] = []
    if lang == "python":
        try:
            tree = ast.parse(content)
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    for n in node.names:
                        out.append((n.name, "import", node.lineno))
                elif isinstance(node, ast.ImportFrom):
                    mod = ("." * (node.level or 0)) + (node.module or "")
                    out.append((mod, "from", node.lineno))
            return out
        except SyntaxError:
            pass
    if lang in ("javascript", "typescript"):
        for i, line in enumerate(content.splitlines(), 1):
            for m in _JS_IMPORT.finditer(line):
                tgt = m.group(1) or m.group(2) or m.group(3)
                if tgt:
                    out.append((tgt, "import", i))
        return out
    if lang == "go":
        for i, line in enumerate(content.splitlines(), 1):
            m = _GO_IMPORT.search(line)
            if m and "/" in m.group(1):
                out.append((m.group(1), "import", i))
        return out
    if lang == "rust":
        for i, line in enumerate(content.splitlines(), 1):
            m = _RUST_USE.match(line)
            if m:
                out.append((m.group(1), "use", i))
        return out
    if lang == "java":
        for i, line in enumerate(content.splitlines(), 1):
            m = _JAVA_IMPORT.match(line)
            if m:
                out.append((m.group(1), "import", i))
    return out


def _heuristic_summary(lang: str, content: str) -> str:
    for line in content.splitlines():
        s = line.strip()
        if not s:
            continue
        if lang == "python" and (s.startswith('"""') or s.startswith("'''")):
            return s.strip("\"'").strip()[:160]
        if s.startswith(("#", "//", "/*", "*", "<!--")):
            continue
        return s[:160]
    return ""


def _sanitize_fts(query: str) -> str:
    """Make an arbitrary string safe-ish for FTS5 MATCH by quoting bare terms."""
    toks = re.findall(r'"[^"]*"|\S+', query)
    safe = []
    for t in toks:
        if t.startswith('"') and t.endswith('"'):
            safe.append(t)
        elif re.fullmatch(r"[A-Za-z0-9_]+", t):
            safe.append(t)
        else:
            safe.append('"' + t.replace('"', "") + '"')
    return " ".join(safe) or query


# ---------------------------------------------------------------- indexing

def _index_one_record(proj: str, rel: str, content: str, st_size: int, st_mtime: float,
                      now: str, indexed_paths: set[str]) -> None:
    lang = LANGS.get(Path(rel).suffix.lower(), "text")
    h = hashlib.sha1(content.encode()).hexdigest()
    store.execute(
        "INSERT INTO files(project,path,lang,size,mtime,hash,lines,content,indexed_at) "
        "VALUES(?,?,?,?,?,?,?,?,?) "
        "ON CONFLICT(project,path) DO UPDATE SET "
        "lang=excluded.lang,size=excluded.size,mtime=excluded.mtime,hash=excluded.hash,"
        "lines=excluded.lines,content=excluded.content,indexed_at=excluded.indexed_at",
        (proj, rel, lang, st_size, st_mtime, h, content.count("\n") + 1, content, now),
    )
    store.execute("DELETE FROM symbols WHERE project=? AND path=?", (proj, rel))
    syms = _extract_symbols(rel, lang, content)
    if syms:
        store.executemany(
            "INSERT INTO symbols(project,path,name,kind,lineno,end_lineno) VALUES(?,?,?,?,?,?)",
            [(proj, rel, n, k, ln, el) for (n, k, ln, el) in syms],
        )
    store.execute("DELETE FROM imports WHERE project=? AND path=?", (proj, rel))
    imps = _extract_imports(rel, lang, content)
    if imps:
        store.executemany(
            "INSERT INTO imports(project,path,target,resolved,kind,lineno) VALUES(?,?,?,?,?,?)",
            [(proj, rel, tgt, _resolve_import(proj, rel, tgt, lang, indexed_paths), k, ln)
             for (tgt, k, ln) in imps],
        )
    store.execute("DELETE FROM files_fts WHERE project=? AND path=?", (proj, rel))
    store.execute(
        "INSERT INTO files_fts(content,summary,path,project) VALUES(?,?,?,?)",
        (content, "", rel, proj),
    )


@mcp.tool
def index_project(path: str, max_file_kb: int = 512, respect_gitignore: bool = True) -> dict:
    """Index a whole project: store every file's content + symbols + imports. Sets it as the
    active project. With respect_gitignore=True, .gitignore patterns are honored on top of the
    built-in ignore set. One bad file never aborts the scan."""
    if not isinstance(path, str) or not path.strip():
        return {"error": "path must be a non-empty string"}
    root = _abs(path)
    if not Path(root).is_dir():
        return {"error": f"not a directory: {path}"}
    max_file_kb = max(1, min(int(max_file_kb), 65536))
    indexed = skipped = errors = 0
    skipped_paths: list[str] = []
    now = datetime.now(timezone.utc).isoformat()
    ignore = _load_ignore_spec(root) if respect_gitignore else None

    # Pass 1: collect file list (so import resolution can know what's indexed).
    pending: list[tuple[str, str, int, float]] = []
    for dirpath, dirnames, filenames in os.walk(root):
        kept = []
        for d in dirnames:
            if d in IGNORE_DIRS or d.startswith("."):
                continue
            rel_d = str(Path(dirpath).joinpath(d).relative_to(root))
            if ignore and ignore(rel_d, True):
                continue
            kept.append(d)
        dirnames[:] = kept
        for fn in filenames:
            full = Path(dirpath) / fn
            rel = str(full.relative_to(root))
            ext = full.suffix.lower()
            if ext in BINARY_EXT or fn.startswith("."):
                continue
            if ignore and ignore(rel, False):
                continue
            try:
                stt = full.stat()
            except OSError:
                continue
            if stt.st_size > max_file_kb * 1024:
                skipped += 1
                skipped_paths.append(rel)
                continue
            pending.append((str(full), rel, stt.st_size, stt.st_mtime))

    indexed_paths = {rel for (_f, rel, _s, _m) in pending}
    for full, rel, size, mtime in pending:
        try:
            content = Path(full).read_text(encoding="utf-8", errors="strict")
        except (UnicodeDecodeError, OSError):
            skipped += 1
            continue
        try:
            _index_one_record(root, rel, content, size, mtime, now, indexed_paths)
            indexed += 1
        except Exception:
            errors += 1

    store.execute(
        "INSERT INTO meta(key,value) VALUES('last_project',?) "
        "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (root,),
    )
    return {"project": root, "indexed": indexed, "skipped": skipped, "errors": errors,
            "skipped_examples": skipped_paths[:5]}


@mcp.tool
def reindex(path: str | None = None, project: str | None = None) -> dict:
    """Incrementally refresh the index. With `path` (a single file) update just it; otherwise
    re-scan the project and update only files whose size/mtime changed (cheap freshness)."""
    proj = _proj(project)
    if path:
        index_one = _abs(path)
        try:
            rel = str(Path(index_one).relative_to(proj))
        except ValueError:
            return {"error": f"path is outside the active project: {path}"}
        if _safe_rel(proj, rel) is None:
            return {"error": f"unsafe path: {path}"}
        return _reindex_file(proj, rel)
    changed = 0
    for row in store.query("SELECT path, size, mtime FROM files WHERE project=?", (proj,)):
        full = Path(proj) / row["path"]
        if not full.exists():
            store.execute("DELETE FROM files WHERE project=? AND path=?", (proj, row["path"]))
            store.execute("DELETE FROM symbols WHERE project=? AND path=?", (proj, row["path"]))
            store.execute("DELETE FROM imports WHERE project=? AND path=?", (proj, row["path"]))
            store.execute("DELETE FROM files_fts WHERE project=? AND path=?", (proj, row["path"]))
            continue
        st = full.stat()
        if st.st_size != row["size"] or st.st_mtime != row["mtime"]:
            _reindex_file(proj, row["path"])
            changed += 1
    return {"project": proj, "changed": changed}


def _reindex_file(proj: str, rel: str) -> dict:
    if _safe_rel(proj, rel) is None:
        return {"error": f"unsafe path: {rel}"}
    full = Path(proj) / rel
    try:
        if full.stat().st_size > 8 * 1024 * 1024:  # cap on-disk read at 8MB
            return {"error": f"file too large to reindex: {rel}"}
    except OSError:
        return {"error": f"cannot stat: {rel}"}
    content = full.read_text(encoding="utf-8", errors="ignore")
    indexed_paths = {r["path"] for r in store.query(
        "SELECT path FROM files WHERE project=?", (proj,))}
    indexed_paths.add(rel)
    _index_one_record(proj, rel, content, full.stat().st_size, full.stat().st_mtime,
                      datetime.now(timezone.utc).isoformat(), indexed_paths)
    return {"project": proj, "path": rel, "reindexed": True}


@mcp.tool
def project_map(project: str | None = None, limit: int = 400) -> dict:
    """Tree of files with a one-line summary (cached or heuristic) + symbol count each."""
    proj = _proj(project)
    rows = store.query(
        "SELECT path, lang, lines, summary, content FROM files WHERE project=? ORDER BY path LIMIT ?",
        (proj, limit),
    )
    counts = {r["path"]: r["n"] for r in store.query(
        "SELECT path, COUNT(*) AS n FROM symbols WHERE project=? GROUP BY path", (proj,))}
    files = [{
        "path": r["path"], "lang": r["lang"], "lines": r["lines"],
        "summary": r["summary"] or _heuristic_summary(r["lang"], r["content"]),
        "symbols": counts.get(r["path"], 0),
    } for r in rows]
    total = store.query_one("SELECT COUNT(*) AS n FROM files WHERE project=?", (proj,))["n"]
    return {"project": proj, "total_files": total, "shown": len(files), "files": files}


@mcp.tool
def get_file(path: str, project: str | None = None) -> dict:
    """Return a file's full stored content."""
    proj = _proj(project)
    row = store.query_one("SELECT content, lang, lines FROM files WHERE project=? AND path=?",
                          (proj, path))
    if not row:
        return {"error": f"not indexed: {path}"}
    return {"path": path, "lang": row["lang"], "lines": row["lines"], "content": row["content"]}


@mcp.tool
def get_lines(path: str, start: int, end: int, project: str | None = None) -> dict:
    """Return lines [start, end] (1-based, inclusive) of a file."""
    proj = _proj(project)
    row = store.query_one("SELECT content FROM files WHERE project=? AND path=?", (proj, path))
    if not row:
        return {"error": f"not indexed: {path}"}
    if start < 1:
        start = 1
    if end < start:
        return {"error": f"bad range: end({end}) < start({start})"}
    lines = row["content"].splitlines()
    seg = lines[start - 1:end]
    return {"path": path, "start": start, "end": min(end, len(lines)), "content": "\n".join(seg)}


@mcp.tool
def set_summary(path: str, summary: str, project: str | None = None) -> dict:
    """Cache an agent-written one/two-line summary for a file (refreshed when its hash changes)."""
    proj = _proj(project)
    row = store.query_one("SELECT hash FROM files WHERE project=? AND path=?", (proj, path))
    if not row:
        return {"error": f"not indexed: {path}"}
    store.execute("UPDATE files SET summary=?, summary_for_hash=? WHERE project=? AND path=?",
                  (summary, row["hash"], proj, path))
    store.execute("UPDATE files_fts SET summary=? WHERE project=? AND path=?", (summary, proj, path))
    return {"ok": True, "path": path}


@mcp.tool
def file_summary(path: str, project: str | None = None) -> dict:
    """Get a file's cached summary (and whether it's stale vs the current content hash)."""
    proj = _proj(project)
    row = store.query_one("SELECT summary, summary_for_hash, hash, lang, content "
                          "FROM files WHERE project=? AND path=?", (proj, path))
    if not row:
        return {"error": f"not indexed: {path}"}
    return {
        "path": path,
        "summary": row["summary"] or _heuristic_summary(row["lang"], row["content"]),
        "agent_written": bool(row["summary"]),
        "stale": bool(row["summary"]) and row["summary_for_hash"] != row["hash"],
    }


@mcp.tool
def symbols(path: str, project: str | None = None) -> list[dict]:
    """List functions/classes/symbols defined in a file (with line numbers)."""
    proj = _proj(project)
    return store.query(
        "SELECT name, kind, lineno, end_lineno FROM symbols WHERE project=? AND path=? ORDER BY lineno",
        (proj, path))


@mcp.tool
def outline(path: str, project: str | None = None) -> dict:
    """Hierarchical symbol outline for a file: each symbol with its kind and line range, nested
    by containment (a method inside a class is a child of that class)."""
    proj = _proj(project)
    rows = store.query(
        "SELECT name, kind, lineno, end_lineno FROM symbols WHERE project=? AND path=? "
        "ORDER BY lineno", (proj, path))
    if not rows:
        return {"path": path, "outline": []}
    nodes = [{"name": r["name"], "kind": r["kind"], "lineno": r["lineno"],
              "end_lineno": r["end_lineno"], "children": []} for r in rows]
    roots: list[dict] = []
    stack: list[dict] = []
    for n in nodes:
        while stack and not (stack[-1]["end_lineno"] and
                             n["lineno"] <= stack[-1]["end_lineno"]):
            stack.pop()
        if stack and stack[-1]["end_lineno"] and n["lineno"] <= stack[-1]["end_lineno"]:
            stack[-1]["children"].append(n)
        else:
            roots.append(n)
        stack.append(n)
    return {"path": path, "outline": roots}


@mcp.tool
def definition(name: str, project: str | None = None) -> list[dict]:
    """Where is `name` defined? Returns file + line for each matching symbol."""
    proj = _proj(project)
    return store.query("SELECT path, kind, lineno FROM symbols WHERE project=? AND name=? ORDER BY path",
                       (proj, name))


@mcp.tool
def references(name: str, project: str | None = None, limit: int = 50) -> list[dict]:
    """Find references to `name` across the project (word-boundary match), with line snippets."""
    proj = _proj(project)
    pat = re.compile(rf"\b{re.escape(name)}\b")
    out: list[dict] = []
    for row in store.query("SELECT path, content FROM files WHERE project=?", (proj,)):
        for i, line in enumerate(row["content"].splitlines(), 1):
            if pat.search(line):
                out.append({"path": row["path"], "lineno": i, "line": line.strip()[:200]})
                if len(out) >= limit:
                    return out
    return out


@mcp.tool
def callers(name: str, project: str | None = None, limit: int = 50) -> list[dict]:
    """Find call sites of `name` (references that look like `name(`)."""
    proj = _proj(project)
    pat = re.compile(rf"\b{re.escape(name)}\s*\(")
    out: list[dict] = []
    for row in store.query("SELECT path, content FROM files WHERE project=?", (proj,)):
        for i, line in enumerate(row["content"].splitlines(), 1):
            if pat.search(line):
                out.append({"path": row["path"], "lineno": i, "line": line.strip()[:200]})
                if len(out) >= limit:
                    return out
    return out


def _enclosing_symbol(proj: str, path: str, lineno: int) -> str | None:
    rows = store.query(
        "SELECT name, lineno, end_lineno FROM symbols WHERE project=? AND path=? "
        "AND kind IN ('function','method') ORDER BY lineno", (proj, path))
    best = None
    for r in rows:
        if r["lineno"] <= lineno and (r["end_lineno"] is None or lineno <= r["end_lineno"]):
            best = r["name"]
    return best


@mcp.tool
def call_graph(name: str, project: str | None = None, depth: int = 1, limit: int = 60) -> dict:
    """Shallow call graph for a function symbol: callers (functions whose body calls `name`) and
    callees (functions `name` calls). Uses symbol line ranges to attribute each call site to its
    enclosing function. depth>1 expands callers transitively (best-effort)."""
    proj = _proj(project)
    call_pat = lambda n: re.compile(rf"\b{re.escape(n)}\s*\(")  # noqa: E731

    def callers_of(target: str) -> list[dict]:
        pat = call_pat(target)
        seen: list[dict] = []
        for row in store.query("SELECT path, content FROM files WHERE project=?", (proj,)):
            for i, line in enumerate(row["content"].splitlines(), 1):
                if pat.search(line):
                    fn = _enclosing_symbol(proj, row["path"], i)
                    seen.append({"path": row["path"], "lineno": i,
                                 "in_function": fn, "line": line.strip()[:160]})
                    if len(seen) >= limit:
                        return seen
        return seen

    # Callees: scan the body of each definition of `name`.
    callees: list[str] = []
    defs = store.query(
        "SELECT path, lineno, end_lineno FROM symbols WHERE project=? AND name=? "
        "AND kind IN ('function','method')", (proj, name))
    known = {r["name"] for r in store.query("SELECT DISTINCT name FROM symbols WHERE project=?", (proj,))}
    for d in defs:
        frow = store.query_one("SELECT content FROM files WHERE project=? AND path=?", (proj, d["path"]))
        if not frow:
            continue
        body = frow["content"].splitlines()[d["lineno"]:(d["end_lineno"] or d["lineno"])]
        for ln in body:
            for m in re.finditer(r"\b([A-Za-z_]\w*)\s*\(", ln):
                cand = m.group(1)
                if cand in known and cand != name and cand not in callees:
                    callees.append(cand)

    callers = callers_of(name)
    expanded: dict = {}
    if depth > 1:
        for c in {x["in_function"] for x in callers if x["in_function"]}:
            expanded[c] = callers_of(c)
    return {"symbol": name, "callers": callers, "callees": callees[:limit],
            "callers_of_callers": expanded}


@mcp.tool
def imports(path: str, project: str | None = None) -> dict:
    """What does this file import? Returns each import target, its kind, line, and the resolved
    in-project file path when it could be mapped (external deps have resolved=null)."""
    proj = _proj(project)
    rows = store.query(
        "SELECT target, resolved, kind, lineno FROM imports WHERE project=? AND path=? ORDER BY lineno",
        (proj, path))
    return {"path": path, "imports": rows,
            "internal": [r["resolved"] for r in rows if r["resolved"]],
            "external": sorted({r["target"] for r in rows if not r["resolved"]})}


@mcp.tool
def imported_by(path: str, project: str | None = None) -> list[dict]:
    """Reverse import edges: which indexed files import `path`."""
    proj = _proj(project)
    return store.query(
        "SELECT path AS importer, target, lineno FROM imports WHERE project=? AND resolved=? "
        "ORDER BY path", (proj, path))


@mcp.tool
def import_graph(project: str | None = None, limit: int = 500) -> dict:
    """Project import graph: file→file edges (resolved internal imports) plus the most-imported
    files (hubs) and external dependency frequency."""
    proj = _proj(project)
    edges = store.query(
        "SELECT path AS src, resolved AS dst FROM imports WHERE project=? AND resolved IS NOT NULL "
        "LIMIT ?", (proj, limit))
    hubs = store.query(
        "SELECT resolved AS file, COUNT(*) AS n FROM imports WHERE project=? AND resolved IS NOT NULL "
        "GROUP BY resolved ORDER BY n DESC LIMIT 15", (proj,))
    externals = store.query(
        "SELECT target AS dep, COUNT(*) AS n FROM imports WHERE project=? AND resolved IS NULL "
        "GROUP BY target ORDER BY n DESC LIMIT 20", (proj,))
    return {"project": proj, "edges": edges, "hubs": hubs, "top_externals": externals}


@mcp.tool
def search(query: str, project: str | None = None, limit: int = 20) -> list[dict]:
    """Full-text search file contents + summaries (FTS5), best matches first."""
    proj = _proj(project)
    try:
        return store.query(
            "SELECT path, snippet(files_fts, 0, '«', '»', ' … ', 10) AS snippet "
            "FROM files_fts WHERE files_fts MATCH ? AND project=? ORDER BY rank LIMIT ?",
            (_sanitize_fts(query), proj, limit),
        )
    except Exception:
        like = f"%{query}%"
        return store.query(
            "SELECT path FROM files WHERE project=? AND content LIKE ? LIMIT ?", (proj, like, limit)
        )


@mcp.tool
def grep(pattern: str, project: str | None = None, regex: bool = True, limit: int = 80,
         ignore_case: bool = False) -> list[dict]:
    """Regex (or literal) search across stored file contents with file:line:snippet. Complements
    `search` (FTS) when you need true regex / exact patterns FTS can't express."""
    proj = _proj(project)
    if not isinstance(pattern, str) or pattern == "":
        return [{"error": "pattern must be a non-empty string"}]
    limit = max(1, min(int(limit), 1000))
    flags = re.IGNORECASE if ignore_case else 0
    try:
        pat = re.compile(pattern if regex else re.escape(pattern), flags)
    except re.error as e:
        return [{"error": f"bad regex: {e}"}]
    out: list[dict] = []
    for row in store.query("SELECT path, content FROM files WHERE project=?", (proj,)):
        for i, line in enumerate(row["content"].splitlines(), 1):
            if pat.search(line):
                out.append({"path": row["path"], "lineno": i, "line": line.strip()[:200]})
                if len(out) >= limit:
                    return out
    return out


@mcp.tool
def todo_scan(project: str | None = None, markers: list[str] | None = None,
              limit: int = 200) -> dict:
    """Scan the project for TODO/FIXME/HACK/XXX/BUG (and custom markers) comments, returning
    file:line:marker:text so the agent can triage outstanding work."""
    proj = _proj(project)
    marks = [m.upper() for m in (markers or list(TODO_MARKERS))]
    pat = re.compile(r"\b(" + "|".join(re.escape(m) for m in marks) + r")\b[:\s\-]*(.*)")
    out: list[dict] = []
    counts: dict[str, int] = {}
    for row in store.query("SELECT path, content FROM files WHERE project=?", (proj,)):
        for i, line in enumerate(row["content"].splitlines(), 1):
            m = pat.search(line)
            if m:
                mk = m.group(1).upper()
                counts[mk] = counts.get(mk, 0) + 1
                if len(out) < limit:
                    out.append({"path": row["path"], "lineno": i, "marker": mk,
                                "text": m.group(2).strip()[:200]})
    return {"project": proj, "total": sum(counts.values()), "by_marker": counts, "items": out}


@mcp.tool
def duplicate_code(project: str | None = None, min_lines: int = 6, limit: int = 30) -> dict:
    """Cheap structural duplicate detector: hash sliding windows of normalized (whitespace-
    stripped, non-blank) lines and report blocks of >=min_lines that appear in 2+ places."""
    proj = _proj(project)
    try:
        min_lines = max(2, int(min_lines))
        limit = max(1, int(limit))
    except (TypeError, ValueError):
        min_lines, limit = 6, 30
    seen: dict[str, list[dict]] = {}
    for row in store.query("SELECT path, content FROM files WHERE project=? AND lang!='markdown'",
                           (proj,)):
        raw = row["content"].splitlines()
        norm = [(i + 1, ln.strip()) for i, ln in enumerate(raw)]
        norm = [(i, ln) for (i, ln) in norm if ln]
        for j in range(0, max(0, len(norm) - min_lines + 1)):
            window = norm[j:j + min_lines]
            if not window:
                continue
            block = "\n".join(ln for (_n, ln) in window)
            if len(block) < min_lines * 4:
                continue
            h = hashlib.sha1(block.encode()).hexdigest()
            seen.setdefault(h, []).append({"path": row["path"], "start": window[0][0],
                                           "end": window[-1][0]})
    clusters = []
    for h, locs in seen.items():
        uniq_paths = {l["path"] for l in locs}
        if len(locs) >= 2 and (len(uniq_paths) >= 2 or len(locs) >= 3):
            clusters.append({"occurrences": len(locs), "locations": locs[:6]})
    clusters.sort(key=lambda c: c["occurrences"], reverse=True)
    return {"project": proj, "min_lines": min_lines, "clusters": clusters[:limit]}


@mcp.tool
def dead_code_hints(project: str | None = None, limit: int = 50) -> dict:
    """Heuristic dead-code hints: defined functions/classes whose name has no reference outside
    its own file. HINTS ONLY (dynamic dispatch, exports, reflection, and entrypoints are not
    detected). Skips dunder names and obvious entrypoints."""
    proj = _proj(project)
    syms = store.query(
        "SELECT name, path, kind, lineno FROM symbols WHERE project=? AND kind IN "
        "('function','class','method') ", (proj,))
    files = {r["path"]: r["content"] for r in store.query(
        "SELECT path, content FROM files WHERE project=?", (proj,))}
    out: list[dict] = []
    for s in syms:
        name = s["name"]
        if name.startswith("__") or name in ("main", "setup", "handler"):
            continue
        pat = re.compile(rf"\b{re.escape(name)}\b")
        external = False
        for p, content in files.items():
            if p == s["path"]:
                continue
            if pat.search(content):
                external = True
                break
        if not external:
            own = files.get(s["path"], "")
            uses = len(pat.findall(own))
            if uses <= 1:  # only the definition itself
                out.append({"name": name, "kind": s["kind"], "path": s["path"],
                            "lineno": s["lineno"]})
        if len(out) >= limit:
            break
    return {"project": proj, "hints": out, "note": "heuristic — verify before deleting"}


# ---------------------------------------------------------------- git diff

def _is_git_repo(root: str) -> bool:
    try:
        r = subprocess.run(["git", "-C", root, "rev-parse", "--is-inside-work-tree"],
                           capture_output=True, text=True, timeout=10)
        return r.returncode == 0 and r.stdout.strip() == "true"
    except Exception:
        return False


@mcp.tool
def changed_files(ref: str = "HEAD", project: str | None = None) -> dict:
    """List files changed since a git ref (name + status), using the working tree. Requires the
    project to be a git repo."""
    proj = _proj(project)
    if _safe_ref(ref) is None:
        return {"error": f"invalid git ref: {ref!r}", "project": proj}
    if not _is_git_repo(proj):
        return {"error": "not a git repo", "project": proj}
    r = subprocess.run(["git", "-C", proj, "diff", "--name-status", ref, "--"],
                       capture_output=True, text=True, timeout=30)
    files = []
    for ln in r.stdout.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            files.append({"status": parts[0], "path": parts[-1]})
    return {"project": proj, "ref": ref, "changed": files}


@mcp.tool
def diff_since(ref: str = "HEAD", project: str | None = None, max_files: int = 30) -> dict:
    """What changed since a git ref: name-status list, a --stat summary, and (per changed file)
    a compact patch. Use to brief an agent on the delta before it works. Requires a git repo."""
    proj = _proj(project)
    if _safe_ref(ref) is None:
        return {"error": f"invalid git ref: {ref!r}", "project": proj}
    if not _is_git_repo(proj):
        return {"error": "not a git repo", "project": proj}
    names = subprocess.run(["git", "-C", proj, "diff", "--name-status", ref, "--"],
                           capture_output=True, text=True, timeout=30).stdout
    stat = subprocess.run(["git", "-C", proj, "diff", "--stat", ref, "--"],
                          capture_output=True, text=True, timeout=30).stdout
    changed = []
    for ln in names.splitlines():
        parts = ln.split("\t")
        if len(parts) >= 2:
            changed.append({"status": parts[0], "path": parts[-1]})
    patches = []
    for c in changed[:max_files]:
        p = subprocess.run(["git", "-C", proj, "diff", ref, "--", c["path"]],
                           capture_output=True, text=True, timeout=30).stdout
        patches.append({"path": c["path"], "status": c["status"], "patch": p[:4000]})
    return {"project": proj, "ref": ref, "changed": changed, "stat": stat.strip(),
            "patches": patches}


# ---------------------------------------------------------------- semantic

MODEL2VEC_MODEL = "minishlab/potion-base-8M"


class _Model2VecWrapper:
    """Adapter giving a model2vec StaticModel the same .encode(...) surface this server uses.

    model2vec is a FREE, fully-local, dependency-light static-embedding backend (no torch). We
    normalize vectors ourselves so cosine ranking is comparable to the sentence-transformers path.
    """

    name = MODEL2VEC_MODEL

    def __init__(self, model):
        self._m = model

    def encode(self, texts, normalize_embeddings: bool = True, show_progress_bar: bool = False):
        vecs = self._m.encode(list(texts))
        out = []
        for v in vecs:
            row = [float(x) for x in v]
            if normalize_embeddings:
                norm = math.sqrt(sum(x * x for x in row)) or 1.0
                row = [x / norm for x in row]
            out.append(row)
        return out


def _embedder():
    """Lazy LOCAL embedder. Prefers model2vec (light, FREE, no torch); falls back to
    sentence-transformers; returns None if neither is installed (callers degrade to FTS)."""
    if getattr(_embedder, "_cache", "x") != "x":
        return _embedder._cache  # type: ignore[attr-defined]
    model = None
    try:
        from model2vec import StaticModel  # type: ignore
        model = _Model2VecWrapper(StaticModel.from_pretrained(MODEL2VEC_MODEL))
    except Exception:
        try:
            from sentence_transformers import SentenceTransformer  # type: ignore
            model = SentenceTransformer(EMBED_MODEL)
        except Exception:
            model = None
    _embedder._cache = model  # type: ignore[attr-defined]
    return model


def _engine_name() -> str:
    """Human-readable name of the active local embedding backend."""
    m = _embedder()
    if m is None:
        return "unavailable"
    return getattr(m, "name", EMBED_MODEL)


def _pack(vec) -> bytes:
    return struct.pack(f"<{len(vec)}f", *vec)


def _unpack(blob: bytes) -> list[float]:
    return list(struct.unpack(f"<{len(blob) // 4}f", blob))


def _embed_meta_key(proj: str) -> str:
    return f"embed_meta::{proj}"


def _save_embed_meta(proj: str, model: str, dim: int) -> None:
    """Record which embedding backend + vector dim a project's chunks were built with, so a later
    semantic_search can detect drift (different model/dim) and degrade instead of ranking garbage."""
    import json as _json
    store.execute(
        "INSERT INTO meta(key,value) VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
        (_embed_meta_key(proj), _json.dumps({"model": model, "dim": int(dim)})),
    )


def _load_embed_meta(proj: str) -> dict | None:
    import json as _json
    row = store.query_one("SELECT value FROM meta WHERE key=?", (_embed_meta_key(proj),))
    if not row or not row.get("value"):
        return None
    try:
        return _json.loads(row["value"])
    except Exception:
        return None


def _cosine(a: list[float], b: list[float]) -> float:
    dot = sum(x * y for x, y in zip(a, b))
    na = math.sqrt(sum(x * x for x in a)) or 1.0
    nb = math.sqrt(sum(y * y for y in b)) or 1.0
    return dot / (na * nb)


@mcp.tool
def embed_index(project: str | None = None, window: int = 40) -> dict:
    """Build a LOCAL semantic index: chunk each file into ~`window`-line windows and store an
    embedding (sentence-transformers, runs offline). Enables semantic_search / hybrid context.
    No-op with a hint if sentence-transformers isn't installed."""
    proj = _proj(project)
    window = max(5, min(int(window), 500))
    model = _embedder()
    if model is None:
        return {"engine": "unavailable",
                "hint": "pip install model2vec (FREE, local, no torch) or sentence-transformers "
                        "to enable semantic search"}
    store.execute("DELETE FROM chunks WHERE project=?", (proj,))
    n = 0
    rows = store.query("SELECT path, content, lang FROM files WHERE project=? AND lang NOT IN "
                       "('json','yaml','toml')", (proj,))
    batch_texts: list[str] = []
    batch_meta: list[tuple[str, int, int, str]] = []
    for r in rows:
        lines = r["content"].splitlines()
        for j in range(0, len(lines), window):
            seg = "\n".join(lines[j:j + window]).strip()
            if not seg:
                continue
            batch_texts.append(seg[:2000])
            batch_meta.append((r["path"], j + 1, min(j + window, len(lines)), seg[:2000]))
    dim = 0
    if batch_texts:
        vecs = model.encode(batch_texts, normalize_embeddings=True, show_progress_bar=False)
        packed = [(proj, m[0], m[1], m[2], m[3], _pack(list(map(float, v))))
                  for m, v in zip(batch_meta, vecs)]
        store.executemany(
            "INSERT INTO chunks(project,path,start,end,text,embedding) VALUES(?,?,?,?,?,?)",
            packed)
        n = len(batch_texts)
        if vecs:
            dim = len(list(vecs[0]))
    # Record the backend + vector dim so semantic_search can detect drift and degrade safely.
    _save_embed_meta(proj, _engine_name(), dim)
    return {"engine": _engine_name(), "project": proj, "chunks": n, "dim": dim}


@mcp.tool
def semantic_search(query: str, project: str | None = None, k: int = 8) -> dict:
    """Local semantic search over embedded chunks (run embed_index first). Ranks by cosine
    similarity. Returns path + line range + snippet + score. Falls back with a hint if the model
    or index is missing."""
    proj = _proj(project)
    if not isinstance(query, str) or not query.strip():
        return {"error": "query must be a non-empty string"}
    k = max(1, min(int(k), 100))
    model = _embedder()
    if model is None:
        return {"engine": "unavailable",
                "hint": "pip install model2vec (FREE, local, no torch) or sentence-transformers, "
                        "then call embed_index()"}
    rows = store.query("SELECT path, start, end, text, embedding FROM chunks WHERE project=?", (proj,))
    if not rows:
        return {"engine": _engine_name(), "hint": "no chunks — call embed_index() first", "results": []}
    qv = list(map(float, model.encode([query], normalize_embeddings=True)[0]))
    qdim = len(qv)

    # Embedding-dimension drift guard: if the chunks were packed with a different model/dim than the
    # current embedder produces, cosine over mismatched-length vectors would silently rank garbage
    # (zip truncates to the shorter length). Detect drift via stored meta AND the actual blob length,
    # then degrade to FTS instead of returning meaningless scores.
    meta = _load_embed_meta(proj)
    cur_engine = _engine_name()
    stored_dim = (meta or {}).get("dim")
    stored_model = (meta or {}).get("model")
    blob_dim = len(rows[0]["embedding"]) // 4 if rows[0]["embedding"] else 0
    mismatch = (
        (stored_dim is not None and stored_dim != qdim)
        or (stored_model is not None and stored_model != cur_engine)
        or (blob_dim and blob_dim != qdim)
    )
    if mismatch:
        fts = search(query, project=proj, limit=k)
        return {
            "engine": cur_engine,
            "query": query,
            "degraded": "fts",
            "reason": "embedding dimension/model mismatch — index was built with "
                      f"{stored_model or 'an unknown model'} (dim {stored_dim or blob_dim}), "
                      f"current backend is {cur_engine} (dim {qdim})",
            "hint": "re-run embed_index() to rebuild the semantic index with the current model",
            "results": [{"path": r.get("path"), "start": None, "end": None,
                         "score": None, "snippet": ""} for r in fts],
        }

    # Skip any individual chunk whose stored vector length doesn't match the query (defensive —
    # a partially-rebuilt index can mix dims); never feed mismatched vectors to _cosine.
    scored = []
    for r in rows:
        cand = _unpack(r["embedding"])
        if len(cand) != qdim:
            continue
        scored.append((_cosine(qv, cand), r))
    if not scored:
        fts = search(query, project=proj, limit=k)
        return {
            "engine": cur_engine,
            "query": query,
            "degraded": "fts",
            "reason": "no stored chunk matched the current embedding dimension",
            "hint": "re-run embed_index() to rebuild the semantic index with the current model",
            "results": [{"path": r.get("path"), "start": None, "end": None,
                         "score": None, "snippet": ""} for r in fts],
        }
    scored.sort(key=lambda x: x[0], reverse=True)
    results = [{"path": r["path"], "start": r["start"], "end": r["end"],
                "score": round(s, 4), "snippet": r["text"][:300]} for s, r in scored[:k]]
    return {"engine": cur_engine, "query": query, "results": results}


@mcp.tool
def relevant_context(task: str, project: str | None = None, max_files: int = 6,
                     budget_chars: int = 6000, mode: str = "fts") -> dict:
    """Curated context bundle for a task: top matching files with summary, key symbols, and a
    content head — sized to fit a model's window (not a full dump). mode ∈ {fts, semantic,
    hybrid}; fts is the default (no model needed). hybrid merges FTS + local semantic hits."""
    proj = _proj(project)
    if not isinstance(task, str) or not task.strip():
        return {"error": "task must be a non-empty string"}
    if mode not in ("fts", "semantic", "hybrid"):
        mode = "fts"
    max_files = max(1, min(int(max_files), 50))
    budget_chars = max(200, min(int(budget_chars), 200_000))
    ranked: list[str] = []
    if mode in ("fts", "hybrid"):
        ranked += [h["path"] for h in search(task, project=proj, limit=max_files)]
    if mode in ("semantic", "hybrid"):
        sem = semantic_search(task, project=proj, k=max_files)
        if isinstance(sem, dict) and sem.get("results"):
            ranked += [r["path"] for r in sem["results"]]
    seen: set[str] = set()
    paths: list[str] = []
    for p in ranked:
        if p not in seen:
            seen.add(p)
            paths.append(p)
    bundle = []
    used = 0
    for path in paths[:max_files]:
        frow = store.query_one("SELECT content, lang, summary FROM files WHERE project=? AND path=?",
                               (proj, path))
        if not frow:
            continue
        syms = store.query("SELECT name, kind, lineno FROM symbols WHERE project=? AND path=? "
                           "ORDER BY lineno LIMIT 8", (proj, path))
        head = "\n".join(frow["content"].splitlines()[:30])
        if used + len(head) > budget_chars:
            head = head[: max(0, budget_chars - used)]
        used += len(head)
        bundle.append({
            "path": path,
            "summary": frow["summary"] or _heuristic_summary(frow["lang"], frow["content"]),
            "symbols": syms,
            "head": head,
        })
        if used >= budget_chars:
            break
    return {"project": proj, "task": task, "mode": mode, "files": bundle}


# ---------------------------------------------------------------- export

def _build_digest(proj: str) -> str:
    pm = project_map(project=proj, limit=200)
    name = Path(proj).name
    lines = [
        f"# Project context: {name}",
        "",
        f"_Auto-generated by codeindex. {pm['total_files']} files indexed._",
        "",
        "When you start: call `codeindex.relevant_context(<task>)` for focused context, and "
        "`project-memory.resume()` to rehydrate prior decisions.",
        "",
    ]
    try:
        ig = import_graph(project=proj)
        if ig["hubs"]:
            lines.append("## Key files (most imported)")
            lines.append("")
            for h in ig["hubs"][:8]:
                lines.append(f"- `{h['file']}` ({h['n']} importers)")
            lines.append("")
    except Exception:
        pass
    try:
        td = todo_scan(project=proj, limit=0)
        if td["total"]:
            lines.append(f"_Open markers: {td['total']} ({td['by_marker']})._")
            lines.append("")
    except Exception:
        pass
    lines += ["## Files", ""]
    for f in pm["files"][:120]:
        s = f["summary"] or ""
        lines.append(f"- `{f['path']}` ({f['lang']}, {f['lines']}L, {f['symbols']} symbols)" +
                     (f" — {s}" if s else ""))
    return "\n".join(lines) + "\n"


@mcp.tool
def export_context_file(project: str | None = None, write: bool = True,
                        extra_targets: bool = True) -> dict:
    """Write a compact project digest into auto-load files so every client starts familiar with
    the project: CLAUDE.md, AGENTS.md, .github/copilot-instructions.md (always), plus (when
    extra_targets) Cursor (.cursor/rules/project-context.mdc), Windsurf (.windsurfrules), and
    Zed/Cursor-compatible (.rules)."""
    proj = _proj(project)
    digest = _build_digest(proj)

    targets = [Path(proj) / "CLAUDE.md", Path(proj) / "AGENTS.md",
               Path(proj) / ".github" / "copilot-instructions.md"]
    extra = []
    if extra_targets:
        mdc = ("---\ndescription: Auto-generated project context\nalwaysApply: true\n---\n\n"
               + digest)
        extra = [(Path(proj) / ".cursor" / "rules" / "project-context.mdc", mdc),
                 (Path(proj) / ".windsurfrules", digest),
                 (Path(proj) / ".rules", digest)]
    written = []
    if write:
        for t in targets:
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(digest, encoding="utf-8")
            written.append(str(t))
        for t, body in extra:
            t.parent.mkdir(parents=True, exist_ok=True)
            t.write_text(body, encoding="utf-8")
            written.append(str(t))
    return {"project": proj, "written": written, "bytes": len(digest), "preview": digest[:400]}


@mcp.tool
def status(project: str | None = None) -> dict:
    """Index stats for the active (or given) project."""
    proj = _proj(project)
    f = store.query_one("SELECT COUNT(*) AS n, SUM(lines) AS L FROM files WHERE project=?", (proj,))
    s = store.query_one("SELECT COUNT(*) AS n FROM symbols WHERE project=?", (proj,))
    imp = store.query_one("SELECT COUNT(*) AS n FROM imports WHERE project=?", (proj,))
    ch = store.query_one("SELECT COUNT(*) AS n FROM chunks WHERE project=?", (proj,))
    return {"project": proj, "files": f["n"], "lines": f["L"] or 0, "symbols": s["n"],
            "imports": imp["n"], "embedded_chunks": ch["n"],
            "semantic_ready": ch["n"] > 0}


if __name__ == "__main__":
    mcp.run()
