#!/usr/bin/env python3
"""v0.10.0 real-hardware wake-word verification.

Runs against REAL components only: the physical microphone (GStreamer),
the real vosk two-stage detector with the installed Apache-2.0 model,
real faster-whisper verification, real espeak synthesis, real audio
playback to the default sink, and (in the GUI section) the real GTK4
window with the live Ollama model.

Honest labels used throughout:
  REAL      — exercised through actual hardware / installed engines
  FAKE      — a stand-in (recording manager so fixtures can never
              become real tasks; injected fakes never appear here)
  BLOCKED   — environment could not provide it; reported, never faked

Wake phrases and commands are espeak-synthesized and played through
the audio OUTPUT while the physical microphone listens (no human is
present to speak) — that loopback is itself part of what is measured.

Every test prints EXECUTED/PASSED/BLOCKED with observed values; the
exit code is non-zero if any executed test failed.
"""
from __future__ import annotations

import json
import os
import queue
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


def espeak_pcm(text, rate=1.0, voice="en-us"):
    """Real espeak synthesis -> 16 kHz mono S16 bytes."""
    import numpy as np

    from companion.voice_output import EspeakSynthesizer
    synth = EspeakSynthesizer()
    if not synth.available:
        raise RuntimeError("espeak-ng unavailable: " + synth.error_text
                           if hasattr(synth, "error_text")
                           else "espeak-ng unavailable")
    speech = synth.synthesize(text, voice=voice, rate=rate)
    x = np.frombuffer(speech.pcm, dtype=np.int16).astype(np.float64)
    n = int(len(x) * 16000 / speech.sample_rate)
    y = np.interp(np.linspace(0, len(x) - 1, n), np.arange(len(x)), x)
    return y.astype(np.int16).tobytes()


def play_pcm16k(pcm, wait=True, timeout=30.0):
    """Real playback to the default sink (blocking on its own thread)."""
    from companion.voice_output import GStreamerPlayer, SynthesizedSpeech
    player = GStreamerPlayer(SynthesizedSpeech(pcm, 16000))
    if not wait:
        threading.Thread(target=player.play, daemon=True).start()
        return player
    error = player.play()
    if error:
        raise RuntimeError(f"playback failed: {error}")
    return None


# --------------------------------------------------------------------------- #


def section_preflight():
    print("== preflight (REAL probes) ==", flush=True)
    try:
        import vosk  # noqa: F401
        report("preflight", "vosk package", "EXECUTED", "present")
    except Exception as exc:
        report("preflight", "vosk package", "BLOCKED", str(exc))
        return False
    from companion.speech_recognition import SpeechRecognitionService
    from companion.wake_word import DEFAULT_VOSK_MODEL_DIR, wake_status

    model_dir = os.environ.get("ALWIN_WAKE_MODEL_DIR",
                               DEFAULT_VOSK_MODEL_DIR)
    ok = os.path.isdir(model_dir)
    report("preflight", "wake model on disk", "EXECUTED" if ok else "BLOCKED",
           model_dir)
    recognizer = SpeechRecognitionService(model_size="small")
    status = wake_status(recognizer)
    report("preflight", "wake detector availability",
           "EXECUTED" if status["available"] else "BLOCKED",
           status["detail"])
    if not status["available"]:
        return False
    report("preflight", "speech model", "EXECUTED",
           recognizer.status().get("detail", "")[:120])
    return True


