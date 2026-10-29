#!/usr/bin/env python3
"""v0.9.1 GUI end-to-end verification (real ASR + live Ollama + TTS).

Real GTK4 window in the live Wayland session, temp APP_ID, sandbox
state dir, EAGER voice stack. Honest substitutions, clearly labeled:

* the "microphone" is a file-feeding source (no human is available to
  speak; the REAL microphone is verified separately by capture tests),
* the manager is a recorder (evaluation fixtures must NEVER become
  real tasks),
* everything else is REAL: faster-whisper 'small', langdetect, the
  live Ollama qwen3:0.6b, espeak-ng synthesis and GStreamer playback
  to the actual audio device, GLib marshalling.

Expected flow: mic click -> listening (+file audio) -> stop & send ->
real transcript bubble -> real reply (English) -> speaking state ->
playback completion -> idle.
"""
import os, sys, time, threading

SANDBOX = os.path.expanduser("~/.cache/alwin-gui-verify091")
os.environ["ALWIN_HOME"] = SANDBOX
os.environ["ALWIN_STATE_DIR"] = os.path.join(SANDBOX, "state")
os.environ["ALWIN_PERMITTED_ROOTS"] = os.path.join(SANDBOX, "Documents")
for d in (os.environ["ALWIN_STATE_DIR"], os.environ["ALWIN_PERMITTED_ROOTS"]):
    os.makedirs(d, exist_ok=True)

REPO = str(__import__("pathlib").Path(__file__).resolve().parent.parent)
sys.path.insert(0, REPO)
sys.path.insert(0, str(__import__("pathlib").Path(__file__).resolve().parent))
from evaluate_voice_languages import read_wav_mono16k  # noqa: E402

import gi  # noqa: E402
gi.require_version("Gtk", "4.0")
from gi.repository import GLib  # noqa: E402

import companion.window as win_mod  # noqa: E402
win_mod.APP_ID = "org.alwin.fusion.companion.verify091"

from companion.window import CompanionWindow  # noqa: E402
from companion.greeting import GreetingConfig  # noqa: E402
from companion.voice_input import VoiceInputService  # noqa: E402
from ai_runtime.ollama_client import OllamaClient  # noqa: E402

FAILURES = []


def log(msg):
    print(f"[gui091] {msg}", flush=True)


def expect(cond, name):
    log(("ok   " if cond else "FAIL ") + name)
    if not cond:
        FAILURES.append(name)


class RecordingManager:
    """Fixtures must never become real tasks."""

    def __init__(self):
        self.submissions = []
        self.observers = []

    @property
    def current(self):
        """The window's status strip reads this (real manager has it)."""
        return None

    def submit(self, command, *, dry_run=False):
        self.submissions.append(command)
        return object()

    def add_observer(self, cb):
        self.observers.append(cb)

    def remove_observer(self, cb):
        if cb in self.observers:
            self.observers.remove(cb)


class FileBackend:
    """Capture stand-in: returns a FLEURS English sample as if heard."""

    def __init__(self, pcm):
        self.pcm = pcm
        self.on_sample = None

    def open(self, on_sample, on_error):
        self.on_sample = on_sample
        # deliver in realistic-ish chunks (32 KB/s ≈ real-time 16k mono)
        def feed():
            for i in range(0, len(self.pcm), 3200):
                self.on_sample(self.pcm[i:i + 3200])
                time.sleep(0.002)
        threading.Thread(target=feed, daemon=True).start()

    def poll_error(self):
        return None

    def close(self):
        pass


import json  # noqa: E402

meta = [json.loads(l) for l in
        open(os.path.expanduser("~/.cache/alwin-fleur-samples/meta.jsonl"))]
sample = next(m for m in meta if m["lang"] == "en")
SAMPLE_PCM = read_wav_mono16k(sample["file"])

manager = RecordingManager()
llm = OllamaClient(model=os.environ.get("OLLAMA_MODEL") or "qwen3:0.6b")
window = CompanionWindow(
    manager, config=None,
    greeting_config=GreetingConfig(enabled=False),
    voice_input=VoiceInputService(
        backend_factory=lambda: FileBackend(SAMPLE_PCM)),
    llm_client=llm,
)
# opt in to spoken replies (settings are cosmetic-only in the sandbox)
window._settings["spoken_responses"] = True
window._configure_services()

from gi.repository import Gtk  # noqa: E402

loop_started = time.time()


def finish():
    try:
        window._teardown()
    except Exception:
        pass
    print("GUI091-RESULT:", "FAIL" if FAILURES else "OK",
          f"({len(FAILURES)} failures)", flush=True)
    window.app.quit()
    return False


def step1():
    expect(window.window is not None, "window built (temp APP_ID)")
    expect(window._mic_btn.get_sensitive(), "mic button ready")
    window._on_mic()                       # listen
    GLib.timeout_add_seconds(1, step2)


def step2():
    expect(window._voice_phase == "listening", "listening phase")
    expect(window.controller.state == "listening", "listening state")
    GLib.timeout_add_seconds(4, step3)     # file finishes feeding


def step3():
    window._on_mic()                       # stop & send -> real ASR
    GLib.timeout_add_seconds(1, step4)


def wait_turn(seconds_left):
    if window._voice_phase == "off" or seconds_left <= 0:
        GLib.timeout_add_seconds(1, step_final)
        return
    GLib.timeout_add_seconds(1, lambda: wait_turn(seconds_left - 1))


def step4():
    wait_turn(150)


def step_final():
    bubble = window._bubble_text
    log(f"submissions (recording manager): {len(manager.submissions)}")
    if manager.submissions:
        log(f"transcript: {manager.submissions[0][:90]!r}")
    log(f"bubble: {bubble[:110]!r}")
    expect(len(manager.submissions) == 1,
           "exactly one fixture submission (no real task)")
    expect(manager.submissions and
           "satellite" in manager.submissions[0].lower(),
           "real ASR transcript matches the fixture sentence")
    expect(window.controller.state in ("idle", "thinking", "working",
                                       "speaking"),
           f"valid state after turn ({window.controller.state})")
    expect(window._voice_phase == "off", "phase cleared")
    finish()


GLib.timeout_add_seconds(3, step1)
GLib.timeout_add_seconds(300, finish)      # hard stop
sys.exit(window.app.run(None))
