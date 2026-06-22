"""voice — a free, local, REALTIME multilingual voice agent (and the brain of WhatsApp calls).

This is the suite's voice layer: speech-to-text ↔ LLM brain ↔ text-to-speech, built to feel ALIVE,
not robotic. It runs a full-duplex realtime session with voice-activity endpointing and BARGE-IN
(it stops talking the instant the other person speaks), a natural neural voice (Kokoro), optional
cloning of YOUR voice, and a live two-way interpreter — all local and free.

GRACEFUL TIER LADDER (health() reports the active tier + how to climb):
  • Tier 0  — zero install: macOS `say` TTS + ffmpeg/faster-whisper fixed-window listen(). Robotic.
  • Tier 1  — `uv sync --group voice`: RealtimeSTT + RealtimeTTS + Kokoro + Silero VAD → realtime,
              interruptible, natural voice. The main experience.
  • Tier 2  — `uv sync --group voice-clone` + a sample: clone YOUR voice (Coqui XTTS / OpenVoice).
  • Tier 3  — optional Ollama: a fully-local ~500ms turbo brain; else the host agent is the brain.
A missing component never crashes — it returns err(hint=…) and the session falls back a tier.

THE LOOP: the engine owns sub-second audio; the LLM is the brain. Agent-in-the-loop:
  start_session() → next_utterance() (blocks until they finish a turn) → you compose the reply →
  say_now() (streamed, barge-in-aware) → repeat. Hands-off: call_autopilot() drives a whole
  WhatsApp call via a background `claude -p` agent.

WHATSAPP CALLS: call audio is end-to-end encrypted, so the agent joins via FREE virtual-audio
routing — BlackHole as Chrome's mic (agent → call) + an Aggregate device captured for STT
(call → agent). setup_guide()/diagnose() walk you through the one-time setup; call-mode tools
refuse (never pretend) until it's wired. macOS + Chrome only.
"""
from __future__ import annotations

import datetime as dt
import functools
import json
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
import wave
from pathlib import Path

from mcp_base import (Jobs, data_dir, err, get_env, get_env_int, get_logger,
                      make_server, not_found, ok, repo_root)

log = get_logger("voice")

# --------------------------------------------------------------------------- optional engines (tiered)
# Detect availability WITHOUT importing — these libs pull in torch and take seconds to load, and the
# system hub imports every server module at startup. The real (heavy) imports happen lazily, only when
# a realtime session actually starts (_Session.start / _build_tts).
import importlib.util as _ilu


def _installed(mod: str) -> bool:
    try:
        return _ilu.find_spec(mod) is not None
    except Exception:  # noqa: BLE001
        return False


_WHISPER = _installed("faster_whisper")
_REALTIME_STT = _installed("RealtimeSTT")
_REALTIME_TTS = _installed("RealtimeTTS")
_CHATTERBOX = _installed("chatterbox")
try:
    import sounddevice as _sd  # type: ignore  # light (portaudio); used only for device queries
except Exception:  # noqa: BLE001
    _sd = None


def _has(cmd: str) -> str | None:
    return shutil.which(cmd)


def _kokoro_ready() -> bool:
    return _REALTIME_TTS and _installed("kokoro")


def _coqui_ready() -> bool:
    return _REALTIME_TTS and _installed("TTS")


def _ollama_ready() -> bool:
    return bool(_has("ollama"))


def _chatterbox_ready() -> bool:
    return _CHATTERBOX


def _voice_engines() -> list[str]:
    """Ordered list of available one-shot synthesis engines (quality-first)."""
    out = []
    if _chatterbox_ready():
        out.append("chatterbox")
    if _coqui_ready():
        out.append("xtts")
    if _kokoro_ready():
        out.append("kokoro")
    if _has("say"):
        out.append("say")
    return out


# --------------------------------------------------------------------------- voice profile persistence
def _profile_path(name: str) -> Path:
    return VOICES_DIR / f"{name}.json"


def _load_profile(name: str) -> dict | None:
    p = _profile_path(name)
    if p.exists():
        try:
            return json.loads(p.read_text())
        except Exception:  # noqa: BLE001
            return None
    return None


def _save_profile(name: str, engine: str, sample: str, language: str = "") -> None:
    _profile_path(name).write_text(json.dumps(
        {"engine": engine, "sample": sample, "language": language or ""}, indent=2))


mcp = make_server(
    "voice",
    instructions=(
        "Free, local, REALTIME multilingual voice agent + the brain of WhatsApp calls. Natural neural "
        "voice (Kokoro), barge-in (stops talking when interrupted), clone-your-voice, live interpreter. "
        "Call health() first — it reports the active TIER (0 say → 1 realtime+Kokoro → 2 voice-clone → "
        "3 ollama turbo) and how to climb. Loop: start_session() → next_utterance() (blocks until they "
        "finish) → compose reply → say_now() (streamed, interruptible). Standalone mode needs zero setup "
        "(say_to_speakers/listen). For WhatsApp CALLS, the agent joins via BlackHole virtual-audio "
        "routing (setup_guide()); call-mode refuses until wired. Hands-off: call_autopilot(contact, "
        "directive). macOS + Chrome only; everything degrades gracefully when a component is absent."
    ),
)

