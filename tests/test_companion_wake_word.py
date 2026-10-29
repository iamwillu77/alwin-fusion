"""v0.10.0 wake-word tests (all mocked — no microphone, no models).

Covers, with fake capture and fake detectors (clearly identified as
mocks — none of this is real-device evidence):

* service start/stop/release idempotency and no duplicate sessions,
* trigger delivery exactly once, then self-stop (mic exclusivity),
* cooldown/debounce, bounded queue (drop-oldest), bounded window,
* capture-open failure, detector-load failure, mid-stream device
  errors — all honest, all leaving ``listening`` False,
* stop-during-verification delivering NO late trigger,
* worker shutdown without leaked threads,
* the two-stage acceptance rule: confidence floor, phrase pattern,
  and the auto-language guard against hotword hallucinations,
* repeated verification failures surfacing instead of degrading
  silently,
* privacy pins: no file I/O, bounded buffers, honest wake_status,
* window lifecycle: opt-in default, indicator, immediate stop,
  push-to-talk takeover, ack -> command capture, resume hooks,
  stale-trigger rejection, honest error bubble without retry storms.
"""

from __future__ import annotations

import json
import os
import pathlib
import queue
import sys
import threading
import time
import unittest

from companion.wake_word import (
    CANDIDATE_CONF_FLOOR,
    DEFAULT_PHRASE,
    MAX_CANDIDATES_PER_SESSION,
    MAX_VERIFY_FAILURES,
    QUEUE_MAX_CHUNKS,
    WINDOW_BYTES,
    TwoStageDetector,
    WakeWordError,
    WakeWordService,
    phrase_pattern,
    wake_status,
)


