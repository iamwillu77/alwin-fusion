"""v0.9.0 voice output tests (mocked synth/player — no audio device).

Pins the speaking contract:

* the engine implements the existing VoiceEngine seam honestly:
  ``available`` is probed (never assumed), ``speak`` returns False
  instead of pretending, ``status`` explains a missing engine,
* every speak reports its completion truth through ``on_done`` —
  synthesis failure, device failure and user-cancel are distinguishable
  and never counted as "spoken",
* ``stop`` (and a superseding ``speak``) invalidate in-flight speech:
  the stale worker reports "stopped" and can never play afterwards,
* language/rate settings apply live; an unknown language is refused
  (the current voice is kept, no silent substitution),
* speech text is bounded and passed verbatim to the synthesizer,
* all real work happens on a worker thread — the calling thread only
  dispatches.
"""

from __future__ import annotations

import threading
import time
import unittest

from companion.voice_output import (
    MAX_SPEECH_CHARS,
    CompanionVoice,
    SynthesizedSpeech,
    VoiceOutputError,
)


def wait_until(predicate, timeout=5.0) -> bool:
    deadline = time.time() + timeout
    while time.time() < deadline:
        if predicate():
            return True
        time.sleep(0.01)
    return predicate()


class FakeSynthesizer:
    """Duck-typed EspeakSynthesizer with scripted outcomes."""

    def __init__(self, *, available=True, fail=None, gate=None,
                 load_error=None):
        self._available = available
        self.fail = fail
        self.gate = gate  # threading.Event the synth waits on
        self.load_error = load_error
        self.calls = []

    @property
    def available(self) -> bool:
        return self._available

    @property
    def sample_rate(self) -> int:
        return 22050

    def synthesize(self, text, *, voice, rate=1.0):
        self.calls.append({"text": text, "voice": voice, "rate": rate})
        if self.gate is not None:
            self.gate.wait(timeout=5.0)
        if self.fail is not None:
            raise self.fail
        return SynthesizedSpeech(b"\x01\x40" * 1000, 22050)


class FakePlayer:
    """Duck-typed GStreamerPlayer (blocking play, interruptible)."""

    def __init__(self, *, error=None, gate=None):
        self.error = error
        self.gate = gate
        self.played = False
        self.stopped = False

    def play(self):
        if self.gate is not None:
            self.gate.wait(timeout=5.0)
        self.played = True
        return self.error

    def stop(self):
        self.stopped = True
        if self.gate is not None:
            self.gate.set()


class RecordingVoice:
    """A voice with recorded on_done callbacks."""

    def __init__(self, **kwargs):
        self.synth = kwargs.pop("synth", None) or FakeSynthesizer()
        players = kwargs.pop("players", None)
        self.players = players if players is not None else []
        error = kwargs.pop("player_error", None)

        def factory(speech):
            player = FakePlayer(error=error)
            self.players.append(player)
            return player

        self.completions = []
        kwargs.setdefault("language", "en")
        self.voice = CompanionVoice(
            synthesizer=self.synth,
            player_factory=factory,
            on_done=lambda ok, err: self.completions.append((ok, err)),
            **kwargs,
        )

    def wait_completion(self, count=1, timeout=5.0) -> bool:
        return wait_until(lambda: len(self.completions) >= count, timeout)


class CapabilityTests(unittest.TestCase):
    def test_unavailable_engine_never_claims_to_speak(self):
        voice = CompanionVoice(
            synthesizer=FakeSynthesizer(
                available=False,
                load_error="espeak-ng is not installed.",
            ),
            player_factory=lambda s: FakePlayer(),
        )
        self.assertFalse(voice.available)
        self.assertFalse(voice.speak("hello"))
        self.assertIn("espeak-ng is not installed", voice.status)

    def test_available_without_loader_reports_off_status(self):
        voice = CompanionVoice(
            synthesizer=FakeSynthesizer(available=False),
            player_factory=lambda s: FakePlayer(),
        )
        self.assertFalse(voice.available)
        self.assertIn("Voice off", voice.status)

    def test_status_names_language_and_voice_when_ready(self):
        voice = RecordingVoice(language="ta")
        self.assertTrue(voice.voice.available)
        status = voice.voice.status
        self.assertIn("Tamil", status)
        self.assertIn("tamil", status)

    def test_empty_text_returns_false_without_dispatch(self):
        rec = RecordingVoice()
        self.assertFalse(rec.voice.speak(""))
        self.assertFalse(rec.voice.speak("   \n "))
        self.assertEqual(rec.synth.calls, [])

    def test_status_is_bounded(self):
        voice = RecordingVoice()
        self.assertLessEqual(len(voice.voice.status), 200)


