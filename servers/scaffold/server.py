"""scaffold — generate a new project from built-in stack templates (boilerplate, README,
LICENSE, .gitignore, CI) so you skip the repetitive setup every time.

Stacks: python-uv, node-ts, static-web, fastapi, react-vite, go, rust, cli, mcp-server.
Extras: license choice, pre-commit, devcontainer, Makefile, .editorconfig, git init."""
from __future__ import annotations

import subprocess
from datetime import date
from pathlib import Path

from mcp_base import make_server

mcp = make_server(
    "scaffold",
    instructions=("new_project(stack, name, dest); list_templates; list_licenses; add_ci; "
                  "add_license; add_precommit; add_devcontainer; add_makefile; git_init."),
)

GITIGNORE = "__pycache__/\n*.pyc\n.venv/\nnode_modules/\ndist/\nbuild/\ntarget/\n.env\n.DS_Store\n"

# --- licenses (full texts, abbreviated where the spec allows) ----------------
_MIT = """MIT License

Copyright (c) {year} {author}

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
"""

_BSD3 = """BSD 3-Clause License

Copyright (c) {year}, {author}

Redistribution and use in source and binary forms, with or without
modification, are permitted provided that the following conditions are met:

1. Redistributions of source code must retain the above copyright notice, this
   list of conditions and the following disclaimer.
2. Redistributions in binary form must reproduce the above copyright notice,
   this list of conditions and the following disclaimer in the documentation
   and/or other materials provided with the distribution.
3. Neither the name of the copyright holder nor the names of its contributors
   may be used to endorse or promote products derived from this software
   without specific prior written permission.

THIS SOFTWARE IS PROVIDED BY THE COPYRIGHT HOLDERS AND CONTRIBUTORS "AS IS"
AND ANY EXPRESS OR IMPLIED WARRANTIES ARE DISCLAIMED. IN NO EVENT SHALL THE
COPYRIGHT HOLDER OR CONTRIBUTORS BE LIABLE FOR ANY DIRECT, INDIRECT,
INCIDENTAL, SPECIAL, EXEMPLARY, OR CONSEQUENTIAL DAMAGES ARISING IN ANY WAY
OUT OF THE USE OF THIS SOFTWARE, EVEN IF ADVISED OF THE POSSIBILITY OF SUCH
DAMAGE.
"""

_APACHE = """                                 Apache License
                           Version 2.0, January 2004
                        http://www.apache.org/licenses/

Copyright {year} {author}

Licensed under the Apache License, Version 2.0 (the "License");
you may not use this file except in compliance with the License.
You may obtain a copy of the License at

    http://www.apache.org/licenses/LICENSE-2.0

Unless required by applicable law or agreed to in writing, software
distributed under the License is distributed on an "AS IS" BASIS,
WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
See the License for the specific language governing permissions and
limitations under the License.

(Full license text: https://www.apache.org/licenses/LICENSE-2.0.txt)
"""

_GPL3 = """{name}
Copyright (C) {year} {author}

This program is free software: you can redistribute it and/or modify
it under the terms of the GNU General Public License as published by
the Free Software Foundation, either version 3 of the License, or
(at your option) any later version.

This program is distributed in the hope that it will be useful,
but WITHOUT ANY WARRANTY; without even the implied warranty of
MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
GNU General Public License for more details.

You should have received a copy of the GNU General Public License
along with this program.  If not, see <https://www.gnu.org/licenses/>.

(Full license text: https://www.gnu.org/licenses/gpl-3.0.txt)
"""

_UNLICENSE = """This is free and unencumbered software released into the public domain.

Anyone is free to copy, modify, publish, use, compile, sell, or distribute this
software, either in source code form or as a compiled binary, for any purpose,
commercial or non-commercial, and by any means.

For more information, please refer to <https://unlicense.org>
"""

