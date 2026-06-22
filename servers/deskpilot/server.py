"""deskpilot — the universal macOS "computer use" connector: drive ANY app like a human.

deskpilot is the layer below mac-control. Where mac-control types into whatever already has
focus, deskpilot can SEE the screen, READ the UI of any app via the macOS Accessibility tree,
and CLICK / TYPE / DRAG / SCROLL anywhere with pixel-precise synthetic input. A vision-capable
model (Fable 5 / Claude / Qwen-VL) is the brain; deskpilot is the hands + eyes. This is what
lets an agent open WhatsApp/Slack/Mail/Finder, find a contact, type a message, and send it.

THE LOOP (call health() first):
  1. orient   — health(); get_frontmost() or open_app_and_wait("WhatsApp")
  2. perceive — ui_elements()/find_element() (Accessibility = precise, cheap, gives id + bbox).
                For poor-a11y apps (Electron: WhatsApp/Slack/Discord, games) fall back to
                screenshot() and let the vision model pick a coordinate; element_at(x,y) bridges
                a picked pixel back to an element id.
  3. act      — prefer press_element(id)/set_field(id,text)/click_element(id); fall back to
                click(x,y)/type_text/press_keys.
  4. verify   — screenshot() and/or read_text_field(id); loop.

ENGINE: PyObjC (free) — Quartz CGEvent for mouse/keyboard, ApplicationServices AX* for the
accessibility tree, AppKit for app activation; the system `screencapture`/`sips` CLIs for images.
  install:  uv sync --group desktop   (macOS only)

PERMISSIONS (the #1 gotcha): the app that LAUNCHES this server (Terminal / iTerm2 / VS Code /
the Qwen app) must be granted, in System Settings → Privacy & Security:
  • Accessibility   — for the AX tree + synthetic mouse/keyboard
  • Screen Recording — for screenshots to contain real pixels
…and that app must be RESTARTED after granting (the trust flag often won't flip live). health()
reports exactly what is missing.

AUTONOMY: fully autonomous by default — act tools execute immediately (every action is logged;
typed text is never logged verbatim). Set DESKPILOT_REQUIRE_CONFIRM=1 to add a brake: act tools
then require confirm=True. Coordinates are clamped to the union of active displays. macOS only;
on any other platform every tool degrades to a clean err() and health() still answers.
"""
from __future__ import annotations

import datetime as dt
import functools
import os
import re
import shutil
import subprocess
import sys
import time
from pathlib import Path

from mcp_base import data_dir, err, get_env_bool, get_env_int, get_logger, make_server, not_found, ok

log = get_logger("deskpilot")

# --------------------------------------------------------------------------- optional engine
# Every PyObjC import is gated so the module imports cleanly on non-macOS / before `uv sync`.
_PYOBJC = False
_IMPORT_ERR: str | None = None
try:  # pragma: no cover - exercised only on a configured macOS host
    import Quartz  # type: ignore
    import ApplicationServices as AXS  # type: ignore
    from AppKit import (  # type: ignore
        NSRunningApplication,
        NSScreen,
        NSWorkspace,
        NSApplicationActivateAllWindows,
        NSApplicationActivateIgnoringOtherApps,
    )

    _PYOBJC = True
except Exception as e:  # noqa: BLE001
    _IMPORT_ERR = f"{type(e).__name__}: {e}"

# FastMCP wraps an Image return into an MCP ImageContent. Its location moved across versions.
try:
    from fastmcp.utilities.types import Image  # fastmcp 3.x
except Exception:  # noqa: BLE001
    try:
        from fastmcp import Image  # older fastmcp re-export
    except Exception:  # noqa: BLE001
        Image = None  # type: ignore

mcp = make_server(
    "deskpilot",
    instructions=(
        "Universal macOS computer-use: operate ANY app (incl. WhatsApp/Slack/Mail/Finder). "
        "Call health() FIRST (needs Accessibility + Screen Recording granted to the launching "
        "app, then a restart). Loop: perceive (ui_elements/find_element = precise Accessibility "
        "tree with id+bbox; screenshot() for vision when a11y is thin, e.g. Electron apps) -> act "
        "(prefer press_element(id)/set_field/click_element; else click(x,y)/type_text/press_keys) "
        "-> verify (screenshot/read_text_field). Coordinates are points (top-left origin); element "
        "bbox is already in points so click_element avoids Retina pixel math. open_app_and_wait "
        "removes the launch race. recipe(name) returns a step plan. Needs a vision-capable model "
        "for the screenshot path. macOS only; install with `uv sync --group desktop`."
    ),
)

# --------------------------------------------------------------------------- layout
ROOT = data_dir("deskpilot")
TMP = ROOT / "shots"
TMP.mkdir(parents=True, exist_ok=True)

MAX_TEXT = 10000

# --------------------------------------------------------------------------- AX names (plain strings — robust across PyObjC versions)
ACTIONABLE_ROLES = {
    "AXButton", "AXMenuButton", "AXMenuItem", "AXMenuBarItem", "AXPopUpButton", "AXCheckBox",
    "AXRadioButton", "AXTextField", "AXTextArea", "AXComboBox", "AXSearchField", "AXLink",
    "AXCell", "AXTabGroup", "AXRadioGroup", "AXSlider", "AXIncrementor", "AXDisclosureTriangle",
    "AXSegmentedControl", "AXToolbar", "AXStepper", "AXColorWell",
}
ACTIONABLE_ACTIONS = {"AXPress", "AXConfirm", "AXIncrement", "AXDecrement", "AXShowMenu", "AXPick"}

