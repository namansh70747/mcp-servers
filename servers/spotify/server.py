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


# ---------- recommendations / audio-features (Web API, free dev keys) ----------
# A small mood -> target audio-feature map. Values follow the Web API's audio-features scale
# (0.0-1.0 for valence/energy/danceability/acousticness; tempo in BPM). Tunable but sensible.
_MOODS: dict[str, dict] = {
    "happy":     {"target_valence": 0.85, "target_energy": 0.75, "target_danceability": 0.7},
    "sad":       {"target_valence": 0.2, "target_energy": 0.3, "target_acousticness": 0.6},
    "chill":     {"target_valence": 0.5, "target_energy": 0.35, "target_acousticness": 0.6,
                  "max_energy": 0.6},
    "relaxed":   {"target_valence": 0.5, "target_energy": 0.3, "target_acousticness": 0.7},
    "energetic": {"target_energy": 0.9, "target_danceability": 0.75, "min_tempo": 120},
    "hype":      {"target_energy": 0.95, "target_danceability": 0.8, "min_tempo": 125},
    "focus":     {"target_energy": 0.4, "target_valence": 0.4, "target_instrumentalness": 0.7,
                  "max_speechiness": 0.3},
    "study":     {"target_energy": 0.35, "target_instrumentalness": 0.75, "max_speechiness": 0.25},
    "workout":   {"target_energy": 0.92, "target_danceability": 0.7, "min_tempo": 128},
    "party":     {"target_valence": 0.8, "target_energy": 0.85, "target_danceability": 0.85},
    "calm":      {"target_valence": 0.45, "target_energy": 0.25, "target_acousticness": 0.75},
    "romantic":  {"target_valence": 0.6, "target_energy": 0.4, "target_acousticness": 0.5},
    "angry":     {"target_valence": 0.2, "target_energy": 0.9, "min_tempo": 120},
    "sleep":     {"target_energy": 0.15, "target_acousticness": 0.85, "target_instrumentalness": 0.7},
    "upbeat":    {"target_valence": 0.8, "target_energy": 0.7, "target_danceability": 0.7},
}

# Genres the Web API has historically accepted as recommendation seeds (a safe subset; the live
# endpoint validates anyway). Used to detect when a seed string is itself a genre.
_KNOWN_GENRES = {
    "acoustic", "afrobeat", "alt-rock", "alternative", "ambient", "blues", "chill", "classical",
    "club", "country", "dance", "deep-house", "disco", "drum-and-bass", "dubstep", "edm",
    "electronic", "folk", "funk", "gospel", "groove", "guitar", "happy", "hard-rock", "hip-hop",
    "house", "indie", "indie-pop", "jazz", "k-pop", "latin", "lo-fi", "metal", "metalcore",
    "pop", "punk", "r-n-b", "rap", "reggae", "reggaeton", "rock", "rock-n-roll", "sad", "salsa",
    "samba", "ska", "sleep", "soul", "soundtracks", "study", "techno", "trance", "trap", "work-out",
}


def _moods_list() -> list[str]:
    return sorted(_MOODS.keys())


def _id_from_uri(uri: str) -> str | None:
    """Pull the bare id from a spotify URI/URL/raw id (track or artist)."""
    s = (uri or "").strip()
    if not s:
        return None
    if s.startswith("spotify:"):
        parts = s.split(":")
        return parts[-1] if parts and parts[-1] else None
    if "open.spotify.com" in s:
        tail = s.split("?")[0].rstrip("/").split("/")[-1]
        return tail or None
    return s  # assume it's already a bare id


