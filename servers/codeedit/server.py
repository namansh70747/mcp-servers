"""codeedit — the safe code-editing MCP server (the missing core of the suite).

Every mutating operation writes a BACKUP first (to data_dir('codeedit')/backups/), applies the
change, and — when validate=True — runs a syntax check and AUTO-ROLLS BACK from the backup on
failure. Each mutating op returns a real unified diff of what changed plus an ok/err envelope.

Free + portable: pure-python where possible; shells out only to git / py_compile / node --check /
gofmt / black / ruff / prettier / pytest / jest / cargo when present, and DEGRADES GRACEFULLY
(returns a 'skipped: not installed' note) whenever a tool is absent. Never hard-requires anything.

Path safety: edits are confined under a resolved root (cwd, or a given repo). Traversal and
absolute escapes are rejected.

Tools (exact contract):
  Editing:  preview_patch, apply_patch, replace_in_file, insert_lines, delete_lines,
            replace_lines, write_file, multi_edit, undo, list_backups
  Validation: validate, syntax_check, format_code, lint, run_tests
"""
from __future__ import annotations

import difflib
import os
import shutil
import subprocess
import time
from pathlib import Path

from mcp_base import data_dir, err, make_server, ok

mcp = make_server(
    "codeedit",
    instructions=(
        "Safe code editing with auto-backup + auto-rollback. Mutating ops: preview_patch, "
        "apply_patch, replace_in_file, insert_lines, delete_lines, replace_lines, write_file, "
        "multi_edit (atomic), undo, list_backups. Validation (degrade gracefully if tool absent): "
        "validate, syntax_check, format_code, lint, run_tests. Every write backs up first and "
        "rolls back on a failed syntax check when validate=True."
    ),
)

BACKUP_DIR = data_dir("codeedit") / "backups"
BACKUP_DIR.mkdir(parents=True, exist_ok=True)


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #
def _root(repo: str | None = None) -> Path:
    if repo:
        if not isinstance(repo, str) or not repo.strip() or "\x00" in repo:
            raise ValueError(f"invalid repo: {repo!r}")
        return Path(repo).expanduser().resolve()
    return Path.cwd().resolve()


def _safe_path(path: str, repo: str | None = None) -> Path:
    """Resolve `path` and confine it under the root. Raises ValueError on bad input or escape.

    Rejects non-string, empty/whitespace, and NUL-containing paths so garbage/None inputs
    surface as a caught ValueError (handled by every tool) instead of crashing."""
    if not isinstance(path, str) or not path.strip():
        raise ValueError(f"invalid path: {path!r}")
    if "\x00" in path:
        raise ValueError("invalid path: contains NUL byte")
    if repo is not None and not isinstance(repo, str):
        raise ValueError(f"invalid repo: {repo!r}")
    root = _root(repo)
    p = Path(path).expanduser()
    full = (p if p.is_absolute() else (root / p)).resolve()
    try:
        full.relative_to(root)
    except ValueError:
        raise ValueError(f"path escapes root {root}: {path}")
    return full


def _tool_exists(name: str) -> bool:
    return shutil.which(name) is not None


# --------------------------------------------------------------------------- #
# Backups
# --------------------------------------------------------------------------- #
def _backup(full: Path) -> str | None:
    """Copy `full` into the backups dir with a timestamped name. Returns backup path (str)."""
    if not full.exists():
        return None
    safe = str(full).replace(os.sep, "__").lstrip("_")
    stamp = time.strftime("%Y%m%d-%H%M%S") + f"-{int(time.time() * 1000) % 1000:03d}"
    dest = BACKUP_DIR / f"{safe}.{stamp}.bak"
    shutil.copy2(full, dest)
    return str(dest)


def _backups_for(full: Path) -> list[Path]:
    safe = str(full).replace(os.sep, "__").lstrip("_")
    return sorted(BACKUP_DIR.glob(f"{safe}.*.bak"))


def _restore(full: Path, backup: str | None) -> None:
    """Restore from a backup path; if backup is None the file was newly created -> remove it."""
    if backup is None:
        if full.exists():
            full.unlink()
        return
    shutil.copy2(backup, full)


def _unified(before: str, after: str, path: str) -> str:
    diff = difflib.unified_diff(
        before.splitlines(keepends=True),
        after.splitlines(keepends=True),
        fromfile=f"a/{path}",
        tofile=f"b/{path}",
    )
    return "".join(diff)


def _read(full: Path) -> str:
    return full.read_text(encoding="utf-8") if full.exists() else ""


