"""mac-control — system control hub for your MacBook: volume, brightness, dark mode, battery,
clipboard, notifications, screenshots, windows/apps, wallpaper, caffeinate, lock, eject, Spotlight,
text/keystroke input, Focus/Shortcuts, system info. Shells to osascript + built-in macOS CLIs (free).

Some actions need permissions (Automation/Accessibility) or optional CLIs (`brightness`, `blueutil`
via brew). Mutating/destructive actions are gated behind confirm=True."""
from __future__ import annotations

import datetime as dt
import re
import shutil
import subprocess

from mcp_base import data_dir, make_server

mcp = make_server("mac-control", instructions="Control macOS: volume, dark_mode, battery, clipboard, notify, screenshot, windows, wallpaper, caffeinate, lock, eject, spotlight, type_text, shortcuts, system info.")

# Track caffeinate processes started by this server so they can be stopped.
_CAFFEINATE: dict[int, subprocess.Popen] = {}


def _run(args: list[str], inp: str | None = None, timeout: int = 15) -> dict:
    try:
        p = subprocess.run(args, capture_output=True, text=True, input=inp, timeout=timeout)
        return {"ok": p.returncode == 0, "code": p.returncode, "out": p.stdout.strip(), "err": p.stderr.strip()}
    except FileNotFoundError:
        return {"ok": False, "err": f"command not found: {args[0]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)}


def _osa(script: str) -> dict:
    return _run(["osascript", "-e", script])


def _osa_e(*lines: str) -> dict:
    """Run a multi-line AppleScript via repeated -e flags (safer than embedding newlines)."""
    args = ["osascript"]
    for ln in lines:
        args += ["-e", ln]
    return _run(args)


def _q(s: str) -> str:
    """Escape a string for safe interpolation inside an AppleScript double-quoted literal.

    Backslashes and quotes are escaped; raw newline/carriage-return/tab are normalised to spaces
    because they cannot appear inside an osascript -e double-quoted literal and would otherwise
    corrupt (or let a caller break out of) the surrounding AppleScript statement."""
    s = s.replace("\\", "\\\\").replace('"', '\\"')
    return s.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _nonempty(value: object, field: str) -> dict | None:
    """Return an error dict if `value` is not a non-empty/blank string, else None."""
    if not isinstance(value, str) or not value.strip():
        return {"ok": False, "err": f"{field} must be a non-empty string"}
    return None


def _has(cmd: str) -> str | None:
    """Return absolute path to an optional CLI, or None."""
    return shutil.which(cmd)


def _need(cmd: str, brew_pkg: str) -> dict:
    return {"ok": False, "err": f"requires `{cmd}` — install with: brew install {brew_pkg}"}


# --------------------------------------------------------------------------- audio

@mcp.tool
def set_volume(level: int) -> dict:
    """Set output volume (0-100). Returns the resulting volume."""
    lvl = max(0, min(100, level))
    r = _osa(f"set volume output volume {lvl}")
    if r.get("ok"):
        r["volume"] = lvl
    return r


@mcp.tool
def get_volume() -> dict:
    """Get current output volume and mute state."""
    r = _osa("output volume of (get volume settings)")
    m = _osa("output muted of (get volume settings)")
    return {"volume": r.get("out"), "muted": (m.get("out") == "true"), **r}


@mcp.tool
def set_mute(on: bool = True) -> dict:
    """Mute or unmute audio output."""
    return _osa(f"set volume output muted {str(on).lower()}")


# --------------------------------------------------------------------------- display

@mcp.tool
def set_dark_mode(on: bool) -> dict:
    """Toggle system Dark Mode."""
    return _osa(f'tell app "System Events" to tell appearance preferences to set dark mode to {str(on).lower()}')


@mcp.tool
def get_dark_mode() -> dict:
    """Report whether Dark Mode is currently enabled."""
    r = _osa('tell app "System Events" to tell appearance preferences to get dark mode')
    return {"dark_mode": (r.get("out") == "true"), **r}


@mcp.tool
def set_brightness(level: float) -> dict:
    """Set display brightness 0.0-1.0 (needs `brightness` CLI: brew install brightness)."""
    if not _has("brightness"):
        return _need("brightness", "brightness")
    return _run(["brightness", str(max(0.0, min(1.0, level)))])


@mcp.tool
def get_brightness() -> dict:
    """Read display brightness 0.0-1.0 (needs `brightness` CLI: brew install brightness)."""
    if not _has("brightness"):
        return _need("brightness", "brightness")
    r = _run(["brightness", "-l"])
    m = re.search(r"brightness ([0-9.]+)", r.get("out", ""))
    return {"brightness": float(m.group(1)) if m else None, **r}


@mcp.tool
def sleep_display() -> dict:
    """Put the display to sleep."""
    return _run(["pmset", "displaysleepnow"])


@mcp.tool
def set_wallpaper(path: str, confirm: bool = False) -> dict:
    """Set the desktop wallpaper to an image file. Mutates appearance — requires confirm=True."""
    import os
    p = os.path.expanduser(path)
    if not os.path.isfile(p):
        return {"ok": False, "err": f"file not found: {p}"}
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to set wallpaper to '{p}'"}
    return _osa(f'tell app "System Events" to set picture of every desktop to POSIX file "{_q(p)}"')


# --------------------------------------------------------------------------- battery / power

@mcp.tool
def battery() -> dict:
    """Battery status (percentage + charging state)."""
    r = _run(["pmset", "-g", "batt"])
    return {"raw": r.get("out"), **r}


@mcp.tool
def battery_detailed() -> dict:
    """Detailed battery health: percent, charging state, time remaining, cycle count, condition, max capacity."""
    batt = _run(["pmset", "-g", "batt"])
    out = batt.get("out", "")
    pct = re.search(r"(\d+)%", out)
    charging = "AC Power" in out or "charging" in out.lower()
    time_rem = re.search(r"(\d+:\d+) remaining", out)
    info: dict = {
        "percent": int(pct.group(1)) if pct else None,
        "charging": charging,
        "time_remaining": time_rem.group(1) if time_rem else None,
    }
    reg = _run(["ioreg", "-r", "-c", "AppleSmartBattery"], timeout=20)
    rout = reg.get("out", "")
    for key, label in (("CycleCount", "cycle_count"), ("DesignCapacity", "design_capacity"),
                       ("MaxCapacity", "max_capacity"), ("AppleRawMaxCapacity", "raw_max_capacity")):
        m = re.search(rf'"{key}"\s*=\s*(\d+)', rout)
        if m:
            info[label] = int(m.group(1))
    cond = re.search(r'"PermanentFailureStatus"\s*=\s*(\d+)', rout)
    if cond:
        info["permanent_failure"] = int(cond.group(1)) != 0
    # Battery health: on Apple Silicon MaxCapacity is already a 0-100 percentage; on Intel both
    # MaxCapacity and DesignCapacity are mAh. Prefer raw mAh ratio, else use the percentage directly.
    design = info.get("design_capacity")
    raw_max = info.get("raw_max_capacity")
    max_cap = info.get("max_capacity")
    if design and raw_max:
        info["health_pct"] = round(100 * raw_max / design, 1)
    elif isinstance(max_cap, int) and max_cap <= 100:
        info["health_pct"] = float(max_cap)
    elif design and max_cap:
        info["health_pct"] = round(100 * max_cap / design, 1)
    return {"ok": batt.get("ok", False), "raw": out, **info}


@mcp.tool
def caffeinate(seconds: int = 0, display: bool = True, confirm: bool = False) -> dict:
    """Prevent sleep using `caffeinate`. seconds=0 keeps awake indefinitely until stop_caffeinate.
    display=True also keeps the screen on. Runs in the background. Requires confirm=True."""
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True to start caffeinate"}
    args = ["caffeinate", "-i"]
    if display:
        args.append("-d")
    if seconds and seconds > 0:
        args += ["-t", str(int(seconds))]
    try:
        p = subprocess.Popen(args)
        _CAFFEINATE[p.pid] = p
        return {"ok": True, "pid": p.pid, "seconds": seconds, "display": display,
                "hint": "call stop_caffeinate to release"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)}


