import asyncio
import importlib.util
import os
import sys
import tempfile
import traceback
from pathlib import Path

os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="mcp-check-")
ROOT = Path("/Users/namansharma/mcp-servers")

servers = sorted(p.parent.name for p in (ROOT / "servers").glob("*/server.py"))
print(f"Found {len(servers)} servers to check\n")

ok, fail = [], []
for name in servers:
    path = ROOT / "servers" / name / "server.py"
    modname = f"srv_{name.replace(chr(45), chr(95))}"
    try:
        spec = importlib.util.spec_from_file_location(modname, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        
        n = "?"
        try:
            import asyncio
            n = len(asyncio.run(mod.mcp.get_tools()))
        except Exception:
            try:
                n = len(list(mod.mcp._tools.keys()))
            except Exception:
                n = "?"
        ok.append((name, n))
        print(f"  ✓ {name:22} - {n} tools")
    except Exception:
        fail.append((name, traceback.format_exc().strip().splitlines()[-1]))
        print(f"  ✗ {name:22} - FAILED")

print(f"\n{'='*50}")
print(f"=== {len(ok)}/{len(servers)} servers import OK ===")
if fail:
    print(f"\n=== {len(fail)} FAILED ===")
    for name, err in fail:
        print(f"  ✗ {name}: {err}")