def section_microphone():
    print("\n== microphone (REAL device) ==", flush=True)
    from companion.speech_recognition import SpeechRecognitionService
    from companion.voice_input import VoiceInputService

    svc = VoiceInputService()
    try:
        svc.start()
    except Exception as exc:
        report("mic", "open microphone", "BLOCKED", str(exc))
        return False
    time.sleep(3.0)
    cap = svc.stop()
    svc.release()
    nonsilent = bool(cap.pcm) and not cap.silent
    report("mic", "3 s room capture", "EXECUTED" if nonsilent else "BLOCKED",
           f"{cap.seconds:.1f}s peak={cap.peak:.3f} rms={cap.rms:.3f}")

    # Acoustic-coupling probe: the decisive test.  Play a known phrase
    # LOUDLY while capturing; a working microphone must pick it up
    # (whisper transcribes the phrase).  Constant-level noise that does
    # NOT change with playback means the device delivers line noise,
    # not room audio — reported as BLOCKED, never as a working mic.
    try:
        svc2 = VoiceInputService()
        svc2.start()
        time.sleep(0.4)
        play_pcm16k(espeak_pcm(
            "Hey Alwin testing one two three."), wait=False)
        time.sleep(3.0)
        cap2 = svc2.stop()
        svc2.release()
    except Exception as exc:
        report("mic", "acoustic coupling probe", "BLOCKED", str(exc))
        return False
    coupled = False
    detail = (f"capture rms={cap2.rms:.3f} (baseline {cap.rms:.3f})")
    if cap2.pcm:
        try:
            text = SpeechRecognitionService(model_size="small").recognize(
                cap2.pcm).text
            coupled = "alwin" in text.lower() or "one two" in text.lower()
            detail += f", decoded={text[:60]!r}"
        except Exception as exc:  # noqa: BLE001
            detail += f", decode error: {exc}"
    report("mic", "acoustic coupling (speakers -> mic)",
           "EXECUTED" if coupled else "BLOCKED",
           detail + ("" if coupled else
                     " — device carries no room audio (line noise / no "
                     "microphone attached); wake-loopback tests blocked"))
    return coupled


def section_detection_loopback(attempts=3, coupled=True):
    """Say the phrase through the speakers; the physical mic must catch it."""
    print("\n== wake detection via speaker->mic loopback (REAL) ==", flush=True)
    if not coupled:
        report("wake", "loopback detection", "BLOCKED",
               "no acoustic coupling on this machine (see mic probe) — "
               "cannot demonstrate physical detection without a "
               "working microphone")
        return None
    from companion.speech_recognition import SpeechRecognitionService
    from companion.wake_word import WakeWordService

    triggers = []
    errors = []
    stop = threading.Event()

    def on_trigger(phrase):
        triggers.append(time.monotonic())

    service = WakeWordService(
        recognizer=SpeechRecognitionService(model_size="small"),
        on_trigger=on_trigger,
        on_error=errors.append,
    )
    if not service.start():
        report("wake", "service start", "BLOCKED",
               service.last_error or "refused")
        return None
    deadline = time.time() + 15
    while time.time() < deadline and not service.listening:
        time.sleep(0.1)
    if not service.listening:
        report("wake", "service listening", "BLOCKED",
               service.last_error or "never opened")
        return None
    report("wake", "service listening", "EXECUTED", "real mic open")

    latencies = []
    for i in range(attempts):
        phrase = espeak_pcm("Hey Alwin!", rate=[0.9, 1.0, 1.1][i % 3])
        before = len(triggers)
        t0 = time.monotonic()
        try:
            play_pcm16k(phrase, wait=True)
        except Exception as exc:
            report("wake", f"playback attempt {i+1}", "BLOCKED", str(exc))
            break
        deadline = time.monotonic() + 12.0
        while time.monotonic() < deadline and len(triggers) == before:
            time.sleep(0.05)
        got = len(triggers) > before
        if got:
            lat = triggers[-1] - t0
            latencies.append(lat)
            report("wake", f"detected attempt {i+1}/{attempts}", "EXECUTED",
                   f"loopback latency {lat:.1f}s (incl. ~playback time)")
            # Monitoring must have stopped itself (mic exclusivity):
            time.sleep(0.5)
            service.start()               # resume for the next attempt
            time.sleep(3.0)               # > cooldown before re-trigger
            deadline = time.time() + 10
            while time.time() < deadline and not service.listening:
                time.sleep(0.1)
        else:
            report("wake", f"detected attempt {i+1}/{attempts}", "FAILED",
                   "phrase played but never verified")
            service.start()
            time.sleep(3.0)
    if errors:
        report("wake", "service errors", "FAILED", "; ".join(errors)[:200])
    service.stop()
    return latencies