@mcp.tool
def stop_caffeinate(pid: int = 0) -> dict:
    """Stop a caffeinate process started by this server. pid=0 stops all of them."""
    killed: list[int] = []
    targets = list(_CAFFEINATE) if pid == 0 else [pid]
    for t in targets:
        proc = _CAFFEINATE.pop(t, None)
        if proc:
            try:
                proc.terminate()
                killed.append(t)
            except Exception:  # noqa: BLE001
                pass
    return {"ok": True, "stopped": killed, "remaining": list(_CAFFEINATE)}


@mcp.tool
def lock_screen() -> dict:
    """Lock the screen immediately (sleeps display + suspends the login session)."""
    cg = "/System/Library/CoreServices/Menu Extras/User.menu/Contents/Resources/CGSession"
    if shutil.which(cg) or __import__("os").path.exists(cg):
        r = _run([cg, "-suspend"])
        if r.get("ok"):
            return r
    # Fallback: Cmd-Ctrl-Q via System Events (requires Accessibility permission).
    return _osa('tell application "System Events" to keystroke "q" using {control down, command down}')


@mcp.tool
def sleep(confirm: bool = False) -> dict:
    """Put the whole Mac to sleep. Requires confirm=True."""
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True to sleep the Mac"}
    return _osa('tell application "System Events" to sleep')