# US-QWERTY virtual keycodes for chord shortcuts (cmd+c etc.) — unicode typing can't trigger these.
_CHAR_KEYCODES = {
    "a": 0, "s": 1, "d": 2, "f": 3, "h": 4, "g": 5, "z": 6, "x": 7, "c": 8, "v": 9, "b": 11,
    "q": 12, "w": 13, "e": 14, "r": 15, "y": 16, "t": 17, "1": 18, "2": 19, "3": 20, "4": 21,
    "6": 22, "5": 23, "=": 24, "9": 25, "7": 26, "-": 27, "8": 28, "0": 29, "]": 30, "o": 31,
    "u": 32, "[": 33, "i": 34, "p": 35, "l": 37, "j": 38, "'": 39, "k": 40, ";": 41, "\\": 42,
    ",": 43, "/": 44, "n": 45, "m": 46, ".": 47, "`": 50,
}
_NAMED_KEYCODES = {
    "return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51, "backspace": 51,
    "forwarddelete": 117, "escape": 53, "esc": 53, "left": 123, "right": 124, "down": 125,
    "up": 126, "home": 115, "end": 119, "pageup": 116, "pagedown": 121, "capslock": 57,
    "f1": 122, "f2": 120, "f3": 99, "f4": 118, "f5": 96, "f6": 97, "f7": 98, "f8": 100,
    "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
# Shifted US symbols -> (base key char) so press_keys("!") becomes shift+1.
_SHIFTED = {
    "!": "1", "@": "2", "#": "3", "$": "4", "%": "5", "^": "6", "&": "7", "*": "8", "(": "9",
    ")": "0", "_": "-", "+": "=", "{": "[", "}": "]", "|": "\\", ":": ";", '"': "'", "<": ",",
    ">": ".", "?": "/", "~": "`",
}


def _guard(fn):
    """Wrap a tool so any uncaught exception becomes an err() envelope (suite no-crash invariant)."""
    @functools.wraps(fn)
    def wrapper(*a, **k):
        try:
            return fn(*a, **k)
        except Exception as e:  # noqa: BLE001
            log.exception("tool %s failed", fn.__name__)
            return err(f"{fn.__name__} failed: {e}")
    return wrapper


# --------------------------------------------------------------------------- engine guards / config
def _require_pyobjc() -> dict | None:
    """Return an err() envelope if the PyObjC engine isn't usable, else None."""
    if _PYOBJC:
        return None
    if sys.platform != "darwin":
        return err("deskpilot is macOS-only", hint="this server controls a macOS desktop")
    return err("PyObjC engine not installed", hint="uv sync --group desktop", detail=_IMPORT_ERR)


def _require_confirm() -> bool:
    return get_env_bool("DESKPILOT_REQUIRE_CONFIRM", False)


def _gate(confirm: bool) -> dict | None:
    """If the global brake is on, block act tools unless confirm=True."""
    if _require_confirm() and not confirm:
        return {"ok": False, "blocked": "confirm required",
                "hint": "DESKPILOT_REQUIRE_CONFIRM is set — call again with confirm=True"}
    return None


def _settle() -> None:
    ms = get_env_int("DESKPILOT_SETTLE_MS", 60) or 0
    if ms > 0:
        time.sleep(min(ms, 3000) / 1000.0)


# --------------------------------------------------------------------------- displays / coordinates
def _display_info() -> list[dict]:
    out: list[dict] = []
    if not _PYOBJC:
        return out
    try:
        e, ids, n = Quartz.CGGetActiveDisplayList(16, None, None)
        if e != 0 or not ids:
            ids = [Quartz.CGMainDisplayID()]
    except Exception:  # noqa: BLE001
        ids = [Quartz.CGMainDisplayID()]
    for i, did in enumerate(ids):
        try:
            b = Quartz.CGDisplayBounds(did)
            x, y = float(b.origin.x), float(b.origin.y)
            w, h = float(b.size.width), float(b.size.height)
            try:
                px = Quartz.CGDisplayPixelsWide(did)
                scale = round(px / w, 2) if w else 1.0
            except Exception:  # noqa: BLE001
                scale = 1.0
            out.append({"index": i, "main": bool(Quartz.CGDisplayIsMain(did)),
                        "bbox": [x, y, w, h], "scale": scale})
        except Exception:  # noqa: BLE001
            continue
    return out


def _bounds() -> tuple[float, float, float, float]:
    """Union rect (x0,y0,x1,y1) of all active displays, in points."""
    disp = _display_info()
    if not disp:
        return (0.0, 0.0, 4096.0, 4096.0)
    x0 = min(d["bbox"][0] for d in disp)
    y0 = min(d["bbox"][1] for d in disp)
    x1 = max(d["bbox"][0] + d["bbox"][2] for d in disp)
    y1 = max(d["bbox"][1] + d["bbox"][3] for d in disp)
    return (x0, y0, x1, y1)


def _clamp(x: float, y: float) -> tuple[float, float] | None:
    try:
        x, y = float(x), float(y)
    except Exception:  # noqa: BLE001
        return None
    if x != x or y != y or abs(x) > 1e6 or abs(y) > 1e6:  # NaN / absurd
        return None
    x0, y0, x1, y1 = _bounds()
    return (min(max(x, x0), x1 - 1), min(max(y, y0), y1 - 1))


# --------------------------------------------------------------------------- AX helpers
_AX_CACHE: dict[str, object] = {}
_ID_SEQ = 0


def _new_id(el) -> str:
    global _ID_SEQ
    _ID_SEQ += 1
    eid = f"e{_ID_SEQ}"
    _AX_CACHE[eid] = el
    if len(_AX_CACHE) > 6000:
        for k in list(_AX_CACHE)[:2000]:
            _AX_CACHE.pop(k, None)
    return eid


def _get_el(eid: str):
    return _AX_CACHE.get(eid)


def _ax_get(el, attr: str):
    try:
        e, val = AXS.AXUIElementCopyAttributeValue(el, attr, None)
        return val if e == 0 else None
    except Exception:  # noqa: BLE001
        return None


def _ax_actions(el) -> list[str]:
    try:
        e, names = AXS.AXUIElementCopyActionNames(el, None)
        if e == 0 and names:
            return [str(n) for n in names]
    except Exception:  # noqa: BLE001
        pass
    return []


def _ax_pair(el, attr: str, type_const: int):
    v = _ax_get(el, attr)
    if v is None:
        return None
    try:
        ok_, out = AXS.AXValueGetValue(v, type_const, None)
        if ok_:
            return out
    except Exception:  # noqa: BLE001
        pass
    return None


# AXValue type ids are stable: CGPoint=1, CGSize=2.
_T_POINT = getattr(AXS, "kAXValueCGPointType", 1) if _PYOBJC else 1
_T_SIZE = getattr(AXS, "kAXValueCGSizeType", 2) if _PYOBJC else 2


def _ax_bbox(el) -> list[float] | None:
    p = _ax_pair(el, "AXPosition", _T_POINT)
    s = _ax_pair(el, "AXSize", _T_SIZE)
    if p is None or s is None:
        return None
    try:
        return [round(float(p.x), 1), round(float(p.y), 1), round(float(s.width), 1), round(float(s.height), 1)]
    except Exception:  # noqa: BLE001
        return None


def _s(v) -> str | None:
    if v is None:
        return None
    try:
        out = str(v).strip()
        return out or None
    except Exception:  # noqa: BLE001
        return None


def _trunc(v: str | None, n: int = 200) -> str | None:
    if v is None:
        return None
    return v if len(v) <= n else v[:n] + "…"


def _describe(el) -> dict:
    role = _s(_ax_get(el, "AXRole"))
    enabled = _ax_get(el, "AXEnabled")
    focused = _ax_get(el, "AXFocused")
    actions = _ax_actions(el)
    d = {
        "id": _new_id(el),
        "role": role,
        "subrole": _s(_ax_get(el, "AXSubrole")),
        "title": _trunc(_s(_ax_get(el, "AXTitle"))),
        "label": _trunc(_s(_ax_get(el, "AXDescription")) or _s(_ax_get(el, "AXRoleDescription"))),
        "value": _trunc(_s(_ax_get(el, "AXValue"))),
        "placeholder": _trunc(_s(_ax_get(el, "AXPlaceholderValue"))),
        "enabled": (bool(enabled) if enabled is not None else None),
        "focused": (bool(focused) if focused is not None else None),
        "actions": actions or None,
        "bbox": _ax_bbox(el),
    }
    d["actionable"] = bool((role in ACTIONABLE_ROLES) or (set(actions) & ACTIONABLE_ACTIONS))
    return {k: v for k, v in d.items() if v is not None}


def _running_app(name: str):
    """Return the best-matching NSRunningApplication for `name` (exact, then prefix, then substring)."""
    ws = NSWorkspace.sharedWorkspace()
    apps = [a for a in ws.runningApplications() if a.localizedName()]
    nm = name.strip().lower()
    exact = [a for a in apps if (a.localizedName() or "").lower() == nm]
    if exact:
        return exact[0]
    bundle = [a for a in apps if (a.bundleIdentifier() or "").lower() == nm]
    if bundle:
        return bundle[0]
    pref = [a for a in apps if (a.localizedName() or "").lower().startswith(nm)]
    if pref:
        return pref[0]
    sub = [a for a in apps if nm in (a.localizedName() or "").lower()]
    return sub[0] if sub else None


def _frontmost_app():
    return NSWorkspace.sharedWorkspace().frontmostApplication()


def _activate(app, settle: bool = True) -> None:
    try:
        app.activateWithOptions_(NSApplicationActivateIgnoringOtherApps | NSApplicationActivateAllWindows)
        if settle:
            time.sleep(0.25)
    except Exception:  # noqa: BLE001
        pass


def _resolve_app(app_name: str):
    """Resolve a target app element. Returns (app_element, NSRunningApplication, err_dict|None)."""
    if app_name:
        ra = _running_app(app_name)
        if ra is None:
            running = sorted({a.localizedName() for a in NSWorkspace.sharedWorkspace().runningApplications() if a.localizedName()})
            return None, None, not_found("running app", app_name, running[:60],
                                         hint="open it first (open_app_and_wait) or check the exact name")
        _activate(ra)
    else:
        ra = _frontmost_app()
        if ra is None:
            return None, None, err("no frontmost app")
    el = AXS.AXUIElementCreateApplication(ra.processIdentifier())
    return el, ra, None


def _flat_walk(el, depth: int, max_depth: int, budget: dict) -> None:
    if budget["n"] >= budget["max"]:
        budget["truncated"] = True
        return
    budget["n"] += 1
    d = _describe(el)
    d["depth"] = depth
    budget["items"].append(d)
    if depth < max_depth:
        for ch in (_ax_get(el, "AXChildren") or []):
            if budget["n"] >= budget["max"]:
                budget["truncated"] = True
                break
            _flat_walk(ch, depth + 1, max_depth, budget)


def _tree_walk(el, depth: int, max_depth: int, budget: dict) -> dict | None:
    if budget["n"] >= budget["max"]:
        budget["truncated"] = True
        return None
    budget["n"] += 1
    node = _describe(el)
    node.pop("actionable", None)
    kids: list[dict] = []
    if depth < max_depth:
        for ch in (_ax_get(el, "AXChildren") or []):
            if budget["n"] >= budget["max"]:
                budget["truncated"] = True
                break
            cn = _tree_walk(ch, depth + 1, max_depth, budget)
            if cn is not None:
                kids.append(cn)
    if kids:
        node["children"] = kids
    return node


def _prune_tree(node: dict) -> dict | None:
    """Keep nodes that are actionable or have a kept descendant (used by ui_tree actionable_only)."""
    role = node.get("role")
    actionable = (role in ACTIONABLE_ROLES) or bool(set(node.get("actions") or []) & ACTIONABLE_ACTIONS)
    kept = [k for k in (_prune_tree(c) for c in node.get("children", [])) if k]
    if kept:
        node["children"] = kept
    else:
        node.pop("children", None)
    if actionable or kept:
        return node
    return None


# --------------------------------------------------------------------------- permissions
def _accessibility_ok() -> bool | None:
    if not _PYOBJC:
        return None
    try:
        return bool(AXS.AXIsProcessTrusted())
    except Exception:  # noqa: BLE001
        return None


def _screen_recording_ok() -> bool | None:
    if not _PYOBJC:
        return None
    fn = getattr(Quartz, "CGPreflightScreenCaptureAccess", None)
    if fn is None:
        return None  # pre-10.15: no per-app gate
    try:
        return bool(fn())
    except Exception:  # noqa: BLE001
        return None


def _require_ax() -> dict | None:
    if (e := _require_pyobjc()):
        return e
    if _accessibility_ok() is False:
        return err("Accessibility permission not granted",
                   hint="System Settings → Privacy & Security → Accessibility: enable the app "
                        "running this server (Terminal/iTerm2/VS Code/Qwen), then RESTART it")
    return None


def _host_process() -> dict:
    return {"term_program": os.environ.get("TERM_PROGRAM"),
            "term_program_version": os.environ.get("TERM_PROGRAM_VERSION"),
            "python": sys.executable}


# --------------------------------------------------------------------------- screenshot
def _capture(region: list[float] | None, display_index: int | None) -> tuple[bool, str, str]:
    ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S-%f")
    path = str(TMP / f"shot-{ts}.png")
    args = ["screencapture", "-x"]
    if region and len(region) == 4:
        args += ["-R", f"{int(region[0])},{int(region[1])},{int(region[2])},{int(region[3])}"]
    elif display_index is not None:
        args += ["-D", str(display_index + 1)]  # screencapture -D is 1-based
    args.append(path)
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=30)
        if p.returncode == 0 and os.path.exists(path):
            return True, path, ""
        return False, path, (p.stderr or "screencapture failed").strip()
    except Exception as e:  # noqa: BLE001
        return False, path, str(e)