# --------------------------------------------------------------------------- config
ROOT = repo_root()
DATA = data_dir("voice")
TMP = DATA / "tmp"
VOICES_DIR = DATA / "voices"  # cloned-voice reference samples
for _d in (TMP, VOICES_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DEFAULT_LANGUAGE = (get_env("VOICE_DEFAULT_LANGUAGE", "en") or "en").strip().lower()
WHISPER_MODEL = (get_env("VOICE_WHISPER_MODEL", "base") or "base").strip()
TTS_ENGINE = (get_env("VOICE_TTS_ENGINE", "auto") or "auto").strip().lower()  # auto|kokoro|coqui|piper|say
CALL_OUT_DEVICE = (get_env("VOICE_CALL_OUTPUT_DEVICE", "BlackHole 2ch") or "").strip()
CALL_IN_DEVICE = (get_env("VOICE_CALL_INPUT_DEVICE", "") or "").strip()
LISTEN_MAX = max(2, get_env_int("VOICE_LISTEN_MAX_SECONDS", 60) or 60)
MAX_TEXT = 8000
AUTOPILOT_STOPWORDS = [s.strip().lower() for s in (get_env(
    "VOICE_AUTOPILOT_STOPWORDS", "bye,goodbye,hang up,that's all,khatam,bye bye") or "").split(",") if s.strip()]

JOBS = Jobs("voice", max_concurrent=2, inline_wait=4.0)

# A small language → Kokoro voice hint (Kokoro voice ids; first is a sensible default per language).
_KOKORO_VOICES = {
    "en": ["af_heart", "af_bella", "am_michael", "bf_emma"],
    "hi": ["hf_alpha", "hf_beta", "hm_omega", "hm_psi"],
    "es": ["ef_dora", "em_alex"], "fr": ["ff_siwis"], "it": ["if_sara", "im_nicola"],
    "pt": ["pf_dora", "pm_alex"], "ja": ["jf_alpha", "jm_kumo"], "zh": ["zf_xiaobei", "zm_yunjian"],
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


def _run(args: list[str], inp: str | None = None, timeout: int = 60) -> dict:
    try:
        p = subprocess.run(args, capture_output=True, text=True, input=inp, timeout=timeout)
        return {"ok": p.returncode == 0, "code": p.returncode, "out": p.stdout, "err": p.stderr}
    except FileNotFoundError:
        return {"ok": False, "err": f"command not found: {args[0]}"}
    except subprocess.TimeoutExpired:
        return {"ok": False, "err": f"timed out after {timeout}s"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "err": str(e)}


def _claude_bin() -> str | None:
    cand = get_env("CLAUDE_BIN") or shutil.which("claude")
    if cand and Path(cand).exists():
        return cand
    for p in (Path.home() / ".claude/local/claude", Path("/opt/homebrew/bin/claude"),
              Path("/usr/local/bin/claude")):
        if p.exists():
            return str(p)
    return cand


# --------------------------------------------------------------------------- one-shot synthesis dispatch
def _synthesize_to_file(text: str, language: str, engine: str,
                        sample_path: str = "", out_path: str = "") -> tuple[bool, str]:
    """Synthesize text to a WAV file via the named engine. Returns (ok, path_or_error)."""
    lang = (language or DEFAULT_LANGUAGE)[:2]
    dest = out_path or str(TMP / f"synth-{dt.datetime.now():%H%M%S-%f}.wav")

    if engine == "chatterbox":
        if not _chatterbox_ready():
            return False, "chatterbox not installed (uv sync --group voice-clone)"
        try:
            import torch  # noqa: PLC0415
            import torchaudio  # noqa: PLC0415
            from chatterbox.mtl_tts import ChatterboxMultilingualTTS  # noqa: PLC0415
            dev = "mps" if torch.backends.mps.is_available() else "cpu"
            m = ChatterboxMultilingualTTS.from_pretrained(device=dev)
            wav = m.generate(text, language_id=lang,
                             audio_prompt_path=sample_path or None,
                             exaggeration=0.45, cfg_weight=0.5, temperature=0.7)
            torchaudio.save(dest, wav.detach().cpu(), m.sr)
            return True, dest
        except Exception as e:  # noqa: BLE001
            return False, f"chatterbox error: {e}"

    if engine == "xtts":
        if not _coqui_ready():
            return False, "coqui/XTTS not installed (uv sync --group voice-clone)"
        try:
            from TTS.api import TTS as CoquiTTS  # noqa: PLC0415
            tts = CoquiTTS("tts_models/multilingual/multi-dataset/xtts_v2", gpu=False)
            tts.tts_to_file(text=text, speaker_wav=sample_path or None,
                            language=lang, file_path=dest)
            return True, dest
        except Exception as e:  # noqa: BLE001
            return False, f"xtts error: {e}"

    if engine == "kokoro":
        if not _kokoro_ready():
            return False, "kokoro not installed (uv sync --group voice)"
        try:
            from kokoro import KPipeline  # noqa: PLC0415
            lc = {"en": "a", "hi": "h", "es": "e", "fr": "f", "ja": "j", "zh": "z"}.get(lang, "a")
            vid = _KOKORO_VOICES.get(lang, _KOKORO_VOICES["en"])[0]
            pipeline = KPipeline(lang_code=lc)
            frames: list[int] = []
            sr = 24000
            for _, _, audio in pipeline(text, voice=vid):
                sr = 24000
                frames.extend(max(-32768, min(32767, int(s * 32767))) for s in audio.tolist())
            with wave.open(dest, "w") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(sr)
                wf.writeframes(struct.pack(f"<{len(frames)}h", *frames))
            return True, dest
        except Exception as e:  # noqa: BLE001
            return False, f"kokoro error: {e}"

    if engine == "say":
        v = _say_voice_for(language)
        ok_f, path = _say_to_file(text, v, 0)
        if ok_f:
            if _has("ffmpeg") and path.endswith(".aiff"):
                r = _run(["ffmpeg", "-hide_banner", "-y", "-i", path, dest], timeout=30)
                return (True, dest) if r.get("ok") else (True, path)
            return True, path
        return False, "say synthesis failed"

    return False, f"unknown engine: {engine}"


# --------------------------------------------------------------------------- tiers / capabilities
def _tier() -> int:
    """Voice-capability ladder: 0 say · 1 realtime+neural(Kokoro) · 2 +cloned voice · 3 +ollama turbo."""
    if not (_REALTIME_STT and _REALTIME_TTS and (_kokoro_ready() or _coqui_ready())):
        return 0
    has_clone = _coqui_ready() and any(VOICES_DIR.glob("*.wav"))
    if has_clone and _ollama_ready():
        return 3
    if has_clone:
        return 2
    return 1


def _capabilities() -> dict:
    return {
        "say": bool(_has("say")), "afplay": bool(_has("afplay")), "ffmpeg": bool(_has("ffmpeg")),
        "faster_whisper": _WHISPER, "realtime_stt": _REALTIME_STT, "realtime_tts": _REALTIME_TTS,
        "kokoro": _kokoro_ready(), "coqui_clone": _coqui_ready(), "chatterbox": _chatterbox_ready(),
        "sounddevice": bool(_sd), "switchaudio": bool(_has("SwitchAudioSource")), "ollama": _ollama_ready(),
        "cloned_voices": sorted(p.stem for p in VOICES_DIR.glob("*.wav")),
        "voice_engines": _voice_engines(),
        "voice_profiles": sorted(p.stem for p in VOICES_DIR.glob("*.json")),
    }


# --------------------------------------------------------------------------- audio devices
def _ffmpeg_devices() -> dict:
    """Parse `ffmpeg -f avfoundation -list_devices` (used by the one-shot ffmpeg listen path)."""
    if not _has("ffmpeg"):
        return {"audio": [], "video": []}
    r = _run(["ffmpeg", "-hide_banner", "-f", "avfoundation", "-list_devices", "true", "-i", ""], timeout=15)
    audio, section = [], None
    for line in (r.get("err", "") or "").splitlines():
        if "AVFoundation audio devices" in line:
            section = "audio"
            continue
        if "AVFoundation video devices" in line:
            section = "video"
            continue
        m = re.search(r"\[(\d+)\]\s+(.+?)\s*$", line)
        if m and section == "audio":
            audio.append({"index": int(m.group(1)), "name": m.group(2).strip()})
    return {"audio": audio}


def _sd_index(name: str, kind: str) -> int | None:
    """Resolve a device NAME to a sounddevice index (for the realtime engines), or None."""
    if not _sd or not name:
        return None
    want = name.strip().lower()
    try:
        for i, d in enumerate(_sd.query_devices()):
            ch = d.get("max_input_channels" if kind == "input" else "max_output_channels", 0)
            if ch > 0 and want in str(d.get("name", "")).lower():
                return i
    except Exception:  # noqa: BLE001
        return None
    return None


def _ffmpeg_index(name: str) -> int | None:
    if not name:
        return None
    want = name.strip().lower()
    for d in _ffmpeg_devices().get("audio", []):
        if want in d["name"].lower():
            return d["index"]
    return None


def _output_devices() -> list[str]:
    if _has("SwitchAudioSource"):
        r = _run(["SwitchAudioSource", "-a", "-t", "output"], timeout=10)
        if r.get("ok"):
            return [ln.strip() for ln in (r.get("out") or "").splitlines() if ln.strip()]
    if _sd:
        try:
            return [d["name"] for d in _sd.query_devices() if d.get("max_output_channels", 0) > 0]
        except Exception:  # noqa: BLE001
            pass
    return []


def _blackhole_present() -> bool:
    names = " ".join(_output_devices()).lower()
    if "blackhole" in names:
        return True
    return any("blackhole" in d["name"].lower() for d in _ffmpeg_devices().get("audio", []))


# --------------------------------------------------------------------------- whisper (one-shot STT)
_WHISPER_CACHE: dict[str, object] = {}


def _load_whisper(model: str):
    if not _WHISPER:
        return None, err("speech-to-text needs faster-whisper", hint="uv sync --group voice")
    if model in _WHISPER_CACHE:
        return _WHISPER_CACHE[model], None
    try:
        from faster_whisper import WhisperModel
        m = WhisperModel(model, device="cpu", compute_type="int8")
        _WHISPER_CACHE[model] = m
        return m, None
    except Exception as e:  # noqa: BLE001
        return None, err(f"could not load whisper model '{model}': {e}", hint="try VOICE_WHISPER_MODEL=base")


# --------------------------------------------------------------------------- TTS (say baseline + routing)
def _say_voice_for(language: str) -> str:
    """Pick a macOS `say` voice whose locale matches a language code (best-effort)."""
    lang = (language or DEFAULT_LANGUAGE).strip().lower()[:2]
    r = _run(["say", "-v", "?"], timeout=10)
    best = ""
    for line in (r.get("out") or "").splitlines():
        m = re.match(r"^(.+?)\s+([a-z]{2}_[A-Z]{2})", line)
        if m and m.group(2).lower().startswith(lang):
            return m.group(1).strip()
        if m and not best:
            best = m.group(1).strip()
    return best


def _say_to_file(text: str, voice: str, rate: int) -> tuple[bool, str]:
    out = str(TMP / f"say-{dt.datetime.now():%H%M%S-%f}.aiff")
    args = ["say"]
    if voice:
        args += ["-v", voice]
    if rate and rate > 0:
        args += ["-r", str(int(rate))]
    args += ["-o", out, "--", text]
    r = _run(args, timeout=60)
    return (r.get("ok") and Path(out).exists()), out


def _play(path: str, device: str = "") -> dict:
    """Play a file via afplay, optionally routing the default output to `device` (the BlackHole mic)."""
    if not _has("afplay"):
        return err("afplay not found (macOS only)")
    prev = ""
    if device and _has("SwitchAudioSource"):
        cur = _run(["SwitchAudioSource", "-c", "-t", "output"], timeout=8)
        prev = (cur.get("out") or "").strip()
        _run(["SwitchAudioSource", "-t", "output", "-s", device], timeout=8)
    try:
        r = _run(["afplay", path], timeout=120)
    finally:
        if prev:
            _run(["SwitchAudioSource", "-t", "output", "-s", prev], timeout=8)
    return ok() if r.get("ok") else err(f"afplay failed: {r.get('err')}")


# =========================================================================== realtime session engine
class _Session:
    """A live full-duplex voice session: VAD-endpointed STT + streaming TTS with barge-in."""

    def __init__(self, sid: str, language: str, voice: str, interpret_to: str,
                 device_in: str, device_out: str):
        self.id = sid
        self.language = language
        self.voice = voice
        self.interpret_to = interpret_to
        self.device_in = device_in
        self.device_out = device_out
        self.transcript: list[dict] = []
        self.utterances: "queue.Queue[dict]" = queue.Queue()
        self.recorder = None
        self.stream = None
        self.speaking = False
        self.stop_flag = threading.Event()
        self.error: str | None = None
        self.started = time.time()

    def _build_tts(self):
        from RealtimeTTS import TextToAudioStream  # type: ignore
        engine = None
        clones = sorted(VOICES_DIR.glob("*.wav"))
        # If the chosen profile uses a batch engine (chatterbox), fall back to Kokoro for streaming.
        profile = _load_profile(self.voice) if self.voice else None
        if profile and profile.get("engine") == "chatterbox":
            log.info("voice profile engine=chatterbox; live session uses kokoro (streaming)")
            if _kokoro_ready():
                from RealtimeTTS import KokoroEngine  # type: ignore
                tgt = (self.interpret_to or self.language or DEFAULT_LANGUAGE)[:2]
                vid = _KOKORO_VOICES.get(tgt, _KOKORO_VOICES["en"])[0]
                engine = KokoroEngine(voice=vid)
                kw = {"on_audio_stream_stop": self._on_audio_stop}
                idx = _sd_index(self.device_out or CALL_OUT_DEVICE, "output")
                if idx is not None:
                    kw["output_device_index"] = idx
                return TextToAudioStream(engine, **kw)
        if (TTS_ENGINE in ("auto", "coqui")) and _coqui_ready() and (self.voice and (VOICES_DIR / f"{self.voice}.wav").exists() or (TTS_ENGINE == "coqui" and clones)):
            from RealtimeTTS import CoquiEngine  # type: ignore
            ref = (VOICES_DIR / f"{self.voice}.wav")
            ref = str(ref if ref.exists() else clones[0])
            engine = CoquiEngine(voice=ref, language=(self.language or DEFAULT_LANGUAGE)[:2])
        elif (TTS_ENGINE in ("auto", "kokoro")) and _kokoro_ready():
            from RealtimeTTS import KokoroEngine  # type: ignore
            tgt = (self.interpret_to or self.language or DEFAULT_LANGUAGE)[:2]
            vid = self.voice or _KOKORO_VOICES.get(tgt, _KOKORO_VOICES["en"])[0]
            try:
                engine = KokoroEngine(voice=vid)
            except Exception:  # noqa: BLE001
                engine = KokoroEngine()
        else:
            try:
                from RealtimeTTS import SystemEngine  # type: ignore
                engine = SystemEngine()
            except Exception as e:  # noqa: BLE001
                raise RuntimeError("no neural TTS engine available — install Kokoro (uv sync --group voice)") from e
        kw = {"on_audio_stream_stop": self._on_audio_stop}
        idx = _sd_index(self.device_out or CALL_OUT_DEVICE, "output")
        if idx is not None:
            kw["output_device_index"] = idx
        return TextToAudioStream(engine, **kw)

    def _on_audio_stop(self):
        self.speaking = False

    def _on_vad_start(self):
        # Barge-in: the other party started speaking → cut our own speech immediately.
        if self.speaking and self.stream is not None:
            try:
                self.stream.stop()
            except Exception:  # noqa: BLE001
                pass
            self.speaking = False

    def start(self):
        from RealtimeSTT import AudioToTextRecorder  # type: ignore
        self.stream = self._build_tts()
        rec_kw = {
            "model": WHISPER_MODEL, "spinner": False, "use_microphone": True,
            "on_vad_detect_start": self._on_vad_start,
            "post_speech_silence_duration": 0.6,
        }
        if not self.interpret_to and self.language:
            rec_kw["language"] = self.language[:2]
        idx = _sd_index(self.device_in or CALL_IN_DEVICE, "input")
        if idx is not None:
            rec_kw["input_device_index"] = idx
        self.recorder = AudioToTextRecorder(**rec_kw)

        def loop():
            while not self.stop_flag.is_set():
                try:
                    text = self.recorder.text()  # blocks until a full utterance ends
                except Exception as e:  # noqa: BLE001
                    self.error = str(e)
                    break
                if self.stop_flag.is_set():
                    break
                text = (text or "").strip()
                if not text:
                    continue
                turn = {"role": "them", "text": text, "t": time.time(),
                        "language": getattr(self.recorder, "detected_language", "") or self.language}
                self.transcript.append(turn)
                self.utterances.put(turn)

        threading.Thread(target=loop, daemon=True).start()

    def say(self, text: str) -> None:
        if self.stream is None:
            return
        self.speaking = True
        self.transcript.append({"role": "me", "text": text, "t": time.time()})
        try:
            self.stream.feed(text)
            self.stream.play_async()
        finally:
            # play_async returns immediately; mark not-speaking when playback ends is handled by barge-in
            pass

    def stop(self) -> None:
        self.stop_flag.set()
        for obj, meth in ((self.stream, "stop"), (self.recorder, "shutdown")):
            if obj is not None:
                try:
                    getattr(obj, meth)()
                except Exception:  # noqa: BLE001
                    pass


_SESSIONS: dict[str, _Session] = {}
_SESS_LOCK = threading.Lock()


def _realtime_ready() -> dict | None:
    if not (_REALTIME_STT and _REALTIME_TTS):
        return err("realtime voice needs RealtimeSTT + RealtimeTTS", hint="uv sync --group voice")
    if not (_kokoro_ready() or _coqui_ready() or _has("say")):
        return err("no TTS engine available", hint="uv sync --group voice (Kokoro)")
    return None


def _call_ready() -> tuple[bool, list[str]]:
    reasons = []
    if not _has("ffmpeg"):
        reasons.append("ffmpeg missing (brew install ffmpeg)")
    if not _WHISPER:
        reasons.append("faster-whisper missing (uv sync --group voice)")
    if not (_kokoro_ready() or _has("say")):
        reasons.append("no TTS engine")
    if not _blackhole_present():
        reasons.append("BlackHole virtual device not found (brew install blackhole-2ch + Aggregate device)")
    if not _has("SwitchAudioSource") and not _sd:
        reasons.append("cannot route audio to BlackHole (brew install switchaudio-osx)")
    if not (CALL_OUT_DEVICE):
        reasons.append("VOICE_CALL_OUTPUT_DEVICE not set")
    return (not reasons), reasons


# =========================================================================== HEALTH / SETUP
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
    """Liveness + TIER report. CALL FIRST. Shows the active tier (0 say → 1 realtime+Kokoro → 2 clone
    → 3 ollama), standalone vs whatsapp-call readiness, capabilities, and how to climb."""
    caps = _capabilities()
    tier = _tier()
    standalone_ok = bool(caps["say"] or (caps["realtime_tts"] and caps["kokoro"]))
    call_ok, call_reasons = _call_ready()
    hints = {}
    if tier == 0:
        hints["realtime"] = "uv sync --group voice  (RealtimeSTT/TTS + Kokoro + Silero VAD → realtime, natural, barge-in)"
    if not caps["coqui_clone"]:
        hints["clone_voice"] = "uv sync --group voice-clone  (then clone_voice('me', record_seconds=20))"
    if not caps["ollama"]:
        hints["turbo_brain"] = "install Ollama for an optional fully-local ~500ms brain (else the host agent is the brain)"
    if not call_ok:
        hints["whatsapp_call"] = "see setup_guide() — BlackHole + Aggregate device + Chrome mic"
    return ok(server="voice", tier=tier,
              tier_name={0: "say (Tier 0)", 1: "realtime+Kokoro (Tier 1)", 2: "voice-clone (Tier 2)",
                         3: "ollama turbo (Tier 3)"}.get(tier),
              standalone_ready=standalone_ok,
              whatsapp_call_ready=call_ok, whatsapp_call_reasons=call_reasons or None,
              capabilities=caps, default_language=DEFAULT_LANGUAGE, whisper_model=WHISPER_MODEL,
              tts_engine=TTS_ENGINE, active_sessions=len(_SESSIONS),
              voice_engines=_voice_engines(),
              voice_profiles=sorted(p.stem for p in VOICES_DIR.glob("*.json")),
              hints=hints or None)


@mcp.tool
@_guard
def setup_guide() -> dict:
    """The exact one-time steps to unlock each tier + WhatsApp-call audio routing (all free)."""
    return ok(
        tier1_realtime=["uv sync --group voice  (RealtimeSTT + RealtimeTTS[kokoro] + faster-whisper + Silero VAD)",
                        "first run downloads the Kokoro + whisper models (~hundreds MB)"],
        tier2_clone=["uv sync --group voice-clone", "clone_voice('me', record_seconds=20)  (or sample_path=…)"],
        tier3_turbo=["install Ollama + a small model (e.g. `ollama pull llama3.2`); pass brain='ollama'"],
        whatsapp_call_audio=[
            "brew install blackhole-2ch switchaudio-osx",
            "Audio MIDI Setup → create an Aggregate/Multi-Output device with your speakers + 'BlackHole 2ch'",
            "Chrome / WhatsApp Web call settings → set the MICROPHONE to 'BlackHole 2ch' (so the agent's voice is what they hear)",
            "in .env: VOICE_CALL_OUTPUT_DEVICE=\"BlackHole 2ch\" and VOICE_CALL_INPUT_DEVICE=\"<your Aggregate device>\"",
            "then run voice.diagnose() — it should report whatsapp_call_ready: true",
        ],
        ethics="Only clone your OWN voice, or with explicit consent.",
    )


@mcp.tool
@_guard
def diagnose() -> dict:
    """Actionable readiness for WhatsApp-call voice: each precondition with pass/fail + the fix."""
    caps = _capabilities()
    call_ok, reasons = _call_ready()
    checks = [
        {"check": "ffmpeg", "ok": caps["ffmpeg"], "fix": "brew install ffmpeg"},
        {"check": "faster-whisper", "ok": caps["faster_whisper"], "fix": "uv sync --group voice"},
        {"check": "realtime engine", "ok": caps["realtime_stt"] and caps["realtime_tts"], "fix": "uv sync --group voice"},
        {"check": "natural voice (Kokoro)", "ok": caps["kokoro"], "fix": "uv sync --group voice"},
        {"check": "BlackHole virtual device", "ok": _blackhole_present(), "fix": "brew install blackhole-2ch + Aggregate device"},
        {"check": "audio routing (SwitchAudioSource/sounddevice)", "ok": caps["switchaudio"] or caps["sounddevice"], "fix": "brew install switchaudio-osx"},
        {"check": "VOICE_CALL_OUTPUT_DEVICE set", "ok": bool(CALL_OUT_DEVICE), "fix": "set it in .env (e.g. 'BlackHole 2ch')"},
    ]
    nxt = next((c["fix"] for c in checks if not c["ok"]), "ready — try call_autopilot()")
    return ok(whatsapp_call_ready=call_ok, tier=_tier(), checks=checks, reasons=reasons or None, next_action=nxt)


@mcp.tool
@_guard
def get_audio_setup() -> dict:
    """Raw audio state: installed tools, input/output devices, BlackHole/clone presence, env config."""
    return ok(capabilities=_capabilities(), ffmpeg_inputs=_ffmpeg_devices().get("audio", []),
              output_devices=_output_devices(), blackhole=_blackhole_present(),
              call_output_device=CALL_OUT_DEVICE, call_input_device=CALL_IN_DEVICE)


# =========================================================================== DEVICES / VOICES
@mcp.tool
@_guard
def list_audio_devices() -> dict:
    """List audio input (ffmpeg avfoundation) + output devices, flagging the virtual BlackHole device."""
    return ok(inputs=_ffmpeg_devices().get("audio", []), outputs=_output_devices(),
              blackhole_present=_blackhole_present(), sounddevice=bool(_sd))


@mcp.tool
@_guard
def set_output_device(name: str) -> dict:
    """Set the macOS default OUTPUT device by name (needs SwitchAudioSource)."""
    if not name.strip():
        return err("device name required")
    if not _has("SwitchAudioSource"):
        return err("needs SwitchAudioSource", hint="brew install switchaudio-osx")
    r = _run(["SwitchAudioSource", "-t", "output", "-s", name], timeout=10)
    return ok(output_device=name) if r.get("ok") else err(f"could not set output: {r.get('err') or r.get('out')}")


@mcp.tool
@_guard
def set_input_device(name: str) -> dict:
    """Set the macOS default INPUT device by name (needs SwitchAudioSource)."""
    if not name.strip():
        return err("device name required")
    if not _has("SwitchAudioSource"):
        return err("needs SwitchAudioSource", hint="brew install switchaudio-osx")
    r = _run(["SwitchAudioSource", "-t", "input", "-s", name], timeout=10)
    return ok(input_device=name) if r.get("ok") else err(f"could not set input: {r.get('err') or r.get('out')}")


@mcp.tool
@_guard
def list_voices(language: str = "") -> dict:
    """List available voices grouped by language — macOS `say` voices (+ Kokoro voices if installed).
    Proves any-language output. Filter to a language code (e.g. 'hi')."""
    by_lang: dict[str, list[str]] = {}
    r = _run(["say", "-v", "?"], timeout=10)
    for line in (r.get("out") or "").splitlines():
        m = re.match(r"^(.+?)\s+([a-z]{2}_[A-Z]{2})", line)
        if m:
            by_lang.setdefault(m.group(2), []).append(m.group(1).strip())
    if language:
        lang = language.strip().lower()[:2]
        by_lang = {k: v for k, v in by_lang.items() if k.lower().startswith(lang)}
    return ok(say_voices_by_locale=by_lang,
              kokoro_voices=(_KOKORO_VOICES if _kokoro_ready() else None),
              cloned_voices=sorted(p.stem for p in VOICES_DIR.glob("*.wav")) or None)


# =========================================================================== ONE-SHOT (standalone)
@mcp.tool
@_guard
def listen(seconds: int = 6, device: str = "", language: str = "") -> dict:
    """Record `seconds` of audio from an input device (default mic) and transcribe it (faster-whisper,
    auto-detects language). Standalone 'ear'. For a call, set device=<Aggregate that carries them>."""
    if not _has("ffmpeg"):
        return err("listen needs ffmpeg", hint="brew install ffmpeg")
    secs = max(1, min(int(seconds), LISTEN_MAX))
    idx = _ffmpeg_index(device) if device else 0
    if device and idx is None:
        return not_found("audio input", device, [d["name"] for d in _ffmpeg_devices().get("audio", [])])
    m, e = _load_whisper(WHISPER_MODEL)  # after device validation (model load can be heavy)
    if e:
        return e
    wav = str(TMP / f"listen-{dt.datetime.now():%H%M%S-%f}.wav")
    r = _run(["ffmpeg", "-hide_banner", "-y", "-f", "avfoundation", "-i", f":{idx}", "-t", str(secs),
              "-ar", "16000", "-ac", "1", wav], timeout=secs + 30)
    if not (r.get("ok") and Path(wav).exists()):
        return err(f"recording failed: {(r.get('err') or '')[-300:]}", hint="check mic permission for your terminal")
    segs, info = m.transcribe(wav, language=(language or None))
    text = " ".join(s.text.strip() for s in segs).strip()
    return ok(text=text, language=getattr(info, "language", "") or language, seconds=secs, device=device or "default")


@mcp.tool
@_guard
def speak(text: str, language: str = "", voice: str = "", device: str = "", rate: int = 0) -> dict:
    """Speak text, routing output to `device` (e.g. the BlackHole mic for a call). Uses Kokoro if
    available else macOS `say`. Auto-picks a voice matching `language` when `voice` is empty."""
    if not text.strip():
        return err("text is required")
    if len(text) > MAX_TEXT:
        return err(f"text too long (>{MAX_TEXT})")
    out_dev = device or CALL_OUT_DEVICE
    if device and not _blackhole_present():
        ok_, reasons = _call_ready()
        if not ok_:
            return err("call-audio routing not set up", missing=reasons, hint="run setup_guide()")
    # If voice refers to an enrolled profile, use the chosen engine for synthesis.
    profile = _load_profile(voice) if voice else None
    if profile:
        eng = profile.get("engine", "say")
        sample = profile.get("sample", "")
        lang_hint = language or profile.get("language", "")
        ok_s, synth_path = _synthesize_to_file(text, lang_hint, eng, sample_path=sample)
        if ok_s:
            res = _play(synth_path, device=out_dev if device else "")
            if res.get("ok"):
                log.info("speak via profile", engine=eng, chars=len(text))
                return ok(spoke=len(text), voice=voice, engine=eng,
                          language=lang_hint or DEFAULT_LANGUAGE, device=(out_dev if device else "speakers"))
        log.warning("profile synthesis failed, falling back to say", engine=eng, error=synth_path)
    v = voice or _say_voice_for(language)
    okf, path = _say_to_file(text, v, rate)
    if not okf:
        return err("TTS failed (say)", hint="check the voice name with list_voices()")
    res = _play(path, device=out_dev if device else "")
    if not res.get("ok"):
        return res
    log.info("speak", chars=len(text), voice=v, device=out_dev if device else "speakers")
    return ok(spoke=len(text), voice=v, language=language or DEFAULT_LANGUAGE, device=(out_dev if device else "speakers"))


@mcp.tool
@_guard
def say_to_speakers(text: str, language: str = "", voice: str = "", rate: int = 0) -> dict:
    """Standalone: speak text to your speakers (zero setup). Multilingual via macOS `say`."""
    return speak(text, language=language, voice=voice, device="", rate=rate)


# =========================================================================== VOICE CLONING
@mcp.tool
@_guard
def clone_voice(name: str, sample_path: str = "", record_seconds: int = 0) -> dict:
    """Create a cloned voice from a short clean sample of YOUR voice (Coqui XTTS / OpenVoice). Provide
    sample_path=<wav/aiff>, or record_seconds=N to capture from the mic now. Use it via set_voice(name)
    or voice= in start_session. Only clone your own voice (or with explicit consent)."""
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip())[:40]
    if not name:
        return err("name is required")
    if not _coqui_ready():
        return err("voice cloning needs the clone engine", hint="uv sync --group voice-clone")
    dest = VOICES_DIR / f"{name}.wav"
    if record_seconds and int(record_seconds) > 0:
        if not _has("ffmpeg"):
            return err("recording needs ffmpeg", hint="brew install ffmpeg")
        secs = max(5, min(int(record_seconds), 60))
        r = _run(["ffmpeg", "-hide_banner", "-y", "-f", "avfoundation", "-i", ":0", "-t", str(secs),
                  "-ar", "22050", "-ac", "1", str(dest)], timeout=secs + 30)
        if not (r.get("ok") and dest.exists()):
            return err(f"recording failed: {(r.get('err') or '')[-300:]}")
    elif sample_path:
        src = Path(sample_path).expanduser()
        if not src.exists():
            return err(f"sample not found: {src}")
        if src.suffix.lower() == ".wav":
            shutil.copyfile(src, dest)
        else:
            if not _has("ffmpeg"):
                return err("converting the sample needs ffmpeg", hint="brew install ffmpeg")
            _run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-ar", "22050", "-ac", "1", str(dest)], timeout=60)
        if not dest.exists():
            return err("could not prepare the voice sample")
    else:
        return err("provide sample_path or record_seconds")
    return ok(cloned=name, sample=str(dest), hint=f"use it: start_session(voice='{name}') or set_voice('{name}')")


@mcp.tool
@_guard
def set_voice(name: str) -> dict:
    """Set the default voice for new sessions (a cloned voice name, a Kokoro voice id, or a `say` voice)."""
    if (VOICES_DIR / f"{name}.wav").exists():
        return ok(voice=name, kind="cloned", note="pass voice= per start_session to use it")
    return ok(voice=name, kind="named", note="pass voice= per start_session/speak to use it")


# =========================================================================== VOICE STUDIO (enroll → compare → choose)
@mcp.tool
@_guard
def enroll_voice(name: str, record_seconds: int = 0, sample_path: str = "") -> dict:
    """Enroll a voice reference for cloning. Captures from the mic (record_seconds>0) or imports a
    file (sample_path=), then applies an ffmpeg cleaning pipeline (highpass/lowpass/afftdn/loudnorm/
    silenceremove) for top clone quality. After enrolling, run compare_voices() to A/B engines.
    Only enroll your own voice (or with explicit consent)."""
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip())[:40]
    if not name:
        return err("name is required")
    if not _has("ffmpeg"):
        return err("enrollment needs ffmpeg", hint="brew install ffmpeg")
    if not record_seconds and not sample_path:
        return err("provide sample_path or record_seconds")
    raw = TMP / f"{name}_raw.wav"
    dest = VOICES_DIR / f"{name}.wav"
    if record_seconds and int(record_seconds) > 0:
        secs = max(5, min(int(record_seconds), 120))
        r = _run(["ffmpeg", "-hide_banner", "-y", "-f", "avfoundation", "-i", ":0",
                  "-t", str(secs), "-ar", "24000", "-ac", "1", str(raw)], timeout=secs + 30)
        if not (r.get("ok") and raw.exists()):
            return err(f"recording failed: {(r.get('err') or '')[-300:]}")
        src = raw
    else:
        src = Path(sample_path).expanduser()
        if not src.exists():
            return err(f"sample not found: {src}")
    af = ("highpass=f=70,lowpass=f=8000,afftdn=nf=-25,loudnorm,"
          "silenceremove=start_periods=1:start_silence=0.2:start_threshold=-40dB")
    r2 = _run(["ffmpeg", "-hide_banner", "-y", "-i", str(src), "-af", af,
               "-ar", "24000", "-ac", "1", str(dest)], timeout=120)
    if not (dest.exists() and dest.stat().st_size > 1000):
        return err(f"cleaning failed: {(r2.get('err') or '')[-300:]}")
    return ok(enrolled=name, sample=str(dest), size_bytes=dest.stat().st_size,
              hint=f"run compare_voices('{name}') to A/B all engines and pick the best")