class SpeechFlowTests(unittest.TestCase):
    def test_successful_speech_reports_true_completion(self):
        rec = RecordingVoice()
        self.assertTrue(rec.voice.speak("hello there"))
        self.assertTrue(rec.wait_completion())
        self.assertEqual(rec.completions, [(True, None)])
        self.assertEqual(rec.synth.calls[0]["text"], "hello there")
        self.assertTrue(rec.players[0].played)
        self.assertFalse(rec.voice.speaking)

    def test_speak_happens_on_a_worker_thread(self):
        seen = {}

        class ThreadChecking(FakeSynthesizer):
            def synthesize(self, text, *, voice, rate=1.0):
                seen["thread"] = threading.current_thread().name
                return super().synthesize(text, voice=voice, rate=rate)

        rec = RecordingVoice(synth=ThreadChecking())
        caller = threading.current_thread().name
        self.assertTrue(rec.voice.speak("hello"))
        self.assertTrue(rec.wait_completion())
        self.assertEqual(seen["thread"], "alwin-tts")
        self.assertNotEqual(seen["thread"], caller)

    def test_speak_dispatch_is_nonblocking(self):
        gate = threading.Event()
        rec = RecordingVoice(synth=FakeSynthesizer(gate=gate))
        start = time.time()
        ok = rec.voice.speak("a long thing to say")
        elapsed = time.time() - start
        self.assertTrue(ok)
        self.assertLess(elapsed, 0.5)  # returns while synth is blocked
        self.assertTrue(rec.voice.speaking)
        gate.set()
        self.assertTrue(rec.wait_completion())

    def test_synthesis_failure_is_reported_not_claimed(self):
        rec = RecordingVoice(
            synth=FakeSynthesizer(
                fail=VoiceOutputError("The voice 'cmn' is not available.")
            )
        )
        self.assertTrue(rec.voice.speak("你好"))
        self.assertTrue(rec.wait_completion())
        ok, error = rec.completions[0]
        self.assertFalse(ok)
        self.assertIn("not available", error)
        self.assertEqual(rec.players, [])  # playback never started
        self.assertIn("not available", rec.voice.last_error)

    def test_player_device_failure_is_reported(self):
        rec = RecordingVoice(player_error="No audio output device could be opened.")
        rec.voice.speak("hello")
        self.assertTrue(rec.wait_completion())
        ok, error = rec.completions[0]
        self.assertFalse(ok)
        self.assertIn("No audio output device", error)
        self.assertIn("No audio output device", rec.voice.status)

    def test_bounded_text_is_passed_to_the_synth(self):
        rec = RecordingVoice()
        rec.voice.speak("word " * 500)
        self.assertTrue(rec.wait_completion())
        spoken = rec.synth.calls[0]["text"]
        self.assertLessEqual(len(spoken), MAX_SPEECH_CHARS)


class StopAndSupersedeTests(unittest.TestCase):
    def test_stop_interrupts_in_flight_speech(self):
        gate = threading.Event()
        synth = FakeSynthesizer(gate=gate)
        rec = RecordingVoice(synth=synth)
        rec.voice.speak("hello")
        wait_until(lambda: len(synth.calls) == 1)
        rec.voice.stop()
        gate.set()  # let the blocked synth finish its checkpoint
        self.assertTrue(rec.wait_completion())
        ok, error = rec.completions[0]
        self.assertFalse(ok)
        self.assertEqual(error, "stopped")

    def test_stop_mid_playback_stops_the_player(self):
        gate = threading.Event()
        rec = RecordingVoice()
        players = []

        from companion.voice_output import CompanionVoice as CV

        def factory(speech):
            player = FakePlayer(gate=gate)
            players.append(player)
            return player

        voice = CV(
            synthesizer=FakeSynthesizer(),
            player_factory=factory,
            on_done=lambda ok, err: rec.completions.append((ok, err)),
        )
        voice.speak("hello")
        wait_until(lambda: len(players) == 1)
        voice.stop()
        self.assertTrue(players[0].stopped)
        self.assertTrue(wait_until(lambda: len(rec.completions) == 1))
        ok, error = rec.completions[0]
        self.assertFalse(ok)
        self.assertEqual(error, "stopped")
        self.assertFalse(voice.speaking)

    def test_stop_when_idle_is_a_noop(self):
        rec = RecordingVoice()
        rec.voice.stop()  # must not raise
        self.assertFalse(rec.voice.speaking)
        self.assertEqual(rec.completions, [])

    def test_superseding_speak_stops_the_old_player(self):
        gate = threading.Event()
        players = []
        from companion.voice_output import CompanionVoice as CV

        def factory(speech):
            player = FakePlayer(gate=gate)
            players.append(player)
            return player

        completions = []
        voice = CV(
            synthesizer=FakeSynthesizer(),
            player_factory=factory,
            on_done=lambda ok, err: completions.append((ok, err)),
        )
        voice.speak("first")
        # Wait until the old worker has registered its player (the
        # registration happens under the same lock as superseding).
        self.assertTrue(wait_until(
            lambda: voice._current_player is not None
            and voice._current_player is players[0]
        ))
        voice.speak("second")
        # The first player was invalidated by the supersede...
        self.assertTrue(wait_until(lambda: players[0].stopped))
        gate.set()  # release whatever is still blocked
        self.assertTrue(wait_until(lambda: len(completions) >= 2))
        outcomes = sorted(completions)
        self.assertEqual(outcomes, [(False, "stopped"), (True, None)])