# --------------------------------------------------------------------------- #
# Syntax checking (by extension), degrades gracefully
# --------------------------------------------------------------------------- #
def _syntax_check(full: Path) -> dict:
    """Return {ok: bool, checker: str, output: str, skipped: bool}."""
    ext = full.suffix.lower()
    if not full.exists():
        return {"ok": False, "checker": "none", "output": "file does not exist", "skipped": False}

    if ext == ".py":
        import py_compile

        try:
            py_compile.compile(str(full), doraise=True)
            return {"ok": True, "checker": "py_compile", "output": "", "skipped": False}
        except py_compile.PyCompileError as e:
            return {"ok": False, "checker": "py_compile", "output": str(e), "skipped": False}

    if ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
        if not _tool_exists("node"):
            return {"ok": True, "checker": "node --check", "output": "skipped: node not installed",
                    "skipped": True}
        # node --check only handles plain JS; for TS it still catches gross syntax errors.
        r = subprocess.run(["node", "--check", str(full)], capture_output=True, text=True)
        return {"ok": r.returncode == 0, "checker": "node --check",
                "output": (r.stderr or r.stdout).strip(), "skipped": False}

    if ext == ".go":
        if not _tool_exists("gofmt"):
            return {"ok": True, "checker": "gofmt", "output": "skipped: gofmt not installed",
                    "skipped": True}
        r = subprocess.run(["gofmt", "-e", str(full)], capture_output=True, text=True)
        return {"ok": r.returncode == 0, "checker": "gofmt",
                "output": r.stderr.strip(), "skipped": False}

    if ext == ".rs":
        if not _tool_exists("rustc"):
            return {"ok": True, "checker": "rustc", "output": "skipped: rustc not installed",
                    "skipped": True}
        r = subprocess.run(["rustc", "--edition", "2021", "--emit", "metadata",
                            "-o", os.devnull, str(full)], capture_output=True, text=True)
        return {"ok": r.returncode == 0, "checker": "rustc",
                "output": r.stderr.strip(), "skipped": False}

    if ext == ".json":
        import json

        try:
            json.loads(full.read_text(encoding="utf-8"))
            return {"ok": True, "checker": "json", "output": "", "skipped": False}
        except Exception as e:  # noqa: BLE001
            return {"ok": False, "checker": "json", "output": str(e), "skipped": False}

    return {"ok": True, "checker": "none",
            "output": f"skipped: no syntax checker for '{ext or 'no-ext'}'", "skipped": True}


# --------------------------------------------------------------------------- #
# Core write helper: backup -> apply -> validate -> rollback-on-failure
# --------------------------------------------------------------------------- #
def _commit_change(full: Path, new_content: str, before: str, rel: str,
                   validate: bool, op: str) -> dict:
    """Atomic single-file change with optional validate + auto-rollback. Returns an envelope."""
    backup = _backup(full)
    full.parent.mkdir(parents=True, exist_ok=True)
    full.write_text(new_content, encoding="utf-8")
    diff = _unified(before, new_content, rel)

    result = {
        "op": op,
        "path": str(full),
        "diff": diff,
        "backup": backup,
        "validated": validate,
    }

    if validate:
        sc = _syntax_check(full)
        result["syntax_check"] = sc
        if not sc["ok"]:
            _restore(full, backup)
            result["rolled_back"] = True
            return err(f"{op}: syntax check failed, rolled back", **result)
        result["rolled_back"] = False

    return ok(**result)


# --------------------------------------------------------------------------- #
# Unified-diff application (git apply if present, else pure-python applier)
# --------------------------------------------------------------------------- #
def _parse_hunks(diff_text: str) -> list[dict]:
    """Parse a single-file unified diff into hunks. Each hunk: {old_start, old_count, lines}."""
    hunks: list[dict] = []
    cur: dict | None = None
    for line in diff_text.splitlines():
        if line.startswith("@@"):
            # @@ -l,s +l,s @@
            try:
                seg = line.split("@@")[1].strip()
                old = seg.split(" ")[0]  # -l,s
                old = old[1:]  # strip leading -
                if "," in old:
                    os_, oc_ = old.split(",")
                    old_start, old_count = int(os_), int(oc_)
                else:
                    old_start, old_count = int(old), 1
            except Exception:  # noqa: BLE001
                continue
            cur = {"old_start": old_start, "old_count": old_count, "lines": []}
            hunks.append(cur)
        elif cur is not None and line and line[0] in (" ", "+", "-", "\\"):
            cur["lines"].append(line)
    return hunks


