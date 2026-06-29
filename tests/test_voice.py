"""Offline tests for the voice server — the realtime multilingual voice agent.

Runs on any machine and at any tier: the server degrades gracefully when RealtimeSTT/TTS/Kokoro/
faster-whisper are absent. This exercises tool registration, the tiered health report, pure helpers,
and every error/rejection path — WITHOUT recording audio or speaking (only validation/gating paths of
the audio tools are hit; realtime sessions are started only when the engine is absent, i.e. they err).

    VIRTUAL_ENV= .venv/bin/python tests/test_voice.py
"""
import asyncio
import importlib.util
import os
import sys
import tempfile
from pathlib import Path

os.environ["MCP_DATA_DIR"] = tempfile.mkdtemp(prefix="voice-test-")
ROOT = Path(__file__).resolve().parents[1]
from fastmcp import Client  # noqa: E402

EXPECTED = [
    "health", "setup_guide", "diagnose", "get_audio_setup",
    "list_audio_devices", "set_output_device", "set_input_device", "list_voices",
    "listen", "speak", "say_to_speakers",
    "enroll_voice", "compare_voices", "choose_voice",
    "clone_voice", "set_voice",
    "start_session", "next_utterance", "say_now", "live_transcript", "session_status", "stop_session",
    "call_brief", "call_autopilot", "call_status", "call_sequence", "schedule_call", "selftest",
]


def load():
    spec = importlib.util.spec_from_file_location("vz_server", ROOT / "servers" / "voice" / "server.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod
    spec.loader.exec_module(mod)
    return mod


def test_pure_helpers(s):
    assert isinstance(s._tier(), int) and 0 <= s._tier() <= 3
    caps = s._capabilities()
    for k in ("say", "ffmpeg", "faster_whisper", "realtime_stt", "realtime_tts", "kokoro"):
        assert k in caps
    ready, reasons = s._call_ready()
    assert isinstance(ready, bool) and isinstance(reasons, list)
    d = s._call_directive("Mamma", "wish her happy birthday", "hi", "", "", 10)
    assert "Mamma" in d and "happy birthday" in d and "start_call" in d
    print("pure helpers OK (tier, capabilities, call_ready, directive)")


async def main():
    s = load()
    rt = s._REALTIME_STT and s._REALTIME_TTS
    async with Client(s.mcp) as c:
        tools = {t.name for t in await c.list_tools()}
        for t in EXPECTED:
            assert t in tools, f"missing tool {t}"
        assert len(tools) == len(EXPECTED), f"unexpected tool set: {sorted(tools ^ set(EXPECTED))}  (expected {len(EXPECTED)}, got {len(tools)})"

        # --- health: tiered + well-formed on any platform ---
        h = (await c.call_tool("health", {})).data
        assert h.get("ok") is True and h["server"] == "voice"
        for k in ("tier", "tier_name", "standalone_ready", "whatsapp_call_ready", "capabilities"):
            assert k in h, f"health missing {k}"
        assert isinstance(h["tier"], int)

        # --- setup/diagnose/audio reports are well-formed ---
        assert (await c.call_tool("setup_guide", {})).data.get("ok") is True
        dg = (await c.call_tool("diagnose", {})).data
        assert "whatsapp_call_ready" in dg and isinstance(dg.get("checks"), list)
        assert (await c.call_tool("get_audio_setup", {})).data.get("ok") is True
        lv = (await c.call_tool("list_voices", {})).data
        assert lv.get("ok") is True  # say -v '?' (or empty groups) — never crashes

        # --- validation / rejection paths (NO audio side effects) ---
        assert (await c.call_tool("speak", {"text": ""})).data.get("ok") is False
        assert (await c.call_tool("speak", {"text": "x" * 9000})).data.get("ok") is False
        # listen with a bogus device → err/not_found before any recording (whisper-gated first)
        assert (await c.call_tool("listen", {"device": "no-such-mic-xyz"})).data.get("ok") is False
        # voice clone without the clone engine → clean err
        if not s._coqui_ready():
            cv = (await c.call_tool("clone_voice", {"name": "me"})).data
            assert cv.get("ok") is False and cv.get("hint")
        # device set without SwitchAudioSource → err with hint
        if not s._has("SwitchAudioSource"):
            so = (await c.call_tool("set_output_device", {"name": "BlackHole 2ch"})).data
            assert so.get("ok") is False and so.get("hint")

        # --- Voice Studio: enroll/compare/choose validation ---
        # enroll_voice: empty name → err
        assert (await c.call_tool("enroll_voice", {"name": ""})).data.get("ok") is False
        # enroll_voice: no source (no record_seconds, no sample_path) → err
        ev_no_src = (await c.call_tool("enroll_voice", {"name": "x", "record_seconds": 0, "sample_path": ""})).data
        assert ev_no_src.get("ok") is False
        # enroll_voice: non-existent sample → err
        ev_bad = (await c.call_tool("enroll_voice", {"name": "x", "sample_path": "/no/such/file.wav"})).data
        assert ev_bad.get("ok") is False
        # compare_voices: un-enrolled name → err
        cv2 = (await c.call_tool("compare_voices", {"name": "no_such_voice_xyz"})).data
        assert cv2.get("ok") is False
        # choose_voice: invalid engine → err
        chv_bad = (await c.call_tool("choose_voice", {"name": "x", "engine": "bad_engine"})).data
        assert chv_bad.get("ok") is False
        # choose_voice: unenrolled name → err (wav doesn't exist in test data dir)
        chv_no_wav = (await c.call_tool("choose_voice", {"name": "no_such_voice_xyz", "engine": "kokoro"})).data
        assert chv_no_wav.get("ok") is False

        # --- session tools: unknown id → not_found (never touches audio) ---
        for tool, args in [("next_utterance", {"session_id": "nope"}), ("say_now", {"session_id": "nope", "text": "hi"}),
                           ("live_transcript", {"session_id": "nope"}), ("stop_session", {"session_id": "nope"})]:
            assert (await c.call_tool(tool, args)).data.get("ok") is False, f"{tool} should reject unknown id"
        assert (await c.call_tool("session_status", {})).data.get("ok") is True  # lists (empty) — no crash

        # start_session: when the realtime engine is absent it must err with an install hint (and never
        # start a mic). Only exercised in that safe case to avoid opening real audio devices in tests.
        if not rt:
            ss = (await c.call_tool("start_session", {"mode": "standalone"})).data
            assert ss.get("ok") is False and ss.get("hint")

        # --- call bridge: gating (no spawn / no ring) ---
        assert (await c.call_tool("call_brief", {"contact": ""})).data.get("ok") is False
        cb = (await c.call_tool("call_brief", {"contact": "TestPerson"})).data
        ready, _ = s._call_ready()
        if not ready:
            assert cb.get("ok") is False and cb.get("missing"), "call_brief must gate when audio unrouted"
        ca = (await c.call_tool("call_autopilot", {"contact": ""})).data
        assert ca.get("ok") is False

    test_pure_helpers(s)
    print(f"\nVOICE OK — {len(tools)} tools; tier={s._tier()}; realtime={rt}; health, gating, validation, no-crash")


asyncio.run(main())
