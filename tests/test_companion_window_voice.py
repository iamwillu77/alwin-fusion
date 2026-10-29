"""v0.9.0 companion-window voice workflow tests (headless, all mocked).

The window's push-to-talk cycle is exercised end-to-end *without* a
display, microphone, model or speaker — capture, recognition, the LLM
reply and speech are all fakes, while the real marshalling
(``GLib.idle_add`` onto the default main context) is pumped for real:

* mic click starts listening and a second click submits **verbatim**
  through the shared conversation pipeline (the same path typed input
  uses),
* silence is refused before recognition ever runs,
* a cancelled turn's late result is dropped, never delivered,
* recognition failures surface as honest bubbles (no fake transcripts),
* uncertain detection asks the user instead of guessing,
* spoken replies enter the ``speaking`` state and return to idle only
  when the voice engine reports completion truth (success *and*
  failure),
* settings (mic off, spoken replies, rate/voice/language) apply live,
* teardown releases the microphone, cancels recognition and stops
  speech so nothing outlives the window.
"""

from __future__ import annotations

import threading
import time
import unittest

from companion.renderer import NullRenderer

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk, GLib
except (ImportError, ValueError):  # pragma: no cover - headless machine
    Gtk = None
    GLib = None


def pump_until(predicate, timeout=5.0) -> bool:
    """Spin the GLib default context until *predicate* or timeout.

    The window marshals every worker result through ``GLib.idle_add``;
    without a running main loop these tests must pump the context
    themselves — which also proves the marshalling really is used.
    """
    ctx = GLib.MainContext.default()
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        ctx.iteration(False)
        time.sleep(0.005)
    return bool(predicate())


# --------------------------------------------------------------------------- #
# Fakes
# --------------------------------------------------------------------------- #


class FakeBackend:
    """Microphone stand-in: records fed PCM, never touches a device."""

    def __init__(self):
        self.opened = False
        self.closed = False
        self.on_sample = None

    def open(self, on_sample, on_error):
        self.opened = True
        self.on_sample = on_sample

    def poll_error(self):
        return None

    def close(self):
        self.closed = True

    def feed(self, data: bytes) -> None:
        assert self.opened and not self.closed
        self.on_sample(data)


class Segment:
    def __init__(self, text, no_speech_prob=0.0):
        self.text = text
        self.no_speech_prob = no_speech_prob


class Info:
    def __init__(self, language, probability):
        self.language = language
        self.language_probability = probability


class FakeEngine:
    """Whisper stand-in: canned transcript + engine language metadata."""

    def __init__(self):
        self.text = "வணக்கம், என் உலாவியைத் திறக்க வேண்டும்."
        self.language = "ta"
        self.probability = 0.93
        self.error = None
        self.block = None          # threading.Event to wait on
        self.calls = []

    def transcribe(self, audio, language=None, beam_size=None):
        self.calls.append({"language": language})
        if self.block is not None:
            self.block.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        return ([Segment(self.text)], Info(self.language, self.probability))


class FakeManager:
    """Duck-typed TaskManager (no observer wiring needed headless)."""

    def __init__(self):
        self.busy = False
        self.submissions = []

    def submit(self, command, *, dry_run=False):
        self.submissions.append(command)
        return object()


class FakeLLM:
    def __init__(self, response="நல்லது, இப்போது திறக்கிறேன்."):
        self.response = response
        self.calls = []
        self.error = None       # raised instead of answering
        self.block = None       # threading.Event to hold generate()

    def generate(self, prompt, *, system=None, **kwargs):
        self.calls.append({"prompt": prompt, "system": system})
        if self.block is not None:
            self.block.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        return self.response