def _downscale(path: str, max_dim: int) -> None:
    if max_dim and max_dim > 0 and shutil.which("sips"):
        try:
            subprocess.run(["sips", "-Z", str(max_dim), path], capture_output=True, text=True, timeout=30)
        except Exception:  # noqa: BLE001
            pass


# --------------------------------------------------------------------------- mouse / keyboard primitives
def _src():
    try:
        return Quartz.CGEventSourceCreate(Quartz.kCGEventSourceStateHIDSystemState)
    except Exception:  # noqa: BLE001
        return None


def _pt(x: float, y: float):
    return Quartz.CGPointMake(float(x), float(y))


_BUTTONS = {"left": ("kCGMouseButtonLeft", "kCGEventLeftMouseDown", "kCGEventLeftMouseUp", "kCGEventLeftMouseDragged"),
            "right": ("kCGMouseButtonRight", "kCGEventRightMouseDown", "kCGEventRightMouseUp", "kCGEventRightMouseDragged"),
            "middle": ("kCGMouseButtonCenter", "kCGEventOtherMouseDown", "kCGEventOtherMouseUp", "kCGEventOtherMouseDragged")}


def _move(x: float, y: float) -> None:
    ev = Quartz.CGEventCreateMouseEvent(_src(), Quartz.kCGEventMouseMoved, _pt(x, y), Quartz.kCGMouseButtonLeft)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)


