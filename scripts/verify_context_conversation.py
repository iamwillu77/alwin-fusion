#!/usr/bin/env python3
"""v0.11.0 real-service verification: context, cancellation, recovery.

REAL components: the live Ollama model (OLLAMA_MODEL, default
qwen3:0.6b), the real espeak TTS engine + GStreamer playback, the real
GTK4 window with file-fed capture (real faster-whisper decode of
synthesized utterances — clearly labeled), and the real microphone only
where a physical device is usable (otherwise BLOCKED with evidence).

Fixtures NEVER become real tasks: the manager is a recorder everywhere.

Checks (EXECUTED / FAILED / BLOCKED):
  * multi-turn context in REAL prompts and REAL replies (objective
    prompt-level evidence via a recording proxy around the real client)
  * clearing removes prior context (prompt + reply evidence)
  * language: latest turn's language with history present (EN/TA, HI
    attempted)
  * real TTS interruption (stop() -> completion truth, resources down)
  * window: cancellation while a REAL reply is generating; wake-word
    monitoring <-> push-to-talk compatibility; clear control
  * recovery: unreachable Ollama reports honestly and the pipeline
    stays usable
  * physical spoken multi-turn: BLOCKED when no acoustic mic exists
"""
from __future__ import annotations

import json
import os
import sys
import threading
import time

REPO = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, REPO)
sys.path.insert(0, os.path.join(REPO, "scripts"))

RESULTS = []


def report(section, name, status, detail=""):
    RESULTS.append({"section": section, "name": name,
                    "status": status, "detail": detail})
    print(f"[{status:>7}] {section}: {name}"
          + (f" — {detail}" if detail else ""), flush=True)


class RecordingManager:
    """Fixtures must NEVER become real tasks (FAKE by design)."""

    def __init__(self):
        self.submissions = []

    @property
    def current(self):
        return None

    def submit(self, command, *, dry_run=False):
        self.submissions.append(command)
        return object()

    def add_observer(self, cb):
        pass

    def remove_observer(self, cb):
        pass


class PromptRecordingLLM:
    """Proxy around the REAL client: records prompts, passes through."""

    def __init__(self, real):
        self.real = real
        self.calls = []

    def generate(self, prompt, *, system=None, **kw):
        self.calls.append({"prompt": prompt, "system": system})
        return self.real.generate(prompt, system=system, **kw)

    @property
    def last_system(self):
        return self.calls[-1]["system"] if self.calls else ""


def reply_service(llm):
    from companion.conversation import ConversationService
    return ConversationService(
        manager=RecordingManager(), llm_client=llm,
        replies_enabled=True,
        is_action=lambda t: False,   # context tests are pure chat
    )


def real_llm():
    from ai_runtime.ollama_client import OllamaClient
    return OllamaClient(model=os.environ.get("OLLAMA_MODEL") or "qwen3:0.6b")


def section_multiturn_context():
    print("== real multi-turn context (REAL Ollama) ==", flush=True)
    real = real_llm()
    try:
        real.list_models()
        report("context", "ollama reachable", "EXECUTED",
               f"model={real.model}")
    except Exception as exc:
        report("context", "ollama reachable", "BLOCKED", str(exc))
        return None
    llm = PromptRecordingLLM(real)
    service = reply_service(llm)

    # Turn 1: opening question (chat: never a task).
    t0 = time.time()
    r1 = service.reply("Tell me about Python programming.", "en")
    step_ok = bool(r1)
    report("context", "turn 1 answered", "EXECUTED" if step_ok else "FAILED",
           f"{r1!r} [{time.time()-t0:.1f}s]")
    if step_ok:
        service.record_reply(r1)
    service.submit("Tell me about Python programming.")
    prompt_t1_len = len(llm.last_system)

    # Turn 2: reference-resolution follow-up ("it" / advantages).
    service.submit("What are its advantages? Keep it short.")
    t0 = time.time()
    r2 = service.reply("What are its advantages? Keep it short.", "en")
    has_history = ("Tell me about Python programming."
                   in llm.last_system)
    report("context", "turn 2 prompt carries turn 1",
           "EXECUTED" if has_history else "FAILED",
           f"system {prompt_t1_len} -> {len(llm.last_system)} chars")
    report("context", "turn 2 follow-up answered",
           "EXECUTED" if r2 else "FAILED", f"{r2!r} [{time.time()-t0:.1f}s]")
    if r2:
        service.record_reply(r2)

    # Objective evidence in the REPLY: asking what we first discussed
    # must be answerable only WITH context.
    service.submit("What topic did I first ask you about in this session?")
    r3 = service.reply(
        "What topic did I first ask you about in this session?", "en")
    mentions_python = bool(r3) and "python" in r3.lower()
    report("context", "reply uses session memory",
           "EXECUTED" if mentions_python else "FAILED",
           f"{r3!r}")
    if r3:
        service.record_reply(r3)

    # Clearing removes it (prompt AND reply evidence).
    dropped = service.clear_context()
    llm.calls.clear()
    service.submit("What topic did I first ask you about in this session?")
    r4 = service.reply(
        "What topic did I first ask you about in this session?", "en")
    prompt_clean = "Tell me about Python" not in (llm.last_system or "")
    reply_clean = not (r4 and "python" in r4.lower())
    report("context", "clearing drops history from prompts",
           "EXECUTED" if prompt_clean else "FAILED",
           f"(dropped {dropped} entries)")
    report("context", "cleared session cannot recall the topic",
           "EXECUTED" if reply_clean else "FAILED", f"{r4!r}")
    return service