@mcp.tool
@_guard
def compare_voices(name: str, text: str = "", language: str = "") -> dict:
    """Run a bake-off: synthesize one line through every installed engine using your enrolled voice
    reference, play each one back-to-back, and return paths + timings. Pick the winner with
    choose_voice(). Available engines depend on what's installed (chatterbox/xtts/kokoro/say)."""
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip())[:40]
    sample = VOICES_DIR / f"{name}.wav"
    if not sample.exists():
        return err(f"voice '{name}' not enrolled", hint=f"run enroll_voice('{name}') first")
    lang = (language or DEFAULT_LANGUAGE)[:2]
    line = (text or "Hi, this is my voice. Does it sound like me? Testing quality and naturalness.").strip()[:500]
    engines = _voice_engines()
    if not engines:
        return err("no synthesis engine installed", hint="uv sync --group voice")
    results = []
    for eng in engines:
        t0 = time.time()
        ok_e, path = _synthesize_to_file(line, lang, eng, sample_path=str(sample))
        elapsed = round(time.time() - t0, 1)
        if ok_e and _has("afplay"):
            _run(["afplay", path], timeout=60)
        results.append({"engine": eng, "ok": ok_e,
                        "path": path if ok_e else None,
                        "error": None if ok_e else path,
                        "synthesis_seconds": elapsed})
    return ok(name=name, text=line, language=lang, results=results,
              hint=f"pick the best: choose_voice('{name}', '<engine>')")