class LanguageAndRateTests(unittest.TestCase):
    def test_language_switch_selects_the_registry_voice(self):
        rec = RecordingVoice(language="en")
        self.assertTrue(rec.voice.set_language("zh"))
        rec.voice.speak("你好")
        self.assertTrue(rec.wait_completion())
        self.assertEqual(rec.synth.calls[0]["voice"], "cmn")

    def test_unknown_language_is_refused_and_voice_kept(self):
        rec = RecordingVoice(language="ta")
        self.assertFalse(rec.voice.set_language("klingon"))
        self.assertEqual(rec.voice.language, "ta")
        rec.voice.speak("hello")
        self.assertTrue(rec.wait_completion())
        self.assertEqual(rec.synth.calls[0]["voice"], "tamil")

    def test_rate_is_clamped_and_forwarded(self):
        rec = RecordingVoice(language="en", rate=1.5)
        rec.voice.set_rate(99.0)
        self.assertEqual(rec.voice.rate, 2.0)
        rec.voice.set_rate(0.1)
        self.assertEqual(rec.voice.rate, 0.5)
        rec.voice.set_rate("not a number")  # ignored, no crash
        self.assertEqual(rec.voice.rate, 0.5)
        rec.voice.speak("hello")
        self.assertTrue(rec.wait_completion())
        self.assertEqual(rec.synth.calls[0]["rate"], 0.5)

    def test_every_registry_language_has_a_voice_selector(self):
        from companion.language_registry import LANGUAGES

        for spec in LANGUAGES:
            with self.subTest(lang=spec.code):
                rec = RecordingVoice(language=spec.code)
                self.assertTrue(rec.voice.set_language(spec.code))
                rec.voice.speak("test")
                self.assertTrue(rec.wait_completion())
                self.assertEqual(rec.synth.calls[0]["voice"], spec.espeak)


class VoiceEngineSeamTests(unittest.TestCase):
    def test_implements_the_abstract_interface(self):
        from companion.voice import VoiceEngine, voice_status

        rec = RecordingVoice()
        self.assertIsInstance(rec.voice, VoiceEngine)
        view = voice_status(rec.voice)
        self.assertTrue(view["available"])
        self.assertIn("espeak-ng", view["label"])

    def test_null_voice_still_reports_off(self):
        from companion.voice import NullVoice, voice_status

        view = voice_status(NullVoice())
        self.assertFalse(view["available"])
        self.assertEqual(view["label"], "Voice off")

    def test_on_done_callback_errors_do_not_crash_the_worker(self):
        def exploding(_ok, _err):
            raise RuntimeError("UI exploded")

        from companion.voice_output import CompanionVoice as CV

        done = []
        voice = CV(
            synthesizer=FakeSynthesizer(),
            player_factory=lambda s: FakePlayer(),
            on_done=exploding,
        )
        voice.speak("hello")
        # The worker must survive the bad callback (isolation).
        self.assertTrue(wait_until(lambda: not voice.speaking))

    def test_per_call_callback_overrides_the_instance_one(self):
        from companion.voice_output import CompanionVoice as CV

        instance_calls = []
        per_call = []
        voice = CV(
            synthesizer=FakeSynthesizer(),
            player_factory=lambda s: FakePlayer(),
            on_done=lambda ok, err: instance_calls.append((ok, err)),
        )
        voice.speak("hello", on_done=lambda ok, err: per_call.append((ok, err)))
        self.assertTrue(wait_until(lambda: len(per_call) == 1))
        self.assertEqual(instance_calls, [])


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
