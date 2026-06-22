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


# --------------------------------------------------------------------------- read-only audits / plans

def _digits(s: str) -> tuple[int, ...]:
    """Extract leading numeric components of a version string as a tuple of ints.

    Handles dotted ('3.2.6'), date-stamped ('20250814.1'), and suffixed ('4.0.2_1') versions.
    Non-numeric / missing -> empty tuple."""
    import re
    parts: list[int] = []
    for tok in re.split(r"[._\-+]", str(s or "")):
        m = re.match(r"\d+", tok)
        if m:
            parts.append(int(m.group(0)))
        else:
            break
    return tuple(parts)


def _version_gap(installed: str, current: str) -> dict:
    """Best-effort heuristic for how far an installed version is behind the current one.

    Returns {'behind': 'major'|'minor'|'patch'|'unknown', 'major_jump': int, 'far_behind': bool}.
    `far_behind` flags a major-version jump >= 1, or a date-stamped version that is over a
    year stale (leading component differs by >= 10000, i.e. a YYYYMMDD bump of a full year)."""
    iv, cv = _digits(installed), _digits(current)
    if not iv or not cv:
        return {"behind": "unknown", "major_jump": 0, "far_behind": False}
    imaj, cmaj = iv[0], cv[0]
    major_jump = cmaj - imaj if cmaj > imaj else 0
    # date-stamped (YYYYMMDD-style, 8-digit) leading component: treat a >=1y delta as far behind
    date_stamped = imaj >= 10_000_000 and cmaj >= 10_000_000
    if date_stamped:
        far = (cmaj - imaj) >= 10000  # ~1 year on a YYYYMMDD stamp
        return {"behind": "major" if far else "minor", "major_jump": 0, "far_behind": bool(far)}
    if major_jump >= 1:
        behind = "major"
    elif len(cv) > 1 and len(iv) > 1 and cv[1] > iv[1]:
        behind = "minor"
    else:
        behind = "patch"
    return {"behind": behind, "major_jump": int(major_jump), "far_behind": major_jump >= 1}


@mcp.tool
def security_audit(major_only: bool = False) -> dict:
    """Read-only audit of outdated formulae & casks, flagging packages that are FAR BEHIND.

    Runs `brew outdated --json=v2` (no network mutation, never installs/upgrades) and computes a
    best-effort version gap for each package. A package is `far_behind` when it is at least one
    major version behind (or a date-stamped version that is ~1 year+ stale). Pinned packages are
    reported but excluded from the upgrade recommendation.

    major_only=True returns only the far-behind packages in the lists. Always returns a dict; never raises."""
    r = _brew_json("outdated", "--json=v2")
    if not r.get("ok"):
        return {"ok": False, "err": r.get("err", "could not run brew outdated"),
                "hint": "ensure Homebrew is installed (https://brew.sh)"}
    data = r.get("data") or {}
    formulae_out: list[dict] = []
    casks_out: list[dict] = []
    for kind, src, dest in (("formula", data.get("formulae", []), formulae_out),
                            ("cask", data.get("casks", []), casks_out)):
        for item in src or []:
            if not isinstance(item, dict):
                continue
            installed_list = item.get("installed_versions") or []
            installed = installed_list[0] if installed_list else ""
            current = item.get("current_version") or ""
            gap = _version_gap(installed, current)
            entry = {
                "name": item.get("name"),
                "kind": kind,
                "installed": installed,
                "current": current,
                "pinned": bool(item.get("pinned")),
                "behind": gap["behind"],
                "major_jump": gap["major_jump"],
                "far_behind": gap["far_behind"],
            }
            if major_only and not gap["far_behind"]:
                continue
            dest.append(entry)
    all_items = formulae_out + casks_out
    far = [e for e in all_items if e["far_behind"]]
    pinned = [e for e in all_items if e["pinned"]]
    upgradable = [e["name"] for e in all_items if not e["pinned"] and e["name"]]
    return {
        "ok": True,
        "total_outdated": len(all_items),
        "far_behind_count": len(far),
        "formulae": formulae_out,
        "casks": casks_out,
        "far_behind": far,
        "pinned": [e["name"] for e in pinned],
        "recommend_upgrade": upgradable,
        "hint": "review then run upgrade(name=..., confirm=True); pinned packages need unpin() first" if upgradable else "everything up to date",
    }


def _parse_cleanup_freed(out: str) -> dict:
    """Pull the 'would free approximately <N><unit>' summary from a `brew cleanup --dry-run` run.

    Returns {'human': '122.6MB', 'mb': 122.6} or {'human': None, 'mb': 0.0} when absent."""
    import re
    m = re.search(r"free approximately\s+([\d.,]+)\s*([KMGT]?B)", out or "", re.IGNORECASE)
    if not m:
        return {"human": None, "mb": 0.0}
    num = float(m.group(1).replace(",", ""))
    unit = m.group(2).upper()
    factor = {"B": 1 / 1_048_576, "KB": 1 / 1024, "MB": 1.0, "GB": 1024.0, "TB": 1024.0 * 1024.0}.get(unit, 1.0)
    return {"human": f"{m.group(1)}{m.group(2)}", "mb": round(num * factor, 2)}


@mcp.tool
def cleanup_plan() -> dict:
    """Dry-run plan of reclaimable disk space: `brew cleanup --dry-run` + unused dependency leaves.

    Strictly read-only — never removes anything. Combines:
      - `brew cleanup --dry-run`: stale downloads / old versions, with the 'would free' estimate.
      - `brew autoremove --dry-run`: formulae installed only as dependencies that are now unneeded.
    Returns the previews plus a count of removable items and the estimated MB freed. Never raises;
    to actually reclaim space call cleanup(dry_run=False, confirm=True) / autoremove(dry_run=False, confirm=True)."""
    c = _brew("cleanup", "--dry-run", timeout=300)
    a = _brew("autoremove", "--dry-run", timeout=300)
    cleanup_lines = _lines(c.get("out", "")) if c.get("ok") else []
    would_remove = [ln for ln in cleanup_lines if ln.lower().startswith("would remove")]
    freed = _parse_cleanup_freed(c.get("out", "")) if c.get("ok") else {"human": None, "mb": 0.0}
    auto_lines = _lines(a.get("out", "")) if a.get("ok") else []
    # autoremove dry-run prints a "Would remove:" header then formula names; collect plausible names
    unused_leaves = []
    for ln in auto_lines:
        s = ln.strip()
        low = s.lower()
        if low.startswith("==>") or low.startswith("would remove") or "autoremove" in low:
            continue
        unused_leaves.append(s)
    return {
        "ok": bool(c.get("ok") or a.get("ok")),
        "estimated_freed": freed["human"],
        "estimated_freed_mb": freed["mb"],
        "cleanup_removable_count": len(would_remove),
        "cleanup_preview": would_remove[:200],
        "unused_leaves": unused_leaves[:200],
        "unused_leaves_count": len(unused_leaves),
        "cleanup_ok": bool(c.get("ok")),
        "autoremove_ok": bool(a.get("ok")),
        "errors": {k: v for k, v in (("cleanup", c.get("err")), ("autoremove", a.get("err"))) if v and not (c.get("ok") if k == "cleanup" else a.get("ok"))},
        "hint": "to reclaim: cleanup(dry_run=False, confirm=True) and/or autoremove(dry_run=False, confirm=True)",
    }


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