@mcp.tool
@_guard
def choose_voice(name: str, engine: str, language: str = "") -> dict:
    """Lock the winning engine as a persistent voice profile (JSON in VOICES_DIR). The profile is
    used automatically when you pass voice=name to speak/start_session/call_autopilot.
    engine: 'chatterbox' | 'xtts' | 'kokoro' | 'say'."""
    name = re.sub(r"[^a-zA-Z0-9_-]+", "_", (name or "").strip())[:40]
    if not name or not engine.strip():
        return err("name and engine are required")
    valid = ["chatterbox", "xtts", "kokoro", "say"]
    if engine not in valid:
        return err(f"engine must be one of {valid}")
    sample = VOICES_DIR / f"{name}.wav"
    if not sample.exists():
        return err(f"voice '{name}' not enrolled — run enroll_voice('{name}') first")
    _save_profile(name, engine, str(sample), language)
    return ok(chosen=name, engine=engine, sample=str(sample),
              language=language or DEFAULT_LANGUAGE,
              note=(f"voice '{name}' ({engine}) will be used when you pass voice='{name}' to "
                    "speak/start_session. Live sessions use kokoro streaming; chatterbox is used "
                    "for studio one-shots (speak/compare_voices)."))


# =========================================================================== REALTIME SESSIONS
@mcp.tool
@_guard
def start_session(mode: str = "standalone", language: str = "", voice: str = "", persona: str = "",
                  interpret_to: str = "", brain: str = "agent", device_in: str = "", device_out: str = "") -> dict:
    """Start a REALTIME duplex voice session (VAD-endpointed STT + streaming TTS with barge-in).
    mode: 'standalone' (your mic/speakers) | 'call' (BlackHole devices for a WhatsApp call). Then loop
    next_utterance()→compose reply→say_now(). interpret_to='<lang>' enables live translation. Returns
    {session_id}. Realtime needs Tier 1 (uv sync --group voice)."""
    if (e := _realtime_ready()):
        return e
    if mode == "call":
        ok_, reasons = _call_ready()
        if not ok_:
            return err("whatsapp-call audio is not routed", missing=reasons, hint="run setup_guide()")
    sid = f"sess_{dt.datetime.now():%H%M%S}_{len(_SESSIONS)}"
    lang = (language or DEFAULT_LANGUAGE).strip().lower()
    sess = _Session(sid, lang, voice, (interpret_to or "").strip().lower(),
                    device_in or (CALL_IN_DEVICE if mode == "call" else ""),
                    device_out or (CALL_OUT_DEVICE if mode == "call" else ""))
    try:
        sess.start()
    except Exception as e:  # noqa: BLE001
        return err(f"could not start realtime engine: {e}", hint="check the mic device + uv sync --group voice")
    with _SESS_LOCK:
        _SESSIONS[sid] = sess
    return ok(session_id=sid, mode=mode, language=lang, interpret_to=sess.interpret_to or None,
              voice=voice or None, brain=brain,
              note="loop: next_utterance(session_id) → compose a reply → say_now(session_id, text). stop_session when done.")