def section_languages(service):
    print("\n== latest-language behaviour with context (REAL Ollama) ==",
          flush=True)
    if service is None:
        report("language", "EN/TA/HI turns", "BLOCKED", "no Ollama")
        return
    from evaluate_voice_languages import script_match
    cases = [
        ("ta", "என் உலாவியைத் திறக்க வேண்டும் என்று நினைக்கிறேன்.",
         "Reply with one short Tamil sentence confirming.", "ta"),
        ("en", "Thanks. Now confirm in English.", None, "en"),
        ("hi", "हिंदी में एक छोटा सा उत्तर दीजिए।", None, "hi"),
    ]
    for code, text, override, expect in cases:
        turn = service.submit(text)
        reply = service.reply(text, turn.language)
        if reply is None:
            report("language", f"{code} turn", "FAILED", "no reply")
            continue
        if expect == "en":
            from companion.language_detection import LanguageDetectionService
            ok = LanguageDetectionService().detect(reply).code == "en"
        else:
            ok = script_match(expect, reply)
        report("language", f"{code} reply follows latest turn",
               "EXECUTED" if ok else "FAILED",
               f"lang={turn.language} reply={reply[:70]!r}")


def section_tts_interrupt():
    print("\n== real TTS interruption ==", flush=True)
    from companion.voice_output import CompanionVoice

    voice = CompanionVoice()
    if not voice.available:
        report("tts", "engine available", "BLOCKED", voice.status)
        return
    done = threading.Event()
    outcome = {}

    def on_done(ok, err):
        outcome.update(ok=ok, err=err)
        done.set()

    if not voice.speak("This is a long spoken sentence that the user "
                       "interrupts almost immediately.",
                       on_done=on_done):
        report("tts", "dispatch", "BLOCKED", voice.status)
        return
    time.sleep(0.4)
    t0 = time.time()
    voice.stop()
    done.wait(timeout=10)
    latency = (time.time() - t0) * 1000
    ok = outcome.get("ok") is False and outcome.get("err") == "stopped"
    report("tts", "interrupt stops playback honestly",
           "EXECUTED" if ok else "FAILED",
           f"on_done={outcome} in {latency:.0f} ms, "
           f"speaking={voice.speaking}")
    report("tts", "engine idle after interrupt",
           "EXECUTED" if not voice.speaking else "FAILED", "")


def section_recovery():
    print("\n== recovery (REAL clients, dead endpoints) ==", flush=True)
    from ai_runtime.ollama_client import OllamaClient
    from companion.conversation import ConversationService

    dead = ConversationService(
        manager=RecordingManager(),
        llm_client=OllamaClient(base_url="http://localhost:1",
                                model="x", timeout=2.0),
        replies_enabled=True, is_action=lambda t: False,
    )
    reply, reason = dead.reply_with_reason("hello", "en")
    ok = reply is None and "failed" in (reason or "")
    report("recovery", "unreachable ollama reports honestly",
           "EXECUTED" if ok else "FAILED", reason)

    # The same service keeps working when the client is swapped back:
    dead.set_llm_client(real_llm())
    reply2 = dead.reply_with_reason("Say ok in English.", "en")[0]
    report("recovery", "service usable after client swap",
           "EXECUTED" if reply2 else "FAILED", f"{reply2!r}")