# --------------------------------------------------------------------------- clipboard

@mcp.tool
def get_clipboard() -> dict:
    """Read the clipboard text."""
    r = _run(["pbpaste"])
    return {"text": r.get("out"), "ok": r.get("ok")}


@mcp.tool
def set_clipboard(text: str) -> dict:
    """Write text to the clipboard."""
    return _run(["pbcopy"], inp=text)


@mcp.tool
def clear_clipboard() -> dict:
    """Clear the clipboard (write empty text)."""
    return _run(["pbcopy"], inp="")


# --------------------------------------------------------------------------- notifications / speech

@mcp.tool
def notify(title: str, message: str, sound: bool = False, subtitle: str = "") -> dict:
    """Show a macOS notification (title, message, optional subtitle + sound)."""
    if (e := _nonempty(title, "title")) or (e := _nonempty(message, "message")):
        return e
    s = f'display notification "{_q(message)}" with title "{_q(title)}"'
    if subtitle:
        s += f' subtitle "{_q(subtitle)}"'
    if sound:
        s += ' sound name "Glass"'
    return _osa(s)


@mcp.tool
def say(text: str, voice: str = "", rate: int = 0) -> dict:
    """Speak text aloud via the `say` CLI. Optional voice (e.g. 'Samantha') and rate (words/min)."""
    if (e := _nonempty(text, "text")):
        return e
    if len(text) > 10000:
        return {"ok": False, "err": "text too long (max 10000 chars)"}
    args = ["say"]
    if voice:
        # `say` reads option values as list args (no shell), but reject names that look like flags.
        if voice.startswith("-"):
            return {"ok": False, "err": "invalid voice"}
        args += ["-v", voice]
    if rate and rate > 0:
        args += ["-r", str(int(rate))]
    args += ["--", text]
    return _run(args, timeout=60)


# --------------------------------------------------------------------------- apps / windows

@mcp.tool
def open_app(name: str) -> dict:
    """Open / focus an application by name."""
    if (e := _nonempty(name, "name")):
        return e
    return _run(["open", "-a", name])


@mcp.tool
def open_url(url: str) -> dict:
    """Open a URL in the default browser (http/https/mailto/etc — not local file paths)."""
    if (e := _nonempty(url, "url")):
        return e
    u = url.strip()
    if "://" not in u and not u.lower().startswith("mailto:"):
        return {"ok": False, "err": "url must include a scheme (e.g. https://...)"}
    if u.lower().startswith("file:"):
        return {"ok": False, "err": "use open_file for local files"}
    return _run(["open", u])


@mcp.tool
def open_file(path: str) -> dict:
    """Open a file/folder with its default app (or Finder)."""
    import os
    if (e := _nonempty(path, "path")):
        return e
    return _run(["open", "--", os.path.expanduser(path)])


@mcp.tool
def activate_app(name: str) -> dict:
    """Bring an application to the foreground (launching it if needed)."""
    if (e := _nonempty(name, "name")):
        return e
    r = _osa(f'tell application "{_q(name)}" to activate')
    if r.get("ok"):
        return r
    return _run(["open", "-a", name])


@mcp.tool
def quit_app(name: str, confirm: bool = False) -> dict:
    """Quit an application by name. Mutating — requires confirm=True."""
    if (e := _nonempty(name, "name")):
        return e
    if not confirm:
        return {"blocked": "confirm required", "hint": f"call again with confirm=True to quit '{name}'"}
    return _osa(f'tell application "{_q(name)}" to quit')


@mcp.tool
def hide_app(name: str) -> dict:
    """Hide an application's windows (does not quit it)."""
    if (e := _nonempty(name, "name")):
        return e
    return _osa(f'tell application "System Events" to set visible of process "{_q(name)}" to false')