def _apply_hunks_py(original: str, diff_text: str) -> str:
    """Apply a unified diff to `original` text. Raises ValueError if context doesn't match."""
    hunks = _parse_hunks(diff_text)
    if not hunks:
        raise ValueError("no hunks found in diff")
    src = original.splitlines()
    out: list[str] = []
    idx = 0  # 0-based pointer into src

    for h in hunks:
        target = h["old_start"] - 1  # 0-based
        if target < 0:
            target = 0
        # copy unchanged lines up to the hunk start
        if target > len(src):
            raise ValueError(f"hunk start {h['old_start']} beyond end of file")
        out.extend(src[idx:target])
        idx = target
        for ln in h["lines"]:
            tag, content = ln[0], ln[1:]
            if tag == "\\":  # "\ No newline at end of file"
                continue
            if tag == " ":
                if idx >= len(src) or src[idx] != content:
                    raise ValueError(
                        f"context mismatch at line {idx + 1}: expected {content!r}, "
                        f"got {src[idx] if idx < len(src) else '<EOF>'!r}"
                    )
                out.append(src[idx])
                idx += 1
            elif tag == "-":
                if idx >= len(src) or src[idx] != content:
                    raise ValueError(
                        f"removal mismatch at line {idx + 1}: expected {content!r}, "
                        f"got {src[idx] if idx < len(src) else '<EOF>'!r}"
                    )
                idx += 1
            elif tag == "+":
                out.append(content)
    out.extend(src[idx:])

    trailing = "\n" if original.endswith("\n") or not original else ""
    return "\n".join(out) + (trailing if out else "")


def _diff_target_path(diff_text: str) -> str | None:
    """Extract the target path from a unified diff's +++ header (strips a/ b/ prefixes)."""
    for line in diff_text.splitlines():
        if line.startswith("+++ "):
            p = line[4:].strip().split("\t")[0]
            if p.startswith("b/"):
                p = p[2:]
            return None if p == "/dev/null" else p
        if line.startswith("--- ") and "+++" not in diff_text:
            p = line[4:].strip().split("\t")[0]
            if p.startswith("a/"):
                p = p[2:]
            return None if p == "/dev/null" else p
    return None


# --------------------------------------------------------------------------- #
# EDITING TOOLS
# --------------------------------------------------------------------------- #
@mcp.tool
def preview_patch(path_or_repo: str, unified_diff: str) -> dict:
    """Dry-run a unified diff (NO write). Shows the resulting content + whether it applies cleanly.

    path_or_repo may be a repo dir (target taken from the diff's +++ header) or a file path."""
    try:
        base = Path(path_or_repo).expanduser().resolve()
        repo = str(base) if base.is_dir() else None
        if repo:
            tgt = _diff_target_path(unified_diff)
            if not tgt:
                return err("preview_patch: could not determine target path from diff")
            full = _safe_path(tgt, repo)
        else:
            full = _safe_path(path_or_repo, None)
    except ValueError as e:
        return err(str(e))

    before = _read(full)
    try:
        after = _apply_hunks_py(before, unified_diff)
    except ValueError as e:
        return err(f"preview_patch: diff does not apply: {e}", applies=False, path=str(full))

    return ok(
        applies=True,
        path=str(full),
        diff=_unified(before, after, full.name),
        preview=after,
        dry_run=True,
    )


@mcp.tool
def apply_patch(unified_diff: str, repo: str | None = None, validate: bool = True) -> dict:
    """Apply a unified diff. Uses `git apply` when available, else a pure-python applier.

    Backs up the target first; on a failed syntax check (validate=True) AUTO-ROLLS BACK."""
    tgt = _diff_target_path(unified_diff)
    if not tgt:
        return err("apply_patch: could not determine target path from diff")
    try:
        full = _safe_path(tgt, repo)
    except ValueError as e:
        return err(str(e))

    before = _read(full)
    backend = "python"

    # Try git apply first (more robust with fuzz), falling back to the pure-python applier.
    new_content: str | None = None
    if _tool_exists("git"):
        root = _root(repo)
        proc = subprocess.run(
            ["git", "apply", "--whitespace=nowarn", "-"],
            input=unified_diff if unified_diff.endswith("\n") else unified_diff + "\n",
            cwd=str(root), capture_output=True, text=True,
        )
        if proc.returncode == 0:
            backend = "git apply"
            new_content = _read(full)
            # git apply already wrote the file; emulate the backup+validate flow by
            # restoring original first, then routing through _commit_change.
            full.write_text(before, encoding="utf-8") if full.exists() else None

    if new_content is None:
        try:
            new_content = _apply_hunks_py(before, unified_diff)
        except ValueError as e:
            return err(f"apply_patch: diff does not apply: {e}", path=str(full))

    res = _commit_change(full, new_content, before, full.name, validate, "apply_patch")
    res["backend"] = backend
    return res