class FakeVoice:
    """VoiceEngine stand-in: records speech, holds its completion."""

    def __init__(self, *, available=True):
        self.name = "fake"
        self._available = available
        self.spoken = []
        self.stopped = 0
        self.speaking = False
        self.language = None
        self.rate = None
        self.voice_id = None
        self._on_done = None

    @property
    def available(self) -> bool:
        return self._available

    @property
    def status(self) -> str:
        return "fake voice ready" if self._available else "fake voice off"

    def speak(self, text, *, on_done=None):
        if not self._available:
            return False
        self.spoken.append(text)
        self._on_done = on_done
        self.speaking = True
        return True

    def stop(self):
        self.stopped += 1
        self.speaking = False
        if self._on_done is not None:
            cb, self._on_done = self._on_done, None
            cb(False, "stopped")

    def finish(self, success: bool, error):
        """Simulate the TTS worker's completion callback."""
        self.speaking = False
        cb, self._on_done = self._on_done, None
        if cb is not None:
            cb(success, error)

    def set_language(self, code):
        self.language = code
        return True

    def set_rate(self, rate):
        self.rate = rate

    def set_voice_id(self, voice_id):
        self.voice_id = voice_id


# --------------------------------------------------------------------------- #
# Harness
# --------------------------------------------------------------------------- #