@mcp.tool
@_guard
def next_utterance(session_id: str, timeout: int = 30) -> dict:
    """Block until the other party finishes a spoken turn; return {text, language}. The agent then
    composes a reply (in persona/your voice, translated if interpreting) and calls say_now()."""
    with _SESS_LOCK:
        sess = _SESSIONS.get(session_id)
    if not sess:
        return not_found("session", session_id, list(_SESSIONS))
    if sess.error:
        return err(f"session error: {sess.error}")
    try:
        turn = sess.utterances.get(timeout=max(1, min(int(timeout), 300)))
    except queue.Empty:
        return ok(session_id=session_id, text="", idle=True, hint="no speech yet — call again or stop_session")
    return ok(session_id=session_id, text=turn["text"], language=turn.get("language") or sess.language)


@mcp.tool
@_guard
def say_now(session_id: str, text: str) -> dict:
    """Speak text into a live session — streamed and BARGE-IN-aware (cut off if they start talking)."""
    with _SESS_LOCK:
        sess = _SESSIONS.get(session_id)
    if not sess:
        return not_found("session", session_id, list(_SESSIONS))
    if not text.strip():
        return err("text is required")
    if len(text) > MAX_TEXT:
        return err(f"text too long (>{MAX_TEXT})")
    sess.say(text)
    return ok(session_id=session_id, said=len(text))