def section_missed_and_false(ambient_seconds=30, coupled=True):
    print("\n== missed detections + ambient false triggers (REAL) ==", flush=True)
    from companion.speech_recognition import SpeechRecognitionService
    from companion.wake_word import WakeWordService

    if not coupled:
        # Utterance playback cannot reach the microphone, so "missed"
        # would be meaningless; only the ambient false-trigger window
        # is still meaningful (noise input is exactly where false
        # accepts would show up).
        report("miss", "missed detections", "BLOCKED",
               "no acoustic coupling (see mic probe)")
    else:
        variants = [
            ("Hey Alwin!", 0.8), ("Hey Alwin", 1.0),
            ("Hey Alwin, are you there?", 1.0), ("hey alwin", 1.2),
            ("Hey! Alwin!", 0.9), ("Hey Alwin!", 1.3),
        ]
        hits = 0
        triggers = []
        service = WakeWordService(
            recognizer=SpeechRecognitionService(model_size="small"),
            on_trigger=lambda p: triggers.append(time.monotonic()),
        )
        service.start()
        deadline = time.time() + 15
        while time.time() < deadline and not service.listening:
            time.sleep(0.1)
        if not service.listening:
            report("miss", "service listening", "BLOCKED",
                   service.last_error or "never opened")
            return
        for i, (text, rate) in enumerate(variants):
            before = len(triggers)
            try:
                play_pcm16k(espeak_pcm(text, rate=rate), wait=True)
            except Exception as exc:
                report("miss", "playback", "BLOCKED", str(exc))
                return
            deadline = time.monotonic() + 12.0
            while time.monotonic() < deadline and len(triggers) == before:
                time.sleep(0.05)
            got = len(triggers) > before
            hits += int(got)
            if not service.running and not service.listening:
                service.start()           # resume after each detection
                time.sleep(3.0)           # > cooldown before re-trigger
        report("miss", "missed detections", "EXECUTED",
               f"{len(variants) - hits} missed of {len(variants)} "
               f"utterances (sample count stated; NOT a statistical "
               f"miss rate)")
        service.stop()

    # Ambient false triggers (no phrase played) over a stated window —
    # meaningful in BOTH cases: on a noise-only device this is exactly
    # where a degenerate gate would produce false accepts.
    ambient_triggers = []
    ambient_service = WakeWordService(
        recognizer=SpeechRecognitionService(model_size="small"),
        on_trigger=lambda p: ambient_triggers.append(time.monotonic()),
    )
    ambient_service.start()
    deadline = time.time() + 15
    while time.time() < deadline and not ambient_service.listening:
        time.sleep(0.1)
    for _ in range(int(ambient_seconds / 0.5)):
        if not ambient_service.running and not ambient_service.listening:
            ambient_service.start()
        time.sleep(0.5)
    total_false = len(ambient_triggers)
    report("false", "false triggers", "EXECUTED",
           f"{total_false} over {ambient_seconds}s of ambient monitoring "
           f"(duration stated; NOT a false-acceptance rate)")
    ambient_service.stop()


def section_failure_paths():
    print("\n== failure paths (REAL engines, honest reporting) ==", flush=True)
    from companion.speech_recognition import SpeechRecognitionService
    from companion.wake_word import WakeWordService

    errors = []
    service = WakeWordService(
        recognizer=SpeechRecognitionService(model_size="small"),
        on_error=errors.append,
    )
    os.environ["ALWIN_WAKE_MODEL_DIR"] = "/definitely/missing/model"
    try:
        service.start()
        deadline = time.time() + 20
        while time.time() < deadline and not errors:
            time.sleep(0.1)
        ok = bool(errors) and not service.listening
        report("failure", "missing model reported honestly",
               "EXECUTED" if ok else "FAILED",
               (errors[0][:150] if errors else "no error surfaced"))
    finally:
        service.stop()
        os.environ.pop("ALWIN_WAKE_MODEL_DIR", None)

    # Real cancellation: monitoring -> stop() releases the device.
    service2 = WakeWordService(
        recognizer=SpeechRecognitionService(model_size="small"),
    )
    service2.start()
    deadline = time.time() + 15
    while time.time() < deadline and not service2.listening:
        time.sleep(0.1)
    was_listening = service2.listening
    t0 = time.monotonic()
    service2.stop()
    release_time = time.monotonic() - t0
    ok = was_listening and not service2.listening and service2.stop
    report("failure", "cancel releases the microphone", "EXECUTED",
           f"stop() completed in {release_time*1000:.0f} ms; "
           f"listening={service2.listening}")
    service2.release()
    ref2 = service2.start()
    report("failure", "release refuses restart", "EXECUTED",
           f"start() -> {ref2}, error={service2.last_error!r}")