def _resolve_seed(seed: str) -> dict:
    """Turn a free-text seed into recommendation seed params. Returns
    {"params": {...}, "resolved": {...}} or {"error": ...}. Prefers a known genre,
    then a matched track, then a matched artist."""
    s = (seed or "").strip()
    if not s:
        return {"error": "empty seed"}
    norm = s.lower().replace(" ", "-")
    if norm in _KNOWN_GENRES:
        return {"params": {"seed_genres": norm}, "resolved": {"genre": norm}}

    # If they handed us a URI/URL/id directly, honour its type.
    if s.startswith("spotify:track:") or "open.spotify.com/track/" in s:
        tid = _id_from_uri(s)
        if tid:
            return {"params": {"seed_tracks": tid}, "resolved": {"track_id": tid}}
    if s.startswith("spotify:artist:") or "open.spotify.com/artist/" in s:
        aid = _id_from_uri(s)
        if aid:
            return {"params": {"seed_artists": aid}, "resolved": {"artist_id": aid}}

    # Otherwise search the catalog: try a track first, then an artist.
    res = search(s, type="track", limit=1)
    if res.get("ok") and res.get("results"):
        tid = _id_from_uri((res["results"][0] or {}).get("uri") or "")
        if tid:
            return {"params": {"seed_tracks": tid},
                    "resolved": {"track": res["results"][0].get("name"),
                                 "artist": res["results"][0].get("artist"), "track_id": tid}}
    res = search(s, type="artist", limit=1)
    if res.get("ok") and res.get("results"):
        aid = _id_from_uri((res["results"][0] or {}).get("uri") or "")
        if aid:
            return {"params": {"seed_artists": aid},
                    "resolved": {"artist": res["results"][0].get("name"), "artist_id": aid}}
    # search() can fail for a key/network reason — surface that envelope verbatim.
    if not res.get("ok"):
        return {"error": res.get("error") or "search failed", "_envelope": res}
    return {"error": f"no track/artist/genre matched {s!r}"}


def _recommendations(tok: str, params: dict, limit: int) -> dict:
    """Call the Web API recommendations endpoint and normalize tracks. Returns
    {"ok": True, "tracks": [...]} or {"ok": False, "error": ..., "hint"?}."""
    q = {"limit": limit, "market": "from_token", **params}
    r = http.request("GET", "https://api.spotify.com/v1/recommendations",
                     headers={"Authorization": f"Bearer {tok}"}, params=q, timeout=20)
    if not r.get("ok"):
        status = r.get("status")
        # 404 here almost always means Spotify retired this endpoint for newer apps (Nov 2024).
        if status in (403, 404):
            return {"ok": False, "error": f"recommendations unavailable (HTTP {status})",
                    "hint": "Spotify deprecated /v1/recommendations + /v1/audio-features for apps "
                            "created after 2024-11-27. Older apps still work; otherwise use "
                            "search() + play_song() to build a queue from named tracks."}
        return {"ok": False, "error": r.get("error") or f"HTTP {status}"}
    tracks = ((r.get("json") or {}).get("tracks") or [])
    out = []
    for it in tracks:
        if not isinstance(it, dict):
            continue
        artists = ", ".join(a.get("name", "") for a in (it.get("artists") or [])) or None
        out.append({"name": it.get("name"), "artist": artists,
                    "album": (it.get("album") or {}).get("name"),
                    "uri": it.get("uri"),
                    "url": (it.get("external_urls") or {}).get("spotify")})
    return {"ok": True, "tracks": out}


@mcp.tool
def list_moods() -> dict:
    """List the mood keywords auto_queue() understands (free, offline)."""
    return {"ok": True, "moods": _moods_list()}


@mcp.tool
def audio_features(uri: str) -> dict:
    """Fetch a track's audio features (tempo, energy, valence, danceability, ...) from the free Web
    API. `uri` may be a spotify: URI, an open.spotify.com URL, or a bare track id. Needs
    SPOTIFY_CLIENT_ID/SECRET. Note: Spotify retired this endpoint for apps created after 2024-11-27."""
    tid = _id_from_uri(uri)
    if not tid:
        return {"ok": False, "error": "uri/track id is required",
                "hint": "pass a spotify:track:... URI, an open.spotify.com/track/... URL, or a raw id"}
    tok = _token()
    if not tok:
        return _no_keys()
    r = http.request("GET", f"https://api.spotify.com/v1/audio-features/{tid}",
                     headers={"Authorization": f"Bearer {tok}"}, timeout=20)
    if not r.get("ok"):
        status = r.get("status")
        if status in (403, 404):
            return {"ok": False, "error": f"audio-features unavailable (HTTP {status})",
                    "hint": "Spotify deprecated /v1/audio-features for apps created after "
                            "2024-11-27. Older apps still work."}
        return {"ok": False, "error": r.get("error") or f"HTTP {status}"}
    feats = r.get("json") if isinstance(r.get("json"), dict) else None
    if not isinstance(feats, dict) or feats.get("id") is None:
        return {"ok": False, "error": "no audio features returned", "track_id": tid}
    keep = ("danceability", "energy", "valence", "tempo", "acousticness", "instrumentalness",
            "speechiness", "liveness", "loudness", "key", "mode", "time_signature", "duration_ms")
    return {"ok": True, "track_id": tid, "features": {k: feats.get(k) for k in keep}}