@mcp.tool
@_guard
def live_transcript(session_id: str, since: int = 0) -> dict:
    """The rolling transcript of a session (each turn: role them/me, text, language, timestamp)."""
    with _SESS_LOCK:
        sess = _SESSIONS.get(session_id)
    if not sess:
        return not_found("session", session_id, list(_SESSIONS))
    turns = sess.transcript[max(0, int(since)):]
    return ok(session_id=session_id, turns=turns, total=len(sess.transcript))


@mcp.tool
@_guard
def session_status(session_id: str = "") -> dict:
    """Status of one session (or all): listening/speaking, turn count, language, uptime, errors."""
    if not session_id:
        with _SESS_LOCK:
            return ok(sessions=[{"id": s.id, "turns": len(s.transcript), "speaking": s.speaking,
                                 "error": s.error} for s in _SESSIONS.values()], count=len(_SESSIONS))
    with _SESS_LOCK:
        sess = _SESSIONS.get(session_id)
    if not sess:
        return not_found("session", session_id, list(_SESSIONS))
    return ok(session_id=sess.id, language=sess.language, interpret_to=sess.interpret_to or None,
              speaking=sess.speaking, turns=len(sess.transcript), uptime_s=round(time.time() - sess.started, 1),
              error=sess.error)


@mcp.tool
@_guard
def stop_session(session_id: str) -> dict:
    """Stop a realtime session and free its audio devices. Returns the final transcript."""
    with _SESS_LOCK:
        sess = _SESSIONS.pop(session_id, None)
    if not sess:
        return not_found("session", session_id, list(_SESSIONS))
    sess.stop()
    return ok(session_id=session_id, turns=len(sess.transcript), transcript=sess.transcript)


