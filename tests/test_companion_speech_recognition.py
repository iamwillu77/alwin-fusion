"""v0.9.0 speech-recognition tests (mocked engine — no model, no mic).

Pins the recognition contract:

* silence and too-short audio never reach the model (Whisper
  hallucinates over silence — the documented failure this prevents),
* what the model returns is what the caller gets, verbatim; segments
  marked non-speech are dropped and an empty result stays empty —
  no placeholder words are ever invented,
* a forced decode language is never reported as detection evidence
  (``asr_hint is None``), while auto-decode reports engine metadata,
* a missing engine/model is an honest, actionable
  :class:`RecognitionError` — and **no download is ever attempted**
  (the real engine is built with ``local_files_only=True``),
* async recognition runs on a worker, refuses double-starts, and
  ``cancel`` discards racing results instead of delivering them late,
* model status reflects the local cache only.
"""

from __future__ import annotations

import os
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest import mock

from companion import speech_recognition as sr
from companion.speech_recognition import (
    MODEL_SIZES,
    RecognitionError,
    RecognitionResult,
    SpeechRecognitionService,
    model_installed,
    model_status,
)


def pcm(seconds: float, nonzero: bool = True) -> bytes:
    n = int(seconds * 16000)
    if not nonzero:
        return b"\x00\x00" * n
    return (b"\x01\x40" if nonzero else b"\x00\x00") * n


class Segment:
    def __init__(self, text, no_speech_prob=0.0):
        self.text = text
        self.no_speech_prob = no_speech_prob


class Info:
    def __init__(self, language="ta", language_probability=0.87):
        self.language = language
        self.language_probability = language_probability


class FakeEngine:
    """Duck-typed WhisperModel: records calls, returns canned output."""

    def __init__(self, segments=None, info=None, error=None, block=None):
        self.segments = segments if segments is not None else [
            Segment(" hello world ")
        ]
        self.info = info or Info()
        self.error = error
        self.block = block  # threading.Event to wait on (or None)
        self.calls = []

    def transcribe(self, audio, language=None, beam_size=None):
        self.calls.append({"language": language, "beam_size": beam_size,
                           "audio_len": len(audio)})
        if self.block is not None:
            self.block.wait(timeout=5.0)
        if self.error is not None:
            raise self.error
        return list(self.segments), self.info


def make_service(engine=None, **kwargs):
    created = []

    def factory(size):
        created.append(size)
        if engine is None:
            raise AssertionError("engine should not be built in this test")
        return engine

    kwargs.setdefault("engine_factory", factory)
    service = SpeechRecognitionService(**kwargs)
    return service, created


class SilenceTests(unittest.TestCase):
    def test_pure_silence_never_reaches_the_model(self):
        engine = FakeEngine()
        service, created = make_service(engine)
        result = service.recognize(pcm(1.0, nonzero=False))
        self.assertTrue(result.no_speech)
        self.assertEqual(result.text, "")
        self.assertEqual(engine.calls, [])
        self.assertEqual(created, [])  # engine not even constructed

    def test_empty_and_tiny_audio_are_no_speech(self):
        engine = FakeEngine()
        service, _ = make_service(engine)
        for payload in (b"", pcm(0.05)):
            result = service.recognize(payload)
            self.assertTrue(result.no_speech, len(payload))
        self.assertEqual(engine.calls, [])

    def test_audio_at_threshold_reaches_the_model(self):
        engine = FakeEngine(segments=[Segment(" hi ")])
        service, _ = make_service(engine)
        result = service.recognize(pcm(0.2))
        self.assertEqual(len(engine.calls), 1)


class TranscriptTests(unittest.TestCase):
    def test_text_is_returned_verbatim_and_joined(self):
        engine = FakeEngine(segments=[
            Segment(" வணக்கம், "), Segment("நண்பரே.")
        ])
        service, _ = make_service(engine)
        result = service.recognize(pcm(1.0))
        self.assertEqual(result.text, "வணக்கம், நண்பரே.")
        self.assertFalse(result.no_speech)
        self.assertFalse(result.empty)

    def test_non_speech_segments_are_dropped(self):
        engine = FakeEngine(segments=[
            Segment("real words", no_speech_prob=0.1),
            Segment("hallucinated over silence", no_speech_prob=0.95),
        ])
        service, _ = make_service(engine)
        result = service.recognize(pcm(1.0))
        self.assertEqual(result.text, "real words")

    def test_all_non_speech_means_empty_not_invented(self):
        engine = FakeEngine(segments=[
            Segment("Thank you for watching!", no_speech_prob=0.99),
            Segment("", no_speech_prob=0.0),
        ])
        service, _ = make_service(engine)
        result = service.recognize(pcm(1.0))
        self.assertEqual(result.text, "")
        self.assertTrue(result.no_speech)

    def test_model_error_becomes_recognition_error(self):
        engine = FakeEngine(error=RuntimeError("cuda exploded"))
        service, _ = make_service(engine)
        with self.assertRaises(RecognitionError) as ctx:
            service.recognize(pcm(1.0))
        self.assertIn("cuda exploded", str(ctx.exception))