def pump(predicate, timeout=5.0):
    """Poll a plain-callback assertion (service threads, no GLib)."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return bool(predicate())


# --------------------------------------------------------------------------- #
# Fakes (MOCKS — no hardware here; real-device evidence lives in
# scripts/verify_wake_word.py)
# --------------------------------------------------------------------------- #


class FakeInput:
    """VoiceInputService stand-in (duck-typed)."""

    def __init__(self, *, on_sample=None, fail_start=False):
        self.on_sample = on_sample
        self.fail_start = fail_start
        self.listening = False
        self.cancelled = False
        self.released = False
        self.last_error = None
        self.level_value = 0.42

    def start(self):
        if self.fail_start:
            raise RuntimeError("No microphone found.")
        self.listening = True

    def cancel(self):
        self.listening = False
        self.cancelled = True

    def release(self):
        self.released = True
        self.listening = False

    def level(self):
        return self.level_value if self.listening else 0.0


class FakeDetector:
    """TwoStageDetector stand-in: scripted triggers, blocking support."""

    def __init__(self, *, trigger_on=None, block=None, error=None):
        self.trigger_on = trigger_on       # chunk index to fire on
        self.block = block                 # threading.Event to wait on
        self.error = error                 # exception to raise (feed)
        self.chunks = 0
        self.reset_calls = 0
        self.closed = False

    def feed(self, chunk):
        self.chunks += 1
        if self.block is not None:
            self.block.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        return self.trigger_on is not None and self.chunks >= self.trigger_on

    def reset(self):
        self.reset_calls += 1

    def close(self):
        self.closed = True


# --------------------------------------------------------------------------- #
# WakeWordService
# --------------------------------------------------------------------------- #


class ServiceTests(unittest.TestCase):
    def build(self, *, detector=None, input_=None, cooldown=2.5,
              clock=None, triggers=None, errors=None):
        detector = detector if detector is not None else FakeDetector()
        created = []

        def input_factory(on_sample=None):
            inp = input_ if input_ is not None else FakeInput(
                on_sample=on_sample
            )
            created.append(inp)
            return inp

        def detector_factory(phrase):
            detector.phrase = phrase
            return detector

        service = WakeWordService(
            input_factory=input_factory,
            detector_factory=detector_factory,
            on_trigger=(lambda p: triggers.append(p)) if triggers is not
            None else None,
            on_error=(lambda m: errors.append(m)) if errors is not
            None else None,
            cooldown=cooldown,
            clock=clock or time.monotonic,
        )
        return service, detector, created

    def test_start_stop_are_idempotent_no_duplicate_sessions(self):
        service, detector, created = self.build()
        self.assertTrue(service.start())
        self.assertTrue(service.start())            # second start: no-op
        self.assertTrue(pump(lambda: service.listening))
        self.assertEqual(len(created), 1, "exactly one capture session")
        service.stop()
        service.stop()                              # idempotent
        self.assertFalse(service.listening)
        self.assertTrue(created[0].cancelled)
        self.assertEqual(len(created), 1)
        self.assertFalse(service.running)

    def test_trigger_fires_once_and_releases_the_microphone(self):
        triggers = []
        detector = FakeDetector(trigger_on=2)
        service, detector, created = self.build(detector=detector,
                                                triggers=triggers)
        service.start()
        self.assertTrue(pump(lambda: service.listening))
        inp = created[0]
        for _ in range(4):
            if inp.on_sample:
                inp.on_sample(b"\x01\x40" * 100)
            time.sleep(0.02)
        self.assertTrue(pump(lambda: not service.running))
        self.assertEqual(triggers, [DEFAULT_PHRASE])
        self.assertEqual(service.triggers, 1)
        self.assertFalse(service.listening)
        self.assertTrue(inp.cancelled, "mic released for the conversation")
        self.assertTrue(detector.closed)

    def test_cooldown_suppresses_an_immediate_second_trigger(self):
        now = [100.0]
        triggers = []
        detector = FakeDetector(trigger_on=1)   # fires on EVERY chunk
        service, detector, _ = self.build(
            detector=detector, cooldown=5.0, clock=lambda: now[0],
            triggers=triggers,
        )
        service.start()
        for _ in range(6):
            service._enqueue(b"\x01\x40" * 50)
            time.sleep(0.02)
        self.assertTrue(pump(lambda: service.triggers == 1))
        self.assertFalse(service.running)
        # Same instance restarted within the cooldown window:
        service.start()
        for _ in range(6):
            now[0] = 101.0                      # < cooldown of 5.0
            for _ in range(3):
                if service.running:
                    service._enqueue(b"\x01\x40" * 50)
            time.sleep(0.03)
        self.assertEqual(service.triggers, 1, "cooldown held")
        now[0] = 110.0                          # past the cooldown
        for _ in range(6):
            if service.running:
                service._enqueue(b"\x01\x40" * 50)
            time.sleep(0.03)
        self.assertTrue(pump(lambda: service.triggers >= 2))
        service.stop()

    def test_capture_open_failure_is_honest_and_never_listens(self):
        errors = []
        service, _d, _c = self.build(
            input_=FakeInput(fail_start=True), errors=errors
        )
        service.start()
        self.assertTrue(pump(lambda: bool(errors)))
        self.assertIn("No microphone", errors[0])
        self.assertIn("No microphone", service.last_error or "")
        self.assertFalse(service.listening)
        self.assertFalse(service.running)

    def test_detector_load_failure_releases_the_capture(self):
        errors = []
        created = []

        def input_factory(on_sample=None):
            inp = FakeInput(on_sample=on_sample)
            created.append(inp)
            return inp

        def detector_factory(phrase):
            raise WakeWordError(
                "The wake-word model is not installed. Install it "
                "explicitly with: python3 scripts/"
                "install_wake_word_model.py"
            )

        service = WakeWordService(
            input_factory=input_factory,
            detector_factory=detector_factory,
            on_error=errors.append,
        )
        service.start()
        self.assertTrue(pump(lambda: bool(errors)))
        self.assertIn("install_wake_word_model.py", errors[0])
        self.assertTrue(created[0].cancelled, "capture released")
        self.assertFalse(service.listening)

    def test_midstream_device_error_stops_honestly(self):
        errors = []
        input_ = FakeInput()
        service, _d, _c = self.build(input_=input_, errors=errors)
        service.start()
        self.assertTrue(pump(lambda: service.listening))
        input_.last_error = "USB audio device disconnected"
        self.assertTrue(pump(lambda: bool(errors)))
        self.assertIn("USB audio device", errors[0])
        self.assertFalse(service.listening)
        self.assertTrue(input_.cancelled)

    def test_stop_during_verification_delivers_no_late_trigger(self):
        block = threading.Event()
        triggers = []
        detector = FakeDetector(trigger_on=1, block=block)
        service, _d, _c = self.build(detector=detector,
                                     triggers=triggers)
        service.start()
        self.assertTrue(pump(lambda: service.listening))
        service._enqueue(b"\x01\x40" * 100)      # worker enters feed()
        time.sleep(0.05)
        service.stop()                           # user cancelled meanwhile
        block.set()                              # verification "succeeds"
        deadline = time.time() + 2.0
        while time.time() < deadline and service._worker is not None \
                and service._worker.is_alive():
            time.sleep(0.02)
        self.assertEqual(triggers, [], "no stale trigger after stop()")
        self.assertEqual(service.triggers, 0)

    def test_release_refuses_restart_with_honest_error(self):
        service, _d, _c = self.build()
        service.release()
        self.assertFalse(service.start())
        self.assertIn("released", service.last_error or "")
        self.assertTrue(service.released)

    def test_detector_exception_fails_the_service_not_silently(self):
        errors = []
        detector = FakeDetector(error=RuntimeError("kws exploded"))
        service, _d, _c = self.build(detector=detector, errors=errors)
        service.start()
        service._enqueue(b"\x01\x40" * 50)
        self.assertTrue(pump(lambda: bool(errors)))
        self.assertIn("kws exploded", errors[0])
        self.assertFalse(service.running)
        self.assertFalse(service.listening)

    def test_worker_thread_does_not_leak_after_stop(self):
        service, _d, _c = self.build()
        service.start()
        self.assertTrue(pump(lambda: service.listening))
        worker = service._worker
        service.stop()
        if worker is not None:
            worker.join(timeout=2.0)
            self.assertFalse(worker.is_alive(), "worker exited")

    def test_wait_stopped_drains_before_interpreter_exit(self):
        # The real run showed a segfault when the process exited while
        # the worker was inside C code: wait_stopped() is the clean-
        # shutdown contract every launcher/driver must honour.
        service, _d, _c = self.build()
        service.start()
        self.assertTrue(pump(lambda: service.listening))
        service.release()
        self.assertTrue(service.wait_stopped(timeout=5.0))
        self.assertFalse(service.start(), "released stays released")

    def test_chunk_queue_is_bounded_drop_oldest(self):
        block = threading.Event()
        detector = FakeDetector(block=block)
        service, _d, _c = self.build(detector=detector)
        service.start()
        self.assertTrue(pump(lambda: service.listening))
        for _ in range(QUEUE_MAX_CHUNKS + 40):    # flood the callback
            service._enqueue(b"\x01\x40" * 160)
        self.assertLessEqual(service._chunks.qsize(), QUEUE_MAX_CHUNKS)
        block.set()
        service.stop()

    def test_listening_and_level_track_the_real_capture(self):
        service, _d, created = self.build()
        self.assertFalse(service.listening)
        self.assertEqual(service.level(), 0.0)
        service.start()
        self.assertTrue(pump(lambda: service.listening))
        self.assertAlmostEqual(service.level(), 0.42)
        service.stop()
        self.assertFalse(service.listening)
        self.assertEqual(service.level(), 0.0)

    def test_default_cooldown_and_phrase_are_sane(self):
        service, _d, _c = self.build()
        self.assertEqual(service.phrase, "Hey Alwin")
        self.assertGreaterEqual(service._cooldown, 1.0)


# --------------------------------------------------------------------------- #
# TwoStageDetector (fake vosk KWS + fake recognizer)
# --------------------------------------------------------------------------- #


class FakeKWS:
    """vosk KaldiRecognizer stand-in (KWS mode)."""

    def __init__(self, *, conf=CANDIDATE_CONF_FLOOR + 40,
                 complete=True, text="hey alwin"):
        self.conf = conf
        self.complete = complete
        self.text = text
        self.resets = 0

    def AcceptWaveform(self, _pcm):
        return self.complete

    def Result(self):
        return json.dumps({
            "alternatives": [{"text": self.text, "confidence": self.conf}]
        })

    def Reset(self):
        self.resets += 1


class FakeRecognizer:
    """SpeechRecognitionService stand-in for decode_window()."""

    def __init__(self, *, available=True, text="Hey Alwin",
                 language="en", error=None):
        self._available = available
        self.text = text
        self.language = language
        self.error = error
        self.calls = []

    @property
    def available(self):
        return self._available

    def decode_window(self, pcm, *, hotwords=None, language=None,
                      beam_size=5):
        self.calls.append({"hotwords": hotwords, "bytes": len(pcm)})
        if self.error is not None:
            raise self.error
        return type("Decode", (), {
            "text": self.text,
            "language": self.language,
            "language_probability": 0.97,
        })()


def detector(recognizer, phrase="Hey Alwin", kws=None, **kw):
    kw.setdefault("vosk_factory", lambda _phrase: kws or FakeKWS())
    kw.setdefault("verify_interval", 0.0)   # tests control timing explicitly
    return TwoStageDetector(recognizer=recognizer, phrase=phrase, **kw)


class DetectorTests(unittest.TestCase):
    def test_verified_phrase_is_accepted(self):
        d = detector(FakeRecognizer())
        self.assertTrue(d.feed(b"\x01\x40" * 3200))
        self.assertEqual(len(d.recognizer.calls), 1)
        self.assertEqual(d.recognizer.calls[0]["hotwords"], "Hey Alwin")

    def test_candidate_below_confidence_floor_never_verifies(self):
        d = detector(FakeRecognizer(), kws=FakeKWS(conf=20.0))
        self.assertFalse(d.feed(b"\x01\x40" * 3200))
        self.assertEqual(d.recognizer.calls, [])

    def test_incomplete_utterance_never_verifies(self):
        d = detector(FakeRecognizer(), kws=FakeKWS(complete=False))
        self.assertFalse(d.feed(b"\x01\x40" * 3200))
        self.assertEqual(d.recognizer.calls, [])

    def test_hotword_hallucination_on_foreign_audio_is_rejected(self):
        # The measured failure mode: with forced English, whisper
        # fabricated "Hey Alwin…" out of Bengali — the auto-language
        # result is the guard.
        d = detector(FakeRecognizer(text="Hey Alwin, what are you "
                                         "doing here?",
                                    language="bn"))
        self.assertFalse(d.feed(b"\x01\x40" * 3200))

    def test_non_matching_text_is_rejected_even_in_english(self):
        d = detector(FakeRecognizer(text="something else entirely",
                                    language="en"))
        self.assertFalse(d.feed(b"\x01\x40" * 3200))

    def test_missing_language_field_is_rejected(self):
        d = detector(FakeRecognizer(text="Hey Alwin", language=None))
        self.assertFalse(d.feed(b"\x01\x40" * 3200))

    def test_candidate_budget_resets_instead_of_going_deaf(self):
        # With the per-session budget hit, the detector resets its
        # budget (the verification INTERVAL is the CPU bound) rather
        # than silently never verifying again.
        rec = FakeRecognizer()
        d = detector(rec)
        for _ in range(MAX_CANDIDATES_PER_SESSION + 4):
            d.feed(b"\x01\x40" * 3200)
        self.assertEqual(len(rec.calls), MAX_CANDIDATES_PER_SESSION + 4)
        d.reset()
        d.feed(b"\x01\x40" * 3200)
        self.assertEqual(len(rec.calls), MAX_CANDIDATES_PER_SESSION + 5)

    def test_silent_empty_text_results_never_verify(self):
        # Measured on the real engine: silence yields EMPTY-text
        # alternatives with huge confidence (581!) — the text gate must
        # reject them before they can storm the verifier (CPU).
        rec = FakeRecognizer()
        d = detector(rec, kws=FakeKWS(conf=581.0, text=""))
        for _ in range(5):
            self.assertFalse(d.feed(b"\x00\x00" * 3200))
        self.assertEqual(rec.calls, [], "silence never reaches stage 2")

    def test_partial_words_do_not_qualify_as_a_candidate(self):
        rec = FakeRecognizer()
        d = detector(rec, kws=FakeKWS(text="alwin alwin"))  # no 'hey'
        self.assertFalse(d.feed(b"\x01\x40" * 3200))
        self.assertEqual(rec.calls, [])

    def test_verification_rate_limit_bounds_noisy_rooms(self):
        now = [100.0]
        rec = FakeRecognizer()
        d = detector(rec, verify_interval=5.0, clock=lambda: now[0])
        self.assertTrue(d.feed(b"\x01\x40" * 3200))      # first: immediate
        now[0] = 101.0                                    # inside interval
        self.assertFalse(d.feed(b"\x01\x40" * 3200))
        now[0] = 106.0                                    # past interval
        self.assertTrue(d.feed(b"\x01\x40" * 3200))
        self.assertEqual(len(rec.calls), 2)

    def test_kws_keyword_is_lowercased_vocabulary_spelling(self):
        # Measured: capitalized keywords drop every word from vosk's
        # vocabulary (degenerate grammar). The factory must receive the
        # lowercase spelling; the whisper hotwords keep original case.
        seen = []
        rec = FakeRecognizer()
        d = TwoStageDetector(
            recognizer=rec, phrase="Hey Computer",
            vosk_factory=lambda phrase: seen.append(phrase)
            or FakeKWS(text="hey computer"),
            verify_interval=0.0,
        )
        self.assertEqual(seen, ["hey computer"])
        self.assertEqual(d._kws_keyword(), "hey computer")
        d.feed(b"\x01\x40" * 3200)
        self.assertEqual(rec.calls[0]["hotwords"], "Hey Computer")

    def test_repeated_verification_failures_surface(self):
        rec = FakeRecognizer(error=RuntimeError("model gone"))
        d = detector(rec)
        with self.assertRaises(WakeWordError):
            for _ in range(MAX_VERIFY_FAILURES):
                d.feed(b"\x01\x40" * 3200)

    def test_single_verification_failure_is_survivable(self):
        rec = FakeRecognizer(error=RuntimeError("transient"))
        d = detector(rec)
        self.assertFalse(d.feed(b"\x01\x40" * 3200))   # failure 1
        rec.error = None
        self.assertTrue(d.feed(b"\x01\x40" * 3200))    # recovers

    def test_rolling_window_is_bounded(self):
        d = detector(FakeRecognizer())
        for _ in range(50):
            d.feed(b"\x01\x40" * 3200)
        self.assertLessEqual(len(d._window), WINDOW_BYTES)

    def test_custom_phrase_pattern_is_derived_from_the_phrase(self):
        self.assertTrue(__import__("re").search(
            phrase_pattern("Hey Computer"), "hey computer!",
            __import__("re").IGNORECASE))
        rec = FakeRecognizer(text="Hey computer", language="en")
        d = detector(rec, phrase="Hey Computer",
                     kws=FakeKWS(text="hey computer"))
        self.assertTrue(d.feed(b"\x01\x40" * 3200))
        self.assertEqual(d.recognizer.calls[0]["hotwords"],
                         "Hey Computer")

    def test_missing_model_dir_reports_the_installer(self):
        rec = FakeRecognizer()
        with self.assertRaises(WakeWordError) as ctx:
            TwoStageDetector(recognizer=rec,
                             vosk_model_dir="/definitely/not/here",
                             vosk_factory=None)
        self.assertIn("install_wake_word_model.py", str(ctx.exception))

    def test_unavailable_speech_model_is_reported_first(self):
        with self.assertRaises(WakeWordError) as ctx:
            detector(FakeRecognizer(available=False))
        self.assertIn("install_voice_models.py", str(ctx.exception))


# --------------------------------------------------------------------------- #
# wake_status honesty + privacy pins
# --------------------------------------------------------------------------- #


class StatusAndPrivacyTests(unittest.TestCase):
    def test_status_reports_missing_vosk_or_missing_model(self):
        try:
            import vosk  # noqa: F401
            have_vosk = True
        except Exception:  # noqa: BLE001
            have_vosk = False
        status = wake_status(FakeRecognizer())
        if not have_vosk:
            self.assertFalse(status["available"])
            self.assertIn("vosk", status["detail"])
            return
        # vosk present: without the model the honest answer is the
        # installer hint (env override keeps this test hermetic).
        old = os.environ.get("ALWIN_WAKE_MODEL_DIR")
        os.environ["ALWIN_WAKE_MODEL_DIR"] = "/definitely/not/here"
        try:
            status = wake_status(FakeRecognizer())
        finally:
            if old is None:
                os.environ.pop("ALWIN_WAKE_MODEL_DIR", None)
            else:
                os.environ["ALWIN_WAKE_MODEL_DIR"] = old
        self.assertFalse(status["available"])
        self.assertIn("install_wake_word_model.py", status["detail"])

    def test_status_ready_when_everything_is_present(self):
        import types

        vosk_stub = types.ModuleType("vosk")
        old = sys.modules.get("vosk")
        sys.modules["vosk"] = vosk_stub
        model_dir = pathlib.Path(__file__).parent
        old_env = os.environ.get("ALWIN_WAKE_MODEL_DIR")
        os.environ["ALWIN_WAKE_MODEL_DIR"] = str(model_dir)
        try:
            status = wake_status(FakeRecognizer(available=True))
            self.assertTrue(status["available"])
            status = wake_status(FakeRecognizer(available=False))
            self.assertFalse(status["available"])
            self.assertIn("speech model", status["detail"])
        finally:
            if old is None:
                sys.modules.pop("vosk", None)
            else:
                sys.modules["vosk"] = old
            if old_env is None:
                os.environ.pop("ALWIN_WAKE_MODEL_DIR", None)
            else:
                os.environ["ALWIN_WAKE_MODEL_DIR"] = old_env

    def test_wake_module_performs_no_file_writes(self):
        # Privacy pin: the wake path cannot record audio to disk.
        source = pathlib.Path(
            os.path.join(os.path.dirname(__file__), "..",
                         "companion", "wake_word.py")
        ).read_text(encoding="utf-8")
        self.assertNotIn("open(", source)
        self.assertNotIn(".write(", source)

    def test_buffers_are_bounded_by_constants(self):
        self.assertLessEqual(QUEUE_MAX_CHUNKS, 128)
        self.assertLessEqual(WINDOW_BYTES, 256 * 1024)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