# =========================================================================== WHATSAPP CALL BRIDGE
def _call_directive(contact: str, directive: str, language: str, persona: str,
                    interpret_to: str, max_turns: int) -> str:
    lang = language or DEFAULT_LANGUAGE
    interp = (f" Run in INTERPRETER mode: translate everything they say into your replies and speak "
              f"to them in '{interpret_to}'." if interpret_to else "")
    persona_s = f" Speak in this persona/voice: {persona}." if persona else ""
    stop = ", ".join(AUTOPILOT_STOPWORDS)
    return (
        f"Hold a LIVE SPOKEN WhatsApp call with {contact} in language '{lang}'.{persona_s}{interp}\n"
        f"GOAL: {directive or 'have a brief, friendly conversation'}.\n"
        "Steps: 1) voice.diagnose() — if not whatsapp_call_ready, STOP and report what's missing; do NOT "
        "pretend to talk. 2) whatsapp.start_call(contact) to ring; wait a few seconds to connect. "
        "3) voice.start_session(mode='call', language=lang, interpret_to=…) → session_id. 4) Greet with "
        "voice.say_now(session_id, <short opener>). 5) LOOP: voice.next_utterance(session_id) → understand/"
        "translate → voice.say_now(session_id, <reply>). Keep replies short and natural; barge-in is "
        f"automatic. STOP on a stopword [{stop}], after {max_turns} turns, or 3 idle returns. 6) "
        "voice.stop_session(session_id) then whatsapp.end_call(). Finally save a short summary to notes. "
        "Never make commitments or share sensitive info on the user's behalf."
    )