def _click(x: float, y: float, button: str, clicks: int) -> None:
    btn, down, up, _ = _BUTTONS.get(button, _BUTTONS["left"])
    b = getattr(Quartz, btn)
    dn, upc = getattr(Quartz, down), getattr(Quartz, up)
    _move(x, y)
    for n in range(1, clicks + 1):
        e1 = Quartz.CGEventCreateMouseEvent(_src(), dn, _pt(x, y), b)
        if clicks > 1:
            Quartz.CGEventSetIntegerValueField(e1, Quartz.kCGMouseEventClickState, n)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e1)
        e2 = Quartz.CGEventCreateMouseEvent(_src(), upc, _pt(x, y), b)
        if clicks > 1:
            Quartz.CGEventSetIntegerValueField(e2, Quartz.kCGMouseEventClickState, n)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, e2)


def _parse_combo(combo: str) -> tuple[int, int] | None:
    """('cmd+shift+c') -> (keycode, flags) or None."""
    parts = [p.strip().lower() for p in re.split(r"[+\s]+", combo) if p.strip()]
    if not parts:
        return None
    flags = 0
    mods = {"cmd": "kCGEventFlagMaskCommand", "command": "kCGEventFlagMaskCommand",
            "opt": "kCGEventFlagMaskAlternate", "option": "kCGEventFlagMaskAlternate",
            "alt": "kCGEventFlagMaskAlternate", "ctrl": "kCGEventFlagMaskControl",
            "control": "kCGEventFlagMaskControl", "shift": "kCGEventFlagMaskShift",
            "fn": "kCGEventFlagMaskSecondaryFn"}
    *mod_parts, key = parts
    for m in mod_parts:
        if m in mods:
            flags |= getattr(Quartz, mods[m])
        else:
            return None
    code = _keycode_for(key)
    if code is None:
        return None
    kc, shift = code
    if shift:
        flags |= Quartz.kCGEventFlagMaskShift
    return kc, flags


def _keycode_for(key: str) -> tuple[int, bool] | None:
    """Return (keycode, needs_shift) for a single key name/char, or None."""
    k = key.strip()
    kl = k.lower()
    if kl in _NAMED_KEYCODES:
        return _NAMED_KEYCODES[kl], False
    if len(k) == 1:
        if k in _SHIFTED:
            return _CHAR_KEYCODES[_SHIFTED[k]], True
        if k.isupper() and k.lower() in _CHAR_KEYCODES:
            return _CHAR_KEYCODES[k.lower()], True
        if kl in _CHAR_KEYCODES:
            return _CHAR_KEYCODES[kl], False
    return None


def _post_key(keycode: int, flags: int) -> None:
    down = Quartz.CGEventCreateKeyboardEvent(_src(), keycode, True)
    if flags:
        Quartz.CGEventSetFlags(down, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
    up = Quartz.CGEventCreateKeyboardEvent(_src(), keycode, False)
    if flags:
        Quartz.CGEventSetFlags(up, flags)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)


def _type_unicode(text: str, per_char_delay_ms: int) -> None:
    delay = max(0, min(per_char_delay_ms, 200)) / 1000.0
    for ch in text:
        down = Quartz.CGEventCreateKeyboardEvent(_src(), 0, True)
        Quartz.CGEventKeyboardSetUnicodeString(down, len(ch), ch)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, down)
        up = Quartz.CGEventCreateKeyboardEvent(_src(), 0, False)
        Quartz.CGEventKeyboardSetUnicodeString(up, len(ch), ch)
        Quartz.CGEventPost(Quartz.kCGHIDEventTap, up)
        if delay:
            time.sleep(delay)


# =========================================================================== HEALTH
try:
    mcp.local_provider.remove_tool("health")
except Exception:  # noqa: BLE001
    try:
        mcp.remove_tool("health")
    except Exception:  # noqa: BLE001
        pass


@mcp.tool
@_guard
def health() -> dict:
    """Liveness + capability/permission report. CALL THIS FIRST. Reports PyObjC availability,
    Accessibility & Screen-Recording grants, displays, the launching process, and exact fix hints."""
    ax = _accessibility_ok()
    rec = _screen_recording_ok()
    hints: dict = {}
    if not _PYOBJC:
        if sys.platform != "darwin":
            hints["platform"] = "deskpilot is macOS-only"
        else:
            hints["pyobjc"] = "uv sync --group desktop"
    else:
        if ax is False:
            hints["accessibility"] = ("System Settings → Privacy & Security → Accessibility: enable the "
                                      "app running this server, then RESTART it (trust won't flip live)")
        if rec is False:
            hints["screen_recording"] = ("System Settings → Privacy & Security → Screen Recording: enable "
                                         "the launching app, then RESTART it")
    return ok(
        server="deskpilot",
        platform=sys.platform,
        pyobjc=_PYOBJC,
        accessibility=ax,
        screen_recording=rec,
        ready=bool(_PYOBJC and ax),
        require_confirm=_require_confirm(),
        displays=_display_info(),
        host_process=_host_process(),
        cliclick=bool(shutil.which("cliclick")),
        import_error=_IMPORT_ERR,
        hints=hints or None,
    )