class VoiceWindowTests(unittest.TestCase):
    def setUp(self):
        if Gtk is None or GLib is None:  # pragma: no cover - headless
            self.skipTest("GTK4 not available")

        from companion.conversation import ConversationService
        from companion.language_detection import LanguageDetectionService
        from companion.speech_recognition import SpeechRecognitionService
        from companion.voice_input import VoiceInputService
        from companion.window import CompanionWindow

        self.engine = FakeEngine()
        self.backends = []
        self.manager = FakeManager()
        self.llm = FakeLLM()
        self.voice = FakeVoice()

        def backend_factory():
            backend = FakeBackend()
            self.backends.append(backend)
            return backend

        try:
            self.window = CompanionWindow(
                self.manager,
                renderer=NullRenderer(),
                voice=self.voice,
                llm_client=self.llm,
                voice_input=VoiceInputService(
                    backend_factory=backend_factory
                ),
                recognizer=SpeechRecognitionService(
                    engine_factory=lambda _size: self.engine
                ),
                conversation=ConversationService(
                    manager=self.manager,
                    detector=LanguageDetectionService(),
                    llm_client=self.llm,
                ),
            )
        except Exception as exc:  # pragma: no cover - environment-specific
            self.skipTest(f"cannot construct GTK objects headless: {exc}")

    def tearDown(self):
        self.window._teardown()

    # -- helpers ---------------------------------------------------------- #

    def mic(self):
        """One push-to-talk click (as the button would send it)."""
        self.window._on_mic()

    def start_listening(self, pcm: bytes = b"\x01\x40" * 16000):
        before = len(self.backends)
        self.mic()                      # idle -> listening
        self.assertEqual(self.window._voice_phase, "listening")
        self.backends[-1].feed(pcm)     # one second of (non-silent) audio
        self.assertEqual(len(self.backends), before + 1)  # one new session
        self.mic()                      # listening -> recognizing
        self.assertEqual(self.window._voice_phase, "recognizing")

    def bubble(self) -> str:
        return self.window._bubble_text

    def enable_spoken_replies(self):
        self.window._settings["spoken_responses"] = True
        self.window._apply_settings()

    # -- lifecycle -------------------------------------------------------- #

    def test_services_exist_and_are_idle_at_construction(self):
        self.assertFalse(self.window.voice_input.listening)
        self.assertFalse(self.window.recognizer.busy)
        self.assertEqual(self.window._voice_phase, "off")
        self.assertEqual(self.window.controller.state, "idle")

    def test_construction_applies_default_voice_settings(self):
        # Auto-detection on, speech off (opt-in), rate 1.0 — the
        # defaults the settings schema promises.
        self.assertTrue(self.window.conversation.detector.auto)
        self.assertFalse(self.window.conversation.replies_enabled)
        self.assertEqual(self.window.voice.rate, 1.0)

    # -- listen -> recognize -> submit ------------------------------------- #

    def test_mic_cycle_submits_the_verbatim_transcript(self):
        self.start_listening()
        pump_until(
            lambda: self.window._voice_phase == "off", timeout=5.0
        )
        self.assertEqual(self.window._voice_phase, "off")
        # Verbatim: recognition output is not rewritten by the UI.
        self.assertEqual(self.manager.submissions,
                         ["வணக்கம், என் உலாவியைத் திறக்க வேண்டும்."])
        # The engine decoded freely (auto mode) so its language guess
        # can serve as the detection hint.
        self.assertIsNone(self.engine.calls[0]["language"])
        # The transcript was shown to the user first (plain text).
        self.assertIn("வணக்கம்", self.bubble())

    def test_silent_capture_is_refused_before_the_model_runs(self):
        self.mic()                      # idle -> listening
        self.assertEqual(self.window._voice_phase, "listening")
        self.backends[0].feed(b"\x00\x00" * 16000)   # digital silence
        self.mic()                      # listening -> refused (no ASR)
        self.assertEqual(self.window._voice_phase, "off")
        self.assertEqual(self.engine.calls, [])   # never invoked
        self.assertEqual(self.manager.submissions, [])
        self.assertIn("didn't hear anything", self.bubble())
        self.assertEqual(self.window.controller.state, "idle")

    def test_forced_language_when_auto_detection_is_off(self):
        self.window._settings["language_auto"] = False
        self.window._settings["language_preferred"] = "ta"
        self.window._apply_settings()
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        # The user's deliberate choice is forced into the decode...
        self.assertEqual(self.engine.calls[0]["language"], "ta")
        # ...and a forced hint is never reported as detection evidence.
        self.assertEqual(self.manager.submissions[0],
                         self.engine.text)

    def test_recognition_failure_is_an_honest_bubble(self):
        self.engine.error = RuntimeError("The speech model 'small' is "
                                         "not downloaded.")
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertEqual(self.manager.submissions, [])
        self.assertIn("not downloaded", self.bubble())
        self.assertEqual(self.window.controller.state, "idle")

    def test_cancel_during_recognition_drops_the_late_result(self):
        self.engine.block = threading.Event()
        self.start_listening()
        self.mic()                       # cancel while recognizing
        self.assertEqual(self.window._voice_phase, "off")
        self.assertEqual(self.window.controller.state, "idle")
        self.assertIn("cancelled", self.bubble())
        self.engine.block.set()          # the decode finishes late...
        pump_until(lambda: self.manager.submissions, timeout=0.5)
        self.assertEqual(self.manager.submissions, [])  # ...and is dropped

    def test_cancel_discards_the_captured_audio(self):
        self.mic()
        self.backends[0].feed(b"\x01\x40" * 16000)
        self.engine.block = threading.Event()
        # Cancel from the listening phase directly (e.g. settings).
        self.window._cancel_turn()
        self.assertEqual(self.window._voice_phase, "off")
        # The captured buffer was discarded: a later stop() returns
        # nothing (cancel() drains the PCM for good).
        self.assertEqual(self.window.voice_input.stop().pcm, b"")
        self.assertEqual(self.window.controller.state, "idle")
        self.engine.block.set()

    def test_uncertain_detection_asks_instead_of_guessing(self):
        # Short words the engine itself could not place (no language
        # reported): detection must stay uncertain — no invented claim.
        self.engine.text = "ok"
        self.engine.language = ""
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        # The task still runs (the words were clear enough)...
        self.assertEqual(self.manager.submissions, ["ok"])
        # ...but no language claim is invented: the user is asked.
        self.assertIn("couldn't tell which language", self.bubble())

    def test_busy_task_state_refuses_to_open_the_microphone(self):
        self.window.controller.force("working", reason="test")
        self.mic()
        self.assertEqual(self.window._voice_phase, "off")
        self.assertEqual(self.backends, [])     # no capture was opened
        self.assertIn("busy", self.bubble())

    def test_mic_disabled_in_settings_refuses_and_stops_capture(self):
        self.mic()                              # start listening
        self.window._settings["mic_enabled"] = False
        self.window._apply_settings()           # applies live
        # Listening was stopped and the audio discarded immediately.
        self.assertEqual(self.window._voice_phase, "off")
        self.assertEqual(self.window.controller.state, "idle")
        self.assertTrue(self.backends[0].closed)
        # A further click explains the setting instead of recording.
        self.mic()
        self.assertIn("turned off", self.bubble())
        self.assertEqual(len(self.backends), 1)

    # -- spoken replies ---------------------------------------------------- #

    def test_spoken_reply_speaks_and_returns_to_idle(self):
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(
            lambda: self.window._voice_phase == "speaking", timeout=5.0
        )
        # The reply came from the shared pipeline's language instruction...
        self.assertEqual(len(self.llm.calls), 1)
        self.assertIn("Reply in the language used in the user's latest "
                      "message", self.llm.calls[0]["system"])
        # ...was spoken (in the detected language)...
        self.assertEqual(len(self.voice.spoken), 1)
        self.assertEqual(self.voice.language, "ta")
        # ...and the character shows the speaking state.
        self.assertEqual(self.window.controller.state, "speaking")
        self.assertEqual(self.bubble(), self.llm.response)
        # The reply is NEVER fed back to the manager: exactly one
        # submission — the user's own words.
        self.assertEqual(self.manager.submissions, [self.engine.text])

        # Completion truth from the voice engine ends the turn.
        self.voice.finish(True, None)
        pump_until(
            lambda: self.window.controller.state == "idle", timeout=5.0
        )
        self.assertEqual(self.window._voice_phase, "off")
        self.assertEqual(self.bubble(), self.llm.response)  # text stays

    def test_speech_failure_is_reported_and_never_claimed(self):
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(
            lambda: self.window._voice_phase == "speaking", timeout=5.0
        )
        self.voice.finish(False, "No audio output device could be opened.")
        pump_until(
            lambda: self.window.controller.state == "idle", timeout=5.0
        )
        self.assertIn("Speech failed", self.bubble())
        self.assertIn("No audio output device", self.bubble())
        self.assertEqual(self.window._voice_phase, "off")
        # A playback failure must never leave the mic disabled: the
        # very next click starts a fresh turn.
        self.mic()
        self.assertEqual(self.window._voice_phase, "listening")
        self.mic()          # empty stop -> honest silence refusal
        self.assertIn("didn't hear anything", self.bubble())

    def test_unavailable_voice_engine_means_no_speech_attempt(self):
        self.voice = FakeVoice(available=False)   # engine went away
        self.window.voice = self.voice
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        # The task was submitted; nothing was spoken, no fake reply.
        self.assertEqual(len(self.manager.submissions), 1)
        self.assertEqual(self.voice.spoken, [])
        self.assertNotEqual(self.window.controller.state, "speaking")

    def test_disabled_spoken_replies_never_touch_the_llm(self):
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertEqual(len(self.manager.submissions), 1)
        self.assertEqual(self.llm.calls, [])
        self.assertEqual(self.voice.spoken, [])

    def test_llm_failure_is_an_honest_bubble_not_a_fake_reply(self):
        # v0.9.1: an unusable model response must be *explained*, not
        # silently dropped — but never faked, and never resubmitted.
        self.llm.response = None            # duck-typed failure signal
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertEqual(len(self.manager.submissions), 1)  # task stands
        self.assertEqual(self.voice.spoken, [])
        self.assertNotEqual(self.window.controller.state, "speaking")
        self.assertIn("couldn't write a reply", self.bubble())
        self.assertIn("still sent", self.bubble())

    def test_ollama_connection_failure_is_explained_and_mic_recovers(self):
        self.llm.error = RuntimeError("Ollama unreachable at "
                                      "http://localhost:11434")
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        # Distinguished from ASR: the transcript WAS understood, the
        # model failed, and the bubble says exactly that.
        self.assertEqual(len(self.manager.submissions), 1)
        self.assertIn("the local AI model failed", self.bubble())
        self.assertIn("Ollama unreachable", self.bubble())
        self.assertEqual(self.window._voice_phase, "off")
        # Errors must never permanently disable the microphone: the
        # very next click starts a fresh, working turn.
        self.llm.error = None
        self.mic()
        self.assertEqual(self.window._voice_phase, "listening")
        self.mic()   # stop & send (buffer empty -> honest silence refusal)
        self.assertIn("didn't hear anything", self.bubble())

    def test_empty_model_response_is_explained(self):
        self.llm.response = "   "
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertIn("empty response", self.bubble())
        self.assertEqual(len(self.manager.submissions), 1)

    def test_spoken_replies_without_an_llm_hint_at_the_flag(self):
        # spoken_responses on but no client attached (user forgot --llm):
        # the bubble explains the fix instead of staying mysteriously quiet.
        self.window.conversation.llm_client = None
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertIn("--llm", self.bubble())
        self.assertEqual(len(self.manager.submissions), 1)
        self.assertEqual(self.voice.spoken, [])

    def test_late_reply_from_a_cancelled_turn_is_dropped(self):
        self.llm.block = threading.Event()
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(
            lambda: self.window._voice_phase == "replying", timeout=5.0
        )
        self.mic()                      # cancel while the model thinks
        self.assertEqual(self.window._voice_phase, "off")
        self.assertIn("cancelled", self.bubble())
        self.llm.block.set()            # the model answers far too late
        pump_until(lambda: False, timeout=0.4)
        self.assertEqual(self.voice.spoken, [])      # never spoken
        self.assertNotEqual(self.window.controller.state, "speaking")

    def test_mic_recovers_after_a_recognition_failure(self):
        self.engine.error = RuntimeError("model missing")
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertIn("model missing", self.bubble())
        self.engine.error = None        # problem fixed
        self.mic()
        self.assertEqual(self.window._voice_phase, "listening")
        self.mic()                      # empty stop -> honest refusal
        self.assertIn("didn't hear anything", self.bubble())

    def test_two_consecutive_turns_work_end_to_end(self):
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertEqual(len(self.manager.submissions), 1)
        # With a real manager the task records would drive the character
        # on (planning → …) and back to idle when the task ends; the
        # headless FakeManager sends none, so emulate the calm return.
        self.assertEqual(self.window.controller.state, "thinking")
        self.window.controller.enter("idle", reason="task over")
        self.start_listening()
        pump_until(lambda: self.window._voice_phase == "off", timeout=5.0)
        self.assertEqual(len(self.manager.submissions), 2)
        self.assertEqual(self.window.controller.state, "thinking")

    def test_user_can_cancel_speech_with_the_mic(self):
        self.enable_spoken_replies()
        self.start_listening()
        pump_until(
            lambda: self.window._voice_phase == "speaking", timeout=5.0
        )
        self.mic()                       # press while talking
        self.assertEqual(self.voice.stopped, 1)
        # The interruption is reported through on_done("stopped") on
        # the TTS worker and marshalled back to the main loop.
        pump_until(
            lambda: self.window._voice_phase == "off", timeout=5.0
        )
        self.assertEqual(self.window.controller.state, "idle")
        # Cancellation is a supported path: the mic must work right after.
        self.mic()
        self.assertEqual(self.window._voice_phase, "listening")
        self.mic()

    # -- settings ---------------------------------------------------------- #

    def test_rate_and_voice_settings_apply_to_the_engine(self):
        self.window._settings["speech_rate"] = "1.4"
        self.window._settings["voice_id"] = "cmn"
        self.window._settings["language_preferred"] = "pt-BR"
        self.window._apply_settings()
        self.assertEqual(self.window.voice.rate, 1.4)
        self.assertEqual(self.window.voice.voice_id, "cmn")
        # Preference is normalised into the closed set.
        self.assertEqual(
            self.window.conversation.detector.preferred, "pt"
        )
        # Auto-detection stays on by default: the preference is only
        # the fallback for unclear input.
        self.assertTrue(self.window.conversation.detector.auto)

    def test_invalid_speech_rate_never_reaches_the_engine(self):
        self.window._settings["speech_rate"] = "nonsense"
        self.window._apply_settings()      # falls back to the default
        self.assertEqual(self.window.voice.rate, 1.0)
        self.assertEqual(self.window._settings["speech_rate"], "nonsense")

    # -- teardown ---------------------------------------------------------- #

    def test_teardown_releases_the_microphone_and_cancels_everything(self):
        self.mic()                          # listening
        self.assertTrue(self.window.voice_input.listening)
        self.window._teardown()
        self.assertTrue(self.window._closing)
        self.assertTrue(self.window.voice_input.released)
        self.assertFalse(self.window.voice_input.listening)
        self.assertTrue(self.backends[0].closed)
        self.assertEqual(self.window._voice_phase, "off")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