LICENSES = {
    "MIT": _MIT, "Apache-2.0": _APACHE, "BSD-3-Clause": _BSD3,
    "GPL-3.0": _GPL3, "Unlicense": _UNLICENSE,
}
LICENSE_SUMMARY = {
    "MIT": "Permissive; keep copyright notice.",
    "Apache-2.0": "Permissive with explicit patent grant.",
    "BSD-3-Clause": "Permissive; no endorsement using names.",
    "GPL-3.0": "Copyleft; derivatives must stay GPL.",
    "Unlicense": "Public domain dedication.",
}


def _render(text: str, **ctx) -> str:
    out = text
    for k, v in ctx.items():
        out = out.replace("{" + k + "}", str(v))
    return out


# Reject names that could escape the destination directory (path traversal) or
# contain path separators. A project name is a single directory component.
_SAFE_NAME_RE = __import__("re").compile(r"^[A-Za-z0-9][A-Za-z0-9._-]{0,127}$")


def _valid_name(name: str) -> bool:
    name = (name or "").strip()
    if not name or name in (".", "..") or "/" in name or "\\" in name:
        return False
    return bool(_SAFE_NAME_RE.match(name))


def _license_text(license: str, name: str, author: str, year: str) -> str:
    tmpl = LICENSES.get(license, _MIT)
    return _render(tmpl, name=name, author=author or name, year=year or str(date.today().year))