@mcp.tool
def running_apps(visible_only: bool = True) -> dict:
    """List running applications with frontmost/visible flags."""
    flt = "whose visible is true and background only is false" if visible_only else ""
    script = (
        'tell application "System Events"\n'
        f'set out to ""\n'
        f'repeat with p in (every process {flt})\n'
        'set out to out & name of p & "\t" & (frontmost of p as text) & "\t" & (visible of p as text) & "\n"\n'
        'end repeat\n'
        'return out\n'
        'end tell'
    )
    r = _osa_e(*script.split("\n"))
    apps = []
    for line in (r.get("out", "") or "").splitlines():
        parts = line.split("\t")
        if len(parts) >= 3:
            apps.append({"name": parts[0], "frontmost": parts[1] == "true", "visible": parts[2] == "true"})
    return {"ok": r.get("ok", False), "apps": apps, "count": len(apps)}


@mcp.tool
def frontmost_app() -> dict:
    """Get the name of the frontmost application and its focused window title (if any)."""
    name = _osa('tell application "System Events" to name of first process whose frontmost is true')
    app = name.get("out", "")
    title = ""
    if app:
        wt = _osa(f'tell application "System Events" to tell process "{_q(app)}" to get name of front window')
        if wt.get("ok"):
            title = wt.get("out", "")
    return {"ok": name.get("ok", False), "app": app, "window": title}


@mcp.tool
def list_windows(app: str = "") -> dict:
    """List window titles. If app is empty, lists windows of the frontmost process."""
    if app and (e := _nonempty(app, "app")):
        return e
    if not app:
        fa = frontmost_app()
        app = fa.get("app", "")
        if not app:
            return {"ok": False, "err": "could not determine frontmost app"}
    r = _osa(f'tell application "System Events" to tell process "{_q(app)}" to get name of every window')
    titles = [t.strip() for t in (r.get("out", "") or "").split(",") if t.strip()]
    return {"ok": r.get("ok", False), "app": app, "windows": titles, "count": len(titles)}


# --------------------------------------------------------------------------- input (keystrokes)

@mcp.tool
def type_text(text: str) -> dict:
    """Type text into the frontmost app via keystroke (needs Accessibility permission)."""
    if not isinstance(text, str) or text == "":
        return {"ok": False, "err": "text must be a non-empty string"}
    if len(text) > 10000:
        return {"ok": False, "err": "text too long (max 10000 chars)"}
    return _osa(f'tell application "System Events" to keystroke "{_q(text)}"')


_KEY_CODES = {
    "return": 36, "enter": 76, "tab": 48, "space": 49, "delete": 51, "escape": 53, "esc": 53,
    "left": 123, "right": 124, "down": 125, "up": 126, "home": 115, "end": 119,
    "pageup": 116, "pagedown": 121, "f1": 122, "f2": 120, "f3": 99, "f4": 118,
    "f5": 96, "f6": 97, "f7": 98, "f8": 100, "f9": 101, "f10": 109, "f11": 103, "f12": 111,
}
_MODS = {"cmd": "command down", "command": "command down", "opt": "option down",
         "option": "option down", "alt": "option down", "ctrl": "control down",
         "control": "control down", "shift": "shift down"}


@mcp.tool
def key_stroke(key: str, modifiers: str = "") -> dict:
    """Send a keystroke (needs Accessibility permission). `key` is a single char or a named key
    (return, tab, escape, space, up/down/left/right, f1-f12). `modifiers` is a comma/space list of
    cmd, opt, ctrl, shift (e.g. 'cmd,shift')."""
    if (e := _nonempty(key, "key")):
        return e
    mods = [m.strip().lower() for m in re.split(r"[,\s]+", modifiers) if m.strip()]
    using = [_MODS[m] for m in mods if m in _MODS]
    using_clause = f" using {{{', '.join(using)}}}" if using else ""
    k = key.strip().lower()
    if k in _KEY_CODES:
        script = f"tell application \"System Events\" to key code {_KEY_CODES[k]}{using_clause}"
    else:
        script = f'tell application "System Events" to keystroke "{_q(key)}"{using_clause}'
    return _osa(script)


# --------------------------------------------------------------------------- network

@mcp.tool
def toggle_wifi(on: bool) -> dict:
    """Turn Wi-Fi on/off (uses the en0 device by default)."""
    return _run(["networksetup", "-setairportpower", "en0", "on" if on else "off"])


