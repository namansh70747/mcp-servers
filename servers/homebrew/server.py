"""homebrew — manage packages via Homebrew: search/info/list/outdated/deps/leaves/services/bundle/
doctor/cleanup/install/upgrade/uninstall.

Anything that mutates your system (install, upgrade, uninstall, cleanup, services, taps, bundle install,
update) is gated behind confirm=True. Read-only diagnostics (doctor, config, deps, leaves) are open."""
from __future__ import annotations

import json
import shutil
import subprocess

from mcp_base import data_dir, make_server

mcp = make_server("homebrew", instructions="Manage brew packages: search, info, list, outdated, deps, leaves, services, bundle, doctor, cleanup, install(confirm=True).")


def _brew(*args: str, timeout: int = 180) -> dict:
    brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
    try:
        p = subprocess.run([brew, *args], capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": p.stdout.strip()[:12000], "err": p.stderr.strip()[:4000]}
    except FileNotFoundError:
        return {"ok": False, "err": "Homebrew not installed — see https://brew.sh"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"brew timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)}


def _brew_json(*args: str, timeout: int = 120) -> dict:
    """Run brew with --json=v2 and parse. Returns {'ok', 'data'|'err'}."""
    brew = shutil.which("brew") or "/opt/homebrew/bin/brew"
    try:
        p = subprocess.run([brew, *args], capture_output=True, text=True, timeout=timeout)
        if p.returncode != 0:
            return {"ok": False, "err": p.stderr.strip()[:4000]}
        return {"ok": True, "data": json.loads(p.stdout)}
    except FileNotFoundError:
        return {"ok": False, "err": "Homebrew not installed — see https://brew.sh"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"brew timed out after {timeout}s"}
    except json.JSONDecodeError as e:
        return {"ok": False, "err": f"could not parse brew json: {e}"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)}


def _lines(out: str) -> list[str]:
    return [ln for ln in (out or "").splitlines() if ln.strip()]


def _bad_name(name: str, field: str = "name") -> dict | None:
    """Validate a formula/cask/tap name. Returns an error dict, or None if acceptable.

    Rejects empty values and anything that would be parsed by brew as an option flag (leading '-')
    or contains shell/whitespace metacharacters (defence-in-depth; brew args are passed as a list)."""
    if not isinstance(name, str) or not name.strip():
        return {"ok": False, "err": f"{field} must be a non-empty string"}
    n = name.strip()
    if n.startswith("-"):
        return {"ok": False, "err": f"invalid {field} (must not start with '-')"}
    if any(ch in n for ch in (" ", "\t", "\n", "\r", ";", "|", "&", "$", "`", "(", ")", "<", ">")):
        return {"ok": False, "err": f"invalid {field} (illegal characters)"}
    return None


# --------------------------------------------------------------------------- query / info

@mcp.tool
def search(query: str) -> dict:
    """Search Homebrew for a formula/cask."""
    if not isinstance(query, str) or not query.strip():
        return {"ok": False, "err": "query must be a non-empty string"}
    return _brew("search", "--", query.strip())


@mcp.tool
def info(name: str) -> dict:
    """Show info about a formula/cask."""
    if (e := _bad_name(name)):
        return e
    return _brew("info", "--", name.strip())


@mcp.tool
def info_json(name: str) -> dict:
    """Structured info for a formula or cask: versions, deps, homepage, caveats, install state."""
    if (e := _bad_name(name)):
        return e
    r = _brew_json("info", "--json=v2", "--", name.strip())
    if not r.get("ok"):
        return r
    data = r["data"]
    formulae = data.get("formulae", [])
    casks = data.get("casks", [])
    if formulae:
        f = formulae[0]
        return {"ok": True, "type": "formula", "name": f.get("name"),
                "desc": f.get("desc"), "homepage": f.get("homepage"),
                "stable": (f.get("versions") or {}).get("stable"),
                "installed": [i.get("version") for i in f.get("installed", [])],
                "dependencies": f.get("dependencies", []),
                "build_dependencies": f.get("build_dependencies", []),
                "caveats": f.get("caveats"), "deprecated": f.get("deprecated"),
                "outdated": f.get("outdated")}
    if casks:
        c = casks[0]
        return {"ok": True, "type": "cask", "token": c.get("token"),
                "name": c.get("name"), "desc": c.get("desc"), "homepage": c.get("homepage"),
                "version": c.get("version"), "installed": c.get("installed"),
                "caveats": c.get("caveats"), "depends_on": c.get("depends_on")}
    return {"ok": False, "err": f"no formula or cask named '{name}'"}


@mcp.tool
def list_installed() -> dict:
    """List installed formulae with versions."""
    return _brew("list", "--versions")


@mcp.tool
def list_casks() -> dict:
    """List installed casks with versions."""
    r = _brew("list", "--cask", "--versions")
    return {**r, "casks": _lines(r.get("out", ""))}


@mcp.tool
def leaves() -> dict:
    """List installed formulae that are not dependencies of another formula (top-level packages)."""
    r = _brew("leaves")
    return {**r, "leaves": _lines(r.get("out", ""))}


@mcp.tool
def deps(name: str, tree: bool = False) -> dict:
    """Show dependencies of a formula. tree=True shows the full dependency tree."""
    if (e := _bad_name(name)):
        return e
    args = ["deps"]
    if tree:
        args.append("--tree")
    args += ["--", name.strip()]
    r = _brew(*args)
    if not tree:
        return {**r, "deps": _lines(r.get("out", ""))}
    return r


@mcp.tool
def uses(name: str, installed: bool = True) -> dict:
    """Show formulae that depend on `name` (reverse deps). installed=True limits to what you have installed."""
    if (e := _bad_name(name)):
        return e
    args = ["uses"]
    if installed:
        args.append("--installed")
    args += ["--", name.strip()]
    r = _brew(*args)
    return {**r, "uses": _lines(r.get("out", ""))}


@mcp.tool
def outdated() -> dict:
    """Show outdated packages."""
    return _brew("outdated")


@mcp.tool
def doctor() -> dict:
    """Run `brew doctor` to diagnose common issues (read-only)."""
    return _brew("doctor", timeout=120)


@mcp.tool
def config() -> dict:
    """Show Homebrew's configuration / environment (read-only)."""
    return _brew("config", timeout=60)


@mcp.tool
def list_taps() -> dict:
    """List the currently tapped repositories."""
    r = _brew("tap")
    return {**r, "taps": _lines(r.get("out", ""))}


@mcp.tool
def analytics(on: bool | None = None, confirm: bool = False) -> dict:
    """Read or set Homebrew analytics. on=None reads state; setting requires confirm=True."""
    if on is None:
        return _brew("analytics", "state")
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to turn analytics {'on' if on else 'off'}"}
    return _brew("analytics", "on" if on else "off")


# --------------------------------------------------------------------------- services

@mcp.tool
def services_list() -> dict:
    """List Homebrew-managed background services and their status."""
    return _brew("services", "list", timeout=60)


@mcp.tool
def services_run(action: str, name: str = "", confirm: bool = False) -> dict:
    """Control a brew service. action: start|stop|restart|run. name empty applies to all (where valid).
    Mutating — requires confirm=True."""
    action = action.strip().lower()
    if action not in ("start", "stop", "restart", "run"):
        return {"ok": False, "err": "action must be one of: start, stop, restart, run"}
    if name and (e := _bad_name(name)):
        return e
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to {action} service '{name or 'all'}'"}
    args = ["services", action, name if name else "--all"]
    return _brew(*args, timeout=120)


# --------------------------------------------------------------------------- mutating: install / upgrade / remove

@mcp.tool
def install(name: str, cask: bool = False, confirm: bool = False) -> dict:
    """Install a package. Mutates your system — requires confirm=True."""
    if (e := _bad_name(name)):
        return e
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to install '{name}'"}
    return _brew("install", *(["--cask"] if cask else []), "--", name.strip(), timeout=600)


@mcp.tool
def upgrade(name: str = "", confirm: bool = False) -> dict:
    """Upgrade a package (or all if name empty). Requires confirm=True."""
    if name and (e := _bad_name(name)):
        return e
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True"}
    return _brew("upgrade", *(["--", name.strip()] if name else []), timeout=600)


@mcp.tool
def uninstall(name: str, cask: bool = False, force: bool = False, confirm: bool = False) -> dict:
    """Uninstall a package. Destructive — requires confirm=True. force=True ignores dependents."""
    if (e := _bad_name(name)):
        return e
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to uninstall '{name}'"}
    args = ["uninstall"]
    if cask:
        args.append("--cask")
    if force:
        args.append("--force")
    args += ["--", name.strip()]
    return _brew(*args, timeout=300)


@mcp.tool
def cleanup(name: str = "", dry_run: bool = True, confirm: bool = False) -> dict:
    """Remove stale downloads/old versions. dry_run=True (default) only previews. To actually clean,
    set dry_run=False AND confirm=True."""
    if name and (e := _bad_name(name)):
        return e
    args = ["cleanup"]
    if dry_run:
        args.append("--dry-run")
    elif not confirm:
        return {"blocked": "confirm required", "hint": "call again with dry_run=False and confirm=True to clean"}
    if name:
        args += ["--", name.strip()]
    return _brew(*args, timeout=300)


@mcp.tool
def autoremove(dry_run: bool = True, confirm: bool = False) -> dict:
    """Remove formulae installed only as dependencies that are no longer needed. dry_run previews;
    actual removal needs dry_run=False AND confirm=True."""
    args = ["autoremove"]
    if dry_run:
        args.append("--dry-run")
    elif not confirm:
        return {"blocked": "confirm required", "hint": "call again with dry_run=False and confirm=True to autoremove"}
    return _brew(*args, timeout=300)


@mcp.tool
def pin(name: str) -> dict:
    """Pin a formula to prevent it from being upgraded (reversible with unpin)."""
    if (e := _bad_name(name)):
        return e
    return _brew("pin", "--", name.strip())


@mcp.tool
def unpin(name: str) -> dict:
    """Unpin a previously pinned formula."""
    if (e := _bad_name(name)):
        return e
    return _brew("unpin", "--", name.strip())


@mcp.tool
def tap(name: str = "", confirm: bool = False) -> dict:
    """Add a tap. Empty name lists current taps (read-only). Adding a tap requires confirm=True."""
    if not name:
        return list_taps()
    if (e := _bad_name(name, "tap")):
        return e
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to tap '{name}'"}
    return _brew("tap", "--", name.strip(), timeout=180)


@mcp.tool
def untap(name: str, confirm: bool = False) -> dict:
    """Remove a tap. Requires confirm=True."""
    if (e := _bad_name(name, "tap")):
        return e
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to untap '{name}'"}
    return _brew("untap", "--", name.strip())


@mcp.tool
def update(confirm: bool = False) -> dict:
    """Fetch the newest Homebrew package metadata (`brew update`). Requires confirm=True."""
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True to run brew update"}
    return _brew("update", timeout=300)


# --------------------------------------------------------------------------- bundle (Brewfile)

def _brewfile_path(path: str) -> str:
    import os
    if path:
        return os.path.expanduser(path)
    return str(data_dir("homebrew") / "Brewfile")


@mcp.tool
def bundle_dump(path: str = "", force: bool = False, confirm: bool = False) -> dict:
    """Write a Brewfile snapshot of installed formulae/casks/taps. Writes a file — requires confirm=True.
    Defaults to a Brewfile in the server data dir; force=True overwrites an existing one."""
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True to write the Brewfile"}
    fp = _brewfile_path(path)
    args = ["bundle", "dump", "--file", fp]
    if force:
        args.append("--force")
    r = _brew(*args, timeout=120)
    if r.get("ok"):
        r["path"] = fp
    return r


@mcp.tool
def bundle_check(path: str = "") -> dict:
    """Check whether everything in a Brewfile is installed (read-only)."""
    fp = _brewfile_path(path)
    return _brew("bundle", "check", "--file", fp, timeout=120)


@mcp.tool
def bundle_install(path: str = "", confirm: bool = False) -> dict:
    """Install everything listed in a Brewfile. Mutating — requires confirm=True."""
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True to install from the Brewfile"}
    fp = _brewfile_path(path)
    return _brew("bundle", "install", "--file", fp, timeout=1800)


if __name__ == "__main__":
    mcp.run()