# --- templates ---------------------------------------------------------------
TEMPLATES: dict[str, dict[str, str]] = {
    "python-uv": {
        "pyproject.toml": '[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n',
        "src/{name}/__init__.py": '__version__ = "0.1.0"\n',
        "src/{name}/main.py": 'def main():\n    print("hello from {name}")\n\n\nif __name__ == "__main__":\n    main()\n',
        "README.md": "# {name}\n\n> TODO\n\n## Setup\n\n```bash\nuv sync\nuv run python -m {name}.main\n```\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
    "node-ts": {
        "package.json": '{{\n  "name": "{name}",\n  "version": "0.1.0",\n  "type": "module",\n  "scripts": {{"build": "tsc", "start": "node dist/index.js"}}\n}}\n',
        "tsconfig.json": '{{\n  "compilerOptions": {{"target": "ES2022", "module": "ES2022", "outDir": "dist", "strict": true}}\n}}\n',
        "src/index.ts": 'console.log("hello from {name}");\n',
        "README.md": "# {name}\n\n```bash\nnpm install && npm run build && npm start\n```\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
    "static-web": {
        "index.html": '<!doctype html>\n<html><head><meta charset="utf-8"><title>{name}</title>\n'
                       '<link rel="stylesheet" href="style.css"></head>\n<body><h1>{name}</h1>\n'
                       '<script src="app.js"></script></body></html>\n',
        "style.css": "body{{font-family:system-ui;max-width:48rem;margin:3rem auto;padding:0 1rem}}\n",
        "app.js": 'console.log("{name} ready");\n',
        "README.md": "# {name}\n\nOpen index.html or deploy free to GitHub Pages.\n",
        ".gitignore": GITIGNORE,
    },
    "fastapi": {
        "pyproject.toml": '[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
                          'dependencies = ["fastapi>=0.110", "uvicorn>=0.29"]\n',
        "app/__init__.py": "",
        "app/main.py": ('from fastapi import FastAPI\n\napp = FastAPI(title="{name}")\n\n\n'
                        '@app.get("/")\ndef root():\n    return {{"service": "{name}", "ok": True}}\n\n\n'
                        '@app.get("/health")\ndef health():\n    return {{"status": "healthy"}}\n'),
        "README.md": "# {name}\n\n```bash\nuv sync\nuv run uvicorn app.main:app --reload\n```\n\nThen open http://127.0.0.1:8000/docs\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
    "react-vite": {
        "package.json": '{{\n  "name": "{name}",\n  "private": true,\n  "version": "0.1.0",\n  "type": "module",\n'
                        '  "scripts": {{"dev": "vite", "build": "vite build", "preview": "vite preview"}},\n'
                        '  "dependencies": {{"react": "^18.3.0", "react-dom": "^18.3.0"}},\n'
                        '  "devDependencies": {{"@vitejs/plugin-react": "^4.3.0", "vite": "^5.4.0"}}\n}}\n',
        "vite.config.js": "import {{ defineConfig }} from 'vite'\nimport react from '@vitejs/plugin-react'\n\nexport default defineConfig({{ plugins: [react()] }})\n",
        "index.html": '<!doctype html>\n<html><head><meta charset="utf-8"><title>{name}</title></head>\n'
                      '<body><div id="root"></div><script type="module" src="/src/main.jsx"></script></body></html>\n',
        "src/main.jsx": "import React from 'react'\nimport {{ createRoot }} from 'react-dom/client'\nimport App from './App.jsx'\n\ncreateRoot(document.getElementById('root')).render(<App />)\n",
        "src/App.jsx": "export default function App() {{\n  return <h1>{name}</h1>\n}}\n",
        "README.md": "# {name}\n\n```bash\nnpm install && npm run dev\n```\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
    "go": {
        "go.mod": "module {name}\n\ngo 1.22\n",
        "main.go": 'package main\n\nimport "fmt"\n\nfunc main() {{\n\tfmt.Println("hello from {name}")\n}}\n',
        "README.md": "# {name}\n\n```bash\ngo run .\ngo build ./...\n```\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
    "rust": {
        "Cargo.toml": '[package]\nname = "{name}"\nversion = "0.1.0"\nedition = "2021"\n\n[dependencies]\n',
        "src/main.rs": 'fn main() {{\n    println!("hello from {name}");\n}}\n',
        "README.md": "# {name}\n\n```bash\ncargo run\ncargo test\n```\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
    "cli": {
        "pyproject.toml": '[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\ndependencies = []\n\n'
                          '[project.scripts]\n{name} = "{name}.cli:main"\n',
        "src/{name}/__init__.py": '__version__ = "0.1.0"\n',
        "src/{name}/cli.py": ('import argparse\n\n\ndef main(argv=None):\n'
                              '    p = argparse.ArgumentParser(prog="{name}")\n'
                              '    p.add_argument("name", nargs="?", default="world")\n'
                              '    args = p.parse_args(argv)\n'
                              '    print(f"hello, {{args.name}}")\n\n\n'
                              'if __name__ == "__main__":\n    main()\n'),
        "README.md": "# {name}\n\nA small CLI.\n\n```bash\nuv run python -m {name}.cli world\n```\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
    "mcp-server": {
        "pyproject.toml": '[project]\nname = "{name}"\nversion = "0.1.0"\nrequires-python = ">=3.11"\n'
                          'dependencies = ["fastmcp>=2.0"]\n',
        "server.py": ('"""{name} — a FastMCP server."""\n'
                      'from __future__ import annotations\n\n'
                      'from fastmcp import FastMCP\n\n'
                      'mcp = FastMCP("{name}")\n\n\n'
                      '@mcp.tool\n'
                      'def health() -> dict:\n'
                      '    """Liveness check."""\n'
                      '    return {{"ok": True, "server": "{name}"}}\n\n\n'
                      '@mcp.tool\n'
                      'def echo(text: str) -> dict:\n'
                      '    """Echo back the input text."""\n'
                      '    return {{"echo": text}}\n\n\n'
                      'if __name__ == "__main__":\n    mcp.run()\n'),
        "README.md": "# {name}\n\nA FastMCP (stdio) server.\n\n```bash\nuv sync\nuv run python server.py\n```\n\n"
                     "Register in your MCP client by pointing it at `python server.py`.\n",
        ".gitignore": GITIGNORE,
        "LICENSE": _MIT,
    },
}

CI_GITHUB = {
    "python-uv": ("name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                  "    steps:\n      - uses: actions/checkout@v4\n      - uses: astral-sh/setup-uv@v5\n"
                  "      - run: uv sync\n      - run: uv run python -c \"import {name}\"\n"),
    "node-ts": ("name: CI\non: [push, pull_request]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-node@v4\n"
                "      - run: npm install && npm run build\n"),
    "fastapi": ("name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
                "    steps:\n      - uses: actions/checkout@v4\n      - uses: astral-sh/setup-uv@v5\n"
                "      - run: uv sync\n      - run: uv run python -c \"import app.main\"\n"),
    "go": ("name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
           "    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-go@v5\n"
           "      - run: go build ./... && go test ./...\n"),
    "rust": ("name: CI\non: [push, pull_request]\njobs:\n  test:\n    runs-on: ubuntu-latest\n"
             "    steps:\n      - uses: actions/checkout@v4\n      - run: cargo build --verbose && cargo test --verbose\n"),
    "react-vite": ("name: CI\non: [push, pull_request]\njobs:\n  build:\n    runs-on: ubuntu-latest\n"
                   "    steps:\n      - uses: actions/checkout@v4\n      - uses: actions/setup-node@v4\n"
                   "      - run: npm install && npm run build\n"),
}

PRECOMMIT = {
    "python": ("repos:\n  - repo: https://github.com/astral-sh/ruff-pre-commit\n    rev: v0.6.0\n"
               "    hooks:\n      - id: ruff\n      - id: ruff-format\n"
               "  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v4.6.0\n"
               "    hooks:\n      - id: trailing-whitespace\n      - id: end-of-file-fixer\n"
               "      - id: check-yaml\n"),
    "node": ("repos:\n  - repo: https://github.com/pre-commit/mirrors-prettier\n    rev: v3.1.0\n"
             "    hooks:\n      - id: prettier\n"
             "  - repo: https://github.com/pre-commit/pre-commit-hooks\n    rev: v4.6.0\n"
             "    hooks:\n      - id: trailing-whitespace\n      - id: end-of-file-fixer\n"),
}

DEVCONTAINER = {
    "python": '{{\n  "name": "{name}",\n  "image": "mcr.microsoft.com/devcontainers/python:3.12",\n'
              '  "features": {{"ghcr.io/astral-sh/uv:1": {{}}}},\n'
              '  "postCreateCommand": "uv sync"\n}}\n',
    "node": '{{\n  "name": "{name}",\n  "image": "mcr.microsoft.com/devcontainers/javascript-node:20",\n'
            '  "postCreateCommand": "npm install"\n}}\n',
    "go": '{{\n  "name": "{name}",\n  "image": "mcr.microsoft.com/devcontainers/go:1.22"\n}}\n',
    "rust": '{{\n  "name": "{name}",\n  "image": "mcr.microsoft.com/devcontainers/rust:1"\n}}\n',
}

MAKEFILE = {
    "python": (".PHONY: install test lint run\ninstall:\n\tuv sync\ntest:\n\tuv run pytest\n"
               "lint:\n\tuv run ruff check .\nrun:\n\tuv run python -m {name}.main\n"),
    "node": (".PHONY: install build test run\ninstall:\n\tnpm install\nbuild:\n\tnpm run build\n"
             "test:\n\tnpm test\nrun:\n\tnpm start\n"),
    "go": (".PHONY: build test run\nbuild:\n\tgo build ./...\ntest:\n\tgo test ./...\nrun:\n\tgo run .\n"),
    "rust": (".PHONY: build test run\nbuild:\n\tcargo build\ntest:\n\tcargo test\nrun:\n\tcargo run\n"),
}

EDITORCONFIG = ("root = true\n\n[*]\ncharset = utf-8\nend_of_line = lf\ninsert_final_newline = true\n"
                "trim_trailing_whitespace = true\nindent_style = space\nindent_size = 4\n\n"
                "[*.{js,ts,jsx,tsx,json,yml,yaml,html,css}]\nindent_size = 2\n\n"
                "[Makefile]\nindent_style = tab\n")

# Map a stack to a "family" for extras (precommit/devcontainer/makefile)
_FAMILY = {"python-uv": "python", "fastapi": "python", "cli": "python", "mcp-server": "python",
           "node-ts": "node", "react-vite": "node", "static-web": "node", "go": "go", "rust": "rust"}


@mcp.tool
def list_templates() -> dict:
    """List available stacks and the files each generates."""
    return {k: sorted(v) for k, v in TEMPLATES.items()}


@mcp.tool
def list_licenses() -> dict:
    """List available license ids and a one-line summary of each."""
    return dict(LICENSE_SUMMARY)


@mcp.tool
def new_project(stack: str, name: str, dest: str, license: str = "MIT", author: str = "",
                git: bool = False, ci: bool = False, precommit: bool = False,
                makefile: bool = False, devcontainer: bool = False, editorconfig: bool = False) -> dict:
    """Create a new project of `stack` named `name` under `dest`.

    Optionally choose a license (see list_licenses) and add hygiene files (ci/precommit/
    makefile/devcontainer/editorconfig) and run git init — all in one call. Returns files written."""
    if stack not in TEMPLATES:
        return {"error": f"unknown stack '{stack}'. Available: {list(TEMPLATES)}"}
    if license not in LICENSES:
        return {"error": f"unknown license '{license}'. Available: {list(LICENSES)}"}
    if not _valid_name(name):
        return {"error": "invalid project name (use letters/digits/._- only, no path separators)"}
    if not (dest or "").strip():
        return {"error": "dest is required"}
    root = (Path(dest).expanduser() / name).resolve()
    dest_root = Path(dest).expanduser().resolve()
    if root.parent != dest_root:
        return {"error": "resolved project path escapes destination directory"}
    if root.exists() and any(root.iterdir()):
        return {"error": f"{root} already exists and is non-empty"}
    year = str(date.today().year)
    written = []
    for rel, content in TEMPLATES[stack].items():
        rel = _render(rel, name=name)
        path = root / rel
        path.parent.mkdir(parents=True, exist_ok=True)
        if rel == "LICENSE":
            content = _license_text(license, name, author, year)
        else:
            content = _render(content, name=name, author=author or name, year=year)
        path.write_text(content, encoding="utf-8")
        written.append(str(path))
    extras = {}
    if ci:
        extras["ci"] = _add_ci(str(root), stack, name)
    if precommit:
        extras["precommit"] = _add_precommit(str(root), stack)
    if makefile:
        extras["makefile"] = _add_makefile(str(root), stack, name)
    if devcontainer:
        extras["devcontainer"] = _add_devcontainer(str(root), stack, name)
    if editorconfig:
        extras["editorconfig"] = _add_editorconfig(str(root))
    if git:
        extras["git"] = _git_init(str(root))
    return {"stack": stack, "name": name, "license": license, "root": str(root),
            "files": written, "extras": extras}


def _add_ci(project_dir: str, stack: str, name: str = "app") -> dict:
    if stack not in CI_GITHUB:
        return {"error": f"no CI template for '{stack}'. Have: {list(CI_GITHUB)}"}
    path = Path(project_dir).expanduser() / ".github" / "workflows" / "ci.yml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(CI_GITHUB[stack], name=name), encoding="utf-8")
    return {"ok": True, "path": str(path)}


@mcp.tool
def add_ci(project_dir: str, stack: str, name: str = "app") -> dict:
    """Add a GitHub Actions CI workflow for the given stack."""
    return _add_ci(project_dir, stack, name)


def _add_license(project_dir: str, license: str = "MIT", author: str = "", year: str = "") -> dict:
    if license not in LICENSES:
        return {"error": f"unknown license '{license}'. Available: {list(LICENSES)}"}
    root = Path(project_dir).expanduser()
    name = root.name
    path = root / "LICENSE"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_license_text(license, name, author, year or str(date.today().year)), encoding="utf-8")
    return {"ok": True, "path": str(path), "license": license}


@mcp.tool
def add_license(project_dir: str, license: str = "MIT", author: str = "", year: str = "") -> dict:
    """Write a LICENSE file into an existing project (see list_licenses for ids)."""
    return _add_license(project_dir, license, author, year)


def _add_precommit(project_dir: str, stack: str = "python-uv") -> dict:
    fam = _FAMILY.get(stack, "python")
    cfg = PRECOMMIT.get(fam, PRECOMMIT["python"])
    path = Path(project_dir).expanduser() / ".pre-commit-config.yaml"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(cfg, encoding="utf-8")
    return {"ok": True, "path": str(path), "family": fam}


@mcp.tool
def add_precommit(project_dir: str, stack: str = "python-uv") -> dict:
    """Write a .pre-commit-config.yaml appropriate for the stack family (python/node)."""
    return _add_precommit(project_dir, stack)


def _add_devcontainer(project_dir: str, stack: str = "python-uv", name: str = "") -> dict:
    fam = _FAMILY.get(stack, "python")
    tmpl = DEVCONTAINER.get(fam, DEVCONTAINER["python"])
    root = Path(project_dir).expanduser()
    path = root / ".devcontainer" / "devcontainer.json"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(tmpl, name=name or root.name), encoding="utf-8")
    return {"ok": True, "path": str(path), "family": fam}


@mcp.tool
def add_devcontainer(project_dir: str, stack: str = "python-uv", name: str = "") -> dict:
    """Write a .devcontainer/devcontainer.json for the stack family."""
    return _add_devcontainer(project_dir, stack, name)


def _add_makefile(project_dir: str, stack: str = "python-uv", name: str = "app") -> dict:
    fam = _FAMILY.get(stack, "python")
    tmpl = MAKEFILE.get(fam, MAKEFILE["python"])
    path = Path(project_dir).expanduser() / "Makefile"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(_render(tmpl, name=name), encoding="utf-8")
    return {"ok": True, "path": str(path), "family": fam}


@mcp.tool
def add_makefile(project_dir: str, stack: str = "python-uv", name: str = "app") -> dict:
    """Write a Makefile with install/test/lint/run targets for the stack family."""
    return _add_makefile(project_dir, stack, name)


def _add_editorconfig(project_dir: str) -> dict:
    path = Path(project_dir).expanduser() / ".editorconfig"
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(EDITORCONFIG, encoding="utf-8")
    return {"ok": True, "path": str(path)}


@mcp.tool
def add_editorconfig(project_dir: str) -> dict:
    """Write a sensible .editorconfig into a project."""
    return _add_editorconfig(project_dir)


def _git_init(project_dir: str, initial_commit: bool = True, branch: str = "main") -> dict:
    root = Path(project_dir).expanduser()
    if not root.exists():
        return {"error": f"no such directory: {project_dir}"}

    def run(*args):
        try:
            return subprocess.run(["git", "-C", str(root), *args], capture_output=True,
                                  text=True, timeout=30)
        except Exception as e:  # noqa: BLE001
            return None if isinstance(e, FileNotFoundError) else e

    res = run("init", "-b", branch)
    if res is None:
        return {"error": "git not found on PATH"}
    if (root / ".git").is_dir() is False:
        # older git without -b: fall back
        run("init")
        run("checkout", "-b", branch)
    committed = False
    if initial_commit:
        run("add", "-A")
        c = run("commit", "-m", "Initial commit")
        committed = bool(c and getattr(c, "returncode", 1) == 0)
    return {"ok": True, "path": str(root), "branch": branch, "committed": committed}


@mcp.tool
def git_init(project_dir: str, initial_commit: bool = True, branch: str = "main") -> dict:
    """Initialize a git repo in the project (optionally with a first commit on `branch`)."""
    return _git_init(project_dir, initial_commit, branch)


if __name__ == "__main__":
    mcp.run()