@mcp.tool
def recommend(seed: str = "", mood: str = "", limit: int = 10) -> dict:
    """Recommend tracks from a free-text `seed` (a song name, artist, genre, or spotify URI) and/or
    a `mood` keyword (see list_moods). Uses the free Web API recommendations + audio-features
    endpoints; needs SPOTIFY_CLIENT_ID/SECRET. Returns matches with their spotify: URIs (feed one to
    play_uri, or use auto_queue to play them). Degrades to a clear error+hint when keys are missing
    or the endpoint is unavailable."""
    seed = (seed or "").strip()
    mood = (mood or "").strip().lower()
    if not seed and not mood:
        return {"ok": False, "error": "provide a seed and/or a mood",
                "hint": "e.g. recommend(seed='Bohemian Rhapsody') or recommend(mood='focus'); "
                        "moods: " + ", ".join(_moods_list())}
    if mood and mood not in _MOODS:
        return {"ok": False, "error": f"unknown mood {mood!r}",
                "moods": _moods_list(), "hint": "pick one of the listed moods"}
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10

    tok = _token()
    if not tok:
        return _no_keys()

    params: dict = {}
    resolved: dict = {}
    if seed:
        rs = _resolve_seed(seed)
        if rs.get("error"):
            env = rs.get("_envelope")
            if isinstance(env, dict) and not env.get("ok"):
                return env  # propagate a no-keys / network envelope unchanged
            return {"ok": False, "error": rs["error"],
                    "hint": "try a different song/artist name, a genre (see list_moods for hints), "
                            "or a spotify: URI"}
        params.update(rs.get("params") or {})
        resolved.update(rs.get("resolved") or {})

    if mood:
        params.update(_MOODS[mood])
        resolved["mood"] = mood

    # The endpoint requires at least one seed. If only a mood was given, seed by its genre when the
    # mood name is a known genre, else default to the broadly-accepted "pop" genre.
    has_seed = any(k in params for k in ("seed_tracks", "seed_artists", "seed_genres"))
    if not has_seed:
        params["seed_genres"] = mood if mood in _KNOWN_GENRES else "pop"
        resolved.setdefault("genre", params["seed_genres"])

    rec = _recommendations(tok, params, limit)
    if not rec.get("ok"):
        return {"ok": False, "error": rec.get("error"), **({"hint": rec["hint"]} if rec.get("hint") else {}),
                "resolved": resolved}
    return {"ok": True, "seed": seed or None, "mood": mood or None,
            "resolved": resolved, "results": rec["tracks"]}


@mcp.tool
def auto_queue(mood: str = "", seed: str = "", limit: int = 10, play: bool = True) -> dict:
    """Build a recommendation queue for a `mood` (and/or `seed`) and start it in the Spotify Mac app.
    Combines the free Web API (to pick tracks; needs SPOTIFY_CLIENT_ID/SECRET) with AppleScript
    playback (free, no keys). With play=True it plays the first track via the app; the rest are
    returned as `queue` URIs to play next. Set play=False to only get the list. Degrades to a clear
    error+hint when keys are missing or recommendations are unavailable."""
    try:
        limit = max(1, min(int(limit), 50))
    except (TypeError, ValueError):
        limit = 10
    rec = recommend(seed=seed, mood=mood, limit=limit)
    if not rec.get("ok"):
        return rec  # already a clear err+hint envelope (no-keys / unknown-mood / unavailable)
    tracks = rec.get("results") or []
    if not tracks:
        return {"ok": False, "error": "no recommendations returned",
                "resolved": rec.get("resolved"),
                "hint": "try a different mood/seed, or use search() + play_song()"}

    queue = [t for t in tracks if t.get("uri")]
    out = {"ok": True, "mood": rec.get("mood"), "seed": rec.get("seed"),
           "resolved": rec.get("resolved"), "queue": queue, "count": len(queue)}
    if not play:
        return out

    first = queue[0] if queue else None
    if not first:
        return {"ok": False, "error": "recommendations had no playable URIs",
                "queue": tracks}
    p = play_uri(first["uri"])
    if not p.get("ok"):
        return {"ok": False, "error": "picked a queue but couldn't start playback in the app",
                "detail": p.get("err"), "queue": queue, "count": len(queue)}
    out["now_playing"] = first
    out["up_next"] = queue[1:]
    out["now"] = now_playing()
    return out


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
