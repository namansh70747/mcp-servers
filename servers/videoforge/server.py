"""videoforge — the free, AI-drivable video editor. FFmpeg does the work; Claude does the thinking.

One-shot primitives (trim/concat/crop/scale/speed/fade/reverse/…), audio (extract/replace/mix with
sidechain DUCKING, loudnorm, remove_silence), overlays (watermark/picture_in_picture/add_text/
burn_subtitles), looks (color/lut/denoise/stabilize/sharpen), delivery (convert/compress/to_gif/
slideshow/make_short), a persistent TIMELINE/PROJECT model that composes ONE filter_complex and
encodes in a SINGLE pass (no generation loss), and a background-job system for slow renders
(render/convert/transcribe → job_id; poll job_status / list_jobs / cancel_job).

Local-free AI (optional `video` group: `uv pip install faster-whisper`): transcribe (faster-whisper →
SRT/VTT with word timestamps), auto_captions (burned captions), auto_reframe/make_short (9:16),
smart_cut/auto_highlights (silence+scene detection → structured cut proposals Claude curates).

The engine is a system binary — `brew install ffmpeg`. No API keys, ever. Nothing raises: a missing
ffmpeg/filter/whisper degrades to an ok:False dict with an install hint. Some Homebrew ffmpeg
bottles ship without drawtext/subtitles/vidstab; videoforge detects this per-filter and tells you
exactly how to get a fuller build.
"""
from __future__ import annotations

import functools
import json
import re
import secrets
import shutil
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from mcp_base import data_dir, err, get_env, get_logger, make_server, not_found, ok

log = get_logger("videoforge")

mcp = make_server(
    "videoforge",
    instructions=(
        "Free FFmpeg-powered video editor Claude drives. Analyze (media_info, detect_scenes, "
        "detect_silence, thumbnail, waveform). One-shot edits (trim, concat, crop, scale incl. "
        "9:16/1:1/16:9, rotate, speed, fade, reverse, loop, split). Audio (extract_audio, "
        "replace_audio, mix_audio with sidechain ducking, normalize loudness, remove_silence). "
        "Overlays (watermark, picture_in_picture, add_image, add_text, burn_subtitles). Looks "
        "(color, apply_lut, denoise, stabilize, sharpen). Deliver (convert, compress, to_gif, "
        "extract_frames, slideshow, make_short). Build a persistent project (create_project → "
        "add_clip/add_overlay/add_text_track/add_audio_track/add_transition/set_output → render) "
        "for a SINGLE-PASS no-quality-loss edit; render/convert/transcribe return a job_id — poll "
        "job_status. Local-free AI: transcribe + auto_captions (faster-whisper); smart_cut/"
        "auto_highlights propose cuts you curate. Needs `brew install ffmpeg`; transcription needs "
        "`uv pip install faster-whisper`."
    ),
)

# --------------------------------------------------------------------------- layout
ROOT = data_dir("videoforge")
OUT = ROOT / "output"
PROJ = ROOT / "projects"
JOBS_DIR = ROOT / "jobs"
TMP = ROOT / "tmp"
for _d in (OUT, PROJ, JOBS_DIR, TMP):
    _d.mkdir(parents=True, exist_ok=True)

# --------------------------------------------------------------------------- allow-lists
VIDEO_EXT = {".mp4", ".mov", ".mkv", ".webm", ".avi", ".m4v", ".gif", ".mpg", ".mpeg", ".ts", ".flv", ".wmv"}
AUDIO_EXT = {".mp3", ".m4a", ".aac", ".wav", ".flac", ".ogg", ".opus", ".wma"}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".webp", ".bmp", ".tiff", ".gif"}
SUB_EXT = {".srt", ".vtt", ".ass", ".ssa"}
MEDIA_EXT = VIDEO_EXT | AUDIO_EXT
IN_EXT = VIDEO_EXT | AUDIO_EXT | IMAGE_EXT

FORMATS = {"mp4": "mp4", "mov": "mov", "mkv": "matroska", "webm": "webm", "gif": "gif"}
VCODECS = {"h264": "libx264", "h265": "libx265", "hevc": "libx265", "vp9": "libvpx-vp9",
           "h264_hw": "h264_videotoolbox", "hevc_hw": "hevc_videotoolbox", "copy": "copy"}
ACODECS = {"aac": "aac", "mp3": "libmp3lame", "opus": "libopus", "flac": "flac", "copy": "copy"}
ASPECTS = {"9:16": (1080, 1920), "1:1": (1080, 1080), "16:9": (1920, 1080),
           "4:5": (1080, 1350), "4:3": (1440, 1080), "21:9": (2560, 1080), "3:4": (1080, 1440),
           "2:3": (1080, 1620)}
XFADE_KINDS = {"fade", "fadeblack", "fadewhite", "wipeleft", "wiperight", "wipeup", "wipedown",
               "slideleft", "slideright", "slideup", "slidedown", "circleopen", "circleclose",
               "dissolve", "smoothleft", "smoothright", "pixelize", "radial", "hblur"}
PRESETS = {"ultrafast", "superfast", "veryfast", "faster", "fast", "medium", "slow", "slower", "veryslow"}
CAPTION_STYLES = {
    "tiktok": "FontName=Arial,FontSize=20,Bold=1,PrimaryColour=&H00FFFFFF,OutlineColour=&H00000000,"
              "BorderStyle=1,Outline=3,Shadow=1,Alignment=2,MarginV=80",
    "minimal": "FontName=Arial,FontSize=16,PrimaryColour=&H00FFFFFF,OutlineColour=&H64000000,"
               "BorderStyle=1,Outline=1,Shadow=0,Alignment=2,MarginV=40",
    "broadcast": "FontName=Arial,FontSize=18,PrimaryColour=&H00FFFFFF,BackColour=&H80000000,"
                 "BorderStyle=3,Outline=0,Shadow=0,Alignment=2,MarginV=50",
    "default": "FontSize=18",
}
MAX_INPUT_BYTES = 12 * 1024 ** 3  # 12 GB per-file sanity cap
DEFAULT_TIMEOUT = 300  # seconds for the few quick metadata ops that stay synchronous
MAX_FILTERGRAPH_CHARS = 60_000  # refuse to spawn ffmpeg on a pathologically huge filter_complex

# --- any-length tunables (edit via .env, no code change) ---------------------------------------
# Heavy edits run as background jobs but wait inline up to INLINE_WAIT sec for a snappy result on
# short clips; longer ones return a job_id and keep running UNBOUNDED. Keep under the MCP client's
# request timeout (Qwen/Claude). Hardware (videotoolbox) auto-engages when a source is longer than
# HW_THRESHOLD seconds, making long renders 5-10x faster.
from mcp_base import get_env_int  # noqa: E402

INLINE_WAIT = max(3, get_env_int("VIDEOFORGE_INLINE_WAIT", 20) or 20)
HW_THRESHOLD = max(0, get_env_int("VIDEOFORGE_HW_THRESHOLD", 600) or 600)
ACCEL_MODES = {"auto", "hardware", "software"}
try:
    MAX_CONCURRENT_JOBS = max(1, int(get_env("VIDEOFORGE_MAX_JOBS", "3") or 3))
except (TypeError, ValueError):
    MAX_CONCURRENT_JOBS = 3

# --------------------------------------------------------------------------- binary + filter probing
_BIN_CACHE: dict[str, str | None] = {}
_NOT_INSTALLED = "ffmpeg not installed — run: brew install ffmpeg"
_FULL_BUILD_HINT = ("install a fuller ffmpeg build: `brew tap homebrew-ffmpeg/ffmpeg && "
                    "brew install homebrew-ffmpeg/ffmpeg/ffmpeg --with-libass --with-freetype "
                    "--with-libvidstab` (the default bottle omits these filters)")


def _bin(name: str) -> str | None:
    """Resolve ffmpeg/ffprobe once: env override → PATH → common Homebrew/system locations."""
    if name in _BIN_CACHE:
        return _BIN_CACHE[name]
    env_override = get_env(f"{name.upper()}_BIN")
    found = env_override if (env_override and Path(env_override).is_file()) else shutil.which(name)
    if not found:
        for c in (f"/opt/homebrew/bin/{name}", f"/usr/local/bin/{name}", f"/usr/bin/{name}"):
            if Path(c).is_file():
                found = c
                break
    _BIN_CACHE[name] = found
    return found


_CAPS_CACHE: dict[str, set[str]] = {}


def _caps(kind: str) -> set[str]:
    """Set of available 'filters' or 'encoders' for this ffmpeg build (queried once, cached)."""
    if kind in _CAPS_CACHE:
        return _CAPS_CACHE[kind]
    names: set[str] = set()
    exe = _bin("ffmpeg")
    if exe:
        try:
            p = subprocess.run([exe, "-hide_banner", f"-{kind}"], capture_output=True, text=True, timeout=20)
            for line in p.stdout.splitlines():
                parts = line.split()
                # filter lines: " T.. name  in->out  desc"; encoder lines: " V..... name  desc"
                if len(parts) >= 2 and re.fullmatch(r"[A-Z.]{2,6}", parts[0]):
                    names.add(parts[1])
        except Exception:  # noqa: BLE001
            pass
    _CAPS_CACHE[kind] = names
    return names


def _has_filter(name: str) -> bool:
    return name in _caps("filters")


def _has_encoder(name: str) -> bool:
    return name in _caps("encoders")


def _require_filter(name: str, feature: str):
    """Return an err() envelope if `name` filter is missing, else None."""
    if not _bin("ffmpeg"):
        return err(_NOT_INSTALLED)
    if not _has_filter(name):
        return err(f"{feature} needs the '{name}' filter, which your ffmpeg build lacks",
                   hint=_FULL_BUILD_HINT, missing_filter=name)
    return None


# --------------------------------------------------------------------------- numeric helpers
def _f(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return None


def _i(x):
    try:
        return int(x)
    except (TypeError, ValueError):
        return None


def _fps(r):
    """'30000/1001' -> 29.97; '30' -> 30.0."""
    if r is None:
        return None
    s = str(r)
    if "/" in s:
        n, d = s.split("/", 1)
        ni, di = _i(n), _i(d)
        return round(ni / di, 3) if (ni is not None and di) else None
    return _f(s)


# --------------------------------------------------------------------------- safety
def _safe_in(path: str, exts: set[str] | None = None):
    """Validate a user-supplied INPUT path. Returns a resolved Path, or an err() envelope.

    Inputs may live anywhere on disk (videos are usually in ~/Movies or ~/Downloads) but must be a
    real, allow-listed, size-bounded file. Args are always passed to ffmpeg as a list (no shell),
    so odd characters are harmless; this gate stops non-files / wrong types reaching the encoder.
    """
    exts = exts if exts is not None else IN_EXT
    if not path or not str(path).strip():
        return err("empty input path", hint="pass an absolute path to a media file")
    try:
        p = Path(str(path)).expanduser().resolve()
    except Exception:  # noqa: BLE001
        return err(f"bad path: {path}")
    if not p.exists():
        return err(f"input not found: {p}", hint="check the path; media is usually in ~/Movies or ~/Downloads")
    if not p.is_file():
        return err(f"not a file: {p}")
    if p.suffix.lower() not in exts:
        return err(f"unsupported input type '{p.suffix or '(none)'}'", supported=sorted(exts))
    try:
        if p.stat().st_size > MAX_INPUT_BYTES:
            return err(f"input too large (> {MAX_INPUT_BYTES // 1024 ** 3} GB): {p.name}")
    except OSError:
        pass
    return p


def _safe_out(filename: str, default: str, suffix: str) -> Path:
    """Force a user-supplied OUTPUT name into OUT/ (strip dirs, prevent traversal). Raises on abuse."""
    name = Path(str(filename or default)).name
    if not name or name in (".", ".."):
        name = default
    if not name.lower().endswith(suffix.lower()):
        name += suffix
    p = (OUT / name).resolve()
    if OUT.resolve() not in p.parents and p != OUT.resolve():
        raise RuntimeError("invalid filename")
    return p


def _out_name(stem: str, ext: str, filename: str = "") -> Path:
    """Collision-free output path; honors an explicit user filename (sanitized into OUT)."""
    if filename:
        return _safe_out(filename, f"{stem}{ext}", ext)
    ts = datetime.now().strftime("%Y%m%d-%H%M%S")
    return OUT / f"{stem}_{ts}_{secrets.token_hex(2)}{ext}"


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


# --------------------------------------------------------------------------- ffmpeg / ffprobe
def _ffmpeg(args: list[str], timeout: int | None = DEFAULT_TIMEOUT) -> dict:
    """Run ffmpeg with an arg LIST (never shell=True). timeout=None runs UNBOUNDED (used by background
    job steps that track progress themselves). Returns {ok, code, out, err}."""
    exe = _bin("ffmpeg")
    if not exe:
        return {"ok": False, "code": 127, "out": "", "err": _NOT_INSTALLED}
    cmd = [exe, "-hide_banner", "-nostdin", "-y", *args]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return {"ok": p.returncode == 0, "code": p.returncode,
                "out": (p.stdout or "")[-6000:], "err": (p.stderr or "")[-6000:]}
    except FileNotFoundError:
        return {"ok": False, "code": 127, "out": "", "err": _NOT_INSTALLED}
    except subprocess.TimeoutExpired:
        return {"ok": False, "code": -1, "out": "",
                "err": f"ffmpeg timed out after {timeout}s — use a project + render (background job) "
                       f"for long edits, or operate on a shorter segment"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "code": -1, "out": "", "err": str(e)}


def _probe(path: str) -> dict:
    """Probe a media file → friendly dict. Returns ok:False on any failure (never raises)."""
    exe = _bin("ffprobe")
    if not exe:
        return err("ffprobe " + _NOT_INSTALLED)
    src = _safe_in(path, MEDIA_EXT | IMAGE_EXT)
    if isinstance(src, dict):
        return src
    try:
        p = subprocess.run(
            [exe, "-v", "quiet", "-print_format", "json", "-show_format", "-show_streams", str(src)],
            capture_output=True, text=True, timeout=30)
        if p.returncode != 0:
            return err(f"ffprobe failed: {(p.stderr or '')[:300]}")
        meta = json.loads(p.stdout or "{}")
    except subprocess.TimeoutExpired:
        return err("ffprobe timed out")
    except json.JSONDecodeError as e:
        return err(f"could not parse ffprobe output: {e}")
    except Exception as e:  # noqa: BLE001
        return err(f"probe error: {e}")
    streams = meta.get("streams", [])
    fmt = meta.get("format", {})
    v = next((s for s in streams if s.get("codec_type") == "video"), None)
    a = next((s for s in streams if s.get("codec_type") == "audio"), None)
    info: dict = {"path": str(src), "duration": _f(fmt.get("duration")),
                  "format": fmt.get("format_name"), "size": _i(fmt.get("size")),
                  "bit_rate": _i(fmt.get("bit_rate")), "nb_streams": len(streams),
                  "has_audio": a is not None}
    if v:
        info["video"] = {"w": v.get("width"), "h": v.get("height"), "codec": v.get("codec_name"),
                         "fps": _fps(v.get("r_frame_rate")), "pix_fmt": v.get("pix_fmt"),
                         "duration": _f(v.get("duration")) or info["duration"]}
    if a:
        info["audio"] = {"codec": a.get("codec_name"), "channels": a.get("channels"),
                         "sample_rate": _i(a.get("sample_rate")), "bit_rate": _i(a.get("bit_rate"))}
    return ok(**info)


def _probe_av(path: Path) -> tuple[float | None, int, int, float, bool]:
    """Internal: (duration, w, h, fps, has_audio) for an already-validated Path. Best-effort."""
    r = _probe(str(path))
    if not r.get("ok"):
        return None, 0, 0, 30.0, False
    vid = r.get("video") or {}
    return (r.get("duration"), vid.get("w") or 0, vid.get("h") or 0,
            vid.get("fps") or 30.0, bool(r.get("has_audio")))


def _duration(path: Path) -> float | None:
    return _probe_av(path)[0]


# --------------------------------------------------------------------------- background jobs
_JOBS: dict[str, dict] = {}
_PROCS: dict[str, subprocess.Popen] = {}  # live ffmpeg subprocesses (for cancel + progress)
_ACTIVE: set[str] = set()  # job ids whose worker thread is alive (liveness across multi-step gaps)
_LOCK = threading.RLock()
_PROG_RE = re.compile(r"(\w+)=(\S+)")
# Cap concurrent ffmpeg work: extra jobs wait in "queued" instead of forking unbounded processes.
_JOB_SEM = threading.BoundedSemaphore(MAX_CONCURRENT_JOBS)

import atexit as _atexit  # noqa: E402


@_atexit.register
def _cleanup_tmp() -> None:
    try:
        shutil.rmtree(TMP, ignore_errors=True)
    except Exception:
        pass


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _job_path(jid: str) -> Path:
    return JOBS_DIR / f"{jid}.json"


def _persist(job: dict) -> None:
    try:
        tmp = JOBS_DIR / f".{job['id']}.tmp"
        tmp.write_text(json.dumps(job, indent=2))
        tmp.replace(_job_path(job["id"]))
    except Exception:  # noqa: BLE001 — persistence is best-effort
        log.warning("could not persist job %s", job.get("id"))


def _set(jid: str, **fields) -> dict:
    with _LOCK:
        job = _JOBS.get(jid, {"id": jid})
        job.update(fields)
        _JOBS[jid] = job
        _persist(job)
        return dict(job)


def _new_job(kind: str, output: str) -> dict:
    jid = f"{kind}_{datetime.now():%Y%m%d-%H%M%S}_{secrets.token_hex(3)}"
    job = {"id": jid, "kind": kind, "status": "queued", "percent": 0.0, "output": output,
           "started": _now(), "ended": None, "error": None, "speed": None}
    with _LOCK:
        _JOBS[jid] = job
        _persist(job)
    return job


def _spawn(kind: str, output: str, worker) -> dict:
    """Run worker(job) in a daemon thread; return a job_id immediately. worker must set a terminal
    status via _finish_*; if it doesn't, we mark it errored."""
    if not _bin("ffmpeg"):
        return err(_NOT_INSTALLED)
    job = _new_job(kind, output)

    def run():
        with _LOCK:
            _ACTIVE.add(job["id"])
        acquired = False
        try:
            _JOB_SEM.acquire()  # wait for a free slot; extra jobs stay "queued"
            acquired = True
            if _JOBS.get(job["id"], {}).get("status") == "cancelled":
                return
            _set(job["id"], status="running")
            worker(job)
        except Exception as e:  # noqa: BLE001
            log.exception("job %s failed", job["id"])
            _set(job["id"], status="error", error=str(e), ended=_now())
        finally:
            if acquired:
                _JOB_SEM.release()
            with _LOCK:
                _ACTIVE.discard(job["id"])
                cur = _JOBS.get(job["id"], {})
                if cur.get("status") in ("running", "queued"):
                    _set(job["id"], status="error", error="worker exited without terminal status",
                         ended=_now())

    threading.Thread(target=run, daemon=True).start()
    return ok(job_id=job["id"], status=job["status"], output=output,
              hint="poll job_status(job_id) until status='done' (jobs beyond the concurrency cap wait as 'queued')")


def _ffmpeg_job(job: dict, args: list[str], total_sec: float | None, out: Path) -> tuple[int, str]:
    """Inside a worker: run ffmpeg with -progress (global), stream percent, write to `out`. Returns
    (code, stderr_tail). `args` is everything except the output path — `out` is appended here so a
    caller can never forget it (and -progress stays a global option, before the output)."""
    exe = _bin("ffmpeg")
    cmd = [exe, "-hide_banner", "-nostdin", "-y", "-progress", "pipe:1", "-nostats", *args, str(out)]
    _set(job["id"], cmd=cmd)  # expose the exact ffmpeg command in job_status (transparency)
    try:
        proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, bufsize=1)
    except Exception as e:  # noqa: BLE001
        return -1, str(e)
    with _LOCK:
        _PROCS[job["id"]] = proc
    last = 0.0
    try:
        for line in proc.stdout:  # ffmpeg -progress emits key=value lines here
            m = dict(_PROG_RE.findall(line))
            if total_sec and "out_time_us" in m:
                us = _i(m["out_time_us"])
                if us is not None and time.time() - last > 0.8:
                    pct = max(0.0, min(99.0, round(us / 1_000_000 / total_sec * 100, 1)))
                    _set(job["id"], percent=pct, speed=m.get("speed"))
                    last = time.time()
            if m.get("progress") == "end":
                break
        code = proc.wait(timeout=20)
        tail = (proc.stderr.read() or "")[-3000:]
    except Exception as e:  # noqa: BLE001
        return -1, str(e)
    finally:
        with _LOCK:
            _PROCS.pop(job["id"], None)
    return code, tail