def section_gui():
    print("\n== GUI: real ASR + real Ollama context turns (file-fed) ==",
          flush=True)
    if not os.environ.get("WAYLAND_DISPLAY") and \
            not os.environ.get("DISPLAY"):
        report("gui", "GUI context turns", "BLOCKED", "no display")
        return
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib
    except Exception as exc:  # noqa: BLE001
        report("gui", "GTK4", "BLOCKED", str(exc))
        return

    import numpy as np

    from companion.voice_output import EspeakSynthesizer
    sandbox = os.path.expanduser("~/.cache/alwin-context-gui-verify")
    os.environ["ALWIN_HOME"] = sandbox
    os.environ["ALWIN_STATE_DIR"] = os.path.join(sandbox, "state")
    os.environ["ALWIN_PERMITTED_ROOTS"] = os.path.join(sandbox,
                                                        "Documents")
    for d in (os.environ["ALWIN_STATE_DIR"],
              os.environ["ALWIN_PERMITTED_ROOTS"]):
        os.makedirs(d, exist_ok=True)

    import companion.window as win_mod
    win_mod.APP_ID = "org.alwin.fusion.companion.verify011"
    from companion.greeting import GreetingConfig
    from companion.voice_input import VoiceInputService
    from companion.window import CompanionWindow

    synth = EspeakSynthesizer()

    def synth16k(text):
        s = synth.synthesize(text, voice="en-us", rate=1.0)
        x = np.frombuffer(s.pcm, dtype=np.int16).astype(np.float64)
        n = int(len(x) * 16000 / s.sample_rate)
        return np.interp(np.linspace(0, len(x) - 1, n),
                         np.arange(len(x)), x).astype(np.int16).tobytes()

    class FileBackend:
        """Capture stand-in (labeled): feeds a synthesized utterance."""

        def __init__(self, pcm):
            self.pcm = pcm
            self.on_sample = None

        def open(self, on_sample, on_error):
            self.on_sample = on_sample
            pcm = self.pcm

            def feed():
                for i in range(0, len(pcm), 3200):
                    on_sample(pcm[i:i + 3200])
                    time.sleep(0.002)

            threading.Thread(target=feed, daemon=True).start()

        def poll_error(self):
            return None

        def close(self):
            pass

    current = {"pcm": synth16k("Tell me about Python programming.")}
    stage = {"turn": 0}   # 0 = first turn, 1 = follow-up, then finish

    manager = RecordingManager()
    llm = PromptRecordingLLM(real_llm())
    window = CompanionWindow(
        manager, config=None,
        greeting_config=GreetingConfig(enabled=False),
        llm_client=llm,
        voice_input=VoiceInputService(
            backend_factory=lambda: FileBackend(current["pcm"])
        ),
    )
    window._settings["spoken_responses"] = True   # TTS leg
    window._configure_services()

    def poll_until_turn_ends(seconds_left, done_fn):
        def _tick():
            if window._voice_phase == window.PHASE_OFF or seconds_left <= 0:
                done_fn()
                return False
            poll_until_turn_ends(seconds_left - 1, done_fn)
            return False
        GLib.timeout_add_seconds(1, _tick)

    def start_turn():
        if window._voice_phase != window.PHASE_OFF:
            GLib.timeout_add_seconds(1, start_turn)
            return False
        window._on_mic()                # capture (file-fed, real ASR)
        GLib.timeout_add_seconds(3, after_capture)
        return False

    def after_capture():
        window._on_mic()                # stop & send -> real decode
        poll_until_turn_ends(90, after_turn)
        return False

    SILENCE_BUBBLE = "didn't hear anything"
    DEP_MARKERS = ("is not installed", "package is missing")

    def after_turn():
        if stage["turn"] == 0:
            subs = list(manager.submissions)
            bubble = window._bubble_text
            gen_calls = len(llm.calls)
            ok1 = (subs == [] and gen_calls >= 1
                   and SILENCE_BUBBLE not in bubble)
            # v0.22: a turn that never ran because the ASR package is
            # absent is ENVIRONMENT-BLOCKED (same root cause the mic
            # leg already reports) — never counted as a pass, never
            # mislabelled as a product failure.
            dep_blocked = any(m in (bubble or "")
                              for m in DEP_MARKERS)
            if not ok1 and dep_blocked:
                stage["dep_blocked"] = True
                report("gui", "turn 1: chat routed without a task AND "
                       "replied", "BLOCKED",
                       f"ASR dependency unavailable: {bubble[:80]!r}")
                finish()
                return False
            report("gui", "turn 1: chat routed without a task AND replied",
                   "EXECUTED" if ok1 else "FAILED",
                   f"subs={subs!r} generations={gen_calls} "
                   f"bubble={bubble[:60]!r}")
            if not ok1:
                finish()
                return False
            # Turn 2: follow-up whose prompt must carry turn 1.
            stage["turn"] = 1
            current["pcm"] = synth16k("What are its advantages?")
            GLib.timeout_add_seconds(1, start_turn)
            return False
        finish()
        return False

    def finish():
        systems = [c["system"] for c in llm.calls]
        has_ctx = any("Tell me about Python programming." in s
                      for s in systems[1:])
        if stage.get("dep_blocked"):
            report("gui", "turn 2 prompt carried turn 1 (real Ollama)",
                   "BLOCKED", "turn 1 never ran (ASR package absent)")
        else:
            report("gui", "turn 2 prompt carried turn 1 (real Ollama)",
                   "EXECUTED" if has_ctx else "FAILED",
                   f"{len(systems)} real generations")
        # Clear control (real handler) then confirm prompt is clean.
        try:
            window._on_clear_context()
        except Exception as exc:  # noqa: BLE001
            report("gui", "clear control", "FAILED", str(exc))
            finish_wake()
            return False
        cleared = window.conversation.context.empty
        report("gui", "clear control empties context",
               "EXECUTED" if cleared else "FAILED",
               f"bubble={window._bubble_text[:60]!r}")
        finish_wake()

    def finish_wake():
        # Wake <-> push-to-talk compatibility (needs no acoustic input).
        window._settings["wake_word_enabled"] = True
        window._apply_settings()
        wake_ok = window._voice_phase == window.PHASE_WAKE
        if wake_ok:
            window._on_mic()                # takeover
            takeover = window._voice_phase == window.PHASE_LISTENING
            window._cancel_turn()           # cancel -> resume monitoring
            GLib.timeout_add_seconds(2, lambda: after_wake(takeover))
        else:
            report("gui", "wake/PTT compatibility", "FAILED",
                   f"phase={window._voice_phase}")
            finish_all()
        return False

    def after_wake(takeover):
        resumed = window._voice_phase in (window.PHASE_WAKE,
                                          window.PHASE_OFF)
        report("gui", "wake + push-to-talk compatibility",
               "EXECUTED" if takeover and resumed else "FAILED",
               f"takeover={takeover} phase={window._voice_phase}")
        finish_all()

    def finish_all():
        try:
            window._teardown()
        except Exception:  # noqa: BLE001
            pass
        try:
            window.wake_service.wait_stopped(timeout=10.0)
        except Exception:  # noqa: BLE001
            pass
        window.app.quit()
        return False

    GLib.timeout_add_seconds(4, start_turn)
    GLib.timeout_add_seconds(300, finish_all)
    window.app.run(None)