@mcp.tool
def replace_in_file(path: str, find: str, replace: str, count: int = 0,
                    anchor: str | None = None, repo: str | None = None,
                    validate: bool = True) -> dict:
    """Replace `find` with `replace` in a file. count=0 -> all; anchor restricts replacement to
    lines containing the anchor substring. Auto-backup; auto-rollback on syntax failure."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    if not full.exists():
        return err(f"replace_in_file: no such file: {full}")

    before = full.read_text(encoding="utf-8")
    if anchor is not None:
        lines = before.splitlines(keepends=True)
        n = 0
        new_lines = []
        for ln in lines:
            if anchor in ln and (count == 0 or n < count) and find in ln:
                new_lines.append(ln.replace(find, replace) if count == 0
                                 else ln.replace(find, replace, count - n))
                n += ln.count(find) if count == 0 else min(ln.count(find), count - n)
            else:
                new_lines.append(ln)
        after = "".join(new_lines)
        replaced = n
    else:
        if count == 0:
            replaced = before.count(find)
            after = before.replace(find, replace)
        else:
            replaced = min(before.count(find), count)
            after = before.replace(find, replace, count)

    if after == before:
        return err(f"replace_in_file: no occurrences of {find!r} replaced",
                   replaced=0, path=str(full))

    res = _commit_change(full, after, before, full.name, validate, "replace_in_file")
    if res.get("ok"):
        res["replaced"] = replaced
    return res


@mcp.tool
def insert_lines(path: str, lineno: int, text: str, repo: str | None = None,
                 validate: bool = True) -> dict:
    """Insert `text` BEFORE 1-based `lineno` (lineno > len -> append at end). Auto-backup +
    rollback. `text` may contain multiple lines."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    before = _read(full)
    lines = before.splitlines(keepends=True)
    block = text if text.endswith("\n") else text + "\n"
    insert_at = max(0, min(lineno - 1, len(lines)))
    # ensure preceding line ends with newline so we don't fuse lines
    if insert_at > 0 and lines and not lines[insert_at - 1].endswith("\n"):
        lines[insert_at - 1] += "\n"
    new_lines = lines[:insert_at] + [block] + lines[insert_at:]
    after = "".join(new_lines)
    return _commit_change(full, after, before, full.name, validate, "insert_lines")


@mcp.tool
def delete_lines(path: str, start: int, end: int, repo: str | None = None,
                 validate: bool = True) -> dict:
    """Delete 1-based lines [start, end] inclusive. Auto-backup; auto-rollback on syntax failure."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    if not full.exists():
        return err(f"delete_lines: no such file: {full}")
    before = full.read_text(encoding="utf-8")
    lines = before.splitlines(keepends=True)
    if start < 1 or end > len(lines) or start > end:
        return err(f"delete_lines: invalid range {start}-{end} (file has {len(lines)} lines)")
    new_lines = lines[: start - 1] + lines[end:]
    after = "".join(new_lines)
    return _commit_change(full, after, before, full.name, validate, "delete_lines")


@mcp.tool
def replace_lines(path: str, start: int, end: int, text: str, repo: str | None = None,
                  validate: bool = True) -> dict:
    """Replace 1-based lines [start, end] inclusive with `text`. Auto-backup; auto-rollback."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    if not full.exists():
        return err(f"replace_lines: no such file: {full}")
    before = full.read_text(encoding="utf-8")
    lines = before.splitlines(keepends=True)
    if start < 1 or end > len(lines) or start > end:
        return err(f"replace_lines: invalid range {start}-{end} (file has {len(lines)} lines)")
    block = text if text.endswith("\n") else text + "\n"
    new_lines = lines[: start - 1] + [block] + lines[end:]
    after = "".join(new_lines)
    return _commit_change(full, after, before, full.name, validate, "replace_lines")