@mcp.tool
@_guard
def request_permissions() -> dict:
    """Trigger the macOS permission PROMPTS for Accessibility and Screen Recording (one-time).
    After granting, RESTART the app that launched this server. Returns the post-prompt status."""
    if (e := _require_pyobjc()):
        return e
    prompted = {}
    try:
        opt = getattr(AXS, "kAXTrustedCheckOptionPrompt", "AXTrustedCheckOptionPrompt")
        prompted["accessibility"] = bool(AXS.AXIsProcessTrustedWithOptions({opt: True}))
    except Exception as ex:  # noqa: BLE001
        prompted["accessibility"] = f"prompt failed: {ex}"
    fn = getattr(Quartz, "CGRequestScreenCaptureAccess", None)
    if fn is not None:
        try:
            prompted["screen_recording"] = bool(fn())
        except Exception as ex:  # noqa: BLE001
            prompted["screen_recording"] = f"prompt failed: {ex}"
    return ok(prompted=prompted, accessibility=_accessibility_ok(), screen_recording=_screen_recording_ok(),
              hint="If still false, grant manually in System Settings then RESTART the launching app")


# =========================================================================== PERCEIVE
@mcp.tool
@_guard
def screenshot(app: str = "", window: bool = False, max_dim: int = 1600) -> object:
    """Capture the screen and RETURN the image so a vision model can SEE it. With app set, the app
    is activated first; window=True crops to its frontmost window (via the Accessibility bbox).
    Downscaled to max_dim (longest side) to save tokens. Use screenshot_path() for pixel-accurate
    coordinates. Needs Screen Recording permission."""
    if (e := _require_pyobjc()):
        return e
    if _screen_recording_ok() is False:
        return err("Screen Recording permission not granted",
                   hint="System Settings → Privacy & Security → Screen Recording: enable the launching app, then RESTART it")
    region = None
    if app or window:
        el, ra, e2 = _resolve_app(app)
        if e2:
            return e2
        if window:
            win = _ax_get(el, "AXFocusedWindow") or _ax_get(el, "AXMainWindow")
            if win is not None:
                region = _ax_bbox(win)
    okc, path, msg = _capture(region, None)
    if not okc:
        return err(f"capture failed: {msg}", hint="check Screen Recording permission")
    _downscale(path, max_dim)
    if Image is None:
        return ok(path=path, note="fastmcp Image unavailable; returning path")
    log.info("screenshot", app=app or "screen", window=window)
    return Image(path=path, format="png")


@mcp.tool
@_guard
def screenshot_path(app: str = "", window: bool = False, display: int = -1) -> dict:
    """Capture a full-resolution screenshot to a PNG file and return its path + pixel dimensions +
    display scale (for non-vision callers, or to map screenshot pixels to screen points: screen_point
    = pixel / scale). display>=0 captures only that display index (see health().displays)."""
    if (e := _require_pyobjc()):
        return e
    if _screen_recording_ok() is False:
        return err("Screen Recording permission not granted",
                   hint="grant Screen Recording to the launching app, then RESTART it")
    region = None
    di = display if display >= 0 else None
    if (app or window) and di is None:
        el, ra, e2 = _resolve_app(app)
        if e2:
            return e2
        if window:
            win = _ax_get(el, "AXFocusedWindow") or _ax_get(el, "AXMainWindow")
            if win is not None:
                region = _ax_bbox(win)
    okc, path, msg = _capture(region, di)
    if not okc:
        return err(f"capture failed: {msg}")
    width = height = None
    if shutil.which("sips"):
        try:
            r = subprocess.run(["sips", "-g", "pixelWidth", "-g", "pixelHeight", path],
                               capture_output=True, text=True, timeout=15)
            mw = re.search(r"pixelWidth:\s*(\d+)", r.stdout)
            mh = re.search(r"pixelHeight:\s*(\d+)", r.stdout)
            width = int(mw.group(1)) if mw else None
            height = int(mh.group(1)) if mh else None
        except Exception:  # noqa: BLE001
            pass
    disp = _display_info()
    scale = (disp[di]["scale"] if di is not None and di < len(disp) else (disp[0]["scale"] if disp else 1.0))
    return ok(path=path, pixel_width=width, pixel_height=height, scale=scale, region=region)


@mcp.tool
@_guard
def ui_elements(app: str = "", roles: list[str] | None = None, query: str = "",
                actionable_only: bool = True, max_depth: int = 18, limit: int = 80) -> dict:
    """The everyday perception primitive: a FLAT list of UI elements of an app (frontmost if app=""),
    each with id, role, title/label/value, bbox (points) and actionable flag. Filter by roles
    (e.g. ['AXButton','AXTextField']) and/or query (substring match on title/label/value). Use the
    returned id with click_element/press_element/set_field. Returns truncated=True when capped."""
    if (e := _require_ax()):
        return e
    el, ra, e2 = _resolve_app(app)
    if e2:
        return e2
    budget = {"n": 0, "max": 600, "truncated": False, "items": []}
    _flat_walk(el, 0, max(1, min(max_depth, 30)), budget)
    items = budget["items"]
    if actionable_only:
        items = [d for d in items if d.get("actionable")]
    if roles:
        want = {r.strip() for r in roles if r.strip()}
        items = [d for d in items if d.get("role") in want]
    if query:
        q = query.strip().lower()
        items = [d for d in items if q in " ".join(
            str(d.get(k) or "") for k in ("title", "label", "value", "placeholder", "role")).lower()]
    truncated = budget["truncated"] or len(items) > limit
    items = items[: max(1, limit)]
    for d in items:
        d.pop("actionable", None)
    return ok(app=(ra.localizedName() if ra else None), elements=items, count=len(items),
              truncated=truncated, hint=("narrow with roles=/query= or raise limit" if truncated else None))


@mcp.tool
@_guard
def ui_tree(app: str = "", max_depth: int = 18, max_elements: int = 400, actionable_only: bool = False) -> dict:
    """Nested Accessibility tree of an app (frontmost if app=""). Each node: id, role, subrole,
    title, label, value, bbox, children. actionable_only prunes to interactive branches. Prefer
    ui_elements() for routine work; use this to understand structure/containment. Capped → truncated."""
    if (e := _require_ax()):
        return e
    el, ra, e2 = _resolve_app(app)
    if e2:
        return e2
    budget = {"n": 0, "max": max(10, min(max_elements, 1500)), "truncated": False}
    tree = _tree_walk(el, 0, max(1, min(max_depth, 30)), budget)
    if tree and actionable_only:
        tree = _prune_tree(tree)
    return ok(app=(ra.localizedName() if ra else None), tree=tree,
              elements=budget["n"], truncated=budget["truncated"])


