#!/usr/bin/env python3
"""Phase 9 real-hardware verification for ALWIN FUSION v0.9.1.

Runs against REAL services only: real faster-whisper 'small', real
langdetect, real live Ollama (qwen3:0.6b), real espeak-ng synthesis and
real GStreamer playback, plus the real microphone for capture tests.

Honest substitution: the "speech" in Tests A–D is REAL HUMAN speech
(google/fleurs, CC-BY-4.0 sample files) fed at the capture boundary,
because no human is present to speak into the microphone; the real
microphone is exercised for capture mechanics and recovery (Test E)
and in the GUI check. Every step prints exactly what happened.
"""
import json, os, sys, time, threading

REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from evaluate_voice_languages import read_wav_mono16k, script_match  # noqa: E402

SAMPLES = os.path.expanduser("~/.cache/alwin-fleur-samples")

from companion.speech_recognition import SpeechRecognitionService  # noqa
from companion.language_detection import LanguageDetectionService  # noqa
from companion.conversation import ConversationService  # noqa
from companion.voice_input import VoiceInputService  # noqa
from companion.voice_output import CompanionVoice  # noqa
from companion.voice import NullVoice  # noqa
from ai_runtime.ollama_client import OllamaClient  # noqa

STEP = []


def log(msg):
    print(f"[verify] {msg}", flush=True)


def step(ok, name, detail=""):
    STEP.append({"ok": bool(ok), "name": name, "detail": detail})
    print(f"  {'ok  ' if ok else 'FAIL'} {name}" +
          (f" — {detail}" if detail else ""), flush=True)


meta = {}
with open(os.path.join(SAMPLES, "meta.jsonl"), encoding="utf-8") as fh:
    for line in fh:
        row = json.loads(line)
        meta.setdefault(row["lang"], []).append(row)

asr = SpeechRecognitionService(model_size="small")
detector = LanguageDetectionService()
llm = OllamaClient(model=os.environ.get("OLLAMA_MODEL") or "qwen3:0.6b")
mgr_submissions = []


class RecordingManager:
    def submit(self, command, *, dry_run=False):
        mgr_submissions.append(command)
        return object()


conv = ConversationService(manager=RecordingManager(), llm_client=llm,
                           replies_enabled=True)
voice = CompanionVoice()

log(f"ollama: {llm.base_url} model={llm.model}")
try:
    models = llm.list_models()
    names = [m if isinstance(m, str) else m.get("name")
             for m in models]
    step(True, "ollama reachable", f"models: {names}")
except Exception as exc:
    step(False, "ollama reachable", str(exc))
    models = []

# ------------------------------------------------------------------ #
# Tests A/B/C: English, Tamil, Hindi end-to-end
# ------------------------------------------------------------------ #
LANG_TESTS = [("en", "Test A English"), ("ta", "Test B Tamil"),
              ("hi", "Test C Hindi")]
e2e = {}

for code, label in LANG_TESTS:
    print(f"\n== {label} ==")
    sample = meta[code][0]
    pcm = read_wav_mono16k(sample["file"])
    t0 = time.time()
    result = asr.recognize(pcm, language=None)
    step(bool(result.text.strip()), "transcript produced",
         f"{result.text!r} [{time.time()-t0:.1f}s]")
    det = detector.detect(result.text, asr_hint=result.asr_hint,
                          hint_confidence=result.language_probability)
    log(f"detected: {det.code} (conf {det.confidence:.2f}, "
        f"source={det.source}, uncertain={det.uncertain})")
    turn = conv.submit(result.text, source="voice", asr_hint=result.asr_hint,
                       hint_confidence=result.language_probability)
    step(turn.ok, "submitted through the shared pipeline",
         f"language={turn.language}")
    t0 = time.time()
    reply, reason = conv.reply_with_reason(result.text, turn.language)
    step(bool(reply), "ollama replied",
         (reply[:80] if reply else f"reason: {reason}") +
         f" [{time.time()-t0:.1f}s]")
    lang_ok = None
    if reply:
        if code == "en":
            d2 = detector.detect(reply)
            lang_ok = d2.code == "en"
            step(lang_ok, "reply language matches", f"detected {d2.code}")
        else:
            lang_ok = script_match(code, reply)
            step(lang_ok, "reply uses the expected script",
                 f"{reply[:60]!r}")
        done = threading.Event()
        outcome = {}

        def on_done(ok, err, _o=outcome, _d=done):
            _o["ok"], _o["err"] = ok, err
            _d.set()

        dispatched = voice.speak(reply, on_done=on_done)
        if dispatched:
            done.wait(timeout=60)
            step(outcome.get("ok") is True, "speech playback completed",
                 str(outcome))
        else:
            step(False, "speech playback completed", voice.status)
    e2e[code] = {"transcript": result.text, "reply": reply,
                 "lang_ok": lang_ok}