@mcp.tool
def write_file(path: str, content: str, repo: str | None = None, validate: bool = True) -> dict:
    """Write `content` to `path` (create or overwrite). Auto-backup of any existing file;
    auto-rollback on syntax failure."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    before = _read(full)
    return _commit_change(full, content, before, full.name, validate, "write_file")


@mcp.tool
def multi_edit(edits: list, repo: str | None = None, validate: bool = True) -> dict:
    """Apply many edits ATOMICALLY: back up all targets, apply all, validate all; if ANY edit or
    validation fails, roll back EVERYTHING.

    Each edit is a dict with `op` in {write_file, replace_lines, insert_lines, delete_lines,
    replace_in_file} plus that op's params (path required). Example:
        [{"op": "write_file", "path": "a.py", "content": "x=1\\n"},
         {"op": "replace_lines", "path": "b.py", "start": 2, "end": 2, "text": "y=2"}]
    """
    if not edits:
        return err("multi_edit: no edits provided")

    # Resolve + validate paths and snapshot originals up front.
    journal: list[dict] = []  # {full, before, existed, backup}
    try:
        for e in edits:
            full = _safe_path(e["path"], repo)
            entry = next((j for j in journal if j["full"] == full), None)
            if entry is None:
                journal.append({"full": full, "before": _read(full),
                                "existed": full.exists(), "backup": None})
    except (ValueError, KeyError) as ex:
        return err(f"multi_edit: invalid edit: {ex}")

    # Back up everything first.
    for j in journal:
        if j["existed"]:
            j["backup"] = _backup(j["full"])

    def rollback() -> None:
        for j in journal:
            _restore(j["full"], j["backup"] if j["existed"] else None)

    # Apply all (no per-edit validation; we validate the whole batch after).
    applied: list[dict] = []
    try:
        for e in edits:
            op = e.get("op")
            full = _safe_path(e["path"], repo)
            cur = _read(full)
            if op == "write_file":
                new = e["content"]
            elif op == "replace_lines":
                lines = cur.splitlines(keepends=True)
                s, en = e["start"], e["end"]
                if s < 1 or en > len(lines) or s > en:
                    raise ValueError(f"replace_lines range {s}-{en} invalid for {full}")
                block = e["text"] if e["text"].endswith("\n") else e["text"] + "\n"
                new = "".join(lines[: s - 1] + [block] + lines[en:])
            elif op == "insert_lines":
                lines = cur.splitlines(keepends=True)
                block = e["text"] if e["text"].endswith("\n") else e["text"] + "\n"
                at = max(0, min(e["lineno"] - 1, len(lines)))
                if at > 0 and lines and not lines[at - 1].endswith("\n"):
                    lines[at - 1] += "\n"
                new = "".join(lines[:at] + [block] + lines[at:])
            elif op == "delete_lines":
                lines = cur.splitlines(keepends=True)
                s, en = e["start"], e["end"]
                if s < 1 or en > len(lines) or s > en:
                    raise ValueError(f"delete_lines range {s}-{en} invalid for {full}")
                new = "".join(lines[: s - 1] + lines[en:])
            elif op == "replace_in_file":
                cnt = e.get("count", 0)
                new = cur.replace(e["find"], e["replace"],
                                  *( (cnt,) if cnt else () ))
            else:
                raise ValueError(f"unknown op: {op!r}")
            full.parent.mkdir(parents=True, exist_ok=True)
            full.write_text(new, encoding="utf-8")
            applied.append({"op": op, "path": str(full)})
    except (ValueError, KeyError) as ex:
        rollback()
        return err(f"multi_edit: apply failed, all rolled back: {ex}")

    # Validate every touched file.
    checks = []
    if validate:
        for j in journal:
            sc = _syntax_check(j["full"])
            checks.append({"path": str(j["full"]), **sc})
            if not sc["ok"]:
                rollback()
                return err(
                    f"multi_edit: syntax check failed on {j['full']}, ALL rolled back",
                    failed_path=str(j["full"]), syntax_check=sc, checks=checks,
                )

    diffs = []
    for j in journal:
        diffs.append({"path": str(j["full"]),
                      "diff": _unified(j["before"], _read(j["full"]), j["full"].name)})

    return ok(
        op="multi_edit",
        applied=applied,
        diffs=diffs,
        backups=[j["backup"] for j in journal],
        validated=validate,
        checks=checks,
        rolled_back=False,
    )


@mcp.tool
def undo(path: str, repo: str | None = None) -> dict:
    """Restore the latest backup for `path` (most recent mutating op). Backs up the current
    (post-edit) state first so undo is itself reversible."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    backups = _backups_for(full)
    if not backups:
        return err(f"undo: no backups for {full}", path=str(full))
    latest = backups[-1]
    before = _read(full)
    # Snapshot current state before reverting, then restore.
    _backup(full)
    shutil.copy2(latest, full)
    after = _read(full)
    return ok(op="undo", path=str(full), restored_from=str(latest),
              diff=_unified(before, after, full.name))


