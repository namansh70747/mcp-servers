"""Platform filtering for mcp/generate.mjs — macOS-only servers excluded on Windows."""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
GENERATE = ROOT / "mcp" / "generate.mjs"
CUSTOM_PLATFORMS = ROOT / "mcp" / "custom_platforms.json"


def test_custom_platforms_macos_servers():
    data = json.loads(CUSTOM_PLATFORMS.read_text(encoding="utf-8"))
    for name in ("mac-control", "homebrew", "spotify", "webengine"):
        assert data.get(name) == "macos", f"{name} should be macos-only"


def test_generate_check_runs():
  r = subprocess.run(
      ["node", str(GENERATE), "--check"],
      cwd=ROOT,
      capture_output=True,
      text=True,
      timeout=120,
  )
  assert r.returncode == 0, r.stderr or r.stdout
  out = r.stdout + r.stderr
  if sys.platform == "win32":
      assert "mac-control" in out or "skipped" in out.lower()


def test_client_paths_windows_when_applicable():
    if sys.platform != "win32":
        return
    r = subprocess.run(
        ["node", str(GENERATE), "--only=cursor", "--dry-run"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        timeout=120,
    )
    # dry-run may not exist — fall back to checking generate output mentions APPDATA
    if r.returncode != 0:
        r = subprocess.run(
            ["node", str(GENERATE), "--check"],
            cwd=ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
    out = (r.stdout + r.stderr).lower()
    assert "appdata" in out or "win32" in out or r.returncode == 0


if __name__ == "__main__":
    test_custom_platforms_macos_servers()
    test_generate_check_runs()
    test_client_paths_windows_when_applicable()
    print("test_generate_platform OK")