@mcp.tool
@_guard
def call_brief(contact: str, directive: str = "", language: str = "", persona: str = "",
               interpret_to: str = "", max_turns: int = 20) -> dict:
    """Return the ready-to-run directive + resolved devices for a hands-off voice call WITHOUT spawning
    (gate-checked). Run it yourself via background.run, or use call_autopilot to spawn it."""
    if not contact.strip():
        return err("contact is required")
    ok_, reasons = _call_ready()
    if not ok_:
        return err("whatsapp-call audio is not routed", missing=reasons, hint="run setup_guide()")
    return ok(contact=contact, directive=_call_directive(contact, directive, language, persona, interpret_to, max_turns),
              output_device=CALL_OUT_DEVICE, input_device=CALL_IN_DEVICE, tier=_tier())


@mcp.tool
@_guard
def call_autopilot(contact: str, directive: str = "", language: str = "", persona: str = "",
                   interpret_to: str = "", max_turns: int = 20) -> dict:
    """Hands-off: ring `contact` on WhatsApp and have the agent hold the whole SPOKEN call autonomously
    (greet → listen → reply with barge-in → hang up), via a background `claude -p` agent. Returns a
    job_id (poll job_status). Refuses if call audio isn't routed (run setup_guide()/diagnose())."""
    if not contact.strip():
        return err("contact is required")
    ok_, reasons = _call_ready()
    if not ok_:
        return err("whatsapp-call audio is not routed", missing=reasons, hint="run setup_guide()")
    exe = _claude_bin()
    if not exe:
        return ok(spawned=False, directive=_call_directive(contact, directive, language, persona, interpret_to, max_turns),
                  hint="`claude` CLI not found — run the returned directive via background.run() instead")
    task = _call_directive(contact, directive, language, persona, interpret_to, max_turns)

    def worker(job):
        cmd = [exe, "-p", task, "--permission-mode", "acceptEdits"]
        logf = TMP / f"{job['id']}.log"
        try:
            proc = subprocess.Popen(cmd, cwd=str(ROOT), stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True, stdin=subprocess.DEVNULL)
        except Exception as e:  # noqa: BLE001
            JOBS.finish(job["id"], ok_=False, error=f"could not launch claude: {e}")
            return
        JOBS.track_proc(job["id"], proc)
        out, _ = proc.communicate()
        JOBS.untrack_proc(job["id"])
        try:
            logf.write_text(out or "")
        except Exception:  # noqa: BLE001
            pass
        if proc.returncode == 0:
            JOBS.finish(job["id"], ok_=True, summary=(out or "").strip()[-3000:], log=str(logf))
        else:
            JOBS.finish(job["id"], ok_=False, error=f"call agent exited {proc.returncode}", log=str(logf))

    return JOBS.run_or_job("call", worker, inline_wait=4.0, contact=contact)


@mcp.tool
@_guard
def call_status(job_id: str) -> dict:
    """Status of a hands-off call_autopilot job."""
    return JOBS.status(job_id)


if __name__ == "__main__":
    mcp.run()