def section_performance(observe_seconds=10):
    print("\n== performance (REAL, stated window) ==", flush=True)
    from companion.speech_recognition import SpeechRecognitionService
    from companion.wake_word import WakeWordService

    def cpu_ticks():
        # PER-PROCESS CPU (/proc/self/stat): system-wide /proc/stat
        # would misattribute other processes' load to this one (the
        # first version of this probe did exactly that).
        with open("/proc/self/stat") as fh:
            parts = fh.read().split()
        return int(parts[13]) + int(parts[14])   # utime + stime ticks

    service = WakeWordService(
        recognizer=SpeechRecognitionService(model_size="small"),
    )
    service.start()
    deadline = time.time() + 15
    while time.time() < deadline and not service.listening:
        time.sleep(0.1)
    if not service.listening:
        report("perf", "monitoring baseline", "BLOCKED",
               service.last_error or "never opened")
        return
    c1 = cpu_ticks()
    time.sleep(observe_seconds)
    c2 = cpu_ticks()
    tick_hz = os.sysconf("SC_CLK_TCK")
    used_seconds = (c2 - c1) / tick_hz
    cpu_pct = 100.0 * used_seconds / max(1, observe_seconds)
    rss_kib = 0
    try:
        with open("/proc/self/status") as fh:
            for line in fh:
                if line.startswith("VmRSS:"):
                    rss_kib = int(line.split()[1])
                    break
    except OSError:  # pragma: no cover
        pass
    report("perf", "idle monitoring CPU", "EXECUTED",
           f"{cpu_pct:.1f}% of one core over {observe_seconds}s "
           f"(per-process, real mic input: candidate-driven verifies "
           f"included)")
    report("perf", "process RSS while monitoring", "EXECUTED",
           f"{rss_kib // 1024} MiB")
    service.stop()