# ------------------------------------------------------------------ #
# Test D: language switching across three consecutive turns
# ------------------------------------------------------------------ #
print("\n== Test D language switching (en -> ta -> hi) ==")
switch_ok = True
for code in ("en", "ta", "hi"):
    sample = meta[code][1] if len(meta[code]) > 1 else meta[code][0]
    result = asr.recognize(read_wav_mono16k(sample["file"]), language=None)
    turn = conv.submit(result.text, source="voice", asr_hint=result.asr_hint,
                       hint_confidence=result.language_probability)
    reply, reason = conv.reply_with_reason(result.text, turn.language)
    if reply and code == "en":
        ok = detector.detect(reply).code == "en"
    else:
        ok = bool(reply) and script_match(code, reply)
    step(ok, f"turn {code}: language={turn.language}",
         (reply[:60] if reply else f"no reply: {reason}"))
    switch_ok = switch_ok and ok
step(switch_ok, "each reply follows the latest message language")

# ------------------------------------------------------------------ #
# Test E: recovery — silence, ollama down, missing TTS, cancellation
# ------------------------------------------------------------------ #
print("\n== Test E recovery ==")
# silence: real mic, real room
svc = VoiceInputService()
svc.start()
time.sleep(2.5)
cap = svc.stop()
step(cap.seconds > 2.0, "real mic captured audio",
     f"{cap.seconds:.1f}s peak={cap.peak:.3f}")
amb = asr.recognize(cap.pcm, language=None) if cap.pcm else None
step(amb is None or amb.no_speech or not amb.text.strip(),
     "ambient room audio yields no fake transcript",
     f"no_speech={getattr(amb, 'no_speech', None)} "
     f"text={getattr(amb, 'text', None)!r}")
svc.release()

# ollama unavailable: real client, dead endpoint
dead = OllamaClient(base_url="http://localhost:1", model="x", timeout=2.0)
conv2 = ConversationService(manager=RecordingManager(), llm_client=dead,
                            replies_enabled=True)
reply, reason = conv2.reply_with_reason("hello", "en")
step(reply is None and "failed" in (reason or ""),
     "unreachable ollama reports an honest failure", reason)

# mic still usable after that failure
svc2 = VoiceInputService()
svc2.start()
step(svc2.listening, "microphone usable after the failure")
svc2.cancel()

# missing TTS voice/engine: NullVoice never claims speech
nv = NullVoice()
step(nv.speak("hello") is False, "missing TTS reports honestly")

# cancellation: real in-flight recognition dropped
blocked_pcm = read_wav_mono16k(meta["hi"][0]["file"])
started = asr.recognize_async(blocked_pcm, language=None,
                              on_result=lambda r: None,
                              on_error=lambda e: None)
step(started, "recognition started for the cancel test")
asr.cancel()
time.sleep(0.3)
step(not asr.busy, "cancelled recognition unwound (no zombie worker)")
svc3 = VoiceInputService()
svc3.start()
time.sleep(0.4)
discarded = svc3.cancel()
step(discarded.cancelled and discarded.pcm == b"",
     "cancelled capture discards audio for good")
svc3.release()

# ------------------------------------------------------------------ #
print("\n== SUMMARY ==")
fails = [s for s in STEP if not s["ok"]]
print(f"{len(STEP)} checks, {len(fails)} failures")
for s in fails:
    print(f"  FAILED: {s['name']} — {s['detail']}")
print(json.dumps(e2e, ensure_ascii=False, indent=1)[:1200])
sys.exit(1 if fails else 0)
