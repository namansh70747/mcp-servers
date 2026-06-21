"""spotify — control the local Spotify macOS app (free, no Premium/login) + find songs via the
free Spotify Web API.

Playback is driven by AppleScript against Spotify.app, so play/pause/next/previous/volume/now-playing
work on a FREE Spotify account with no login. Finding a song by name uses the Web API's Client
Credentials flow (search only — no user auth), unlocked by a free developer app
(developer.spotify.com/dashboard). play_song(query) ties them together: search -> play in the app.
"""
from __future__ import annotations

import base64
import subprocess
import time

from mcp_base import get_env, http, make_server

mcp = make_server(
    "spotify",
    instructions=("Control the local Spotify Mac app (free, no Premium): play/pause/play_pause/"
                  "next_track/previous_track/set_volume/now_playing/play_uri. search() and "
                  "play_song(query) use the free Web API (needs SPOTIFY_CLIENT_ID/SECRET) to find a "
                  "track by name and play it. Basic controls work with no keys."),
)

APP = "Spotify"


# ---------- AppleScript helpers (mirrors mac-control) ----------
def _run(args: list[str], timeout: int = 15) -> dict:
    try:
        p = subprocess.run(args, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": p.stdout.strip(), "err": p.stderr.strip()}
    except FileNotFoundError:
        return {"ok": False, "err": f"command not found: {args[0]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)}


def _osa(script: str) -> dict:
    return _run(["osascript", "-e", script])


def _osa_e(*lines: str) -> dict:
    args = ["osascript"]
    for ln in lines:
        args += ["-e", ln]
    return _run(args)


def _q(s: str) -> str:
    """Escape a string for safe interpolation inside an AppleScript double-quoted literal."""
    s = (s or "").replace("\\", "\\\\").replace('"', '\\"')
    return s.replace("\r", " ").replace("\n", " ").replace("\t", " ")


def _tell(action: str) -> dict:
    """Run a simple `tell application "Spotify" to <action>` and report success."""
    r = _osa(f'tell application "{APP}" to {action}')
    out = {"ok": r.get("ok", False), "action": action}
    if r.get("err"):
        out["err"] = r["err"]
    return out


# ---------- Web API (Client Credentials) ----------
_TOKEN: str | None = None
_TOKEN_EXP: float = 0.0


def _token() -> str | None:
    """Get (and cache) a Client-Credentials access token. None if keys are missing or the call fails."""
    global _TOKEN, _TOKEN_EXP
    if _TOKEN and time.time() < _TOKEN_EXP:
        return _TOKEN
    cid, secret = get_env("SPOTIFY_CLIENT_ID"), get_env("SPOTIFY_CLIENT_SECRET")
    if not cid or not secret:
        return None
    try:
        basic = base64.b64encode(f"{cid}:{secret}".encode()).decode()
        r = http.request("POST", "https://accounts.spotify.com/api/token",
                         headers={"Authorization": f"Basic {basic}",
                                  "Content-Type": "application/x-www-form-urlencoded"},
                         content="grant_type=client_credentials", timeout=15)
        data = r.get("json") if isinstance(r, dict) else None
        if isinstance(data, dict) and data.get("access_token"):
            _TOKEN = data["access_token"]
            _TOKEN_EXP = time.time() + int(data.get("expires_in", 3600)) - 60
            return _TOKEN
    except Exception:  # noqa: BLE001
        return None
    return None


def _no_keys() -> dict:
    return {"ok": False, "error": "no SPOTIFY_CLIENT_ID/SECRET",
            "hint": "Free at developer.spotify.com/dashboard -> Create app -> copy Client ID + "
                    "Secret into .env, then reconnect. (Basic playback control works without keys.)"}


# ---------- playback tools (AppleScript, free, no keys) ----------
@mcp.tool
def play() -> dict:
    """Resume playback in the Spotify Mac app."""
    return _tell("play")


@mcp.tool
def pause() -> dict:
    """Pause playback in the Spotify Mac app."""
    return _tell("pause")


@mcp.tool
def play_pause() -> dict:
    """Toggle play/pause in the Spotify Mac app."""
    return _tell("playpause")


@mcp.tool
def next_track() -> dict:
    """Skip to the next track."""
    return _tell("next track")


@mcp.tool
def previous_track() -> dict:
    """Go to the previous track."""
    return _tell("previous track")


@mcp.tool
def set_volume(level: int) -> dict:
    """Set the Spotify app volume (0-100)."""
    try:
        level = max(0, min(100, int(level)))
    except (TypeError, ValueError):
        return {"ok": False, "err": "level must be an integer 0-100"}
    r = _osa(f'tell application "{APP}" to set sound volume to {level}')
    return {"ok": r.get("ok", False), "volume": level, **({"err": r["err"]} if r.get("err") else {})}


@mcp.tool
def now_playing() -> dict:
    """What's playing in the Spotify Mac app: state, track, artist, album, url."""
    r = _osa_e(
        f'tell application "{APP}"',
        'if it is not running then return "not_running"',
        'set st to player state as text',
        'if st is "stopped" then return "stopped"',
        'set tn to name of current track',
        'set ta to artist of current track',
        'set tb to album of current track',
        'set tu to spotify url of current track',
        'return st & "\t" & tn & "\t" & ta & "\t" & tb & "\t" & tu',
        'end tell',
    )
    if not r.get("ok"):
        return {"ok": False, "err": r.get("err", "could not query Spotify")}
    out = r.get("out", "").strip()
    if out in ("not_running", "stopped", ""):
        return {"ok": True, "state": out or "unknown", "playing": False}
    parts = out.split("\t")
    while len(parts) < 5:
        parts.append("")
    return {"ok": True, "state": parts[0], "playing": parts[0] == "playing",
            "name": parts[1], "artist": parts[2], "album": parts[3], "url": parts[4]}


@mcp.tool
def play_uri(uri: str) -> dict:
    """Play a Spotify URI (e.g. 'spotify:track:...', a playlist or album URI) in the Mac app."""
    uri = (uri or "").strip()
    if not uri.startswith("spotify:"):
        return {"ok": False, "err": "uri must start with 'spotify:' (e.g. spotify:track:...)"}
    r = _osa(f'tell application "{APP}" to play track "{_q(uri)}"')
    return {"ok": r.get("ok", False), "uri": uri, **({"err": r["err"]} if r.get("err") else {})}


# ---------- search + headline tool (Web API, free dev keys) ----------
@mcp.tool
def search(query: str, type: str = "track", limit: int = 10) -> dict:
    """Search Spotify's catalog (free Web API). type: track|artist|album|playlist. Returns matches
    with their spotify: URIs (feed a track uri to play_uri / use play_song to do both)."""
    query = (query or "").strip()
    if not query:
        return {"ok": False, "error": "query is required"}
    tok = _token()
    if not tok:
        return _no_keys()
    type = (type or "track").strip().lower()
    if type not in ("track", "artist", "album", "playlist"):
        type = "track"
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10
    r = http.request("GET", "https://api.spotify.com/v1/search",
                     headers={"Authorization": f"Bearer {tok}"},
                     params={"q": query, "type": type, "limit": limit}, timeout=20)
    if not r.get("ok"):
        return {"ok": False, "error": r.get("error") or f"HTTP {r.get('status')}"}
    items = ((r.get("json") or {}).get(f"{type}s", {}) or {}).get("items", []) or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        artists = ", ".join(a.get("name", "") for a in (it.get("artists") or [])) or None
        out.append({"name": it.get("name"), "artist": artists,
                    "album": (it.get("album") or {}).get("name") if type == "track" else None,
                    "uri": it.get("uri"),
                    "url": (it.get("external_urls") or {}).get("spotify")})
    return {"ok": True, "query": query, "type": type, "results": out}


@mcp.tool
def play_song(query: str) -> dict:
    """Find a song by name and play it in the Spotify Mac app (search -> play the top match).
    Needs SPOTIFY_CLIENT_ID/SECRET for the search step."""
    res = search(query, type="track", limit=1)
    if not res.get("ok"):
        return res
    hits = res.get("results") or []
    if not hits or not hits[0].get("uri"):
        return {"ok": False, "error": f"no track found for {query!r}"}
    top = hits[0]
    p = play_uri(top["uri"])
    if not p.get("ok"):
        return {"ok": False, "error": "found the track but couldn't play it", "track": top,
                "detail": p.get("err")}
    return {"ok": True, "playing": top, "now": now_playing()}


if __name__ == "__main__":
    mcp.run()