@mcp.tool
def list_backups(path: str, repo: str | None = None) -> dict:
    """List available backups for `path`, newest last."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    backups = _backups_for(full)
    items = [{"backup": str(b), "size": b.stat().st_size,
              "mtime": time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(b.stat().st_mtime))}
             for b in backups]
    return ok(path=str(full), count=len(items), backups=items)


# --------------------------------------------------------------------------- #
# Quality metrics (read-only, language-aware, never raises)
# --------------------------------------------------------------------------- #
# Per-extension single-line comment markers.
_LINE_COMMENT = {
    ".py": ("#",), ".rb": ("#",), ".sh": ("#",), ".pl": ("#",), ".r": ("#",),
    ".yaml": ("#",), ".yml": ("#",), ".toml": ("#",),
    ".js": ("//",), ".jsx": ("//",), ".mjs": ("//",), ".cjs": ("//",),
    ".ts": ("//",), ".tsx": ("//",), ".go": ("//",), ".rs": ("//",),
    ".java": ("//",), ".c": ("//",), ".h": ("//",), ".cpp": ("//",),
    ".cc": ("//",), ".hpp": ("//",), ".cs": ("//",), ".swift": ("//",),
    ".kt": ("//",), ".php": ("//", "#"),
    ".sql": ("--",), ".lua": ("--",), ".hs": ("--",),
}

# Branch / control-flow keywords used as a crude cyclomatic-complexity proxy.
_BRANCH_KEYWORDS = (
    "if", "elif", "else if", "else", "for", "while", "case", "catch",
    "except", "switch", "&&", "||", "?", "and", "or",
)


def _branch_complexity(text: str) -> int:
    """Count branch/control-flow keyword occurrences as a simple complexity heuristic.

    Word-boundary matched for alphabetic keywords; substring matched for operators."""
    import re

    total = 0
    for kw in _BRANCH_KEYWORDS:
        if kw[0].isalpha():
            total += len(re.findall(r"\b" + re.escape(kw) + r"\b", text))
        else:
            total += text.count(kw)
    return total


def _python_func_lengths(text: str) -> list[dict]:
    """Return [{name, start, end, length}] for each top-level/nested def/async def via AST."""
    import ast

    try:
        tree = ast.parse(text)
    except Exception:  # noqa: BLE001 — syntax errors must not crash metrics
        return []
    funcs: list[dict] = []
    for node in ast.walk(tree):
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            start = node.lineno
            end = getattr(node, "end_lineno", None) or start
            funcs.append({"name": node.name, "start": start, "end": end,
                          "length": end - start + 1})
    return funcs


def _braced_func_lengths(text: str) -> list[dict]:
    """Heuristic longest-function detection for C-family / brace languages.

    Tracks the span between a likely function-opening line and the brace that closes it."""
    import re

    lines = text.splitlines()
    sig = re.compile(r"[A-Za-z_][\w<>:\*&\s,]*\([^;{]*\)\s*\{?\s*$")
    funcs: list[dict] = []
    i = 0
    n = len(lines)
    while i < n:
        line = lines[i]
        stripped = line.strip()
        # A function-ish signature line that opens (or is about to open) a block.
        if sig.search(stripped) and "{" in (stripped + (lines[i + 1] if i + 1 < n else "")):
            start = i + 1  # 1-based
            depth = 0
            seen = False
            j = i
            while j < n:
                depth += lines[j].count("{") - lines[j].count("}")
                if "{" in lines[j]:
                    seen = True
                if seen and depth <= 0:
                    break
                j += 1
            end = min(j + 1, n)  # 1-based
            if end > start:
                funcs.append({"name": stripped[:60], "start": start, "end": end,
                              "length": end - start + 1})
            i = j + 1
            continue
        i += 1
    return funcs


def _quality_metrics(full: Path) -> dict:
    """Compute read-only quality metrics for a file. Never raises; returns a plain dict."""
    if not full.exists():
        return {"exists": False, "error": "file does not exist"}
    if full.is_dir():
        return {"exists": False, "error": "path is a directory, not a file"}
    try:
        text = full.read_text(encoding="utf-8", errors="replace")
    except Exception as ex:  # noqa: BLE001
        return {"exists": True, "error": f"could not read file: {ex}"}

    ext = full.suffix.lower()
    lines = text.splitlines()
    total_lines = len(lines)
    blank = sum(1 for ln in lines if not ln.strip())
    markers = _LINE_COMMENT.get(ext, ("#",))
    comment = 0
    for ln in lines:
        s = ln.strip()
        if s and any(s.startswith(m) for m in markers):
            comment += 1
    code = total_lines - blank - comment
    # TODO/FIXME/XXX/HACK markers (case-insensitive).
    import re

    todo = len(re.findall(r"\b(?:TODO|FIXME|XXX|HACK)\b", text, flags=re.IGNORECASE))

    if ext == ".py":
        funcs = _python_func_lengths(text)
        func_method = "ast"
    elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".go", ".rs",
                 ".java", ".c", ".h", ".cpp", ".cc", ".hpp", ".cs", ".swift", ".kt"):
        funcs = _braced_func_lengths(text)
        func_method = "brace-heuristic"
    else:
        funcs = []
        func_method = "unsupported"

    longest = max((f["length"] for f in funcs), default=0)
    longest_func = max(funcs, key=lambda f: f["length"], default=None) if funcs else None

    return {
        "exists": True,
        "ext": ext or "(none)",
        "loc": total_lines,
        "code_lines": code,
        "comment_lines": comment,
        "blank_lines": blank,
        "comment_ratio": round(comment / total_lines, 4) if total_lines else 0.0,
        "todo_fixme_count": todo,
        "function_count": len(funcs),
        "function_method": func_method,
        "longest_function_length": longest,
        "longest_function": longest_func,
        "branch_keyword_count": _branch_complexity(text),
    }


@mcp.tool
def quality_metrics(path: str, repo: str | None = None) -> dict:
    """Read-only code-quality snapshot for a file. NEVER raises (returns err on bad/missing input).

    Reports LOC (total lines), code/comment/blank line counts, comment ratio, TODO/FIXME count,
    longest-function length (Python via AST; C-family via a brace heuristic), and a simple
    complexity heuristic (count of branch/control-flow keywords like if/for/while/&&/||).
    Unsupported extensions still get LOC + comment + TODO metrics (function length = 0)."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    m = _quality_metrics(full)
    if not m.get("exists"):
        return err(f"quality_metrics: {m.get('error', 'unavailable')}", path=str(full))
    return ok(path=str(full), **{k: v for k, v in m.items() if k != "exists"})