@mcp.tool
@_guard
def find_element(text: str = "", role: str = "", app: str = "", nth: int = 0) -> dict:
    """Find a single UI element by visible text (title/label/value/placeholder substring) and/or role,
    in app (frontmost if app=""). Returns the nth match (0-based) with its id + bbox, plus the total
    match count. Returns not_found (with nearby candidates) when nothing matches."""
    if (e := _require_ax()):
        return e
    if not text and not role:
        return err("provide text and/or role")
    el, ra, e2 = _resolve_app(app)
    if e2:
        return e2
    budget = {"n": 0, "max": 800, "truncated": False, "items": []}
    _flat_walk(el, 0, 28, budget)
    items = budget["items"]
    q = text.strip().lower()
    matches = []
    for d in items:
        if role and d.get("role") != role:
            continue
        if q:
            hay = " ".join(str(d.get(k) or "") for k in ("title", "label", "value", "placeholder")).lower()
            if q not in hay:
                continue
        matches.append(d)
    if not matches:
        sample = sorted({f"{d.get('role')}:{d.get('title') or d.get('label') or ''}".strip(":")
                         for d in items if d.get("actionable")})[:40]
        return not_found("element", text or role, sample, hint="adjust text/role, or screenshot() to look")
    idx = max(0, nth)
    if idx >= len(matches):
        idx = 0
    chosen = dict(matches[idx])
    chosen.pop("actionable", None)
    return ok(element=chosen, match_count=len(matches), index=idx,
              app=(ra.localizedName() if ra else None))


@mcp.tool
@_guard
def element_at(x: float, y: float) -> dict:
    """Hit-test: return the UI element directly under screen point (x,y) — bridges a vision-picked
    pixel/point back to an Accessibility element id you can act on."""
    if (e := _require_ax()):
        return e
    c = _clamp(x, y)
    if c is None:
        return err("invalid coordinates")
    sw = AXS.AXUIElementCreateSystemWide()
    try:
        e2, el = AXS.AXUIElementCopyElementAtPosition(sw, c[0], c[1], None)
    except Exception as ex:  # noqa: BLE001
        return err(f"hit-test failed: {ex}")
    if e2 != 0 or el is None:
        return err("no element at that point", point=[c[0], c[1]])
    d = _describe(el)
    d.pop("actionable", None)
    return ok(element=d, point=[c[0], c[1]])


@mcp.tool
@_guard
def read_text_field(id: str = "", label: str = "", app: str = "") -> dict:
    """Read the current text/value of a field — by element id, or by finding it via label/title in
    app (frontmost if app=""). Use it to verify state before/after typing."""
    if (e := _require_ax()):
        return e
    el = None
    if id:
        el = _get_el(id)
        if el is None:
            return not_found("element id", id, hint="re-perceive (ui_elements/find_element) — ids expire")
    elif label:
        f = find_element(text=label, app=app)
        if not f.get("ok"):
            return f
        el = _get_el(f["element"]["id"])
    else:
        return err("provide id or label")
    if el is None:
        return err("could not resolve element")
    val = _s(_ax_get(el, "AXValue"))
    return ok(value=val, placeholder=_s(_ax_get(el, "AXPlaceholderValue")),
              role=_s(_ax_get(el, "AXRole")))


@mcp.tool
@_guard
def get_frontmost() -> dict:
    """The frontmost app, its focused window title, and the currently focused element (id + role) —
    the cheapest way to orient before acting."""
    if (e := _require_ax()):
        return e
    ra = _frontmost_app()
    if ra is None:
        return err("no frontmost app")
    el = AXS.AXUIElementCreateApplication(ra.processIdentifier())
    win = _ax_get(el, "AXFocusedWindow") or _ax_get(el, "AXMainWindow")
    focused = _ax_get(el, "AXFocusedUIElement")
    return ok(app=ra.localizedName(), bundle=ra.bundleIdentifier(), pid=ra.processIdentifier(),
              window=_s(_ax_get(win, "AXTitle")) if win is not None else None,
              focused_element=(_describe(focused) if focused is not None else None))


@mcp.tool
@_guard
def list_windows(app: str = "") -> dict:
    """List an app's windows (frontmost app if app="") with title, bbox (points), focused & minimized
    flags + an id you can screenshot/act within."""
    if (e := _require_ax()):
        return e
    el, ra, e2 = _resolve_app(app)
    if e2:
        return e2
    wins = _ax_get(el, "AXWindows") or []
    main = _ax_get(el, "AXMainWindow")
    out = []
    for w in wins:
        mini = _ax_get(w, "AXMinimized")
        out.append({"id": _new_id(w), "title": _s(_ax_get(w, "AXTitle")), "bbox": _ax_bbox(w),
                    "main": bool(main is not None and w == main),
                    "minimized": (bool(mini) if mini is not None else None)})
    return ok(app=(ra.localizedName() if ra else None), windows=out, count=len(out))


# =========================================================================== ACT — mouse
@mcp.tool
@_guard
def move(x: float, y: float, confirm: bool = False) -> dict:
    """Move the mouse cursor to screen point (x,y) — points, top-left origin. Clamped to displays."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    c = _clamp(x, y)
    if c is None:
        return err("invalid coordinates")
    _move(*c)
    _settle()
    log.info("move", x=c[0], y=c[1])
    return ok(x=c[0], y=c[1])


@mcp.tool
@_guard
def click(x: float, y: float, button: str = "left", confirm: bool = False) -> dict:
    """Click at screen point (x,y). button: left|right|middle. Points, top-left origin; clamped.
    Prefer click_element(id)/press_element(id) when you have an element — more robust than raw coords."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    if button not in _BUTTONS:
        return err("button must be left, right or middle")
    c = _clamp(x, y)
    if c is None:
        return err("invalid coordinates")
    _click(c[0], c[1], button, 1)
    _settle()
    log.info("click", x=c[0], y=c[1], button=button)
    return ok(x=c[0], y=c[1], button=button)


@mcp.tool
@_guard
def double_click(x: float, y: float, confirm: bool = False) -> dict:
    """Double-click at screen point (x,y)."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    c = _clamp(x, y)
    if c is None:
        return err("invalid coordinates")
    _click(c[0], c[1], "left", 2)
    _settle()
    log.info("double_click", x=c[0], y=c[1])
    return ok(x=c[0], y=c[1])


@mcp.tool
@_guard
def right_click(x: float, y: float, confirm: bool = False) -> dict:
    """Right-click (context menu) at screen point (x,y)."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    c = _clamp(x, y)
    if c is None:
        return err("invalid coordinates")
    _click(c[0], c[1], "right", 1)
    _settle()
    log.info("right_click", x=c[0], y=c[1])
    return ok(x=c[0], y=c[1])