def _finish_ffmpeg(job: dict, code: int, tail: str, out: Path, **extra) -> None:
    if code == 0 and out.exists():
        _set(job["id"], status="done", percent=100.0, out=str(out), ended=_now(), **extra)
    elif _JOBS.get(job["id"], {}).get("status") == "cancelled":
        pass
    else:
        _set(job["id"], status="error", error=f"ffmpeg exited {code}: {tail[-700:]}", ended=_now())


def _run_or_job(kind: str, args: list[str], out: Path, total_sec: float | None = None,
                inline_wait: float = INLINE_WAIT, cleanup: tuple = (), **meta) -> dict:
    """Run an ffmpeg edit as an UNBOUNDED background job, but block inline up to `inline_wait` sec so
    SHORT clips return their finished result directly (snappy). LONG inputs keep running with live
    percent and return a job_id to poll — so videoforge can edit a video of ANY length without ever
    hitting a timeout (server- or client-side). `args` excludes the output path (appended downstream).
    `cleanup` paths are unlinked AFTER the job finishes (e.g. temp textfiles the encode reads)."""
    def worker(job):
        code, tail = _ffmpeg_job(job, args, total_sec, out)
        _finish_ffmpeg(job, code, tail, out, **meta)
        for p in cleanup:
            try:
                Path(p).unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    started = _spawn(kind, str(out), worker)
    if not started.get("ok"):
        return started  # e.g. ffmpeg not installed
    jid = started["job_id"]
    deadline = time.time() + max(0.0, float(inline_wait))
    while True:
        st = (_JOBS.get(jid) or {}).get("status")
        if st == "done":
            return ok(out=str(out), job_id=jid, **meta)
        if st in ("error", "cancelled", "interrupted"):
            return err((_JOBS.get(jid) or {}).get("error") or f"job {st}", job_id=jid)
        if time.time() >= deadline:
            return ok(job_id=jid, status=st or "running", out=str(out), **meta,
                      hint="still rendering a long input — poll job_status(job_id) until status='done'")
        time.sleep(0.3)


def _encode_args(crf: int = 20, preset: str = "medium", accel: str = "auto",
                 duration: float | None = None) -> list[str]:
    """Video-encoder flags. accel='auto' uses software libx264 (best quality) for short sources and
    Apple hardware h264_videotoolbox (5-10x faster) once a source exceeds HW_THRESHOLD seconds;
    'hardware' always uses videotoolbox (if present); 'software' always libx264."""
    mode = accel if accel in ACCEL_MODES else "auto"
    want_hw = mode == "hardware" or (mode == "auto" and duration and duration > HW_THRESHOLD)
    if want_hw and _has_encoder("h264_videotoolbox"):
        # videotoolbox quality: -q:v 1..100 (higher=better). Map x264 crf (0..51, lower=better).
        q = max(1, min(100, int(round(100 - (max(0, min(51, int(crf))) / 51.0) * 100))))
        return ["-c:v", "h264_videotoolbox", "-q:v", str(q), "-realtime", "0", "-pix_fmt", "yuv420p"]
    return ["-c:v", "libx264", "-crf", str(int(crf)), "-preset", preset, "-pix_fmt", "yuv420p"]


_JOB_META_KEYS = {"id", "kind", "status", "output", "started", "ended", "speed", "percent", "error"}


def _data_job(kind: str, worker, inline_wait: float = INLINE_WAIT) -> dict:
    """Run an ANALYSIS job (returns DATA, not a file) — e.g. scene/silence detection on a long video.
    worker(job) computes and _set()s its result fields. Blocks inline up to inline_wait so short
    sources return the data directly; long scans return a job_id whose job_status carries the data."""
    started = _spawn(kind, "", worker)
    if not started.get("ok"):
        return started
    jid = started["job_id"]
    deadline = time.time() + max(0.0, float(inline_wait))
    while True:
        job = _JOBS.get(jid) or {}
        st = job.get("status")
        if st == "done":
            return ok(job_id=jid, **{k: v for k, v in job.items() if k not in _JOB_META_KEYS})
        if st in ("error", "cancelled", "interrupted"):
            return err(job.get("error") or f"job {st}", job_id=jid)
        if time.time() >= deadline:
            return ok(job_id=jid, status=st or "running",
                      hint="scanning a long input — poll job_status(job_id) until status='done'")
        time.sleep(0.3)


def _reconcile(job: dict) -> dict:
    """Flip an orphaned 'running' job (whose worker thread is gone, e.g. after a restart) to
    'interrupted'. Liveness is the worker THREAD, not the subprocess — multi-step/in-process jobs
    (stabilize, transcribe) legitimately have gaps with no live ffmpeg process."""
    if job.get("status") == "running":
        with _LOCK:
            alive = job["id"] in _ACTIVE
        if not alive:
            job = {**job, "status": "interrupted",
                   "error": "worker not running (server restarted while this job was active)"}
    return job


def _load_job(jid: str) -> dict | None:
    if not jid or not str(jid).strip():
        return None
    with _LOCK:
        if jid in _JOBS:
            return dict(_JOBS[jid])
    p = _job_path(jid)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return None
    return None


def _all_jobs() -> list[dict]:
    seen: dict[str, dict] = {}
    for f in JOBS_DIR.glob("*.json"):
        try:
            j = json.loads(f.read_text())
            seen[j["id"]] = j
        except Exception:  # noqa: BLE001
            continue
    with _LOCK:
        seen.update({k: dict(v) for k, v in _JOBS.items()})
    return list(seen.values())


def _job_ids() -> list[str]:
    return [j["id"] for j in _all_jobs()][:10]


# --------------------------------------------------------------------------- position presets
def _overlay_xy(position: str, margin: int) -> tuple[str, str]:
    m = max(0, int(margin))
    table = {
        "top-left": (f"{m}", f"{m}"),
        "top-right": (f"W-w-{m}", f"{m}"),
        "bottom-left": (f"{m}", f"H-h-{m}"),
        "bottom-right": (f"W-w-{m}", f"H-h-{m}"),
        "center": ("(W-w)/2", "(H-h)/2"),
        "top": ("(W-w)/2", f"{m}"),
        "bottom": ("(W-w)/2", f"H-h-{m}"),
        "left": (f"{m}", "(H-h)/2"),
        "right": (f"W-w-{m}", "(H-h)/2"),
    }
    return table.get((position or "bottom-right").lower(), table["bottom-right"])


def _text_xy(position: str) -> str:
    table = {
        "top": "x=(w-text_w)/2:y=h*0.08",
        "bottom": "x=(w-text_w)/2:y=h*0.86-text_h",
        "center": "x=(w-text_w)/2:y=(h-text_h)/2",
        "top-left": "x=w*0.05:y=h*0.08",
        "top-right": "x=w*0.95-text_w:y=h*0.08",
        "bottom-left": "x=w*0.05:y=h*0.86-text_h",
        "bottom-right": "x=w*0.95-text_w:y=h*0.86-text_h",
    }
    return table.get((position or "bottom").lower(), table["bottom"])


# --------------------------------------------------------------------------- text engine (build-independent)
# Many ffmpeg builds (incl. the common Homebrew bottle) ship WITHOUT drawtext/subtitles/libass, so
# native text/caption burning fails. This engine renders text to transparent PNGs with Pillow and
# composites them via the always-present `overlay` filter — so titles + captions work on ANY build.
_FONT_CANDIDATES = (
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/Library/Fonts/Arial Unicode.ttf",
    "/System/Library/Fonts/Supplemental/Helvetica.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
    "/System/Library/Fonts/SFNS.ttf",
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
)


def _font_path() -> str | None:
    for p in _FONT_CANDIDATES:
        if Path(p).exists():
            return p
    return None


def _pillow_ok() -> bool:
    try:
        import PIL  # noqa: F401
        return True
    except Exception:
        return False


def _parse_color(c: str, default=(255, 255, 255, 255)) -> tuple:
    """Parse an ffmpeg-style color: name | #rrggbb | name@alpha (alpha 0..1) -> RGBA tuple."""
    try:
        from PIL import ImageColor
        s = (c or "").strip() or "white"
        alpha = 255
        if "@" in s:
            s, af = s.split("@", 1)
            try:
                alpha = max(0, min(255, int(round(float(af) * 255))))
            except Exception:
                alpha = 255
        rgb = ImageColor.getrgb(s or "white")
        if len(rgb) == 4:
            return rgb
        return (rgb[0], rgb[1], rgb[2], alpha)
    except Exception:
        return default