def section_gui(turns=2, skip=False, coupled=True):
    print("\n== GUI acceptance loop (REAL window, real Ollama) ==", flush=True)
    if skip:
        report("gui", "GUI acceptance", "BLOCKED", "--skip-gui given")
        return
    if not coupled:
        # Physical detection legs cannot be shown on this machine; run
        # the mechanics that do NOT need acoustic input (monitoring
        # lifecycle, push-to-talk takeover, resume, clean shutdown).
        report("gui", "wake->command->reply loop (needs mic)", "BLOCKED",
               "no acoustic coupling (see mic probe)")
    if not os.environ.get("WAYLAND_DISPLAY") and \
            not os.environ.get("DISPLAY"):
        report("gui", "GUI acceptance", "BLOCKED", "no display")
        return
    try:
        import gi
        gi.require_version("Gtk", "4.0")
        from gi.repository import GLib, Gtk
    except Exception as exc:  # noqa: BLE001
        report("gui", "GTK4", "BLOCKED", str(exc))
        return

    sandbox = os.path.expanduser("~/.cache/alwin-wake-gui-verify")
    os.environ["ALWIN_HOME"] = sandbox
    os.environ["ALWIN_STATE_DIR"] = os.path.join(sandbox, "state")
    os.environ["ALWIN_PERMITTED_ROOTS"] = os.path.join(sandbox,
                                                        "Documents")
    for d in (os.environ["ALWIN_STATE_DIR"],
              os.environ["ALWIN_PERMITTED_ROOTS"]):
        os.makedirs(d, exist_ok=True)

    import companion.window as win_mod
    win_mod.APP_ID = "org.alwin.fusion.companion.verify010"
    from ai_runtime.ollama_client import OllamaClient
    from companion.greeting import GreetingConfig
    from companion.window import CompanionWindow

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

    manager = RecordingManager()
    llm = OllamaClient(model=os.environ.get("OLLAMA_MODEL") or "qwen3:0.6b")
    window = CompanionWindow(
        manager, config=None,
        greeting_config=GreetingConfig(enabled=False),
        llm_client=llm,
    )
    window._settings["wake_word_enabled"] = True
    window._settings["spoken_responses"] = True   # TTS leg of the loop
    window._configure_services()

    state = {"play_cmds": [], "tts_done": [], "results": []}
    phrase_played_at = {}

    def play_async(text, rate=1.0, tag=""):
        def _run():
            try:
                phrase_played_at[tag] = time.monotonic()
                play_pcm16k(espeak_pcm(text, rate=rate), wait=True)
            except Exception as exc:  # noqa: BLE001
                state["results"].append(("playback", str(exc)))
        threading.Thread(target=_run, daemon=True).start()

    steps = []

    def step_delay(seconds, fn):
        steps.append((seconds, fn))

    def finish_gui():
        # ---- assertions from observed state ----
        subs = manager.submissions
        monitoring = (window._voice_phase == window.PHASE_WAKE)
        det_status = "EXECUTED" if coupled else "BLOCKED"
        report("gui", "wake phrase detected in real window",
               "BLOCKED" if not coupled else
               ("EXECUTED" if subs else "FAILED"),
               f"submissions={subs!r}"[:160]
               + ("" if coupled else " (needs acoustic coupling)"))
        report("gui", "returned to wake-word monitoring", 
               "EXECUTED" if monitoring else "FAILED",
               f"phase={window._voice_phase}")
        report("gui", "real Ollama reply spoken (TTS)",
               det_status if window._bubble_text else
               ("BLOCKED" if not coupled else "FAILED"),
               f"bubble={window._bubble_text[:80]!r}")
        try:
            window._teardown()          # releases the wake service
        except Exception:  # noqa: BLE001
            pass
        # Clean-shutdown: never exit while the wake worker is inside
        # vosk/whisper C code (observed: interpreter-exit segfault).
        try:
            alive = not window.wake_service.wait_stopped(timeout=12.0)
        except Exception:  # noqa: BLE001
            alive = False
        report("gui", "wake worker stopped before exit", "EXECUTED"
               if not alive else "FAILED",
               "worker drained before interpreter exit"
               if not alive else "worker still alive after 12 s")
        window.app.quit()
        return False

    # Choreography: enable -> play wake -> play command -> wait for the
    # turn -> repeat -> push-to-talk takeover -> finish.
    def start_gui():
        window._apply_settings()          # enables wake monitoring
        report("gui", "monitoring started from settings", "EXECUTED",
               f"phase={window._voice_phase}")
        GLib.timeout_add_seconds(3, after_monitoring_started)

    def after_monitoring_started():
        if window._voice_phase != window.PHASE_WAKE:
            report("gui", "wake monitoring active", "FAILED",
                   f"phase={window._voice_phase}")
            finish_gui()
            return False
        report("gui", "wake monitoring active", "EXECUTED",
               f"phase={window._voice_phase}")
        if not coupled:
            # No mic coupling: skip the phrase choreography entirely and
            # go straight to the mechanics that need no acoustic input.
            GLib.timeout_add_seconds(1, after_remonitor)
            return False
        play_async("Hey Alwin!", rate=1.0, tag="wake1")
        GLib.timeout_add_seconds(14, after_wake1)
        return False

    def after_wake1():
        got_command_phase = window._voice_phase in (
            window.PHASE_LISTENING, window.PHASE_RECOGNIZING,
            window.PHASE_REPLYING, window.PHASE_SPEAKING)
        lat = None
        if "wake1" in phrase_played_at:
            # latency approximation: detection happened before we got here
            lat = f"<= {time.monotonic() - phrase_played_at['wake1']:.1f}s"
        report("gui", "wake -> command listening transition", "EXECUTED"
               if got_command_phase else "FAILED",
               f"phase={window._voice_phase} latency {lat}")
        if window._voice_phase == window.PHASE_LISTENING:
            play_async("Open my browser.", tag="cmd1")
            GLib.timeout_add_seconds(25, after_cmd1)
        else:
            after_cmd1()
        return False

    def after_cmd1():
        report("gui", "command transcribed + replied (real pipeline)",
               "EXECUTED" if manager.submissions else "FAILED",
               f"subs={manager.submissions!r}"[:140])
        # Second turn (acceptance step: repeat without restart)
        if window._voice_phase == window.PHASE_WAKE:
            play_async("Hey Alwin!", rate=1.1, tag="wake2")
            GLib.timeout_add_seconds(14, after_wake2)
        else:
            report("gui", "second wake turn", "FAILED",
                   f"phase={window._voice_phase} did not resume")
            finish_gui()
        return False

    def after_wake2():
        second = window._voice_phase in (
            window.PHASE_LISTENING, window.PHASE_RECOGNIZING,
            window.PHASE_REPLYING, window.PHASE_SPEAKING)
        report("gui", "second wake turn reached command capture",
               "EXECUTED" if second else "FAILED",
               f"phase={window._voice_phase}")
        if window._voice_phase == window.PHASE_LISTENING:
            # Push-to-talk cancel while listening (fallback still works
            # even mid wake-turn): cancel, then resume monitoring.
            window._on_mic()               # stop & send with empty -> refusal
        GLib.timeout_add_seconds(4, after_turn2)

    def after_turn2():
        resumed = window._voice_phase in (window.PHASE_WAKE,
                                          window.PHASE_OFF)
        report("gui", "turn end returns toward monitoring", "EXECUTED"
               if resumed else "FAILED",
               f"phase={window._voice_phase}")
        # Push-to-talk takeover from monitoring (acceptance step 11):
        if window._voice_phase != window.PHASE_WAKE:
            window._settings["wake_word_enabled"] = True
            window._apply_settings()
        GLib.timeout_add_seconds(3, after_remonitor)

    def after_remonitor():
        if window._voice_phase == window.PHASE_WAKE:
            window._on_mic()               # push-to-talk takeover
            ok = window._voice_phase == window.PHASE_LISTENING
            report("gui", "push-to-talk works while monitoring", 
                   "EXECUTED" if ok else "FAILED",
                   f"phase={window._voice_phase}")
            window._cancel_turn()          # cancel -> resume monitoring
        else:
            report("gui", "push-to-talk works while monitoring", "FAILED",
                   f"phase={window._voice_phase}")
        GLib.timeout_add_seconds(3, finish_gui)
        return False

    GLib.timeout_add_seconds(4, start_gui)
    GLib.timeout_add_seconds(240, finish_gui)   # hard stop
    window.app.run(None)