@mcp.tool
@_guard
def drag(from_x: float, from_y: float, to_x: float, to_y: float,
         duration_ms: int = 300, button: str = "left", confirm: bool = False) -> dict:
    """Press at (from_x,from_y), drag to (to_x,to_y) over duration_ms, release. For sliders, files,
    selections, reordering."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    a, b = _clamp(from_x, from_y), _clamp(to_x, to_y)
    if a is None or b is None:
        return err("invalid coordinates")
    btn, down, up, dragged = _BUTTONS.get(button, _BUTTONS["left"])
    bcode = getattr(Quartz, btn)
    _move(*a)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(_src(), getattr(Quartz, down), _pt(*a), bcode))
    steps = max(2, min(int((duration_ms or 0) / 15) + 2, 120))
    for i in range(1, steps + 1):
        t = i / steps
        mx = a[0] + (b[0] - a[0]) * t
        my = a[1] + (b[1] - a[1]) * t
        Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                           Quartz.CGEventCreateMouseEvent(_src(), getattr(Quartz, dragged), _pt(mx, my), bcode))
        time.sleep(max(0, min(duration_ms, 5000)) / 1000.0 / steps)
    Quartz.CGEventPost(Quartz.kCGHIDEventTap,
                       Quartz.CGEventCreateMouseEvent(_src(), getattr(Quartz, up), _pt(*b), bcode))
    _settle()
    log.info("drag", frm=[a[0], a[1]], to=[b[0], b[1]], button=button)
    return ok(**{"from": [a[0], a[1]], "to": [b[0], b[1]], "button": button})


@mcp.tool
@_guard
def scroll(x: float, y: float, dx: int = 0, dy: int = 0, confirm: bool = False) -> dict:
    """Scroll at screen point (x,y) by dx (horizontal) / dy (vertical) pixels. Positive dy scrolls
    content up (wheel away from you); negative dy scrolls down."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    c = _clamp(x, y)
    if c is None:
        return err("invalid coordinates")
    _move(*c)
    ev = Quartz.CGEventCreateScrollWheelEvent(_src(), Quartz.kCGScrollEventUnitPixel, 2, int(dy), int(dx))
    Quartz.CGEventPost(Quartz.kCGHIDEventTap, ev)
    _settle()
    log.info("scroll", x=c[0], y=c[1], dx=dx, dy=dy)
    return ok(x=c[0], y=c[1], dx=dx, dy=dy)


@mcp.tool
@_guard
def click_element(id: str = "", app: str = "", confirm: bool = False) -> dict:
    """Click the center of an element (by id from ui_elements/find_element). Uses the element's
    point-space bbox, so it avoids Retina pixel math. Falls back gracefully if the id expired."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    el = _get_el(id)
    if el is None:
        return not_found("element id", id, hint="re-perceive (ui_elements/find_element) — ids expire on UI change")
    bbox = _ax_bbox(el)
    if not bbox:
        return err("element has no on-screen bbox", hint="try press_element(id) instead")
    cx, cy = bbox[0] + bbox[2] / 2, bbox[1] + bbox[3] / 2
    c = _clamp(cx, cy)
    if c is None:
        return err("element center is off-screen")
    _click(c[0], c[1], "left", 1)
    _settle()
    log.info("click_element", id=id, x=c[0], y=c[1])
    return ok(id=id, x=c[0], y=c[1])


# =========================================================================== ACT — keyboard
@mcp.tool
@_guard
def type_text(text: str, per_char_delay_ms: int = 0, confirm: bool = False) -> dict:
    """Type text into whatever currently has keyboard focus (focus_element/click first). Uses unicode
    events, so emoji and any layout work. Does NOT press Enter. Max 10000 chars."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    if not isinstance(text, str) or text == "":
        return err("text must be a non-empty string")
    if len(text) > MAX_TEXT:
        return err(f"text too long (max {MAX_TEXT} chars)")
    _type_unicode(text, per_char_delay_ms)
    _settle()
    log.info("type_text", chars=len(text))  # text content intentionally not logged
    return ok(typed=len(text))


@mcp.tool
@_guard
def press_keys(combo: str, confirm: bool = False) -> dict:
    """Press a key chord like 'cmd+c', 'cmd+shift+4', 'ctrl+space', 'return'. Modifiers: cmd, ctrl,
    opt/alt, shift, fn. The final token is a single char or a named key (return/tab/escape/space/
    arrows/f1-f12/delete/home/end/pageup/pagedown). This is how you trigger app shortcuts."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    if not combo or not combo.strip():
        return err("combo required, e.g. 'cmd+c'")
    parsed = _parse_combo(combo)
    if parsed is None:
        return err(f"could not parse combo '{combo}'", hint="e.g. cmd+c, cmd+shift+t, return, escape")
    _post_key(*parsed)
    _settle()
    log.info("press_keys", combo=combo.strip().lower())
    return ok(combo=combo.strip().lower())


@mcp.tool
@_guard
def key(name: str, modifiers: str = "", confirm: bool = False) -> dict:
    """Press a single key by name (return, tab, escape, space, up/down/left/right, f1-f12, a single
    char) with optional comma/space/plus-separated modifiers (cmd, opt, ctrl, shift). Loop-friendly
    sibling of press_keys (e.g. key('down') repeatedly)."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    mods = [m for m in re.split(r"[,\s]+", modifiers or "") if m]
    combo = "+".join(mods + [name]) if mods else name
    return press_keys(combo, confirm=True)  # gate already cleared above


# =========================================================================== ACT — element-level
@mcp.tool
@_guard
def press_element(id: str, action: str = "AXPress", app: str = "", confirm: bool = False) -> dict:
    """Perform an Accessibility action on an element (default AXPress = "activate"). The MOST reliable
    way to push a button / pick a menu item / toggle a checkbox — no cursor movement, no coordinates.
    Other actions: AXShowMenu, AXIncrement, AXDecrement, AXConfirm, AXCancel, AXPick, AXRaise."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    el = _get_el(id)
    if el is None:
        return not_found("element id", id, hint="re-perceive (ui_elements/find_element) — ids expire")
    avail = _ax_actions(el)
    if action not in avail and avail:
        return err(f"element does not support '{action}'", available=avail, hint="pick one of available")
    try:
        e2 = AXS.AXUIElementPerformAction(el, action)
    except Exception as ex:  # noqa: BLE001
        return err(f"action failed: {ex}")
    _settle()
    if e2 != 0:
        return err(f"AX action returned error {e2}", hint="element may be disabled or stale")
    log.info("press_element", id=id, action=action)
    return ok(id=id, action=action)


@mcp.tool
@_guard
def focus_element(id: str, confirm: bool = False) -> dict:
    """Give keyboard focus to an element (so a following type_text lands there), raising its window."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    el = _get_el(id)
    if el is None:
        return not_found("element id", id, hint="re-perceive — ids expire")
    try:
        AXS.AXUIElementSetAttributeValue(el, "AXFocused", True)
    except Exception as ex:  # noqa: BLE001
        return err(f"focus failed: {ex}")
    _settle()
    log.info("focus_element", id=id)
    return ok(id=id)


