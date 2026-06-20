"""Import every server in isolation, register tools, and report tool counts + failures."""
import importlib.util
import sys
import traceback
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
servers = sorted(p.parent.name for p in (ROOT / "servers").glob("*/server.py"))

ok, fail = [], []
for name in servers:
    path = ROOT / "servers" / name / "server.py"
    modname = f"srv_{name.replace('-', '_')}"
    try:
        spec = importlib.util.spec_from_file_location(modname, path)
        mod = importlib.util.module_from_spec(spec)
        sys.modules[modname] = mod
        spec.loader.exec_module(mod)
        # count registered tools (FastMCP keeps them; fall back to attribute scan)
        n = "?"
        try:
            import asyncio
            n = len(asyncio.run(mod.mcp.get_tools()))
        except Exception:
            n = "?"
        ok.append((name, n))
    except Exception:
        fail.append((name, traceback.format_exc().strip().splitlines()[-1]))

print(f"=== {len(ok)}/{len(servers)} servers import OK ===")
for name, n in ok:
    print(f"  ✓ {name:18} {n} tools")
if fail:
    print(f"\n=== {len(fail)} FAILED ===")
    for name, err in fail:
        print(f"  ✗ {name}: {err}")
    sys.exit(1)
print("\nALL SERVERS IMPORT OK ✅")