@mcp.tool
def toggle_bluetooth(on: bool) -> dict:
    """Turn Bluetooth on/off (needs `blueutil`: brew install blueutil)."""
    if not _has("blueutil"):
        return _need("blueutil", "blueutil")
    return _run(["blueutil", "--power", "1" if on else "0"])


@mcp.tool
def network_info() -> dict:
    """Current network info: Wi-Fi SSID, local IPv4 on en0, and Wi-Fi power state."""
    ssid = _run(["networksetup", "-getairportnetwork", "en0"])
    ip = _run(["ipconfig", "getifaddr", "en0"])
    power = _run(["networksetup", "-getairportpower", "en0"])
    ssid_name = ""
    m = re.search(r": (.+)$", ssid.get("out", ""))
    if m:
        ssid_name = m.group(1).strip()
    return {
        "ok": True,
        "ssid": ssid_name or None,
        "ip": ip.get("out") or None,
        "wifi_on": "On" in power.get("out", ""),
    }


# --------------------------------------------------------------------------- disks / files

@mcp.tool
def list_volumes() -> dict:
    """List mounted volumes under /Volumes with their sizes (df-based)."""
    import os
    vols = []
    base = "/Volumes"
    try:
        names = sorted(os.listdir(base))
    except OSError:
        names = []
    for n in names:
        path = os.path.join(base, n)
        df = _run(["df", "-h", path])
        line = (df.get("out", "") or "").splitlines()[-1:] or [""]
        vols.append({"name": n, "path": path, "df": line[0]})
    return {"ok": True, "volumes": vols, "count": len(vols)}


@mcp.tool
def eject(name: str = "", all: bool = False, confirm: bool = False) -> dict:
    """Eject a removable volume by name (e.g. 'USB Drive'), or all=True. Requires confirm=True."""
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True to eject"}
    if all:
        return _osa('tell application "Finder" to eject (every disk whose ejectable is true)')
    if not name:
        return {"ok": False, "err": "provide a volume name or set all=True"}
    if "/" in name or name in (".", ".."):
        return {"ok": False, "err": "invalid volume name"}
    r = _run(["diskutil", "eject", f"/Volumes/{name}"])
    if r.get("ok"):
        return r
    return _osa(f'tell application "Finder" to eject disk "{_q(name)}"')


@mcp.tool
def empty_trash(confirm: bool = False) -> dict:
    """Empty the Trash. Destructive — requires confirm=True."""
    if not confirm:
        return {"blocked": "confirm required", "hint": "call again with confirm=True to empty Trash"}
    return _osa('tell application "Finder" to empty trash')


@mcp.tool
def screenshot(path: str = "", to_clipboard: bool = False, interactive: bool = False,
               window: bool = False) -> dict:
    """Capture a screenshot via screencapture. Saves to `path` (defaults to a timestamped PNG in the
    server data dir). to_clipboard copies instead of saving; interactive lets you select a region;
    window captures a clicked window. Returns the absolute file path when saved."""
    import os
    args = ["screencapture", "-x"]  # -x: no sound
    if interactive:
        args.append("-i")
    if window:
        args.append("-W")
    if to_clipboard:
        args.append("-c")
        return {**_run(args, timeout=60), "to_clipboard": True}
    if not path:
        ts = dt.datetime.now().strftime("%Y%m%d-%H%M%S")
        path = str(data_dir("mac-control") / f"screenshot-{ts}.png")
    path = os.path.expanduser(path)
    args.append(path)
    r = _run(args, timeout=60)
    if r.get("ok") and os.path.exists(path):
        r["path"] = os.path.abspath(path)
    return r


# --------------------------------------------------------------------------- Spotlight / search

@mcp.tool
def spotlight_search(query: str, kind: str = "", limit: int = 20, onlyin: str = "") -> dict:
    """Search files via Spotlight (mdfind). `kind` narrows results: app, image, pdf, folder, audio,
    movie, contact, email, presentation. `onlyin` restricts to a directory. Returns up to `limit` paths."""
    import os
    if (e := _nonempty(query, "query")):
        return e
    args = ["mdfind"]
    if onlyin:
        args += ["-onlyin", os.path.expanduser(onlyin)]
    kind = kind.strip().lower()
    if kind:
        kmap = {"app": "application", "image": "image", "pdf": "pdf", "folder": "folder",
                "audio": "audio", "movie": "movie", "contact": "contact", "email": "email",
                "presentation": "presentation"}
        if kind in kmap:
            args.append(f"kind:{kmap[kind]} {query}")
        else:
            args.append(query)
    else:
        args.append(query)
    r = _run(args, timeout=30)
    paths = [p for p in (r.get("out", "") or "").splitlines() if p][: max(1, limit)]
    return {"ok": r.get("ok", False), "results": paths, "count": len(paths), "truncated": False}