@mcp.tool
@_guard
def set_field(id: str = "", text: str = "", label: str = "", app: str = "",
              submit: bool = False, confirm: bool = False) -> dict:
    """Set a text field's value — by id, or by finding it via label/title in app. Tries the direct
    Accessibility set (fast, replaces existing text); falls back to focus + type. submit=True presses
    Return after (e.g. to send a chat message / run a search)."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    el = None
    if id:
        el = _get_el(id)
        if el is None:
            return not_found("element id", id, hint="re-perceive — ids expire")
    elif label:
        f = find_element(text=label, app=app)
        if not f.get("ok"):
            return f
        el = _get_el(f["element"]["id"])
    else:
        return err("provide id or label")
    if el is None:
        return err("could not resolve element")
    if len(text) > MAX_TEXT:
        return err(f"text too long (max {MAX_TEXT} chars)")
    method = "ax_set"
    try:
        e2 = AXS.AXUIElementSetAttributeValue(el, "AXValue", text)
    except Exception:  # noqa: BLE001
        e2 = -1
    if e2 != 0:
        method = "focus_type"
        try:
            AXS.AXUIElementSetAttributeValue(el, "AXFocused", True)
            time.sleep(0.05)
        except Exception:  # noqa: BLE001
            pass
        _type_unicode(text, 0)
    if submit:
        _settle()
        _post_key(_NAMED_KEYCODES["return"], 0)
    _settle()
    log.info("set_field", id=id or None, chars=len(text), method=method, submit=submit)
    return ok(method=method, chars=len(text), submit=submit)


@mcp.tool
@_guard
def set_clipboard_paste(text: str, confirm: bool = False) -> dict:
    """Robust escape hatch: copy text to the clipboard and paste it (cmd+v) into the focused field.
    Best for long or awkward unicode that's slow/unreliable to type. Focus the target first."""
    if (e := _require_ax()) or (e := _gate(confirm)):
        return e
    if not isinstance(text, str) or text == "":
        return err("text must be a non-empty string")
    try:
        subprocess.run(["pbcopy"], input=text, text=True, timeout=10)
    except Exception as ex:  # noqa: BLE001
        return err(f"pbcopy failed: {ex}")
    _post_key(_CHAR_KEYCODES["v"], Quartz.kCGEventFlagMaskCommand)
    _settle()
    log.info("set_clipboard_paste", chars=len(text))
    return ok(pasted=len(text))


# =========================================================================== HIGH-LEVEL
@mcp.tool
@_guard
def open_app_and_wait(name: str, timeout_s: float = 8.0) -> dict:
    """Open/activate an app and WAIT until it has an on-screen window, then return its actionable
    ui_elements — removes the cold-start/activation race. Use this to start any flow."""
    if (e := _require_pyobjc()):
        return e
    if not name or not name.strip():
        return err("name required")
    r = subprocess.run(["open", "-a", name], capture_output=True, text=True, timeout=20)
    if r.returncode != 0:
        return err(f"could not open '{name}': {(r.stderr or '').strip()}",
                   hint="check the exact app name (e.g. 'WhatsApp', 'Google Chrome')")
    deadline = time.time() + max(1.0, min(timeout_s, 30.0))
    ra = None
    while time.time() < deadline:
        ra = _running_app(name)
        if ra is not None:
            el = AXS.AXUIElementCreateApplication(ra.processIdentifier())
            win = _ax_get(el, "AXFocusedWindow") or _ax_get(el, "AXMainWindow") or (_ax_get(el, "AXWindows") or [None])[0]
            if win is not None:
                _activate(ra)
                time.sleep(0.3)
                els = ui_elements(app=name, actionable_only=True, limit=60)
                return ok(app=ra.localizedName(), ready=True,
                          elements=els.get("elements", []), count=els.get("count", 0))
        time.sleep(0.3)
    return ok(app=(ra.localizedName() if ra else name), ready=False,
              hint="app launched but no window yet — call get_frontmost()/ui_elements() shortly")


_RECIPES = {
    "send_message": [
        "open_app_and_wait('<App>')  # e.g. WhatsApp, Messages, Slack, Telegram",
        "find_element(text='Search') OR screenshot() to locate the search/new-chat box",
        "click_element(id) or click(x,y) on the search box, then type_text('<contact name or number>')",
        "wait, then screenshot()/ui_elements() to see results; click the matching conversation",
        "find the message/compose box (often role AXTextArea/AXTextField near the bottom)",
        "click_element(id) to focus it, then type_text('<your message>')",
        "set_field(id, '<message>', submit=True) OR press_keys('return') to SEND",
        "screenshot() to confirm the message appears in the thread",
    ],
    "open_and_read": [
        "open_app_and_wait('<App>')",
        "ui_tree(actionable_only=False) or ui_elements() to map the window",
        "read_text_field(id) for specific fields, or screenshot() for the whole view",
    ],
    "fill_form": [
        "screenshot()/ui_elements(roles=['AXTextField','AXTextArea','AXCheckBox','AXPopUpButton'])",
        "for each field: set_field(id, '<value>')  (or focus_element + type_text)",
        "checkboxes/toggles: press_element(id)",
        "submit: find_element(text='Submit'/'Save') then press_element(id)",
    ],
}


@mcp.tool
@_guard
def recipe(name: str = "") -> dict:
    """Return an ordered step PLAN (not actions) for a common goal, composed from deskpilot
    primitives. name='' lists available recipes. These rely on the perceive→act→verify loop;
    adapt the steps to what you actually see on screen."""
    if not name:
        return ok(recipes=sorted(_RECIPES), hint="call recipe('send_message') for the step plan")
    steps = _RECIPES.get(name.strip().lower())
    if steps is None:
        return not_found("recipe", name, sorted(_RECIPES))
    return ok(recipe=name.strip().lower(), steps=steps,
              note="A plan, not automation — perceive (ui_elements/screenshot) and verify between steps.")


if __name__ == "__main__":
    mcp.run()
