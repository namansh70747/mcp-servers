"""Offline tests for videoforge — the FFmpeg-powered video editor.

No real video, no whisper model, and no dependence on a fully-featured ffmpeg build are needed:
we verify the tool registry, the health/capability report, input-validation + output path-traversal
guards, the JSON timeline/project roundtrip, and the background-jobs registry. Tools that shell out
to ffmpeg/whisper are checked to degrade or queue cleanly — never run to completion, never raise.

If ffmpeg IS present (it usually is on this Mac), a couple of optional checks generate a tiny test
clip with `lavfi` and confirm a real trim + a single-pass project render actually produce output.

Run with the suite venv:
    VIRTUAL_ENV= .venv/bin/python tests/test_video.py
"""
from __future__ import annotations

import asyncio
import importlib.util
import os
import shutil
import subprocess
import tempfile
import time
from pathlib import Path

# Isolate all runtime data in a throwaway dir BEFORE importing the server.
os.environ["MCP_NO_DOTENV"] = "1"
_TMP = tempfile.mkdtemp(prefix="video-test-")
os.environ["MCP_DATA_DIR"] = _TMP

from fastmcp import Client  # noqa: E402

ROOT = Path(__file__).resolve().parents[1]


def _load():
    path = ROOT / "servers" / "videoforge" / "server.py"
    spec = importlib.util.spec_from_file_location("videoforge_t", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod.mcp


async def _tools(mcp):
    async with Client(mcp) as c:
        return {t.name for t in await c.list_tools()}


async def _call(mcp, tool, args):
    async with Client(mcp) as c:
        return (await c.call_tool(tool, args)).data


def _under_data(path: str) -> bool:
    """True if path is inside the temp data dir (i.e. not written outside via traversal)."""
    if not path:
        return False
    return str(Path(path).resolve()).startswith(str(Path(_TMP).resolve()))


def _ffmpeg_present() -> bool:
    return bool(shutil.which("ffmpeg") or Path("/opt/homebrew/bin/ffmpeg").exists())


def _make_clip(dst: Path, size: str = "320x240", dur: int = 2) -> bool:
    exe = shutil.which("ffmpeg") or "/opt/homebrew/bin/ffmpeg"
    try:
        p = subprocess.run([exe, "-hide_banner", "-loglevel", "error", "-y",
                            "-f", "lavfi", "-i", f"testsrc=duration={dur}:size={size}:rate=30",
                            "-f", "lavfi", "-i", f"sine=frequency=440:duration={dur}",
                            "-shortest", "-pix_fmt", "yuv420p", str(dst)], timeout=60)
        return p.returncode == 0 and dst.exists()
    except Exception:  # noqa: BLE001
        return False


def _wait_job(mcp, jid: str, limit: int = 120) -> dict:
    for _ in range(limit):
        s = asyncio.run(_call(mcp, "job_status", {"job_id": jid}))
        if s.get("status") in ("done", "error", "cancelled", "interrupted", "timeout"):
            return s
        time.sleep(0.3)
    return {"status": "timeout"}


# --------------------------------------------------------------------------- registry + health
def test_registry_and_health():
    mcp = _load()
    tools = asyncio.run(_tools(mcp))
    expected = {
        "health", "probe", "media_info", "detect_scenes", "detect_silence", "thumbnail",
        "contact_sheet", "waveform", "list_presets", "list_encoders",
        "trim", "concat", "crop", "scale", "rotate", "flip", "speed", "reverse", "fade", "loop", "split",
        "extract_audio", "replace_audio", "mix_audio", "normalize", "remove_silence",
        "watermark", "picture_in_picture", "add_image", "add_text", "burn_subtitles",
        "color", "apply_lut", "denoise", "sharpen", "stabilize",
        "convert", "compress", "to_gif", "extract_frames", "slideshow", "auto_reframe",
        "transcribe", "auto_captions", "make_short", "smart_cut", "auto_highlights",
        "apply_filter", "run_filtergraph", "chromakey", "blur", "mux_subtitles",
        "create_project", "project_info", "add_clip", "add_overlay", "add_text_track",
        "add_audio_track", "set_captions", "add_transition", "set_output", "render",
        "list_projects", "delete_project", "job_status", "list_jobs", "cancel_job",
    }
    missing = expected - tools
    assert not missing, f"missing tools: {sorted(missing)}"
    # exactly one health tool (rich override replaced make_server's stub)
    assert sum(1 for t in tools if t == "health") == 1

    h = asyncio.run(_call(mcp, "health", {}))
    assert h["ok"] and "ffmpeg" in h and "whisper" in h and "filters" in h
    assert isinstance(h["filters"], dict)


def test_presets():
    mcp = _load()
    p = asyncio.run(_call(mcp, "list_presets", {}))
    assert p["ok"] and "9:16" in p["aspects"] and "mp4" in p["formats"]
    assert "tiktok" in p["caption_styles"]


# --------------------------------------------------------------------------- validation + guards
def test_input_validation():
    mcp = _load()
    # nonexistent input → clean ok:False, never raises
    for tool, args in [("media_info", {"path": "/nonexistent/x.mp4"}),
                       ("trim", {"path": "/nonexistent/x.mp4", "start": 0, "end": 1}),
                       ("crop", {"path": "/nonexistent/x.mp4", "w": 10, "h": 10}),
                       ("detect_silence", {"path": "/nonexistent/x.mp4"})]:
        r = asyncio.run(_call(mcp, tool, args))
        assert r.get("ok") is False, f"{tool} should reject a missing input"

    # concat needs >= 2 paths
    assert asyncio.run(_call(mcp, "concat", {"paths": []}))["ok"] is False
    # bad aspect → not_found with the valid set
    r = asyncio.run(_call(mcp, "scale", {"path": "/nonexistent/x.mp4", "aspect": "banana"}))
    assert r["ok"] is False
    # unknown format on convert → not_found
    r = asyncio.run(_call(mcp, "convert", {"path": "/nonexistent/x.mp4", "format": "banana"}))
    assert r["ok"] is False


def test_filter_gating():
    """add_text / burn_subtitles must degrade cleanly when the build lacks the filter (or accept input)."""
    mcp = _load()
    if not _ffmpeg_present():
        return
    # empty text rejected regardless of build
    assert asyncio.run(_call(mcp, "add_text", {"path": "/nonexistent/x.mp4", "text": ""}))["ok"] is False


def test_path_traversal_guard():
    mcp = _load()
    if not _ffmpeg_present():
        return
    clip = Path(_TMP) / "trav.mp4"
    if not _make_clip(clip):
        return
    r = asyncio.run(_call(mcp, "thumbnail", {"path": str(clip), "out": "../../../../etc/evil.jpg"}))
    # either rejected, or sanitized to a basename strictly inside the data dir
    assert r["ok"] is False or _under_data(r.get("out", "")), r


def _load_mod():
    path = ROOT / "servers" / "videoforge" / "server.py"
    spec = importlib.util.spec_from_file_location("videoforge_mod", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_encode_args_accel():
    """Auto picks software libx264 for short sources, Apple hardware (videotoolbox) for long ones."""
    mod = _load_mod()
    assert "libx264" in mod._encode_args(20, "medium", "auto", 5)
    assert "libx264" in mod._encode_args(20, "medium", "software", 99999), "software mode forces libx264"
    if mod._has_encoder("h264_videotoolbox"):
        assert "h264_videotoolbox" in mod._encode_args(20, "medium", "auto", mod.HW_THRESHOLD + 100)
        assert "h264_videotoolbox" in mod._encode_args(20, "medium", "hardware", 1)


def test_do_anything_and_hybrid():
    """apply_filter/run_filtergraph/chromakey/blur exist; short edits return inline `out`, bad input errs."""
    mcp = _load()
    if not _ffmpeg_present():
        return
    clip = Path(_TMP) / "doany.mp4"
    if not _make_clip(clip, "320x240", 2):
        return
    # apply_filter (greyscale) on a short clip → inline result with an existing file
    r = asyncio.run(_call(mcp, "apply_filter", {"path": str(clip), "filtergraph": "hue=s=0"}))
    assert r["ok"] and (r.get("out") or r.get("job_id")), r
    if r.get("out") and not r.get("status"):
        assert Path(r["out"]).exists()
    # guards: empty filtergraph, missing input, empty inputs list
    assert asyncio.run(_call(mcp, "apply_filter", {"path": str(clip), "filtergraph": ""}))["ok"] is False
    assert asyncio.run(_call(mcp, "apply_filter", {"path": "/nope.mp4", "filtergraph": "hue=s=0"}))["ok"] is False
    assert asyncio.run(_call(mcp, "run_filtergraph", {"inputs": [], "filter_complex": "x"}))["ok"] is False
    # blur + chromakey accept a real clip (inline result or queued job)
    for tool, args in [("blur", {"path": str(clip), "strength": 5}),
                       ("chromakey", {"path": str(clip)})]:
        rr = asyncio.run(_call(mcp, tool, args))
        assert rr["ok"] and (rr.get("out") or rr.get("job_id")), (tool, rr)


# --------------------------------------------------------------------------- timeline / project (pure JSON)
def test_project_roundtrip():
    mcp = _load()
    dummy = Path(_TMP) / "clip.mp4"
    dummy.write_bytes(b"\x00")  # add_clip validates extension + existence, not codec
    created = asyncio.run(_call(mcp, "create_project", {"name": "demo", "aspect": "9:16", "fps": 30}))
    assert created["ok"] and created["resolution"] == "1080x1920"
    pid = created["project_id"]

    asyncio.run(_call(mcp, "add_clip", {"project_id": pid, "path": str(dummy), "end": 3.0}))
    asyncio.run(_call(mcp, "add_text_track", {"project_id": pid, "text": "hello", "start": 0, "end": 2}))
    asyncio.run(_call(mcp, "add_overlay", {"project_id": pid, "image": str(dummy)}))  # bad ext → rejected
    info = asyncio.run(_call(mcp, "project_info", {"project_id": pid}))
    assert info["ok"] and len(info["clips"]) == 1 and len(info["texts"]) == 1

    lst = asyncio.run(_call(mcp, "list_projects", {}))
    assert any(p["project_id"] == pid for p in lst["items"])

    # set_output rejects bad codec, accepts good
    assert asyncio.run(_call(mcp, "set_output", {"project_id": pid, "video_codec": "banana"}))["ok"] is False
    assert asyncio.run(_call(mcp, "set_output", {"project_id": pid, "crf": 18}))["ok"]

    # render of a project (single clip) — must not raise; queues a job OR errors cleanly if no ffmpeg
    r = asyncio.run(_call(mcp, "render", {"project_id": pid}))
    assert "ok" in r  # never raises

    assert asyncio.run(_call(mcp, "delete_project", {"project_id": pid}))["ok"]
    assert asyncio.run(_call(mcp, "project_info", {"project_id": pid}))["ok"] is False


def test_project_unknown_ids():
    mcp = _load()
    for tool in ("project_info", "add_clip", "render", "delete_project"):
        args = {"project_id": "nope"}
        if tool == "add_clip":
            args["path"] = "/nonexistent/x.mp4"
        r = asyncio.run(_call(mcp, tool, args))
        assert r["ok"] is False, f"{tool} should reject an unknown project_id"


# --------------------------------------------------------------------------- jobs
def test_jobs_registry():
    mcp = _load()
    assert asyncio.run(_call(mcp, "list_jobs", {}))["ok"]
    assert asyncio.run(_call(mcp, "job_status", {"job_id": "nope"}))["ok"] is False
    assert asyncio.run(_call(mcp, "cancel_job", {"job_id": "nope"}))["ok"] is False


# --------------------------------------------------------------------------- optional live (only if ffmpeg present)
def test_live_edit_and_render():
    mcp = _load()
    if not _ffmpeg_present():
        print("  (skipped live edit/render: ffmpeg not installed)")
        return
    a = Path(_TMP) / "live_a.mp4"
    b = Path(_TMP) / "live_b.mp4"
    if not (_make_clip(a, "320x240") and _make_clip(b, "640x360")):
        print("  (skipped live edit/render: could not synthesize test clips)")
        return
    # a quick synchronous primitive
    t = asyncio.run(_call(mcp, "trim", {"path": str(a), "start": 0.2, "end": 1.5, "reencode": True}))
    assert t["ok"] and Path(t["out"]).exists(), t

    # single-pass project render with a transition → background job runs to completion
    pid = asyncio.run(_call(mcp, "create_project", {"name": "live", "aspect": "16:9"}))["project_id"]
    asyncio.run(_call(mcp, "add_clip", {"project_id": pid, "path": str(a)}))
    asyncio.run(_call(mcp, "add_clip", {"project_id": pid, "path": str(b)}))
    asyncio.run(_call(mcp, "add_transition", {"project_id": pid, "kind": "fade", "duration": 0.4}))
    rj = asyncio.run(_call(mcp, "render", {"project_id": pid}))
    assert rj["ok"], rj
    s = _wait_job(mcp, rj["job_id"])
    assert s["status"] == "done", s
    assert Path(s["out"]).exists() and Path(s["out"]).stat().st_size > 0


def test_hardening():
    """Concurrency cap, queued-status, atexit temp cleanup, and the filter-graph guard exist."""
    import threading
    path = ROOT / "servers" / "videoforge" / "server.py"
    spec = importlib.util.spec_from_file_location("videoforge_h", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    # concurrency cap + bounded semaphore
    assert mod.MAX_CONCURRENT_JOBS >= 1
    assert hasattr(mod._JOB_SEM, "acquire") and hasattr(mod._JOB_SEM, "release")
    # new jobs start queued (so extras wait instead of forking unbounded ffmpeg)
    j = mod._new_job("test", "x.mp4")
    assert j["status"] == "queued", j
    # filter-graph guard constant present + sane
    assert 1000 < mod.MAX_FILTERGRAPH_CHARS <= 1_000_000
    # temp cleanup hook is registered + callable (no-op safe)
    assert callable(mod._cleanup_tmp)
    mod._cleanup_tmp()  # must not raise even mid-run


if __name__ == "__main__":
    for fn in (test_registry_and_health, test_presets, test_input_validation, test_filter_gating,
               test_path_traversal_guard, test_encode_args_accel, test_do_anything_and_hybrid,
               test_project_roundtrip, test_project_unknown_ids,
               test_jobs_registry, test_hardening, test_live_edit_and_render):
        fn()
        print(fn.__name__, "OK")
    print("ALL VIDEOFORGE TESTS PASSED")