# --------------------------------------------------------------------------- #
# VALIDATION TOOLS (each degrades gracefully if its tool is absent)
# --------------------------------------------------------------------------- #
@mcp.tool
def validate(path: str, repo: str | None = None) -> dict:
    """Run the appropriate syntax check for `path` (alias surface for syntax_check)."""
    return syntax_check(path, repo)


@mcp.tool
def syntax_check(path: str, repo: str | None = None) -> dict:
    """Syntax-check a file by extension: py_compile (.py), node --check (.js/.ts), gofmt (.go),
    rustc (.rs), json. Returns a 'skipped: not installed' note instead of crashing when a tool
    is missing or the extension is unknown."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    sc = _syntax_check(full)
    return ok(path=str(full), valid=sc["ok"], checker=sc["checker"],
              output=sc["output"], skipped=sc["skipped"])


@mcp.tool
def format_code(path: str, write: bool = False, repo: str | None = None) -> dict:
    """Format a file with the right tool if installed: black/ruff (.py), prettier (.js/.ts/.json/
    .css/.md), gofmt (.go). write=False -> report whether changes are needed (diff-style check);
    write=True -> backs up then formats in place. Degrades gracefully: 'skipped: not installed'."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    if not full.exists():
        return err(f"format_code: no such file: {full}")
    ext = full.suffix.lower()

    formatter: list[str] | None = None
    name = ""
    if ext == ".py":
        if _tool_exists("black"):
            name = "black"
            formatter = ["black", "--quiet"] if write else ["black", "--check", "--diff"]
        elif _tool_exists("ruff"):
            name = "ruff"
            formatter = ["ruff", "format"] if write else ["ruff", "format", "--check", "--diff"]
    elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx", ".json", ".css", ".md", ".html"):
        if _tool_exists("prettier"):
            name = "prettier"
            formatter = ["prettier", "--write"] if write else ["prettier", "--check"]
    elif ext == ".go":
        if _tool_exists("gofmt"):
            name = "gofmt"
            formatter = ["gofmt", "-w"] if write else ["gofmt", "-l", "-d"]

    if formatter is None:
        return ok(path=str(full), formatter=ext, skipped=True,
                  note=f"skipped: no formatter installed for '{ext or 'no-ext'}'")

    backup = _backup(full) if write else None
    try:
        r = subprocess.run([*formatter, str(full)], capture_output=True, text=True, timeout=60)
    except Exception as ex:  # noqa: BLE001
        return ok(path=str(full), formatter=name, skipped=True, note=f"skipped: {ex}")

    return ok(
        path=str(full),
        formatter=name,
        wrote=write,
        backup=backup,
        changed=(r.returncode != 0) if not write else True,
        output=(r.stdout or r.stderr).strip()[:4000],
        skipped=False,
    )