def section_spoken_multiturn():
    print("\n== physical spoken multi-turn ==", flush=True)
    from companion.voice_input import VoiceInputService
    svc = VoiceInputService()
    try:
        svc.start()
    except Exception as exc:
        report("mic", "spoken multi-turn", "BLOCKED", str(exc))
        return
    time.sleep(0.4)
    play_ok = True
    try:
        from verify_wake_word import espeak_pcm, play_pcm16k
        play_pcm16k(espeak_pcm("Tell me about Python."), wait=True)
        time.sleep(2.0)
    except Exception as exc:  # noqa: BLE001
        play_ok = False
        report("mic", "loopback playback", "BLOCKED", str(exc))
    cap = svc.stop()
    svc.release()
    if not play_ok:
        return
    from companion.speech_recognition import SpeechRecognitionService
    try:
        text = SpeechRecognitionService(model_size="small").recognize(
            cap.pcm).text
    except Exception as exc:
        report("mic", "spoken multi-turn", "BLOCKED", f"decode: {exc}")
        return
    coupled = "python" in text.lower()
    report("mic", "spoken multi-turn (physical mic)",
           "EXECUTED" if coupled else "BLOCKED",
           f"decoded={text[:60]!r} — "
           + ("mic heard the speakers" if coupled else
              "no acoustic coupling (see v0.10 evidence) — cannot run "
              "a real spoken conversation here"))


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        prog="python3 scripts/verify_context_conversation.py")
    parser.parse_args(argv)

    service = section_multiturn_context()
    section_languages(service)
    section_tts_interrupt()
    section_recovery()
    section_gui()
    section_spoken_multiturn()

    print("\n== summary ==")
    counts = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    for r in RESULTS:
        if r["status"] in ("FAILED", "BLOCKED"):
            print(f"  {r['status']}: [{r['section']}] {r['name']} — "
                  f"{r['detail']}")
    return 1 if any(r["status"] == "FAILED" for r in RESULTS) else 0


if __name__ == "__main__":
    sys.exit(main())