class HintTests(unittest.TestCase):
    def test_auto_decode_reports_engine_language_as_hint(self):
        engine = FakeEngine(info=Info("ta", 0.91))
        service, _ = make_service(engine)
        result = service.recognize(pcm(1.0))
        self.assertEqual(result.asr_hint, "ta")
        self.assertAlmostEqual(result.language_probability, 0.91)
        self.assertIsNone(engine.calls[0]["language"])  # auto mode

    def test_forced_language_is_not_detection_evidence(self):
        engine = FakeEngine(info=Info("ta", 0.99))
        service, _ = make_service(engine)
        result = service.recognize(pcm(1.0), language="zh")
        self.assertIsNone(result.asr_hint)
        self.assertEqual(result.language_probability, 0.0)
        self.assertEqual(engine.calls[0]["language"], "zh")


class AsyncTests(unittest.TestCase):
    def test_recognize_async_delivers_on_a_worker_thread(self):
        engine = FakeEngine(segments=[Segment("hello")])
        service, _ = make_service(engine)
        seen = {}
        done = threading.Event()

        def on_result(result):
            seen["result"] = result
            seen["thread"] = threading.current_thread().name
            done.set()

        started = service.recognize_async(pcm(0.5), on_result=on_result)
        self.assertTrue(started)
        self.assertTrue(done.wait(timeout=5.0))
        self.assertEqual(seen["result"].text, "hello")
        self.assertNotEqual(seen["thread"], threading.current_thread().name)
        self.assertTrue(done.wait(timeout=0.1))  # still set
        # busy clears after completion
        for _ in range(100):
            if not service.busy:
                break
            time.sleep(0.01)
        self.assertFalse(service.busy)

    def test_double_start_is_refused_while_busy(self):
        block = threading.Event()
        engine = FakeEngine(block=block)
        service, _ = make_service(engine)
        self.assertTrue(service.recognize_async(pcm(0.5)))
        self.assertFalse(service.recognize_async(pcm(0.5)))
        block.set()
        for _ in range(200):
            if not service.busy:
                break
            time.sleep(0.01)

    def test_cancel_discards_a_racing_result(self):
        block = threading.Event()
        engine = FakeEngine(block=block)
        service, _ = make_service(engine)
        delivered = []
        errors = []
        service.recognize_async(
            pcm(0.5),
            on_result=delivered.append,
            on_error=errors.append,
        )
        # Cancel while the decode is blocked inside the engine.
        service.cancel()
        block.set()
        time.sleep(0.2)  # let the worker finish and hit the stale check
        self.assertEqual(delivered, [])
        self.assertEqual(errors, [])
        self.assertFalse(service.busy)

    def test_async_error_is_reported_not_raised(self):
        engine = FakeEngine(error=RuntimeError("boom"))
        service, _ = make_service(engine)
        errors = []
        results = []
        done = threading.Event()

        def on_error(exc):
            errors.append(exc)
            done.set()

        service.recognize_async(pcm(0.5), on_result=results.append,
                                on_error=on_error)
        self.assertTrue(done.wait(timeout=5.0))
        self.assertEqual(results, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("boom", str(errors[0]))


class EngineAvailabilityTests(unittest.TestCase):
    def test_unknown_model_size_is_rejected_eagerly(self):
        with self.assertRaises(ValueError):
            SpeechRecognitionService(model_size="huge")
        self.assertEqual(MODEL_SIZES, ("tiny", "base", "small", "medium"))

    def test_missing_model_raises_before_any_network(self):
        # A real engine factory is None; with the cache reported
        # empty, building must fail with the installer instructions.
        service = SpeechRecognitionService(engine_factory=None)
        fake_fw = types.ModuleType("faster_whisper")

        class _NeverCalled:
            def __init__(self, *args, **kwargs):
                raise AssertionError("local_files_only must be respected")

        fake_fw.WhisperModel = _NeverCalled
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_fw}):
            with mock.patch.object(sr, "model_installed", return_value=False):
                with self.assertRaises(RecognitionError) as ctx:
                    service._build_engine()
        message = str(ctx.exception)
        self.assertIn("not downloaded", message)
        self.assertIn("install_voice_models.py", message)
        self.assertIn("no download was attempted", message)

    def test_missing_package_raises_actionable_error(self):
        service = SpeechRecognitionService(engine_factory=None)
        # On a system without faster-whisper the import fails honestly.
        try:
            import faster_whisper  # noqa: F401

            self.skipTest("faster-whisper is installed here")
        except Exception:
            pass
        with self.assertRaises(RecognitionError) as ctx:
            service._build_engine()
        self.assertIn("pip install faster-whisper", str(ctx.exception))

    def test_real_engine_is_built_local_only(self):
        service = SpeechRecognitionService(engine_factory=None)
        fake_fw = types.ModuleType("faster_whisper")
        captured = {}

        class _RecordingModel:
            def __init__(self, size, device=None, compute_type=None,
                         local_files_only=False):
                captured.update(size=size, device=device,
                                local_files_only=local_files_only)

        fake_fw.WhisperModel = _RecordingModel
        with mock.patch.dict(sys.modules, {"faster_whisper": fake_fw}):
            with mock.patch.object(sr, "model_installed", return_value=True):
                engine = service._build_engine()
        self.assertIsInstance(engine, _RecordingModel)
        self.assertTrue(captured["local_files_only"])
        self.assertEqual(captured["size"], "small")

    def test_available_requires_installed_model(self):
        service = SpeechRecognitionService(engine_factory=lambda s: None)
        with mock.patch.object(sr, "model_installed", return_value=True):
            self.assertTrue(service.available)
        with mock.patch.object(sr, "model_installed", return_value=False):
            self.assertFalse(service.available)