def _render_text_png(text: str, vw: int, vh: int, font_size: int = 48, color: str = "white",
                     box: bool = True, box_color: str = "black@0.5", max_frac: float = 0.9) -> Path | None:
    """Render `text` (word-wrapped) to a transparent RGBA PNG sized to the text + optional rounded box.
    Returns the PNG path, or None if Pillow is unavailable. Never raises."""
    try:
        from PIL import Image, ImageDraw, ImageFont
    except Exception:
        return None
    try:
        vw = int(vw) or 1280
        vh = int(vh) or 720
        fs = max(10, int(font_size))
        # scale the font to the actual frame so 48 looks right on 480p and 4K alike
        fs = max(12, int(fs * (vh / 1080.0))) if vh else fs
        fp = _font_path()
        try:
            font = ImageFont.truetype(fp, fs) if fp else ImageFont.load_default()
        except Exception:
            font = ImageFont.load_default()
        pad = max(10, fs // 3)
        scratch = ImageDraw.Draw(Image.new("RGBA", (8, 8)))

        def measure(s: str) -> tuple[int, int]:
            b = scratch.textbbox((0, 0), s or " ", font=font)
            return b[2] - b[0], b[3] - b[1]

        max_w = int(vw * max_frac) - 2 * pad
        words = (text or "").split()
        lines: list[str] = []
        cur = ""
        for w in words:
            cand = (cur + " " + w).strip()
            if measure(cand)[0] <= max_w or not cur:
                cur = cand
            else:
                lines.append(cur)
                cur = w
        if cur:
            lines.append(cur)
        if not lines:
            lines = [" "]
        line_h = measure("Ag")[1] + max(4, fs // 6)
        block_w = min(int(vw * max_frac), max(measure(ln)[0] for ln in lines) + 2 * pad)
        block_h = line_h * len(lines) + 2 * pad
        img = Image.new("RGBA", (block_w, block_h), (0, 0, 0, 0))
        d = ImageDraw.Draw(img)
        if box:
            try:
                d.rounded_rectangle([0, 0, block_w - 1, block_h - 1], radius=max(6, pad // 2),
                                    fill=_parse_color(box_color, (0, 0, 0, 128)))
            except Exception:
                d.rectangle([0, 0, block_w - 1, block_h - 1], fill=_parse_color(box_color, (0, 0, 0, 128)))
        fill = _parse_color(color)
        y = pad
        for ln in lines:
            lw = measure(ln)[0]
            # subtle shadow for legibility on busy footage
            d.text(((block_w - lw) // 2 + 2, y + 2), ln, font=font, fill=(0, 0, 0, 160))
            d.text(((block_w - lw) // 2, y), ln, font=font, fill=fill)
            y += line_h
        TMP.mkdir(parents=True, exist_ok=True)
        out = TMP / f"txt_{secrets.token_hex(5)}.png"
        img.save(str(out))
        return out
    except Exception:
        return None


def _overlay_xy(position: str, margin: float = 0.06) -> tuple[str, str]:
    """Overlay x/y expressions (W,H = main; w,h = overlay) for a named position."""
    cx = "(W-w)/2"
    lx = f"W*{margin}"
    rx = f"W-w-W*{margin}"
    ty = f"H*{margin}"
    by = f"H-h-H*{margin}"
    cy = "(H-h)/2"
    table = {"top": (cx, ty), "bottom": (cx, by), "center": (cx, cy),
             "top-left": (lx, ty), "top-right": (rx, ty),
             "bottom-left": (lx, by), "bottom-right": (rx, by)}
    return table.get((position or "bottom").lower(), (cx, by))


def _between(start: float, end: float) -> str:
    try:
        s, e = float(start or 0), float(end or 0)
    except Exception:
        return ""
    return f":enable='between(t,{s},{e})'" if e > s else ""


def _parse_srt(path: Path) -> list[tuple[float, float, str]]:
    """Parse .srt/.vtt into [(start_s, end_s, text)]. Best-effort; [] on failure."""
    def ts(t: str) -> float:
        t = t.strip().replace(",", ".")
        parts = t.split(":")
        try:
            if len(parts) == 3:
                h, m, s = parts
            elif len(parts) == 2:
                h, m, s = "0", parts[0], parts[1]
            else:
                return 0.0
            return int(h) * 3600 + int(m) * 60 + float(s)
        except Exception:
            return 0.0
    cues: list[tuple[float, float, str]] = []
    try:
        raw = Path(path).read_text(errors="ignore")
    except Exception:
        return cues
    blocks = re.split(r"\n\s*\n", raw.strip())
    for b in blocks:
        m = re.search(r"(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})\s*-->\s*(\d{1,2}:\d{2}(?::\d{2})?[.,]\d{1,3})", b)
        if not m:
            continue
        lines = [ln for ln in b.splitlines() if "-->" not in ln and not ln.strip().isdigit()
                 and ln.strip().upper() != "WEBVTT"]
        txt = " ".join(ln.strip() for ln in lines if ln.strip())
        if txt:
            cues.append((ts(m.group(1)), ts(m.group(2)), txt))
    return cues


def _build_overlay_args(src: Path, specs: list[dict], vw: int, vh: int):
    """Build ffmpeg args (sans output) that composite timed text PNGs over `src` via `overlay`.
    Returns (args, pngs) or an err dict. Caller runs the args and unlinks pngs afterward."""
    if not _pillow_ok():
        return err("text rendering needs Pillow", hint="uv pip install pillow (then reconnect)")
    specs = [s for s in (specs or []) if (s.get("text") or "").strip()][:400]
    if not specs:
        return err("no non-empty text to render")
    pngs: list[Path] = []
    fc_parts: list[str] = []
    label = "0:v"
    for i, s in enumerate(specs):
        png = _render_text_png(s["text"], vw, vh, s.get("font_size", 48), s.get("color", "white"),
                               s.get("box", True), s.get("box_color", "black@0.5"))
        if png is None:
            for p in pngs:
                p.unlink(missing_ok=True)
            return err("could not render text PNG (Pillow/font issue)")
        pngs.append(png)
        x, y = _overlay_xy(s.get("position", "bottom"))
        nxt = f"v{i}"
        fc_parts.append(f"[{label}][{i + 1}:v]overlay=x={x}:y={y}{_between(s.get('start', 0), s.get('end', 0))}[{nxt}]")
        label = nxt
    args = ["-i", str(src)]
    for p in pngs:
        args += ["-i", str(p)]
    args += ["-filter_complex", ";".join(fc_parts), "-map", f"[{label}]", "-map", "0:a?", "-c:a", "copy"]
    return args, pngs


def _overlay_text_job(kind: str, src: Path, specs: list[dict], out: Path, total_sec: float | None,
                      vw: int, vh: int) -> dict:
    """Composite timed text PNGs over `src` via overlay (build-independent), as a snappy job."""
    built = _build_overlay_args(src, specs, vw, vh)
    if isinstance(built, dict):
        return built
    args, pngs = built
    return _run_or_job(kind, args, out, total_sec=total_sec, cleanup=tuple(pngs))


# --------------------------------------------------------------------------- HEALTH
# Replace make_server's stub health with a richer capability report (same tool name).
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
    """Liveness + capability report: ffmpeg/ffprobe presence & version, available filters/encoders, whisper, output dir."""
    exe = _bin("ffmpeg")
    version = None
    if exe:
        try:
            r = subprocess.run([exe, "-version"], capture_output=True, text=True, timeout=10)
            version = (r.stdout.splitlines() or [None])[0]
        except Exception:  # noqa: BLE001
            pass
    filters = {f: _has_filter(f) for f in ("drawtext", "subtitles", "vidstabdetect",
                                           "sidechaincompress", "xfade", "zoompan", "loudnorm")}
    encoders = {c: _has_encoder(v) for c, v in (("h264", "libx264"), ("h265", "libx265"),
                                                ("vp9", "libvpx-vp9"), ("h264_hw", "h264_videotoolbox"))}
    hints = {}
    if not exe:
        hints["ffmpeg"] = "brew install ffmpeg"
    elif not all(filters[f] for f in ("drawtext", "subtitles")):
        hints["text_and_captions"] = _FULL_BUILD_HINT
    if not _whisper_ready():
        hints["transcription"] = "uv pip install faster-whisper (installs faster-whisper; model downloads on first use)"
    with _LOCK:
        active = sum(1 for j in _JOBS.values() if j.get("status") == "running")
    return ok(server="videoforge", ffmpeg=bool(exe), ffprobe=bool(_bin("ffprobe")),
              ffmpeg_version=version, filters=filters, encoders=encoders,
              whisper=_whisper_ready(), output_dir=str(OUT),
              projects=len(list(PROJ.glob("*.json"))), active_jobs=active,
              hints=hints or None)


def _whisper_ready() -> bool:
    try:
        import faster_whisper  # noqa: F401
        return True
    except Exception:  # noqa: BLE001
        return False


# =========================================================================== ANALYZE
@mcp.tool
@_guard
def probe(path: str) -> dict:
    """Raw-ish ffprobe summary of a media file: container, duration, video/audio stream details."""
    return _probe(path)


@mcp.tool
@_guard
def media_info(path: str) -> dict:
    """Friendly one-call summary of a clip (duration, resolution, fps, codecs, has_audio)."""
    return _probe(path)


def _detect_scenes_raw(src: Path, threshold: float) -> dict:
    """Unbounded scene-cut scan → ok(scenes, cut_points, …) or err. Used by the tool + smart_cut."""
    thr = max(0.05, min(0.95, float(threshold)))
    r = _ffmpeg(["-i", str(src), "-filter:v", f"select='gt(scene,{thr})',showinfo",
                 "-f", "null", "-"], timeout=None)
    if not r["ok"]:
        return err(r["err"] or "scene detection failed")
    times = sorted({round(float(m), 3) for m in re.findall(r"pts_time:([0-9.]+)", r["err"])})
    dur = _duration(src) or (times[-1] if times else 0.0)
    bounds = [0.0] + times + [round(dur, 3)]
    scenes = [{"index": i, "start": bounds[i], "end": bounds[i + 1],
               "duration": round(bounds[i + 1] - bounds[i], 3)}
              for i in range(len(bounds) - 1) if bounds[i + 1] > bounds[i]]
    return ok(scenes=scenes, count=len(scenes), cut_points=times, threshold=thr,
              hint="curate these, then trim/concat or build a project")


def _detect_silence_raw(src: Path, noise_db: float, min_dur: float) -> dict:
    """Unbounded silence scan → ok(silences, speech, …) or err. Used by the tool + remove_silence/smart_cut."""
    r = _ffmpeg(["-i", str(src), "-af", f"silencedetect=noise={float(noise_db)}dB:d={max(0.05, float(min_dur))}",
                 "-f", "null", "-"], timeout=None)
    if not r["ok"]:
        return err(r["err"] or "silence detection failed")
    starts = [float(x) for x in re.findall(r"silence_start:\s*([0-9.]+)", r["err"])]
    ends = [float(x) for x in re.findall(r"silence_end:\s*([0-9.]+)", r["err"])]
    silences = [{"start": round(s, 3), "end": round(e, 3), "duration": round(e - s, 3)}
                for s, e in zip(starts, ends)]
    dur = _duration(src) or 0.0
    speech, cursor = [], 0.0
    for s in silences:
        if s["start"] > cursor + 0.01:
            speech.append({"start": round(cursor, 3), "end": round(s["start"], 3),
                           "duration": round(s["start"] - cursor, 3)})
        cursor = s["end"]
    if dur > cursor + 0.01:
        speech.append({"start": round(cursor, 3), "end": round(dur, 3), "duration": round(dur - cursor, 3)})
    return ok(silences=silences, speech=speech, count=len(silences), duration=round(dur, 3))


@mcp.tool
@_guard
def detect_scenes(path: str, threshold: float = 0.4) -> dict:
    """Detect scene-change cut points (0..1 threshold; lower = more cuts). Any length — short returns data inline, long returns a job_id whose job_status carries the scenes."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src

    def worker(job):
        res = _detect_scenes_raw(src, threshold)
        if res.get("ok"):
            _set(job["id"], status="done", percent=100.0, ended=_now(),
                 **{k: v for k, v in res.items() if k != "ok"})
        else:
            _set(job["id"], status="error", error=res.get("error", "scene detection failed"), ended=_now())

    return _data_job("detect_scenes", worker)


@mcp.tool
@_guard
def detect_silence(path: str, noise_db: float = -30.0, min_dur: float = 0.5) -> dict:
    """Detect silent gaps (and the speech segments between them). Feeds remove_silence / smart_cut. Any length (data inline for short, job_id for long)."""
    src = _safe_in(path, MEDIA_EXT)
    if isinstance(src, dict):
        return src

    def worker(job):
        res = _detect_silence_raw(src, noise_db, min_dur)
        if res.get("ok"):
            _set(job["id"], status="done", percent=100.0, ended=_now(),
                 **{k: v for k, v in res.items() if k != "ok"})
        else:
            _set(job["id"], status="error", error=res.get("error", "silence detection failed"), ended=_now())

    return _data_job("detect_silence", worker)


@mcp.tool
@_guard
def thumbnail(path: str, at: float = 1.0, width: int = 640, out: str = "") -> dict:
    """Grab a single frame at `at` seconds as a JPG (scaled to `width`)."""
    src = _safe_in(path, VIDEO_EXT | IMAGE_EXT)
    if isinstance(src, dict):
        return src
    dst = _out_name(f"{src.stem}_thumb", ".jpg", out)
    w = max(16, int(width))
    r = _ffmpeg(["-ss", str(max(0.0, float(at))), "-i", str(src), "-frames:v", "1",
                 "-vf", f"scale={w}:-2", "-q:v", "3", str(dst)], timeout=60)
    return ok(out=str(dst)) if r["ok"] else err(r["err"] or "thumbnail failed")


@mcp.tool
@_guard
def contact_sheet(path: str, rows: int = 4, cols: int = 4, width: int = 1280, out: str = "") -> dict:
    """Montage of evenly-spaced frames (rows×cols) into one image — a quick visual index of a clip."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    rows, cols = max(1, int(rows)), max(1, int(cols))
    n = rows * cols
    dur = _duration(src) or 0.0
    if dur <= 0:
        return err("could not read duration for sampling")
    tile_w = max(80, int(width) // cols)
    rate = max(n / dur, 0.01)
    dst = _out_name(f"{src.stem}_sheet", ".png", out)
    return _run_or_job("contact_sheet", ["-i", str(src),
                                         "-vf", f"fps={rate},scale={tile_w}:-1,tile={cols}x{rows}",
                                         "-frames:v", "1"], dst, frames=n)


@mcp.tool
@_guard
def waveform(path: str, width: int = 1600, height: int = 240, out: str = "") -> dict:
    """Render the audio waveform of a clip to a PNG."""
    src = _safe_in(path, MEDIA_EXT)
    if isinstance(src, dict):
        return src
    w, h = max(100, int(width)), max(60, int(height))
    dst = _out_name(f"{src.stem}_wave", ".png", out)
    return _run_or_job("waveform", ["-i", str(src), "-filter_complex",
                                    f"showwavespic=s={w}x{h}:colors=0x3FA7FF", "-frames:v", "1"], dst)


@mcp.tool
@_guard
def list_presets() -> dict:
    """List supported aspect presets, container formats, codecs, transitions, and caption styles."""
    return ok(aspects=sorted(ASPECTS), formats=sorted(FORMATS), video_codecs=sorted(VCODECS),
              audio_codecs=sorted(ACODECS), transitions=sorted(XFADE_KINDS),
              x264_presets=sorted(PRESETS), caption_styles=sorted(CAPTION_STYLES))


@mcp.tool
@_guard
def list_encoders() -> dict:
    """Report which video/audio encoders and effect filters THIS ffmpeg build actually has."""
    if not _bin("ffmpeg"):
        return err(_NOT_INSTALLED)
    return ok(video={c: _has_encoder(v) for c, v in VCODECS.items() if v != "copy"},
              audio={c: _has_encoder(v) for c, v in ACODECS.items() if v != "copy"},
              filters={f: _has_filter(f) for f in ("drawtext", "subtitles", "vidstabdetect",
                                                   "sidechaincompress", "xfade", "zoompan",
                                                   "loudnorm", "hqdn3d", "unsharp", "lut3d")})


# =========================================================================== PRIMITIVES
@mcp.tool
@_guard
def trim(path: str, start: float = 0.0, end: float = 0.0, duration: float = 0.0,
         reencode: bool = False, out: str = "") -> dict:
    """Cut a clip to [start, end] (or start + duration). Stream-copy by default (fast/lossless); reencode for frame accuracy."""
    src = _safe_in(path, VIDEO_EXT | AUDIO_EXT)
    if isinstance(src, dict):
        return src
    start = max(0.0, float(start))
    if duration and duration > 0:
        dur = float(duration)
    elif end and end > start:
        dur = float(end) - start
    else:
        return err("specify end (> start) or duration (> 0)")
    dst = _out_name(f"{src.stem}_trim", src.suffix, out)
    args = ["-ss", str(start), "-i", str(src), "-t", str(round(dur, 3))]
    args += ["-c", "copy", "-avoid_negative_ts", "make_zero"] if not reencode else \
            ["-c:v", "libx264", "-crf", "18", "-preset", "fast", "-c:a", "aac"]
    return _run_or_job("trim", args, dst, total_sec=round(dur, 3), start=start, duration=round(dur, 3))


def _normalize_chain(idx: int, w: int, h: int, fps: float, has_audio: bool,
                     dur: float | None) -> tuple[list[str], str, str]:
    """Filter lines that normalize input `idx` to a common W×H/fps/format for concat/xfade. Returns
    (lines, video_label, audio_label). Synthesizes silence for clips lacking audio so concat works."""
    v, a = f"v{idx}", f"a{idx}"
    lines = [f"[{idx}:v]scale={w}:{h}:force_original_aspect_ratio=decrease,"
             f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1,fps={fps},format=yuv420p[{v}]"]
    if has_audio:
        lines.append(f"[{idx}:a]aresample=44100,asetpts=PTS-STARTPTS[{a}]")
    else:
        d = dur if (dur and dur > 0) else 1.0
        lines.append(f"anullsrc=channel_layout=stereo:sample_rate=44100,"
                     f"atrim=0:{round(d, 3)},asetpts=PTS-STARTPTS[{a}]")
    return lines, v, a


@mcp.tool
@_guard
def concat(paths: list[str], out: str = "") -> dict:
    """Join clips end-to-end into one video (re-encoded + normalized so mismatched sources still join cleanly)."""
    if not isinstance(paths, list) or len(paths) < 2:
        return err("pass a list of at least 2 file paths")
    srcs = []
    for p in paths:
        s = _safe_in(p, VIDEO_EXT)
        if isinstance(s, dict):
            return err(f"bad input '{p}': {s['error']}")
        srcs.append(s)
    # target geometry = first clip
    dur0, w0, h0, fps0, _ = _probe_av(srcs[0])
    w, h, fps = (w0 or 1920), (h0 or 1080), (fps0 or 30.0)
    inputs, fc, vlabels, alabels = [], [], [], []
    for i, s in enumerate(srcs):
        d, _, _, _, has_a = _probe_av(s)
        inputs += ["-i", str(s)]
        lines, vl, al = _normalize_chain(i, w, h, fps, has_a, d)
        fc += lines
        vlabels.append(vl)
        alabels.append(al)
    fc.append("".join(f"[{v}]" for v in vlabels) + f"concat=n={len(vlabels)}:v=1:a=0[vout]")
    fc.append("".join(f"[{a}]" for a in alabels) + f"concat=n={len(alabels)}:v=0:a=1[aout]")
    dst = _out_name("concat", ".mp4", out)
    args = [*inputs, "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
            "-c:a", "aac", "-b:a", "192k", "-movflags", "+faststart"]
    return _run_or_job("concat", args, dst, clips=len(srcs))


@mcp.tool
@_guard
def crop(path: str, w: int, h: int, x: int = -1, y: int = -1, out: str = "") -> dict:
    """Crop to w×h at (x, y); centers the crop when x/y omitted."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if int(w) <= 0 or int(h) <= 0:
        return err("w and h must be positive")
    xe = f"{int(x)}" if x is not None and x >= 0 else "(in_w-out_w)/2"
    ye = f"{int(y)}" if y is not None and y >= 0 else "(in_h-out_h)/2"
    dst = _out_name(f"{src.stem}_crop", ".mp4", out)
    return _run_or_job("crop", ["-i", str(src), "-vf", f"crop={int(w)}:{int(h)}:{xe}:{ye}", "-c:a", "copy"],
                       dst, total_sec=_duration(src), w=int(w), h=int(h))


@mcp.tool
@_guard
def scale(path: str, width: int = 0, height: int = 0, aspect: str = "", mode: str = "fit",
          out: str = "") -> dict:
    """Resize. Give width/height, or an aspect preset (9:16, 1:1, 16:9, 4:5…). mode: fit(pad)/fill(crop)/stretch."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if aspect:
        if aspect not in ASPECTS:
            return not_found("aspect", aspect, available=sorted(ASPECTS))
        w, h = ASPECTS[aspect]
    elif int(width) > 0 or int(height) > 0:
        w, h = int(width) or -2, int(height) or -2
    else:
        return err("specify width/height or an aspect preset", available_aspects=sorted(ASPECTS))
    mode = (mode or "fit").lower()
    if w > 0 and h > 0 and mode in ("fit", "fill"):
        if mode == "fit":
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=decrease,"
                  f"pad={w}:{h}:(ow-iw)/2:(oh-ih)/2,setsar=1")
        else:  # fill = scale to cover then center-crop
            vf = (f"scale={w}:{h}:force_original_aspect_ratio=increase,"
                  f"crop={w}:{h},setsar=1")
    else:
        vf = f"scale={w}:{h},setsar=1"
    dst = _out_name(f"{src.stem}_scaled", ".mp4", out)
    return _run_or_job("scale", ["-i", str(src), "-vf", vf, "-c:a", "copy"], dst,
                       total_sec=_duration(src), width=w, height=h, mode=mode)


@mcp.tool
@_guard
def rotate(path: str, degrees: int = 90, out: str = "") -> dict:
    """Rotate the video by 90/180/270 degrees (clockwise)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    d = int(degrees) % 360
    vf = {90: "transpose=1", 180: "transpose=1,transpose=1", 270: "transpose=2"}.get(d)
    if not vf:
        return err("degrees must be 90, 180, or 270")
    dst = _out_name(f"{src.stem}_rot", ".mp4", out)
    return _run_or_job("rotate", ["-i", str(src), "-vf", vf, "-c:a", "copy"], dst,
                       total_sec=_duration(src), degrees=d)


@mcp.tool
@_guard
def flip(path: str, direction: str = "h", out: str = "") -> dict:
    """Mirror the video horizontally ('h') or vertically ('v')."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    vf = {"h": "hflip", "v": "vflip", "horizontal": "hflip", "vertical": "vflip"}.get((direction or "h").lower())
    if not vf:
        return err("direction must be 'h' or 'v'")
    dst = _out_name(f"{src.stem}_flip", ".mp4", out)
    return _run_or_job("flip", ["-i", str(src), "-vf", vf, "-c:a", "copy"], dst,
                       total_sec=_duration(src), direction=direction)


def _atempo_chain(factor: float) -> str:
    """atempo only accepts 0.5..2.0; chain to reach any factor (pitch-preserving)."""
    f, parts = factor, []
    while f > 2.0:
        parts.append("atempo=2.0")
        f /= 2.0
    while f < 0.5:
        parts.append("atempo=0.5")
        f /= 0.5
    parts.append(f"atempo={round(f, 4)}")
    return ",".join(parts)


@mcp.tool
@_guard
def speed(path: str, factor: float = 2.0, keep_pitch: bool = True, out: str = "") -> dict:
    """Speed up (>1) or slow down (<1) a clip. keep_pitch=True preserves audio pitch (atempo); False = chipmunk/deep."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    f = float(factor)
    if f <= 0:
        return err("factor must be > 0")
    _, _, _, _, has_a = _probe_av(src)
    vf = f"setpts={round(1.0 / f, 6)}*PTS"
    if has_a:
        af = _atempo_chain(f) if keep_pitch else f"asetrate=44100*{round(f, 4)},aresample=44100"
        fc = f"[0:v]{vf}[v];[0:a]{af}[a]"
        maps = ["-map", "[v]", "-map", "[a]"]
    else:
        fc = f"[0:v]{vf}[v]"
        maps = ["-map", "[v]"]
    dst = _out_name(f"{src.stem}_x{str(f).replace('.', '_')}", ".mp4", out)
    out_dur = (_duration(src) or 0) / f if _duration(src) else None
    return _run_or_job("speed", ["-i", str(src), "-filter_complex", fc, *maps,
                                 "-c:v", "libx264", "-crf", "20", "-preset", "medium"], dst,
                       total_sec=out_dur, factor=f, keep_pitch=bool(keep_pitch))


@mcp.tool
@_guard
def reverse(path: str, audio: bool = True, out: str = "") -> dict:
    """Play a clip backwards (whole clip held in memory — best on short clips)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    _, _, _, _, has_a = _probe_av(src)
    dst = _out_name(f"{src.stem}_rev", ".mp4", out)
    if audio and has_a:
        args = ["-i", str(src), "-vf", "reverse", "-af", "areverse"]
    else:
        args = ["-i", str(src), "-vf", "reverse", "-an"]
    return _run_or_job("reverse", args, dst, total_sec=_duration(src))


@mcp.tool
@_guard
def fade(path: str, fade_in: float = 0.0, fade_out: float = 0.0, audio: bool = True,
         color: str = "black", out: str = "") -> dict:
    """Add a fade-in and/or fade-out at the ends (video + audio)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    fi, fo = max(0.0, float(fade_in)), max(0.0, float(fade_out))
    if fi == 0 and fo == 0:
        return err("set fade_in and/or fade_out (seconds)")
    dur = _duration(src)
    if not dur:
        return err("could not read duration")
    _, _, _, _, has_a = _probe_av(src)
    vparts, aparts = [], []
    if fi > 0:
        vparts.append(f"fade=t=in:st=0:d={fi}:color={color}")
        aparts.append(f"afade=t=in:st=0:d={fi}")
    if fo > 0:
        st = max(0.0, dur - fo)
        vparts.append(f"fade=t=out:st={round(st, 3)}:d={fo}:color={color}")
        aparts.append(f"afade=t=out:st={round(st, 3)}:d={fo}")
    dst = _out_name(f"{src.stem}_fade", ".mp4", out)
    args = ["-i", str(src), "-vf", ",".join(vparts)]
    if audio and has_a:
        args += ["-af", ",".join(aparts)]
    elif not has_a:
        args += ["-an"]
    return _run_or_job("fade", args, dst, total_sec=dur, fade_in=fi, fade_out=fo)


@mcp.tool
@_guard
def loop(path: str, count: int = 2, out: str = "") -> dict:
    """Repeat a clip `count` times back-to-back."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    n = int(count)
    if n < 2:
        return err("count must be >= 2")
    dst = _out_name(f"{src.stem}_loop", src.suffix, out)

    def worker(job):
        # stream_loop repeats the demuxed input; -c copy keeps it lossless when codecs allow
        r = _ffmpeg(["-stream_loop", str(n - 1), "-i", str(src), "-c", "copy", str(dst)], timeout=None)
        if r["ok"] and dst.exists():
            _set(job["id"], status="done", percent=100.0, out=str(dst), ended=_now(), count=n)
            return
        code, tail = _ffmpeg_job(job, ["-stream_loop", str(n - 1), "-i", str(src), "-c:v", "libx264",
                                       "-crf", "20", "-c:a", "aac"], None, dst)
        _finish_ffmpeg(job, code, tail, dst, count=n)

    return _spawn("loop", str(dst), worker)


@mcp.tool
@_guard
def split(path: str, at: list[float], out_prefix: str = "") -> dict:
    """Split a clip at the given timestamps into multiple parts (stream-copy)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if not isinstance(at, list) or not at:
        return err("pass a list of split timestamps (seconds), e.g. [10, 25.5]")
    dur = _duration(src) or 0.0
    cuts = sorted({round(float(t), 3) for t in at if 0 < float(t) < dur})
    bounds = [0.0] + cuts + [round(dur, 3)]
    prefix = (Path(str(out_prefix)).name or src.stem) if out_prefix else src.stem
    parts = []
    for i in range(len(bounds) - 1):
        s, e = bounds[i], bounds[i + 1]
        if e - s < 0.05:
            continue
        dst = _out_name(f"{prefix}_part{i + 1}", src.suffix)
        r = _ffmpeg(["-ss", str(s), "-i", str(src), "-t", str(round(e - s, 3)),
                     "-c", "copy", "-avoid_negative_ts", "make_zero", str(dst)], timeout=120)
        if r["ok"]:
            parts.append(str(dst))
    if not parts:
        return err("no parts produced — check timestamps are within the clip duration")
    return ok(parts=parts, count=len(parts))


# =========================================================================== AUDIO
@mcp.tool
@_guard
def extract_audio(path: str, format: str = "mp3", out: str = "") -> dict:
    """Pull the audio track out to mp3/wav/aac/flac/m4a."""
    src = _safe_in(path, MEDIA_EXT)
    if isinstance(src, dict):
        return src
    fmt = (format or "mp3").lower().lstrip(".")
    enc = {"mp3": ["-c:a", "libmp3lame", "-q:a", "2"], "wav": ["-c:a", "pcm_s16le"],
           "aac": ["-c:a", "aac", "-b:a", "192k"], "m4a": ["-c:a", "aac", "-b:a", "192k"],
           "flac": ["-c:a", "flac"], "opus": ["-c:a", "libopus"], "ogg": ["-c:a", "libvorbis"]}.get(fmt)
    if not enc:
        return err(f"unsupported audio format '{fmt}'", supported=["mp3", "wav", "aac", "m4a", "flac", "opus", "ogg"])
    dst = _out_name(src.stem, f".{fmt}", out)
    return _run_or_job("extract_audio", ["-i", str(src), "-vn", *enc], dst,
                       total_sec=_duration(src), format=fmt)


@mcp.tool
@_guard
def replace_audio(path: str, audio: str, shortest: bool = True, out: str = "") -> dict:
    """Swap a video's soundtrack for a new audio file."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    aud = _safe_in(audio, AUDIO_EXT | VIDEO_EXT)
    if isinstance(aud, dict):
        return err(f"bad audio input: {aud['error']}")
    dst = _out_name(f"{src.stem}_newaudio", ".mp4", out)
    args = ["-i", str(src), "-i", str(aud), "-map", "0:v:0", "-map", "1:a:0",
            "-c:v", "copy", "-c:a", "aac", "-b:a", "192k"]
    if shortest:
        args.append("-shortest")
    return _run_or_job("replace_audio", args, dst, total_sec=_duration(src))


@mcp.tool
@_guard
def mix_audio(path: str, music: str, music_db: float = -12.0, duck: bool = True,
              out: str = "") -> dict:
    """Mix background music under a video's original audio. duck=True sidechain-ducks the music beneath speech."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    mus = _safe_in(music, AUDIO_EXT | VIDEO_EXT)
    if isinstance(mus, dict):
        return err(f"bad music input: {mus['error']}")
    _, _, _, _, has_a = _probe_av(src)
    if not has_a:
        return replace_audio(path, music, shortest=True, out=out)
    gain = float(music_db)
    if duck and _has_filter("sidechaincompress"):
        fc = (f"[1:a]volume={gain}dB[m];"
              f"[m][0:a]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[mq];"
              f"[0:a][mq]amix=inputs=2:duration=first:dropout_transition=0[aout]")
    else:
        fc = (f"[1:a]volume={gain}dB[m];"
              f"[0:a][m]amix=inputs=2:duration=first:dropout_transition=0[aout]")
    dst = _out_name(f"{src.stem}_music", ".mp4", out)
    return _run_or_job("mix_audio", ["-i", str(src), "-stream_loop", "-1", "-i", str(mus),
                                     "-filter_complex", fc, "-map", "0:v", "-map", "[aout]",
                                     "-c:v", "copy", "-c:a", "aac", "-b:a", "192k", "-shortest"],
                       dst, total_sec=_duration(src), ducked=bool(duck))


@mcp.tool
@_guard
def normalize(path: str, target_lufs: float = -14.0, out: str = "") -> dict:
    """Normalize loudness to a target LUFS (EBU R128 loudnorm). -14 = streaming/social standard."""
    src = _safe_in(path, MEDIA_EXT)
    if isinstance(src, dict):
        return src
    if not _has_filter("loudnorm"):
        return err("loudnorm filter missing from this ffmpeg build", hint=_FULL_BUILD_HINT)
    is_video = src.suffix.lower() in VIDEO_EXT
    dst = _out_name(f"{src.stem}_norm", src.suffix if not is_video else ".mp4", out)
    af = f"loudnorm=I={float(target_lufs)}:TP=-1.5:LRA=11"
    args = ["-i", str(src), "-af", af]
    args += (["-c:v", "copy"] if is_video else []) + ["-c:a", "aac" if is_video else "libmp3lame"]
    return _run_or_job("normalize", args, dst, total_sec=_duration(src), target_lufs=float(target_lufs))


@mcp.tool
@_guard
def remove_silence(path: str, noise_db: float = -30.0, min_dur: float = 0.6, pad: float = 0.05,
                   out: str = "") -> dict:
    """Auto jump-cut: detect silent gaps and remove them, keeping only the speech (single-pass background job)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    sil = _detect_silence_raw(src, noise_db, min_dur)
    if not sil.get("ok"):
        return sil
    speech = sil.get("speech") or []
    if not speech:
        return err("no speech segments detected — try a higher noise_db (e.g. -25) or smaller min_dur")
    dur = sil.get("duration") or 0.0
    pad = max(0.0, float(pad))
    segs = [(max(0.0, s["start"] - pad), min(dur, s["end"] + pad)) for s in speech]
    dst = _out_name(f"{src.stem}_nosilence", ".mp4", out)
    fc, vlabels, alabels = [], [], []
    for i, (s, e) in enumerate(segs):
        fc.append(f"[0:v]trim=start={round(s, 3)}:end={round(e, 3)},setpts=PTS-STARTPTS[v{i}]")
        fc.append(f"[0:a]atrim=start={round(s, 3)}:end={round(e, 3)},asetpts=PTS-STARTPTS[a{i}]")
        vlabels.append(f"v{i}")
        alabels.append(f"a{i}")
    fc.append("".join(f"[{v}]" for v in vlabels) + f"concat=n={len(vlabels)}:v=1:a=0[vout]")
    fc.append("".join(f"[{a}]" for a in alabels) + f"concat=n={len(alabels)}:v=0:a=1[aout]")
    args = ["-i", str(src), "-filter_complex", ";".join(fc), "-map", "[vout]", "-map", "[aout]",
            "-c:v", "libx264", "-crf", "20", "-preset", "medium", "-c:a", "aac"]
    total = sum(e - s for s, e in segs)

    def worker(job):
        code, tail = _ffmpeg_job(job, args, total, dst)
        _finish_ffmpeg(job, code, tail, dst, removed_segments=len(speech))

    return _spawn("remove_silence", str(dst), worker)


# =========================================================================== OVERLAYS
@mcp.tool
@_guard
def watermark(path: str, image: str, position: str = "bottom-right", margin: int = 24,
              scale: float = 0.15, opacity: float = 1.0, out: str = "") -> dict:
    """Overlay a logo/PNG watermark in a corner, scaled to a fraction of the video width."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    img = _safe_in(image, IMAGE_EXT)
    if isinstance(img, dict):
        return err(f"bad image: {img['error']}")
    x, y = _overlay_xy(position, margin)
    op = max(0.0, min(1.0, float(opacity)))
    frac = max(0.01, min(1.0, float(scale)))
    fc = (f"[1:v]scale=iw*{frac}:-1,format=rgba,colorchannelmixer=aa={op}[wm];"
          f"[0:v][wm]overlay={x}:{y}[vout]")
    dst = _out_name(f"{src.stem}_wm", ".mp4", out)
    return _run_or_job("watermark", ["-i", str(src), "-i", str(img), "-filter_complex", fc,
                                     "-map", "[vout]", "-map", "0:a?", "-c:a", "copy"],
                       dst, total_sec=_duration(src), position=position)


@mcp.tool
@_guard
def picture_in_picture(base: str, overlay: str, position: str = "top-right", scale: float = 0.3,
                       start: float = 0.0, end: float = 0.0, out: str = "") -> dict:
    """Inset a second video (e.g. webcam) over a base video, positioned/scaled/timed."""
    b = _safe_in(base, VIDEO_EXT)
    if isinstance(b, dict):
        return b
    o = _safe_in(overlay, VIDEO_EXT | IMAGE_EXT)
    if isinstance(o, dict):
        return err(f"bad overlay: {o['error']}")
    x, y = _overlay_xy(position, 24)
    frac = max(0.05, min(1.0, float(scale)))
    enable = ""
    if end and float(end) > float(start):
        enable = f":enable='between(t,{float(start)},{float(end)})'"
    fc = f"[1:v]scale=iw*{frac}:-1[pip];[0:v][pip]overlay={x}:{y}{enable}[vout]"
    dst = _out_name(f"{b.stem}_pip", ".mp4", out)
    return _run_or_job("picture_in_picture", ["-i", str(b), "-i", str(o), "-filter_complex", fc,
                                              "-map", "[vout]", "-map", "0:a?", "-c:a", "copy"],
                       dst, total_sec=_duration(b), position=position)


@mcp.tool
@_guard
def add_image(path: str, image: str, start: float = 0.0, end: float = 0.0,
              position: str = "center", scale: float = 0.5, out: str = "") -> dict:
    """Overlay a static image (lower-third, badge, end-card) for a time window."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    img = _safe_in(image, IMAGE_EXT)
    if isinstance(img, dict):
        return err(f"bad image: {img['error']}")
    x, y = _overlay_xy(position, 24)
    frac = max(0.01, min(1.0, float(scale)))
    enable = ""
    if end and float(end) > float(start):
        enable = f":enable='between(t,{float(start)},{float(end)})'"
    fc = f"[1:v]scale=iw*{frac}:-1[img];[0:v][img]overlay={x}:{y}{enable}[vout]"
    dst = _out_name(f"{src.stem}_img", ".mp4", out)
    return _run_or_job("add_image", ["-i", str(src), "-i", str(img), "-filter_complex", fc,
                                     "-map", "[vout]", "-map", "0:a?", "-c:a", "copy"],
                       dst, total_sec=_duration(src))


@mcp.tool
@_guard
def add_text(path: str, text: str, start: float = 0.0, end: float = 0.0, font_size: int = 48,
             color: str = "white", position: str = "bottom", box: bool = True,
             box_color: str = "black@0.5", out: str = "") -> dict:
    """Burn a text overlay onto the video (needs the drawtext filter; uses a textfile so any characters are safe)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if not (text or "").strip():
        return err("text must be non-empty")
    dst = _out_name(f"{src.stem}_text", ".mp4", out)
    # Preferred: native drawtext (fast). Fallback: Pillow->overlay, which works on ANY ffmpeg build
    # (this machine's ffmpeg lacks drawtext). Same visual result, no libfreetype needed.
    if _has_filter("drawtext"):
        tf = TMP / f"text_{secrets.token_hex(4)}.txt"
        tf.write_text(text)
        enable = ""
        if end and float(end) > float(start):
            enable = f":enable='between(t,{float(start)},{float(end)})'"
        boxpart = f":box=1:boxcolor={box_color}:boxborderw=12" if box else ""
        draw = (f"drawtext=textfile='{tf}':fontsize={int(font_size)}:fontcolor={color}"
                f":{_text_xy(position)}{boxpart}{enable}")
        return _run_or_job("add_text", ["-i", str(src), "-vf", draw, "-c:a", "copy"], dst,
                           total_sec=_duration(src), cleanup=(tf,))
    dur, vw, vh, _, _ = _probe_av(src)
    return _overlay_text_job("add_text", src, [{
        "text": text, "start": start, "end": end, "font_size": font_size, "color": color,
        "box": box, "box_color": box_color, "position": position}], dst, dur, vw, vh)


def _prep_subs(subtitles: str):
    """Validate + copy a subtitle file into TMP under a safe ascii name (avoids subtitles-filter path escaping)."""
    sub = _safe_in(subtitles, SUB_EXT)
    if isinstance(sub, dict):
        return sub
    safe = TMP / f"subs_{secrets.token_hex(4)}{sub.suffix.lower()}"
    try:
        safe.write_bytes(sub.read_bytes())
    except Exception as e:  # noqa: BLE001
        return err(f"could not read subtitle file: {e}")
    return safe


@mcp.tool
@_guard
def burn_subtitles(path: str, subtitles: str, style: str = "default", out: str = "") -> dict:
    """Hard-burn .srt/.vtt/.ass subtitles into the picture (needs the subtitles/libass filter)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    safe = _prep_subs(subtitles)
    if isinstance(safe, dict):
        return safe
    dst = _out_name(f"{src.stem}_subbed", ".mp4", out)
    # Native libass path when available; otherwise render each cue as a PNG and overlay it (works on
    # any ffmpeg build — this machine's lacks the subtitles/ass filter).
    if _has_filter("subtitles"):
        force = CAPTION_STYLES.get(style, CAPTION_STYLES["default"])
        vf = f"subtitles='{safe}':force_style='{force}'"
        return _run_or_job("burn_subtitles", ["-i", str(src), "-vf", vf, "-c:a", "copy"], dst,
                           total_sec=_duration(src), cleanup=(safe,), style=style)
    cues = _parse_srt(safe)
    safe.unlink(missing_ok=True)
    if not cues:
        return err("no subtitle cues parsed", hint="provide a valid .srt/.vtt")
    dur, vw, vh, _, _ = _probe_av(src)
    specs = [{"text": t, "start": s, "end": e, "font_size": 40, "color": "white",
              "box": True, "box_color": "black@0.55", "position": "bottom"} for s, e, t in cues]
    return _overlay_text_job("burn_subtitles", src, specs, dst, dur, vw, vh)


# =========================================================================== LOOKS
@mcp.tool
@_guard
def color(path: str, brightness: float = 0.0, contrast: float = 1.0, saturation: float = 1.0,
          gamma: float = 1.0, out: str = "") -> dict:
    """Color-grade: brightness (-1..1), contrast (~0..3), saturation (0..3), gamma (0.1..3)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    eq = (f"eq=brightness={float(brightness)}:contrast={float(contrast)}"
          f":saturation={float(saturation)}:gamma={float(gamma)}")
    dst = _out_name(f"{src.stem}_color", ".mp4", out)
    return _run_or_job("color", ["-i", str(src), "-vf", eq, "-c:a", "copy"], dst, total_sec=_duration(src))


@mcp.tool
@_guard
def apply_lut(path: str, lut: str, out: str = "") -> dict:
    """Apply a 3D color LUT (.cube) for a cinematic look."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    lp = Path(str(lut)).expanduser()
    if not lp.is_file() or lp.suffix.lower() != ".cube":
        return err("lut must be a .cube file that exists")
    if not _has_filter("lut3d"):
        return err("lut3d filter missing from this ffmpeg build", hint=_FULL_BUILD_HINT)
    dst = _out_name(f"{src.stem}_lut", ".mp4", out)
    return _run_or_job("apply_lut", ["-i", str(src), "-vf", f"lut3d='{lp.resolve()}'", "-c:a", "copy"],
                       dst, total_sec=_duration(src))


@mcp.tool
@_guard
def denoise(path: str, strength: float = 4.0, out: str = "") -> dict:
    """Reduce sensor noise / grain (hqdn3d)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    s = max(0.0, float(strength))
    dst = _out_name(f"{src.stem}_denoise", ".mp4", out)
    return _run_or_job("denoise", ["-i", str(src), "-vf", f"hqdn3d={s}:{s}:{s * 1.5}:{s * 1.5}", "-c:a", "copy"],
                       dst, total_sec=_duration(src))


@mcp.tool
@_guard
def sharpen(path: str, amount: float = 1.0, out: str = "") -> dict:
    """Sharpen the picture (unsharp mask)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    a = max(0.0, min(3.0, float(amount)))
    dst = _out_name(f"{src.stem}_sharp", ".mp4", out)
    return _run_or_job("sharpen", ["-i", str(src), "-vf", f"unsharp=5:5:{a}:5:5:0", "-c:a", "copy"],
                       dst, total_sec=_duration(src))


@mcp.tool
@_guard
def stabilize(path: str, smoothing: int = 10, out: str = "") -> dict:
    """Stabilize shaky footage (2-pass vidstab; needs an ffmpeg built with libvidstab). Background job."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if not _has_filter("vidstabdetect"):
        return err("stabilize needs an ffmpeg built with libvidstab", hint=_FULL_BUILD_HINT,
                   missing_filter="vidstabdetect")
    sm = max(1, int(smoothing))
    trf = TMP / f"vidstab_{secrets.token_hex(4)}.trf"
    dst = _out_name(f"{src.stem}_stable", ".mp4", out)

    def worker(job):
        _set(job["id"], percent=5.0)
        d1 = _ffmpeg(["-i", str(src), "-vf", f"vidstabdetect=shakiness=5:result='{trf}'",
                      "-f", "null", "-"], timeout=None)  # background job — unbounded (any length)
        if not d1["ok"]:
            _set(job["id"], status="error", error=d1["err"], ended=_now())
            return
        _set(job["id"], percent=50.0)
        code, tail = _ffmpeg_job(job, ["-i", str(src), "-vf",
                                       f"vidstabtransform=smoothing={sm}:input='{trf}',unsharp=5:5:0.8",
                                       "-c:a", "copy"], _duration(src), dst)
        _finish_ffmpeg(job, code, tail, dst)
        try:
            trf.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    return _spawn("stabilize", str(dst), worker)


# =========================================================================== DELIVER
@mcp.tool
@_guard
def convert(path: str, format: str = "mp4", video_codec: str = "", audio_codec: str = "",
            crf: int = 20, accel: str = "auto", out: str = "") -> dict:
    """Transcode to another container/codec (default h264+aac mp4). accel auto/hardware/software. Any length; background job."""
    src = _safe_in(path, MEDIA_EXT)
    if isinstance(src, dict):
        return src
    fmt = (format or "mp4").lower()
    if fmt not in FORMATS:
        return not_found("format", fmt, available=sorted(FORMATS))
    vkey = (video_codec or ("vp9" if fmt == "webm" else "h264")).lower()
    akey = (audio_codec or ("opus" if fmt == "webm" else "aac")).lower()
    if vkey not in VCODECS:
        return not_found("video_codec", vkey, available=sorted(VCODECS))
    if akey not in ACODECS:
        return not_found("audio_codec", akey, available=sorted(ACODECS))
    if VCODECS[vkey] != "copy" and not _has_encoder(VCODECS[vkey]):
        return err(f"encoder '{VCODECS[vkey]}' not available in this ffmpeg build",
                   hint="see list_encoders()")
    dur = _duration(src)
    dst = _out_name(src.stem, f".{fmt}", out)
    args = ["-i", str(src)]
    if vkey == "h264":  # default video path → auto-pick libx264 / hardware videotoolbox by length
        args += _encode_args(crf, "medium", accel, dur)
    else:
        args += ["-c:v", VCODECS[vkey]]
        if VCODECS[vkey] not in ("copy",):
            args += ["-crf", str(int(crf))]
            if VCODECS[vkey] == "libvpx-vp9":
                args += ["-b:v", "0", "-row-mt", "1"]  # VP9 constant-quality mode + multithread
    args += ["-c:a", ACODECS[akey]]
    if fmt == "mp4":
        args += ["-movflags", "+faststart"]

    def worker(job):
        code, tail = _ffmpeg_job(job, args, dur, dst)
        _finish_ffmpeg(job, code, tail, dst)

    return _spawn("convert", str(dst), worker)


@mcp.tool
@_guard
def compress(path: str, target_mb: float = 0.0, crf: int = 24, preset: str = "medium",
             out: str = "") -> dict:
    """Shrink a video by CRF (quality) or to hit a target size in MB (2-pass bitrate). Background job."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if preset not in PRESETS:
        return not_found("preset", preset, available=sorted(PRESETS))
    dst = _out_name(f"{src.stem}_small", ".mp4", out)
    dur = _duration(src)
    if target_mb and float(target_mb) > 0:
        if not dur:
            return err("could not read duration for target-size encode")
        total_kbit = float(target_mb) * 8192.0
        a_kbit = 128.0
        v_kbit = max(100.0, total_kbit / dur - a_kbit)

        def worker(job):
            logf = TMP / f"pass_{secrets.token_hex(4)}"
            _set(job["id"], percent=5.0)
            p1 = _ffmpeg(["-i", str(src), "-c:v", "libx264", "-b:v", f"{int(v_kbit)}k", "-pass", "1",
                          "-passlogfile", str(logf), "-preset", preset, "-an", "-f", "mp4", "/dev/null"],
                         timeout=None)  # background job — unbounded (any length)
            if not p1["ok"]:
                _set(job["id"], status="error", error=p1["err"], ended=_now())
                return
            _set(job["id"], percent=50.0)
            code, tail = _ffmpeg_job(job, ["-i", str(src), "-c:v", "libx264", "-b:v", f"{int(v_kbit)}k",
                                           "-pass", "2", "-passlogfile", str(logf), "-preset", preset,
                                           "-c:a", "aac", "-b:a", f"{int(a_kbit)}k",
                                           "-movflags", "+faststart"], dur, dst)
            _finish_ffmpeg(job, code, tail, dst, target_mb=float(target_mb))
            for ext in (".log", ".log.mbtree", "-0.log", "-0.log.mbtree"):
                try:
                    Path(str(logf) + ext).unlink(missing_ok=True)
                except Exception:  # noqa: BLE001
                    pass

        return _spawn("compress", str(dst), worker)

    args = ["-i", str(src), "-c:v", "libx264", "-crf", str(int(crf)), "-preset", preset,
            "-c:a", "aac", "-b:a", "128k", "-movflags", "+faststart"]

    def worker(job):
        code, tail = _ffmpeg_job(job, args, dur, dst)
        _finish_ffmpeg(job, code, tail, dst, crf=int(crf))

    return _spawn("compress", str(dst), worker)


@mcp.tool
@_guard
def to_gif(path: str, start: float = 0.0, duration: float = 0.0, fps: int = 15, width: int = 480,
           out: str = "") -> dict:
    """Make a HIGH-quality GIF from a clip (palettegen/paletteuse), trimmed + scaled."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    fps = max(2, min(50, int(fps)))
    w = max(64, int(width))
    palette = TMP / f"palette_{secrets.token_hex(4)}.png"
    seek = ["-ss", str(max(0.0, float(start)))]
    dur = ["-t", str(float(duration))] if duration and float(duration) > 0 else []
    dst = _out_name(src.stem, ".gif", out)
    total = float(duration) if duration and float(duration) > 0 else _duration(src)

    def worker(job):
        _set(job["id"], percent=10.0)
        p1 = _ffmpeg([*seek, "-i", str(src), *dur, "-vf",
                      f"fps={fps},scale={w}:-1:flags=lanczos,palettegen=stats_mode=diff", str(palette)],
                     timeout=None)  # background job — unbounded (any length)
        if not p1["ok"]:
            _set(job["id"], status="error", error=p1["err"] or "palette generation failed", ended=_now())
            return
        _set(job["id"], percent=50.0)
        code, tail = _ffmpeg_job(job, [*seek, "-i", str(src), "-i", str(palette), *dur, "-lavfi",
                                       f"fps={fps},scale={w}:-1:flags=lanczos[x];[x][1:v]paletteuse=dither=bayer"],
                                 total, dst)
        _finish_ffmpeg(job, code, tail, dst, fps=fps, width=w)
        try:
            palette.unlink(missing_ok=True)
        except Exception:  # noqa: BLE001
            pass

    return _spawn("to_gif", str(dst), worker)


@mcp.tool
@_guard
def extract_frames(path: str, fps: float = 1.0, start: float = 0.0, duration: float = 0.0,
                   width: int = 0, format: str = "png") -> dict:
    """Export a numbered image sequence at `fps` into a fresh folder under the output dir."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    ext = (format or "png").lower().lstrip(".")
    if ext not in ("png", "jpg", "jpeg"):
        return err("format must be png or jpg")
    folder = OUT / f"{src.stem}_frames_{datetime.now():%Y%m%d-%H%M%S}_{secrets.token_hex(2)}"
    folder.mkdir(parents=True, exist_ok=True)
    vf = f"fps={max(0.01, float(fps))}"
    if int(width) > 0:
        vf += f",scale={int(width)}:-2"
    seek = ["-ss", str(max(0.0, float(start)))]
    dur = ["-t", str(float(duration))] if duration and float(duration) > 0 else []
    total = float(duration) if duration and float(duration) > 0 else _duration(src)
    pattern = folder / f"frame_%06d.{ext}"

    def worker(job):
        code, tail = _ffmpeg_job(job, [*seek, "-i", str(src), *dur, "-vf", vf], total, pattern)
        cnt = len(list(folder.glob(f"frame_*.{ext}")))
        if code == 0 and cnt > 0:
            _set(job["id"], status="done", percent=100.0, out=str(folder), dir=str(folder),
                 count=cnt, fps=float(fps), ended=_now())
        else:
            _set(job["id"], status="error", error=f"ffmpeg exited {code}: {tail[-400:]}", ended=_now())

    return _spawn("extract_frames", str(folder), worker)


@mcp.tool
@_guard
def slideshow(images: list[str], seconds_each: float = 3.0, transition: str = "fade",
              ken_burns: bool = False, audio: str = "", aspect: str = "16:9", out: str = "") -> dict:
    """Turn a list of images into a video with crossfades + optional Ken Burns zoom + music. Background job."""
    if not isinstance(images, list) or len(images) < 1:
        return err("pass a list of image paths")
    imgs = []
    for p in images:
        s = _safe_in(p, IMAGE_EXT)
        if isinstance(s, dict):
            return err(f"bad image '{p}': {s['error']}")
        imgs.append(s)
    if aspect not in ASPECTS:
        return not_found("aspect", aspect, available=sorted(ASPECTS))
    w, h = ASPECTS[aspect]
    secs = max(0.5, float(seconds_each))
    trans = transition if transition in XFADE_KINDS else "fade"
    xf_dur = min(1.0, secs / 2)
    use_xfade = len(imgs) > 1 and _has_filter("xfade")
    mus = None
    if audio:
        m = _safe_in(audio, AUDIO_EXT)
        if isinstance(m, dict):
            return err(f"bad audio: {m['error']}")
        mus = m

    inputs, fc, vlabels = [], [], []
    for i, im in enumerate(imgs):
        inputs += ["-loop", "1", "-t", str(secs), "-i", str(im)]
        kb = (f",zoompan=z='min(zoom+0.0015,1.5)':d={int(secs * 25)}:s={w}x{h}:fps=25"
              if ken_burns and _has_filter("zoompan") else "")
        fc.append(f"[{i}:v]scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},"
                  f"setsar=1,fps=25{kb}[v{i}]")
        vlabels.append(f"v{i}")
    if use_xfade:
        cur, off = vlabels[0], 0.0
        for k in range(1, len(vlabels)):
            off += secs - xf_dur
            nl = f"x{k}"
            fc.append(f"[{cur}][{vlabels[k]}]xfade=transition={trans}:duration={xf_dur}:"
                      f"offset={round(off, 3)}[{nl}]")
            cur = nl
        vout = cur
    elif len(vlabels) > 1:
        fc.append("".join(f"[{v}]" for v in vlabels) + f"concat=n={len(vlabels)}:v=1:a=0[vc]")
        vout = "vc"
    else:
        vout = vlabels[0]
    dst = _out_name("slideshow", ".mp4", out)
    args = [*inputs]
    if mus:
        args += ["-stream_loop", "-1", "-i", str(mus)]
    args += ["-filter_complex", ";".join(fc), "-map", f"[{vout}]"]
    if mus:
        args += ["-map", f"{len(imgs)}:a", "-c:a", "aac", "-b:a", "192k", "-shortest"]
    args += ["-c:v", "libx264", "-crf", "20", "-preset", "medium", "-pix_fmt", "yuv420p",
             "-movflags", "+faststart"]
    total = len(imgs) * secs

    def worker(job):
        code, tail = _ffmpeg_job(job, args, total, dst)
        _finish_ffmpeg(job, code, tail, dst, images=len(imgs))

    return _spawn("slideshow", str(dst), worker)


def _reframe_filter(w: int, h: int, mode: str) -> str:
    if mode == "blur-pad":
        return (f"split[main][bg];[bg]scale={w}:{h}:force_original_aspect_ratio=increase,"
                f"crop={w}:{h},gblur=sigma=20[blurred];"
                f"[main]scale={w}:{h}:force_original_aspect_ratio=decrease[fg];"
                f"[blurred][fg]overlay=(W-w)/2:(H-h)/2,setsar=1")
    # center crop
    return f"scale={w}:{h}:force_original_aspect_ratio=increase,crop={w}:{h},setsar=1"


@mcp.tool
@_guard
def auto_reframe(path: str, aspect: str = "9:16", mode: str = "center", accel: str = "auto",
                 out: str = "") -> dict:
    """Reframe a video to a vertical/square aspect (9:16, 1:1…). mode: center (crop) or blur-pad. Any length; background job."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if aspect not in ASPECTS:
        return not_found("aspect", aspect, available=sorted(ASPECTS))
    w, h = ASPECTS[aspect]
    mode = (mode or "center").lower()
    vf = _reframe_filter(w, h, mode)
    dur = _duration(src)
    dst = _out_name(f"{src.stem}_{aspect.replace(':', 'x')}", ".mp4", out)
    args = ["-i", str(src), "-vf", vf, *_encode_args(20, "medium", accel, dur),
            "-c:a", "copy", "-movflags", "+faststart"]

    def worker(job):
        code, tail = _ffmpeg_job(job, args, dur, dst)
        _finish_ffmpeg(job, code, tail, dst, aspect=aspect, mode=mode)

    return _spawn("auto_reframe", str(dst), worker)


# =========================================================================== AI (local, free)
def _load_whisper(model: str):
    """Lazy-load faster-whisper. Returns (model_obj, None) or (None, err_dict)."""
    try:
        from faster_whisper import WhisperModel
    except Exception:
        return None, err("transcription needs faster-whisper",
                         hint="uv pip install faster-whisper (free, local; model downloads on first use)")
    name = model if model in ("tiny", "base", "small", "medium", "large-v3") else "base"
    backend = get_env("WHISPER_BACKEND", "faster-whisper")
    try:
        m = WhisperModel(name, device="cpu", compute_type="int8")
    except Exception as e:  # noqa: BLE001
        return None, err(f"could not load whisper model '{name}': {e}", backend=backend)
    return m, None


def _fmt_ts(t: float, vtt: bool = False) -> str:
    h = int(t // 3600)
    m = int((t % 3600) // 60)
    s = int(t % 60)
    ms = int(round((t - int(t)) * 1000))
    sep = "." if vtt else ","
    return f"{h:02d}:{m:02d}:{s:02d}{sep}{ms:03d}"


def _write_srt(segments: list[dict], path: Path, vtt: bool = False) -> None:
    lines = ["WEBVTT", ""] if vtt else []
    for i, seg in enumerate(segments, 1):
        if not vtt:
            lines.append(str(i))
        lines.append(f"{_fmt_ts(seg['start'], vtt)} --> {_fmt_ts(seg['end'], vtt)}")
        lines.append(seg["text"].strip())
        lines.append("")
    path.write_text("\n".join(lines))


def _transcribe_to(src: Path, model: str, language: str, job: dict | None = None):
    """Run whisper on a (validated) file. Returns ({segments, text, ...}, None) or (None, err_dict)."""
    m, e = _load_whisper(model)
    if e:
        return None, e
    if job:
        _set(job["id"], percent=20.0)
    try:
        seg_iter, info = m.transcribe(str(src), language=(language or None), word_timestamps=True)
        segments = []
        for s in seg_iter:
            words = [{"word": w.word, "start": round(w.start, 3), "end": round(w.end, 3)}
                     for w in (s.words or [])]
            segments.append({"start": round(s.start, 3), "end": round(s.end, 3),
                             "text": s.text.strip(), "words": words})
    except Exception as ex:  # noqa: BLE001
        return None, err(f"transcription failed: {ex}")
    text = " ".join(s["text"] for s in segments).strip()
    return {"segments": segments, "text": text,
            "language": getattr(info, "language", language or "")}, None


@mcp.tool
@_guard
def transcribe(path: str, model: str = "base", language: str = "", write: str = "srt") -> dict:
    """Local speech-to-text (faster-whisper): transcript + word/segment timestamps + an .srt/.vtt file. Background job."""
    src = _safe_in(path, MEDIA_EXT)
    if isinstance(src, dict):
        return src
    if not _whisper_ready():
        return err("transcription needs faster-whisper",
                   hint="uv pip install faster-whisper (free, local; model downloads on first use)")
    fmt = (write or "srt").lower()
    sub_path = _out_name(src.stem, ".vtt" if fmt == "vtt" else ".srt")

    def worker(job):
        res, e = _transcribe_to(src, model, language, job)
        if e:
            _set(job["id"], status="error", error=e["error"], hint=e.get("hint"), ended=_now())
            return
        _set(job["id"], percent=90.0)
        _write_srt(res["segments"], sub_path, vtt=(fmt == "vtt"))
        _set(job["id"], status="done", percent=100.0, out=str(sub_path), ended=_now(),
             text=res["text"][:4000], language=res["language"], segments=len(res["segments"]))

    return _spawn("transcribe", str(sub_path), worker)


def _regroup_words(segments: list[dict], max_words: int) -> list[dict]:
    """Regroup whisper words into short caption chunks (karaoke-friendly), falling back to segments."""
    chunks = []
    for seg in segments:
        words = seg.get("words") or []
        if not words:
            chunks.append({"start": seg["start"], "end": seg["end"], "text": seg["text"]})
            continue
        for i in range(0, len(words), max_words):
            grp = words[i:i + max_words]
            chunks.append({"start": grp[0]["start"], "end": grp[-1]["end"],
                           "text": "".join(w["word"] for w in grp).strip()})
    return chunks


@mcp.tool
@_guard
def auto_captions(path: str, model: str = "base", style: str = "tiktok", max_words: int = 4,
                  language: str = "", out: str = "") -> dict:
    """Transcribe locally, regroup into short styled chunks, and burn captions into the video. Background job."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if not _whisper_ready():
        return err("auto_captions needs faster-whisper", hint="uv pip install faster-whisper")
    dst = _out_name(f"{src.stem}_captioned", ".mp4", out)
    force = CAPTION_STYLES.get(style, CAPTION_STYLES["tiktok"])
    mw = max(1, int(max_words))
    native = _has_filter("subtitles")
    dur, vw, vh, _fps, _ha = _probe_av(src)

    def worker(job):
        res, e = _transcribe_to(src, model, language, job)
        if e:
            _set(job["id"], status="error", error=e["error"], hint=e.get("hint"), ended=_now())
            return
        chunks = _regroup_words(res["segments"], mw)
        _set(job["id"], percent=60.0)
        cleanup: tuple = ()
        if native:
            srt = TMP / f"caps_{secrets.token_hex(4)}.srt"
            _write_srt(chunks, srt)
            args = ["-i", str(src), "-vf", f"subtitles='{srt}':force_style='{force}'", "-c:a", "copy"]
            cleanup = (srt,)
        else:  # Pillow->overlay captions (no libass needed)
            specs = [{"text": ch.get("text", ""), "start": ch.get("start", 0), "end": ch.get("end", 0),
                      "font_size": 44, "color": "white", "box": True, "box_color": "black@0.55",
                      "position": "bottom"} for ch in chunks]
            built = _build_overlay_args(src, specs, vw, vh)
            if isinstance(built, dict):
                _set(job["id"], status="error", error=built["error"], ended=_now())
                return
            args, cleanup = built
        code, tail = _ffmpeg_job(job, args, dur, dst)
        _finish_ffmpeg(job, code, tail, dst, captions=len(chunks), style=style)
        for p in cleanup:
            Path(p).unlink(missing_ok=True)

    return _spawn("auto_captions", str(dst), worker)


@mcp.tool
@_guard
def make_short(path: str, aspect: str = "9:16", start: float = 0.0, end: float = 0.0,
               captions: bool = True, mode: str = "center", model: str = "base",
               caption_style: str = "tiktok", accel: str = "auto", out: str = "") -> dict:
    """One-shot social clip: (optional trim) → reframe to 9:16/1:1 → optional auto-captions. Any length; background job."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    if aspect not in ASPECTS:
        return not_found("aspect", aspect, available=sorted(ASPECTS))
    w, h = ASPECTS[aspect]
    want_caps = bool(captions) and _whisper_ready()  # captions burn via libass OR Pillow->overlay
    dst = _out_name(f"{src.stem}_short", ".mp4", out)
    trim_args = []
    if start and float(start) > 0:
        trim_args += ["-ss", str(float(start))]
    seg_dur = None
    if end and float(end) > float(start):
        seg_dur = float(end) - float(start)
        trim_args += ["-t", str(seg_dur)]
    vf = _reframe_filter(w, h, (mode or "center").lower())
    enc = _encode_args(20, "medium", accel, seg_dur if seg_dur else _duration(src))

    def worker(job):
        warnings = []
        if bool(captions) and not want_caps:
            warnings.append("captions skipped: faster-whisper not installed (uv pip install faster-whisper)")
        reframed = TMP / f"short_{secrets.token_hex(4)}.mp4"
        _set(job["id"], percent=10.0)
        r = _ffmpeg([*trim_args, "-i", str(src), "-vf", vf, *enc,
                     "-c:a", "aac", "-movflags", "+faststart", str(reframed)],
                    timeout=None)  # background job — unbounded (any length)
        if not r["ok"]:
            _set(job["id"], status="error", error=r["err"], ended=_now())
            return
        if not want_caps:
            try:
                reframed.replace(dst)
            except Exception:  # noqa: BLE001
                dst.write_bytes(reframed.read_bytes())
            _set(job["id"], status="done", percent=100.0, out=str(dst), ended=_now(),
                 aspect=aspect, warnings=warnings or None)
            return
        _set(job["id"], percent=45.0)
        res, e = _transcribe_to(reframed, model, "", job)
        if e:
            try:
                reframed.replace(dst)
            except Exception:  # noqa: BLE001
                dst.write_bytes(reframed.read_bytes())
            _set(job["id"], status="done", percent=100.0, out=str(dst), ended=_now(),
                 aspect=aspect, warnings=[f"captions skipped: {e['error']}"])
            return
        chunks = _regroup_words(res["segments"], 4)
        _set(job["id"], percent=70.0)
        extra: tuple = ()
        if _has_filter("subtitles"):
            srt = TMP / f"short_caps_{secrets.token_hex(4)}.srt"
            _write_srt(chunks, srt)
            force = CAPTION_STYLES.get(caption_style, CAPTION_STYLES["tiktok"])
            cap_args = ["-i", str(reframed), "-vf", f"subtitles='{srt}':force_style='{force}'", "-c:a", "copy"]
            extra = (srt,)
        else:  # Pillow->overlay captions on the reframed (w x h) clip
            specs = [{"text": ch.get("text", ""), "start": ch.get("start", 0), "end": ch.get("end", 0),
                      "font_size": 44, "color": "white", "box": True, "box_color": "black@0.55",
                      "position": "bottom"} for ch in chunks]
            built = _build_overlay_args(reframed, specs, w, h)
            if isinstance(built, dict):  # rendering failed → ship the reframed clip uncaptioned
                try:
                    reframed.replace(dst)
                except Exception:  # noqa: BLE001
                    dst.write_bytes(reframed.read_bytes())
                _set(job["id"], status="done", percent=100.0, out=str(dst), ended=_now(),
                     aspect=aspect, warnings=(warnings or []) + [f"captions skipped: {built['error']}"])
                return
            cap_args, extra = built
        code, tail = _ffmpeg_job(job, cap_args, _duration(reframed), dst)
        _finish_ffmpeg(job, code, tail, dst, aspect=aspect, captions=len(chunks), warnings=warnings or None)
        for f in (reframed, *extra):
            try:
                f.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    return _spawn("make_short", str(dst), worker)


@mcp.tool
@_guard
def smart_cut(path: str, noise_db: float = -30.0, min_silence: float = 0.6,
              scene_threshold: float = 0.4) -> dict:
    """Detect-and-PROPOSE (does not cut): combines silence + scene detection into a structured editing plan you curate. Any length (data inline for short, job_id for long)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src

    def worker(job):
        sil = _detect_silence_raw(src, noise_db, min_silence)
        if not sil.get("ok"):
            _set(job["id"], status="error", error=sil.get("error", "silence detection failed"), ended=_now())
            return
        scn = _detect_scenes_raw(src, scene_threshold)
        keep = sil.get("speech") or []
        drop = sil.get("silences") or []
        kept = round(sum(s["duration"] for s in keep), 2)
        _set(job["id"], status="done", percent=100.0, ended=_now(),
             keep_segments=keep, remove_segments=drop,
             scenes=scn.get("scenes") if scn.get("ok") else [],
             kept_seconds=kept, total_seconds=sil.get("duration"),
             hint="curate keep_segments, then call remove_silence, or trim+concat, or build a project")

    return _data_job("smart_cut", worker)


@mcp.tool
@_guard
def auto_highlights(path: str, target_seconds: float = 60.0, scene_threshold: float = 0.4,
                    noise_db: float = -30.0) -> dict:
    """Propose candidate highlight windows (scenes ranked by speech density) summing near target_seconds. Claude picks. Any length (data inline for short, job_id for long)."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src

    def worker(job):
        scn = _detect_scenes_raw(src, scene_threshold)
        if not scn.get("ok"):
            _set(job["id"], status="error", error=scn.get("error", "scene detection failed"), ended=_now())
            return
        sil = _detect_silence_raw(src, noise_db, 0.5)
        speech = sil.get("speech") or [] if sil.get("ok") else []

        def speech_overlap(a0, a1):
            return round(sum(max(0.0, min(a1, s["end"]) - max(a0, s["start"])) for s in speech), 3)

        cands = []
        for sc in scn.get("scenes", []):
            dur = sc["duration"]
            ov = speech_overlap(sc["start"], sc["end"])
            score = round((ov / dur) if dur else 0.0, 3)
            cands.append({"start": sc["start"], "end": sc["end"], "duration": dur,
                          "speech_seconds": ov, "score": score,
                          "reason": "high speech density" if score > 0.6 else "scene"})
        cands.sort(key=lambda c: c["score"], reverse=True)
        picked, acc = [], 0.0
        for c in cands:
            if acc >= float(target_seconds):
                break
            picked.append(c)
            acc += c["duration"]
        picked.sort(key=lambda c: c["start"])
        _set(job["id"], status="done", percent=100.0, ended=_now(),
             candidates=cands[:30], suggested=picked, suggested_seconds=round(acc, 2),
             target_seconds=float(target_seconds),
             hint="pick windows, then trim each and concat (or build a project)")

    return _data_job("auto_highlights", worker)


# =========================================================================== DO-ANYTHING (raw ffmpeg)
@mcp.tool
@_guard
def apply_filter(path: str, filtergraph: str, audio_filter: str = "", complex: bool = False,
                 out: str = "") -> dict:
    """Escape hatch — apply ANY ffmpeg filtergraph Claude builds. `filtergraph` → -vf (or -filter_complex
    if complex=True); `audio_filter` → -af. Lets videoforge do literally anything ffmpeg's filters can
    (hue, eq, curves, tblend, perspective, rotate, fps, deshake, …). Any length; background job."""
    src = _safe_in(path, VIDEO_EXT | AUDIO_EXT)
    if isinstance(src, dict):
        return src
    fg = (filtergraph or "").strip()
    af = (audio_filter or "").strip()
    if not fg and not af:
        return err("provide a filtergraph (and/or audio_filter), e.g. filtergraph='hue=s=0' (greyscale)")
    if len(fg) + len(af) > MAX_FILTERGRAPH_CHARS:
        return err("filtergraph too large")
    dst = _out_name(f"{src.stem}_filtered", ".mp4", out)
    args = ["-i", str(src)]
    if fg:
        args += (["-filter_complex", fg] if complex else ["-vf", fg])
    if af:
        args += ["-af", af]
    return _run_or_job("apply_filter", args, dst, total_sec=_duration(src))


@mcp.tool
@_guard
def run_filtergraph(inputs: list[str], filter_complex: str, maps: list[str] = [], out: str = "") -> dict:
    """Power tool — run an arbitrary -filter_complex over MANY inputs (overlays, blends, side-by-side,
    custom graphs Claude designs), with explicit output -map labels. Anything ffmpeg's filter system can
    express. Any length; background job."""
    if not isinstance(inputs, list) or not inputs:
        return err("pass a list of at least one input path")
    srcs = []
    for p in inputs:
        s = _safe_in(p, VIDEO_EXT | AUDIO_EXT | IMAGE_EXT)
        if isinstance(s, dict):
            return err(f"bad input '{p}': {s['error']}")
        srcs.append(s)
    fg = (filter_complex or "").strip()
    if not fg:
        return err("provide a filter_complex string")
    if len(fg) > MAX_FILTERGRAPH_CHARS:
        return err("filter_complex too large")
    args = []
    for s in srcs:
        args += ["-i", str(s)]
    args += ["-filter_complex", fg]
    for m in (maps or []):
        args += ["-map", str(m)]
    dst = _out_name("filtergraph", ".mp4", out)
    return _run_or_job("run_filtergraph", args, dst, total_sec=_duration(srcs[0]), inputs=len(srcs))


@mcp.tool
@_guard
def chromakey(path: str, key_color: str = "0x00FF00", similarity: float = 0.3, blend: float = 0.1,
              background: str = "", out: str = "") -> dict:
    """Green-screen: remove `key_color` from the video. With `background` (image/video), composite the
    subject over it; without one, keyed areas go black. Any length; background job."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    sim = max(0.01, min(1.0, float(similarity)))
    bl = max(0.0, min(1.0, float(blend)))
    ck = f"chromakey={key_color}:{sim}:{bl}"
    dst = _out_name(f"{src.stem}_keyed", ".mp4", out)
    if background:
        bg = _safe_in(background, VIDEO_EXT | IMAGE_EXT)
        if isinstance(bg, dict):
            return err(f"bad background: {bg['error']}")
        loop_bg = ["-loop", "1"] if bg.suffix.lower() in IMAGE_EXT else []
        fc = f"[1:v][0:v]scale2ref[bg][v];[v]{ck}[k];[bg][k]overlay[vout]"
        args = ["-i", str(src), *loop_bg, "-i", str(bg), "-filter_complex", fc,
                "-map", "[vout]", "-map", "0:a?", "-c:a", "copy", "-shortest"]
        return _run_or_job("chromakey", args, dst, total_sec=_duration(src), background=str(bg))
    return _run_or_job("chromakey", ["-i", str(src), "-vf", ck, "-c:a", "copy"], dst, total_sec=_duration(src))


@mcp.tool
@_guard
def blur(path: str, strength: float = 10.0, out: str = "") -> dict:
    """Blur the whole frame (gaussian; strength ≈ sigma). Any length; background job."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    s = max(0.1, min(100.0, float(strength)))
    dst = _out_name(f"{src.stem}_blur", ".mp4", out)
    return _run_or_job("blur", ["-i", str(src), "-vf", f"gblur=sigma={s}", "-c:a", "copy"],
                       dst, total_sec=_duration(src))


@mcp.tool
@_guard
def mux_subtitles(path: str, subtitles: str, language: str = "eng", out: str = "") -> dict:
    """Embed subtitles as a SOFT, selectable track (toggle on/off in players) via mov_text — works WITHOUT
    libass, unlike burn_subtitles. Stream-copied (fast). Any length."""
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    sub = _safe_in(subtitles, SUB_EXT)
    if isinstance(sub, dict):
        return err(f"bad subtitle file: {sub['error']}")
    dst = _out_name(f"{src.stem}_softsubs", ".mp4", out)
    args = ["-i", str(src), "-i", str(sub), "-map", "0", "-map", "1", "-c", "copy",
            "-c:s", "mov_text", "-metadata:s:s:0", f"language={language}"]
    return _run_or_job("mux_subtitles", args, dst, total_sec=_duration(src), soft=True)


# =========================================================================== TIMELINE / PROJECT
def _proj_path(pid: str) -> Path:
    return PROJ / f"{pid}.json"


def _load_proj(pid: str) -> dict | None:
    if not pid or not str(pid).strip():
        return None
    p = _proj_path(str(pid))
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text())
    except Exception:  # noqa: BLE001
        return None


def _save_proj(proj: dict) -> None:
    proj["updated"] = _now()
    tmp = PROJ / f".{proj['id']}.tmp"
    tmp.write_text(json.dumps(proj, indent=2))
    tmp.replace(_proj_path(proj["id"]))


def _proj_ids() -> list[str]:
    return [p.stem for p in PROJ.glob("*.json")][:10]


def _need_proj(pid: str):
    proj = _load_proj(pid)
    if not proj:
        return None, not_found("project", pid, available=_proj_ids(), hint="create_project() first")
    return proj, None


@mcp.tool
@_guard
def create_project(name: str = "", aspect: str = "16:9", fps: int = 30) -> dict:
    """Start a persistent timeline project; returns a project_id to add clips/overlays/text/audio to, then render."""
    asp = aspect if aspect in ASPECTS else "16:9"
    w, h = ASPECTS[asp]
    f = int(fps) if int(fps) > 0 else 30
    pid = f"proj_{datetime.now():%Y%m%d-%H%M%S}_{secrets.token_hex(2)}"
    proj = {"id": pid, "name": (name or pid), "created": _now(), "updated": _now(),
            "output": {"w": w, "h": h, "fps": f, "format": "mp4", "vcodec": "h264",
                       "acodec": "aac", "crf": 20, "preset": "medium", "filename": ""},
            "clips": [], "overlays": [], "texts": [], "audio_tracks": [], "captions": None,
            "aspect": asp}
    _save_proj(proj)
    return ok(project_id=pid, name=proj["name"], aspect=asp, resolution=f"{w}x{h}", fps=f)


@mcp.tool
@_guard
def project_info(project_id: str) -> dict:
    """Show a project's full timeline: clips, overlays, text, audio tracks, output settings, est. duration."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    est = round(sum(max(0.0, c["out"] - c["in"]) for c in proj["clips"]), 2)
    return ok(project_id=proj["id"], name=proj["name"], output=proj["output"], aspect=proj.get("aspect"),
              clips=proj["clips"], overlays=proj["overlays"], texts=proj["texts"],
              audio_tracks=proj["audio_tracks"], captions=proj.get("captions"),
              est_duration=est)


@mcp.tool
@_guard
def add_clip(project_id: str, path: str, start: float = 0.0, end: float = 0.0) -> dict:
    """Append a (optionally pre-trimmed) source clip to the project's video track."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    src = _safe_in(path, VIDEO_EXT)
    if isinstance(src, dict):
        return src
    dur = _duration(src) or 0.0
    s = max(0.0, float(start))
    en = float(end) if (end and float(end) > s) else dur
    if en <= s:
        return err("clip has zero length — set a valid end, or the source has unknown duration")
    clip = {"id": f"c{len(proj['clips'])}", "src": str(src), "in": round(s, 3),
            "out": round(en, 3), "transition": None}
    proj["clips"].append(clip)
    _save_proj(proj)
    return ok(project_id=proj["id"], clip=clip, clips=len(proj["clips"]))


@mcp.tool
@_guard
def add_overlay(project_id: str, image: str, position: str = "top-right", scale: float = 0.18,
                start: float = 0.0, end: float = 0.0, opacity: float = 1.0) -> dict:
    """Add an image overlay (logo/badge) to the project, positioned/scaled/timed."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    img = _safe_in(image, IMAGE_EXT)
    if isinstance(img, dict):
        return img
    ov = {"id": f"o{len(proj['overlays'])}", "src": str(img), "position": position,
          "scale": max(0.01, min(1.0, float(scale))), "start": float(start),
          "end": float(end), "opacity": max(0.0, min(1.0, float(opacity)))}
    proj["overlays"].append(ov)
    _save_proj(proj)
    return ok(project_id=proj["id"], overlay=ov, overlays=len(proj["overlays"]))


@mcp.tool
@_guard
def add_text_track(project_id: str, text: str, start: float = 0.0, end: float = 0.0,
                   font_size: int = 48, color: str = "white", position: str = "bottom",
                   box: bool = True, box_color: str = "black@0.5") -> dict:
    """Add a timed text layer to the project (burned at render; needs the drawtext filter)."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    if not (text or "").strip():
        return err("text must be non-empty")
    t = {"id": f"t{len(proj['texts'])}", "text": text, "start": float(start), "end": float(end),
         "font_size": int(font_size), "color": color, "position": position,
         "box": bool(box), "box_color": box_color}
    proj["texts"].append(t)
    _save_proj(proj)
    return ok(project_id=proj["id"], text=t, texts=len(proj["texts"]))


@mcp.tool
@_guard
def add_audio_track(project_id: str, path: str, gain_db: float = -12.0, duck: bool = True,
                    loop: bool = True) -> dict:
    """Add a music/voiceover track to the project (mixed at render; duck=True ducks it under clip audio)."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    aud = _safe_in(path, AUDIO_EXT | VIDEO_EXT)
    if isinstance(aud, dict):
        return aud
    tr = {"id": f"a{len(proj['audio_tracks'])}", "src": str(aud), "gain_db": float(gain_db),
          "duck": bool(duck), "loop": bool(loop)}
    proj["audio_tracks"].append(tr)
    _save_proj(proj)
    return ok(project_id=proj["id"], audio_track=tr, audio_tracks=len(proj["audio_tracks"]))


@mcp.tool
@_guard
def set_captions(project_id: str, subtitles: str, style: str = "tiktok") -> dict:
    """Attach a subtitle file to burn into the project at render (needs the subtitles/libass filter)."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    sub = _safe_in(subtitles, SUB_EXT)
    if isinstance(sub, dict):
        return sub
    proj["captions"] = {"src": str(sub), "style": style if style in CAPTION_STYLES else "tiktok"}
    _save_proj(proj)
    return ok(project_id=proj["id"], captions=proj["captions"])


@mcp.tool
@_guard
def add_transition(project_id: str, kind: str = "fade", duration: float = 0.5,
                   between: list[int] = []) -> dict:
    """Set a crossfade transition between two clips (between=[i, i+1]); empty `between` applies it to ALL joins."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    if kind not in XFADE_KINDS:
        return not_found("transition", kind, available=sorted(XFADE_KINDS))
    d = max(0.1, float(duration))
    if not proj["clips"] or len(proj["clips"]) < 2:
        return err("add at least 2 clips before a transition")
    spec = {"type": kind, "duration": d}
    if between and len(between) >= 2:
        later = int(between[1])
        if later < 1 or later >= len(proj["clips"]):
            return err(f"between target out of range (clips: {len(proj['clips'])})")
        proj["clips"][later]["transition"] = spec
        applied = [later]
    else:
        for c in proj["clips"][1:]:
            c["transition"] = spec
        applied = list(range(1, len(proj["clips"])))
    _save_proj(proj)
    return ok(project_id=proj["id"], transition=spec, applied_to_clip_indices=applied)


@mcp.tool
@_guard
def set_output(project_id: str, format: str = "mp4", crf: int = 20, video_codec: str = "",
               audio_codec: str = "", preset: str = "medium", filename: str = "") -> dict:
    """Configure the project's render output (container, codecs, quality, filename)."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    o = proj["output"]
    if format:
        if format not in FORMATS:
            return not_found("format", format, available=sorted(FORMATS))
        o["format"] = format
    if video_codec:
        if video_codec not in VCODECS:
            return not_found("video_codec", video_codec, available=sorted(VCODECS))
        o["vcodec"] = video_codec
    if audio_codec:
        if audio_codec not in ACODECS:
            return not_found("audio_codec", audio_codec, available=sorted(ACODECS))
        o["acodec"] = audio_codec
    if preset:
        if preset not in PRESETS:
            return not_found("preset", preset, available=sorted(PRESETS))
        o["preset"] = preset
    o["crf"] = int(crf)
    if filename:
        o["filename"] = Path(str(filename)).name
    _save_proj(proj)
    return ok(project_id=proj["id"], output=o)


def _build_render(proj: dict) -> tuple[list[str], list[str], str, str, float]:
    """Compile a project into (inputs, filter_complex_lines, vmap, amap, total_seconds). Raises ValueError on bad state."""
    o = proj["output"]
    W, H, FPS = o["w"], o["h"], o["fps"]
    clips = proj["clips"]
    if not clips:
        raise ValueError("project has no clips — add_clip() first")
    inputs, fc, vlabels, alabels, durs = [], [], [], [], []
    for i, c in enumerate(clips):
        d = round(c["out"] - c["in"], 3)
        durs.append(d)
        inputs += ["-ss", str(c["in"]), "-t", str(d), "-i", c["src"]]
        _, _, _, _, has_a = _probe_av(Path(c["src"]))
        lines, vl, al = _normalize_chain(i, W, H, FPS, has_a, d)
        fc += lines
        vlabels.append(vl)
        alabels.append(al)
    use_xfade = any(c.get("transition") for c in clips) and _has_filter("xfade") and len(clips) > 1
    if use_xfade:
        vcur, off = vlabels[0], 0.0
        for k in range(1, len(vlabels)):
            tr = clips[k].get("transition") or {"type": "fade", "duration": 0.3}
            off += durs[k - 1] - tr["duration"]
            nl = f"xv{k}"
            fc.append(f"[{vcur}][{vlabels[k]}]xfade=transition={tr['type']}:duration={tr['duration']}:"
                      f"offset={round(off, 3)}[{nl}]")
            vcur = nl
        acur = alabels[0]
        for k in range(1, len(alabels)):
            d = (clips[k].get("transition") or {}).get("duration", 0.3)
            nl = f"xa{k}"
            fc.append(f"[{acur}][{alabels[k]}]acrossfade=d={d}[{nl}]")
            acur = nl
        vlast, alast = vcur, acur
        total = sum(durs) - sum((clips[k].get("transition") or {"duration": 0.3})["duration"]
                                for k in range(1, len(clips)))
    else:
        fc.append("".join(f"[{v}]" for v in vlabels) + f"concat=n={len(vlabels)}:v=1:a=0[vcat]")
        fc.append("".join(f"[{a}]" for a in alabels) + f"concat=n={len(alabels)}:v=0:a=1[acat]")
        vlast, alast = "vcat", "acat"
        total = sum(durs)

    # extra inputs for overlays / audio tracks start after the clip inputs
    idx = len(clips)
    # image overlays
    for n, ov in enumerate(proj.get("overlays", [])):
        inputs += ["-i", ov["src"]]
        x, y = _overlay_xy(ov["position"], 24)
        enable = ""
        if ov.get("end") and ov["end"] > ov.get("start", 0):
            enable = f":enable='between(t,{ov['start']},{ov['end']})'"
        olabel = f"ovl{n}"
        fc.append(f"[{idx}:v]scale=iw*{ov['scale']}:-1,format=rgba,"
                  f"colorchannelmixer=aa={ov['opacity']}[{olabel}]")
        nl = f"vov{n}"
        fc.append(f"[{vlast}][{olabel}]overlay={x}:{y}{enable}[{nl}]")
        vlast = nl
        idx += 1
    # text layers: native drawtext if available, else Pillow->PNG overlay (build-independent)
    _texts = proj.get("texts", [])
    if _texts and _has_filter("drawtext"):
        for n, t in enumerate(_texts):
            tf = TMP / f"ptext_{proj['id']}_{n}.txt"
            tf.write_text(t["text"])
            enable = ""
            if t.get("end") and t["end"] > t.get("start", 0):
                enable = f":enable='between(t,{t['start']},{t['end']})'"
            boxpart = f":box=1:boxcolor={t['box_color']}:boxborderw=12" if t.get("box") else ""
            nl = f"vtx{n}"
            fc.append(f"[{vlast}]drawtext=textfile='{tf}':fontsize={t['font_size']}:fontcolor={t['color']}"
                      f":{_text_xy(t['position'])}{boxpart}{enable}[{nl}]")
            vlast = nl
    else:
        for n, t in enumerate(_texts):
            png = _render_text_png(t["text"], W, H, t.get("font_size", 48), t.get("color", "white"),
                                   t.get("box", True), t.get("box_color", "black@0.5"))
            if png is None:
                continue
            inputs += ["-i", str(png)]
            x, y = _overlay_xy(t.get("position", "bottom"))
            nl = f"vtx{n}"
            fc.append(f"[{vlast}][{idx}:v]overlay=x={x}:y={y}{_between(t.get('start', 0), t.get('end', 0))}[{nl}]")
            vlast = nl
            idx += 1
    # captions: native subtitles/libass if available, else per-cue PNG overlay
    if proj.get("captions"):
        cap = proj["captions"]
        if _has_filter("subtitles"):
            nl = "vcap"
            fc.append(f"[{vlast}]subtitles='{cap['src']}':force_style='{CAPTION_STYLES.get(cap['style'], '')}'[{nl}]")
            vlast = nl
        else:
            for ci, (s, e, txt) in enumerate(_parse_srt(Path(cap["src"]))[:300]):
                png = _render_text_png(txt, W, H, 40, "white", True, "black@0.55")
                if png is None:
                    continue
                inputs += ["-i", str(png)]
                x, y = _overlay_xy("bottom")
                nl = f"vcap{ci}"
                fc.append(f"[{vlast}][{idx}:v]overlay=x={x}:y={y}{_between(s, e)}[{nl}]")
                vlast = nl
                idx += 1
    # audio tracks (music) mixed over the clip audio, with optional ducking
    music_labels = []
    for n, tr in enumerate(proj.get("audio_tracks", [])):
        if tr.get("loop"):
            inputs += ["-stream_loop", "-1", "-i", tr["src"]]
        else:
            inputs += ["-i", tr["src"]]
        ml = f"mus{n}"
        fc.append(f"[{idx}:a]volume={tr['gain_db']}dB[{ml}]")
        music_labels.append((ml, tr.get("duck", False)))
        idx += 1
    if music_labels:
        mixed = [alast]
        for ml, duck in music_labels:
            if duck and _has_filter("sidechaincompress"):
                dl = ml + "d"
                fc.append(f"[{ml}][{alast}]sidechaincompress=threshold=0.03:ratio=8:attack=20:release=300[{dl}]")
                mixed.append(dl)
            else:
                mixed.append(ml)
        fc.append("".join(f"[{m}]" for m in mixed) +
                  f"amix=inputs={len(mixed)}:duration=first:dropout_transition=0[aout]")
        alast = "aout"
    return inputs, fc, f"[{vlast}]", f"[{alast}]", max(0.1, total)


@mcp.tool
@_guard
def render(project_id: str, accel: str = "auto") -> dict:
    """Compose the WHOLE timeline into one filter_complex and encode in a SINGLE pass (no generation loss). Any length; background job. accel auto/hardware/software."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    if not proj["clips"]:
        return err("project has no clips — add_clip() first")
    # Text layers + captions burn via drawtext/libass when present, else via the Pillow->overlay
    # engine — so a render NEVER fails just because this ffmpeg build lacks drawtext/subtitles.
    if (proj.get("texts") or proj.get("captions")) and not _has_filter("drawtext") and not _pillow_ok():
        return err("project has text/captions but this ffmpeg lacks drawtext AND Pillow is missing",
                   hint="uv pip install pillow (then reconnect)")
    try:
        inputs, fc, vmap, amap, total = _build_render(proj)
    except ValueError as ve:
        return err(str(ve))
    graph_len = len(";".join(fc))
    if graph_len > MAX_FILTERGRAPH_CHARS:
        return err(f"filter graph too large ({graph_len} chars > {MAX_FILTERGRAPH_CHARS}); "
                   "split into fewer clips/overlays or render in parts",
                   hint="too many timeline elements for a single ffmpeg pass")
    o = proj["output"]
    vcodec = VCODECS.get(o["vcodec"], "libx264")
    acodec = ACODECS.get(o["acodec"], "aac")
    if vcodec != "copy" and not _has_encoder(vcodec):
        return err(f"encoder '{vcodec}' not available", hint="set_output(video_codec=...) — see list_encoders()")
    dst = _out_name(proj["name"].replace(" ", "_"), f".{o['format']}", o.get("filename", ""))
    args = [*inputs, "-filter_complex", ";".join(fc), "-map", vmap, "-map", amap]
    if vcodec == "libx264":  # default video path → auto-pick libx264 / hardware videotoolbox by length
        args += _encode_args(o["crf"], o["preset"], accel, total)
    elif vcodec != "copy":
        args += ["-c:v", vcodec, "-crf", str(o["crf"]), "-preset", o["preset"], "-pix_fmt", "yuv420p"]
    else:
        args += ["-c:v", "copy"]
    args += ["-c:a", acodec, "-b:a", "192k"]
    if o["format"] == "mp4":
        args += ["-movflags", "+faststart"]

    def worker(job):
        code, tail = _ffmpeg_job(job, args, total, dst)
        _finish_ffmpeg(job, code, tail, dst, clips=len(proj["clips"]))
        # clean per-render temp text files
        for f in TMP.glob(f"ptext_{proj['id']}_*.txt"):
            try:
                f.unlink(missing_ok=True)
            except Exception:  # noqa: BLE001
                pass

    return _spawn("render", str(dst), worker)


@mcp.tool
@_guard
def list_projects() -> dict:
    """List saved projects: id, name, clip count, est. duration, last modified."""
    items = []
    for f in sorted(PROJ.glob("*.json")):
        try:
            p = json.loads(f.read_text())
            items.append({"project_id": p["id"], "name": p.get("name"),
                          "clips": len(p.get("clips", [])), "aspect": p.get("aspect"),
                          "updated": p.get("updated")})
        except Exception:  # noqa: BLE001
            continue
    return ok(items=items, total=len(items))


@mcp.tool
@_guard
def delete_project(project_id: str) -> dict:
    """Delete a project's timeline JSON (rendered output files are left in place)."""
    proj, e = _need_proj(project_id)
    if e:
        return e
    _proj_path(proj["id"]).unlink(missing_ok=True)
    return ok(deleted=proj["id"])


# =========================================================================== JOBS
@mcp.tool
@_guard
def job_status(job_id: str) -> dict:
    """Status of a background job (render/convert/transcribe/…): status, percent, output path, error."""
    job = _load_job(job_id)
    if not job:
        return not_found("job", job_id, available=_job_ids(), hint="use list_jobs()")
    return ok(**_reconcile(job))


@mcp.tool
@_guard
def list_jobs(limit: int = 20) -> dict:
    """List recent background jobs (running first, then newest)."""
    jobs = [_reconcile(j) for j in _all_jobs()]
    jobs.sort(key=lambda j: (j.get("status") != "running", j.get("started") or ""), reverse=True)
    return ok(items=jobs[:max(1, int(limit))], total=len(jobs))


@mcp.tool
@_guard
def cancel_job(job_id: str) -> dict:
    """Terminate a running background job (stops the underlying ffmpeg process)."""
    job = _load_job(job_id)
    if not job:
        return not_found("job", job_id, available=_job_ids())
    with _LOCK:
        proc = _PROCS.get(job_id)
    if not proc:
        return err(f"job {job_id} is not running (status={job.get('status')})")
    try:
        proc.terminate()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            proc.kill()
    except Exception as ex:  # noqa: BLE001
        return err(f"could not cancel: {ex}")
    return ok(**_set(job_id, status="cancelled", ended=_now()))


if __name__ == "__main__":
    mcp.run()