# --------------------------------------------------------------------------- Shortcuts / Focus

@mcp.tool
def list_shortcuts() -> dict:
    """List the names of your installed Shortcuts (needs the `shortcuts` CLI, built into macOS 12+)."""
    if not _has("shortcuts"):
        return {"ok": False, "err": "the `shortcuts` CLI is unavailable (macOS 12+)"}
    r = _run(["shortcuts", "list"], timeout=30)
    names = [n for n in (r.get("out", "") or "").splitlines() if n.strip()]
    return {"ok": r.get("ok", False), "shortcuts": names, "count": len(names)}


@mcp.tool
def run_shortcut(name: str, input: str = "") -> dict:
    """Run a Shortcut by name, optionally passing text input via stdin. Returns its output."""
    if (e := _nonempty(name, "name")):
        return e
    if not _has("shortcuts"):
        return {"ok": False, "err": "the `shortcuts` CLI is unavailable (macOS 12+)"}
    args = ["shortcuts", "run", name]
    if input:
        args += ["-i", "-"]
    return _run(args, inp=input or None, timeout=120)


@mcp.tool
def set_focus(mode: str = "Do Not Disturb", on: bool = True) -> dict:
    """Toggle a Focus mode (e.g. 'Do Not Disturb'). macOS has no public Focus CLI, so this runs a
    Shortcut you create named exactly like the mode (it should call 'Set Focus'). Returns a setup hint
    if the matching Shortcut is missing."""
    if (e := _nonempty(mode, "mode")):
        return e
    if not _has("shortcuts"):
        return {"ok": False, "err": "the `shortcuts` CLI is unavailable (macOS 12+)"}
    sc_name = mode if on else f"{mode} Off"
    avail = list_shortcuts()
    if avail.get("ok") and sc_name not in avail.get("shortcuts", []):
        return {"ok": False, "err": f"no Shortcut named '{sc_name}'",
                "hint": "Create a Shortcut (named like the Focus mode) that runs the 'Set Focus' action, "
                        "then call this again. macOS exposes no direct Focus CLI."}
    return _run(["shortcuts", "run", sc_name], timeout=30)


# --------------------------------------------------------------------------- system info

@mcp.tool
def get_system_info() -> dict:
    """System overview: macOS version/build, hardware model, CPU, cores, memory (GB), hostname, uptime."""
    info: dict = {"ok": True}
    sw = _run(["sw_vers"])
    for line in (sw.get("out", "") or "").splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            key = k.strip().lower().replace("product", "").replace(" ", "_").strip("_")
            info[key or k.strip()] = v.strip()
    def _sysctl(name: str) -> str | None:
        r = _run(["sysctl", "-n", name])
        return r.get("out") if r.get("ok") else None
    info["model"] = _sysctl("hw.model")
    info["cpu"] = _sysctl("machdep.cpu.brand_string") or _sysctl("hw.model")
    cores = _sysctl("hw.ncpu")
    info["cores"] = int(cores) if cores and cores.isdigit() else cores
    memb = _sysctl("hw.memsize")
    if memb and memb.isdigit():
        info["memory_gb"] = round(int(memb) / (1024 ** 3), 1)
    host = _run(["scutil", "--get", "ComputerName"])
    if host.get("ok"):
        info["computer_name"] = host.get("out")
    up = _run(["uptime"])
    info["uptime"] = up.get("out")
    return info


@mcp.tool
def disk_usage(path: str = "/") -> dict:
    """Disk usage for a path (df -h): total, used, available, percent."""
    import os
    r = _run(["df", "-h", os.path.expanduser(path)])
    lines = (r.get("out", "") or "").splitlines()
    if len(lines) >= 2:
        cols = lines[1].split()
        if len(cols) >= 5:
            return {"ok": True, "filesystem": cols[0], "size": cols[1], "used": cols[2],
                    "available": cols[3], "capacity": cols[4], "raw": r.get("out")}
    return {"ok": r.get("ok", False), "raw": r.get("out"), "err": r.get("err")}


if __name__ == "__main__":
    mcp.run()