class ModelStatusTests(unittest.TestCase):
    def test_status_reports_missing_model_with_installer_command(self):
        with tempfile.TemporaryDirectory() as tmp:
            with mock.patch.object(sr, "_hub_cache_dir", return_value=tmp):
                status = model_status("small")
        self.assertFalse(status["installed"])
        self.assertIn("install_voice_models.py", status["detail"])
        self.assertIn("never downloads", status["detail"])

    def test_status_reports_installed_model(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "models--Systran--faster-whisper-small")
            os.makedirs(os.path.join(target, "blobs"))
            os.makedirs(os.path.join(target, "refs"))
            with mock.patch.object(sr, "_hub_cache_dir", return_value=tmp):
                self.assertTrue(model_installed("small"))
                status = model_status("small")
        self.assertTrue(status["installed"])
        self.assertIn("offline", status["detail"])

    def test_incomplete_download_counts_as_not_installed(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = os.path.join(tmp, "models--Systran--faster-whisper-small")
            os.makedirs(os.path.join(target, "blobs"))  # no refs/
            with mock.patch.object(sr, "_hub_cache_dir", return_value=tmp):
                self.assertFalse(model_installed("small"))

    def test_unknown_size_status_is_honest(self):
        status = model_status("gigantic")
        self.assertFalse(status["installed"])
        self.assertIn("Unknown model", status["detail"])

    def test_service_status_mentions_missing_package(self):
        service = SpeechRecognitionService(engine_factory=None)
        try:
            import faster_whisper  # noqa: F401

            self.skipTest("faster-whisper is installed here")
        except Exception:
            pass
        status = service.status()
        self.assertFalse(status["installed"])
        self.assertIn("not installed", status["detail"])


class ResultShapeTests(unittest.TestCase):
    def test_result_is_a_frozen_value(self):
        result = RecognitionResult("x", "en", 0.9, False, 1.0, "small")
        self.assertTrue(result.empty is False)
        with self.assertRaises(Exception):
            result.text = "y"

    def test_empty_property_matches_whitespace(self):
        self.assertTrue(RecognitionResult("   ", None, 0.0, True, 1.0,
                                          "small").empty)

    def test_service_rejects_unknown_size_at_construction(self):
        with self.assertRaises(ValueError):
            SpeechRecognitionService(model_size="v3")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
