"""Start minimized Chrome/Edge for Apollo and auto-login via ensure_ready(). Called by setup-apollo-web.ps1."""
from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location(
    "apollo_srv", ROOT / "servers" / "apollo" / "server.py")
mod = importlib.util.module_from_spec(spec)
spec.loader.exec_module(mod)
result = mod._ensure_ready_sync()
print(json.dumps(result, indent=2))
sys.exit(0 if result.get("ready") else 1)