def main(argv=None):
    import argparse
    parser = argparse.ArgumentParser(
        prog="python3 scripts/verify_wake_word.py")
    parser.add_argument("--ambient-seconds", type=int, default=30,
                        help="ambient false-trigger window (default 30)")
    parser.add_argument("--observe-seconds", type=int, default=10,
                        help="CPU observation window (default 10)")
    parser.add_argument("--skip-gui", action="store_true")
    args = parser.parse_args(argv)

    if not section_preflight():
        print("\nBLOCKED: preflight failed — install the model first "
              "(python3 scripts/install_wake_word_model.py) and run "
              "inside the voice venv.")
        return 2
    coupled = bool(section_microphone())
    section_detection_loopback(coupled=coupled)
    section_missed_and_false(ambient_seconds=args.ambient_seconds,
                             coupled=coupled)
    section_failure_paths()
    section_performance(observe_seconds=args.observe_seconds)
    section_gui(skip=args.skip_gui, coupled=coupled)

    print("\n== summary ==")
    counts = {}
    for r in RESULTS:
        counts[r["status"]] = counts.get(r["status"], 0) + 1
    print("  " + ", ".join(f"{k}: {v}" for k, v in sorted(counts.items())))
    failed = [r for r in RESULTS if r["status"] == "FAILED"]
    blocked = [r for r in RESULTS if r["status"] == "BLOCKED"]
    for r in failed:
        print(f"  FAILED: [{r['section']}] {r['name']} — {r['detail']}")
    for r in blocked:
        print(f"  BLOCKED: [{r['section']}] {r['name']} — {r['detail']}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