@mcp.tool
def lint(path: str, repo: str | None = None) -> dict:
    """Lint a file with the first available linter: ruff/flake8 (.py), eslint (.js/.ts),
    go vet (.go). Read-only. Degrades gracefully when no linter is installed."""
    try:
        full = _safe_path(path, repo)
    except ValueError as e:
        return err(str(e))
    if not full.exists():
        return err(f"lint: no such file: {full}")
    ext = full.suffix.lower()

    cmd: list[str] | None = None
    name = ""
    cwd = str(_root(repo))
    if ext == ".py":
        if _tool_exists("ruff"):
            name, cmd = "ruff", ["ruff", "check", str(full)]
        elif _tool_exists("flake8"):
            name, cmd = "flake8", ["flake8", str(full)]
        elif _tool_exists("pyflakes"):
            name, cmd = "pyflakes", ["pyflakes", str(full)]
    elif ext in (".js", ".jsx", ".mjs", ".cjs", ".ts", ".tsx"):
        if _tool_exists("eslint"):
            name, cmd = "eslint", ["eslint", str(full)]
    elif ext == ".go":
        if _tool_exists("go"):
            name, cmd = "go vet", ["go", "vet", str(full)]

    if cmd is None:
        return ok(path=str(full), linter=ext, skipped=True,
                  note=f"skipped: no linter installed for '{ext or 'no-ext'}'")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=120, cwd=cwd)
    except Exception as ex:  # noqa: BLE001
        return ok(path=str(full), linter=name, skipped=True, note=f"skipped: {ex}")

    return ok(path=str(full), linter=name, clean=(r.returncode == 0),
              output=(r.stdout or r.stderr).strip()[:6000], skipped=False)


@mcp.tool
def run_tests(repo: str, pattern: str | None = None, timeout_s: int = 120) -> dict:
    """Auto-detect and run a project's test suite: pytest (Python), jest/npm test (JS),
    go test (Go), cargo test (Rust). `pattern` narrows selection where the runner supports it.
    Degrades gracefully: returns a 'skipped: not installed/undetected' note instead of crashing."""
    try:
        root = _root(repo)
    except ValueError as e:
        return err(str(e))
    if not root.is_dir():
        return err(f"run_tests: no such repo dir: {root}")

    cmd: list[str] | None = None
    runner = ""

    has_py = any((root / f).exists() for f in
                 ("pyproject.toml", "setup.py", "setup.cfg", "pytest.ini", "tox.ini")) or \
        any(root.glob("test_*.py")) or any(root.glob("tests"))
    has_node = (root / "package.json").exists()
    has_go = (root / "go.mod").exists() or any(root.glob("*_test.go"))
    has_rust = (root / "Cargo.toml").exists()

    if has_py and _tool_exists("pytest"):
        runner, cmd = "pytest", ["pytest", "-q"]
        if pattern:
            cmd += ["-k", pattern]
    elif has_node and (root / "package.json").exists():
        # Prefer jest if available, else `npm test`.
        if _tool_exists("jest"):
            runner, cmd = "jest", ["jest"]
            if pattern:
                cmd += ["-t", pattern]
        elif _tool_exists("npx"):
            runner, cmd = "npx jest", ["npx", "--no-install", "jest"]
            if pattern:
                cmd += ["-t", pattern]
        elif _tool_exists("npm"):
            runner, cmd = "npm test", ["npm", "test", "--silent"]
    elif has_go and _tool_exists("go"):
        runner, cmd = "go test", ["go", "test", "./..."]
        if pattern:
            cmd += ["-run", pattern]
    elif has_rust and _tool_exists("cargo"):
        runner, cmd = "cargo test", ["cargo", "test"]
        if pattern:
            cmd += [pattern]

    if cmd is None:
        return ok(repo=str(root), skipped=True,
                  note="skipped: no supported test runner detected/installed "
                       "(pytest/jest/go/cargo)")

    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout_s, cwd=str(root))
    except subprocess.TimeoutExpired:
        return err(f"run_tests: {runner} timed out after {timeout_s}s", runner=runner)
    except Exception as ex:  # noqa: BLE001
        return ok(repo=str(root), runner=runner, skipped=True, note=f"skipped: {ex}")

    return ok(
        repo=str(root),
        runner=runner,
        passed=(r.returncode == 0),
        exit_code=r.returncode,
        output=(r.stdout + "\n" + r.stderr).strip()[:8000],
        skipped=False,
    )


if __name__ == "__main__":
    mcp.run()
