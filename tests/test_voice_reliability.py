"""v0.21.0 voice & hands-free reliability tests.

Labels (accurate, per milestone brief):

* **SANDBOX** — capture/state tests run the REAL services with
  injected fake backends (no hardware, no display, no model).
* **MOCKED** — device streams are fakes; no result here is
  physical-microphone evidence (that lives in
  scripts/verify_voice_reliability.py with explicit labels).
* **STATIC** — source pins (no audio persistence, content-free
  diagnostics, no shell/synthetic input).

Covers the failure modes found in the Phase-1 audit: bounded capture
buffer, frame alignment, device-error vs silence, duplicate start/stop,
release semantics, diagnostics shape, state-machine legal/illegal
transitions, and the existing cancellation contracts.
"""

from __future__ import annotations

import ast
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from companion.voice_input import (  # noqa: E402
    FRAME_BYTES,
    MAX_CAPTURE_BYTES,
    MAX_CAPTURE_SECONDS,
    VoiceInputError,
    VoiceInputService,
)
from test_companion_window_voice import FakeBackend  # noqa: E402

SECOND = b"\x01\x40" * 16000      # 1 s of NONZERO S16LE frames (aligned)


class ErroringBackend(FakeBackend):
    """FakeBackend whose stream can die mid-capture."""

    def __init__(self):
        super().__init__()
        self.fail_next_poll = None

    def poll_error(self):
        return self.fail_next_poll


def make_service(**kwargs) -> VoiceInputService:
    kwargs.setdefault("backend_factory", FakeBackend)
    return VoiceInputService(**kwargs)


# --------------------------------------------------------------------------- #
# Capture hardening (SANDBOX over injected backends)
# --------------------------------------------------------------------------- #


class CaptureBoundsTests(unittest.TestCase):

    def test_buffer_never_exceeds_the_hard_cap_and_keeps_newest(self):
        svc = make_service()
        svc.start()
        backend = svc._backend
        # Feed far beyond the cap (cap = 60 s; feed 80 s).
        for _ in range(80):
            backend.feed(SECOND)
        result = svc.stop()
        svc.release()
        self.assertLessEqual(len(result.pcm), MAX_CAPTURE_BYTES)
        self.assertTrue(result.truncated)
        self.assertEqual(len(result.pcm) % FRAME_BYTES, 0)
        # Newest window survives: the cap is full (60 s exactly here).
        self.assertEqual(len(result.pcm), MAX_CAPTURE_BYTES)
        self.assertEqual(MAX_CAPTURE_SECONDS, 60)

    def test_odd_length_chunks_are_dropped_never_stored(self):
        svc = make_service()
        svc.start()
        backend = svc._backend
        backend.feed(SECOND)                # aligned: kept
        backend.on_sample(b"\x01\x02\x03")  # misaligned: dropped
        backend.on_sample(b"")              # empty: dropped
        backend.on_sample(None)             # malformed: dropped
        result = svc.stop()
        svc.release()
        self.assertEqual(len(result.pcm), len(SECOND))
        self.assertEqual(result.dropped_frames, 3)

    def test_device_missing_reports_honestly_and_stays_stopped(self):
        def broken_factory():
            raise VoiceInputError("No microphone could be opened.")

        svc = VoiceInputService(backend_factory=broken_factory)
        with self.assertRaises(VoiceInputError):
            svc.start()
        self.assertFalse(svc.listening)
        self.assertIn("No microphone", svc.last_error)
        # A failed start never leaves a half-open service behind.
        self.assertEqual(svc.stop().seconds, 0.0)
        svc.release()

    def test_backend_exception_is_normalized_to_honest_error(self):
        def exploding_factory():
            class Exploder:
                def open(self, on_sample, on_error):
                    raise RuntimeError("driver exploded")

            return Exploder()

        svc = VoiceInputService(backend_factory=exploding_factory)
        with self.assertRaises(VoiceInputError) as ctx:
            svc.start()
        self.assertIn("driver exploded", str(ctx.exception))
        self.assertFalse(svc.listening)
        svc.release()

    def test_unexpected_stream_termination_surfaces_at_stop(self):
        svc = make_service(backend_factory=ErroringBackend)
        svc.start()
        svc._backend.feed(SECOND)
        svc._backend.fail_next_poll = "device disappeared"
        result = svc.stop()
        svc.release()
        self.assertGreater(result.seconds, 0.0)
        self.assertIn("device disappeared", svc.last_error)

    def test_repeated_start_stop_cycles_create_one_stream_each(self):
        calls = {"open": 0}

        def factory():
            calls["open"] += 1
            return FakeBackend()

        svc = VoiceInputService(backend_factory=factory)
        svc.start()
        svc.start()                    # idempotent: no second stream
        self.assertEqual(calls["open"], 1)
        svc.stop()
        svc.stop()                     # second stop is an empty no-op
        self.assertEqual(svc.stop().seconds, 0.0)
        svc.start()
        self.assertEqual(calls["open"], 2)
        svc.stop()
        svc.release()
        svc.release()                  # duplicate release is safe
        with self.assertRaises(VoiceInputError):
            svc.start()                # released services never reopen

    def test_shutdown_during_capture_discards_everything(self):
        svc = make_service()
        svc.start()
        svc._backend.feed(SECOND)
        svc.release()                  # teardown while listening
        self.assertFalse(svc.listening)
        self.assertTrue(svc.released)
        result = svc.cancel()          # post-release cancel is a no-op
        self.assertEqual(result.pcm, b"")

    def test_cancel_then_stop_never_resurrects_audio(self):
        svc = make_service()
        svc.start()
        svc._backend.feed(SECOND)
        cancelled = svc.cancel()
        self.assertTrue(cancelled.cancelled)
        self.assertEqual(cancelled.pcm, b"")
        after = svc.stop()             # already stopped: empty
        self.assertEqual(after.pcm, b"")
        svc.release()

    def test_observer_exception_never_breaks_capture(self):
        def hostile(_chunk):
            raise RuntimeError("observer bug")

        svc = make_service(on_sample=hostile)
        svc.start()
        svc._backend.feed(SECOND)
        result = svc.stop()
        svc.release()
        self.assertEqual(len(result.pcm), len(SECOND))

    def test_diagnostics_are_bounded_and_content_free(self):
        svc = make_service()
        svc.start()
        svc._backend.feed(SECOND)
        diag = svc.diagnostics()
        svc.release()
        for key in ("listening", "format", "buffer_bytes",
                    "buffer_cap_bytes", "truncated", "chunks",
                    "dropped_chunks", "chunks_per_second",
                    "last_chunk_age_seconds", "peak", "rms", "clipping",
                    "seconds_listening", "last_error"):
            self.assertIn(key, diag)
        self.assertEqual(diag["format"], "16000 Hz / 1 ch / S16LE")
        self.assertLessEqual(diag["buffer_bytes"],
                             diag["buffer_cap_bytes"])
        self.assertIsInstance(diag["clipping"], bool)
        payload = repr(diag)
        self.assertNotIn("pcm", diag)
        self.assertLess(len(payload), 2000)

    def test_clipping_flag_is_measured_from_peak(self):
        loud = b"\x00\x7f" * 16000          # +32767 = full scale
        svc = make_service()
        svc.start()
        svc._backend.feed(loud)
        diag = svc.diagnostics()
        svc.release()
        self.assertTrue(diag["clipping"])
        self.assertGreaterEqual(diag["peak"], 0.99)

    def test_truncated_capture_is_flagged_not_silent(self):
        svc = make_service()
        svc.start()
        for _ in range(MAX_CAPTURE_SECONDS + 20):
            svc._backend.feed(SECOND)
        result = svc.stop()
        svc.release()
        self.assertTrue(result.truncated)
        self.assertFalse(result.silent)


# --------------------------------------------------------------------------- #
# State machine (SANDBOX: real methods on a minimal stand-in)
# --------------------------------------------------------------------------- #


class _PhaseHost:
    """Minimal stand-in carrying exactly what _set_phase touches."""

    ALLOWED_TRANSITIONS = None  # assigned from the real class below

    def __init__(self, phase: str = "off"):
        self._voice_phase = phase
        self._phase_log = []
        self._PHASE_LOG_ENTRIES = 40


def _host(phase: str = "off") -> _PhaseHost:
    from companion.window import CompanionWindow
    host = _PhaseHost(phase)
    host.ALLOWED_TRANSITIONS = CompanionWindow.ALLOWED_TRANSITIONS
    host._set_phase = lambda new, reason="": (
        CompanionWindow._set_phase(host, new, reason))
    return host


class StateMachineTests(unittest.TestCase):

    def test_every_declared_transition_is_accepted(self):
        from companion.window import CompanionWindow
        for current, targets in CompanionWindow.ALLOWED_TRANSITIONS.items():
            for target in targets:
                host = _host(current)
                self.assertTrue(host._set_phase(target, "pin"),
                                f"{current} -> {target}")

    def test_illegal_transition_is_refused_and_logged(self):
        host = _host("speaking")
        self.assertFalse(host._set_phase("listening", "stale callback"))
        self.assertEqual(host._voice_phase, "speaking")
        self.assertTrue(host._phase_log[-1].startswith("REFUSED"))
        host2 = _host("recognizing")
        self.assertFalse(host2._set_phase("waking"))
        self.assertEqual(host2._voice_phase, "recognizing")

    def test_same_phase_is_idempotent(self):
        host = _host("wake")
        self.assertTrue(host._set_phase("wake", "already"))
        self.assertEqual(host._voice_phase, "wake")

    def test_phase_log_is_bounded(self):
        host = _host("off")
        for _ in range(100):
            host._set_phase("wake")           # off -> wake
            host._set_phase("off")            # wake -> off
            host._set_phase("bogus")          # refused every time
        self.assertLessEqual(len(host._phase_log), 40)

    def test_any_phase_can_stop_to_off(self):
        from companion.window import CompanionWindow
        for phase in CompanionWindow.ALLOWED_TRANSITIONS:
            self.assertIn("off",
                          CompanionWindow.ALLOWED_TRANSITIONS[phase])


# --------------------------------------------------------------------------- #
# Security & privacy (STATIC)
# --------------------------------------------------------------------------- #


class VoiceSecurityTests(unittest.TestCase):

    def test_capture_module_persists_no_audio(self):
        # AST-based: no file, network or audio-codec imports anywhere
        # in the capture module (substring bans would false-positive on
        # the module's own security docstring, which NAMES the bans).
        banned = {"wave", "audioop", "requests", "socket", "urllib",
                  "http", "pathlib", "sqlite3", "pickle"}
        imports = set()
        tree = ast.parse(
            (ROOT / "companion" / "voice_input.py").read_text(
                encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertEqual(imports & banned, set())

    def test_diagnostics_never_include_payload_keys(self):
        path = ROOT / "companion" / "voice_input.py"
        source = path.read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "diagnostics")
        segment = ast.get_source_segment(source, fn)
        for banned in ('"pcm"', '"transcript"', '"text"'):
            self.assertNotIn(banned, segment)

    def test_wake_word_stays_default_off(self):
        from companion.settings import defaults
        self.assertFalse(defaults().get("wake_word_enabled", True))

    def test_no_shell_or_synthetic_input_in_voice_modules(self):
        # No shell, no synthetic input, no external control tools; the
        # per-module `os` usage (model paths) is allowed — `os.system`
        # style calls are not (checked as call attributes).
        banned_modules = {"subprocess", "pynput", "xdotool",
                          "pyautogui"}
        banned_calls = {"system", "popen", "check_output"}
        for name in ("voice_input.py", "speech_recognition.py",
                     "voice_output.py", "wake_word.py", "voice.py"):
            imports = set()
            calls = set()
            tree = ast.parse(
                (ROOT / "companion" / name).read_text(encoding="utf-8"))
            for node in ast.walk(tree):
                if isinstance(node, ast.Import):
                    imports.update(a.name.split(".")[0]
                                   for a in node.names)
                elif isinstance(node, ast.ImportFrom) and node.module:
                    imports.add(node.module.split(".")[0])
                elif isinstance(node, ast.Call):
                    func = node.func
                    if isinstance(func, ast.Attribute):
                        calls.add(func.attr)
                    elif isinstance(func, ast.Name):
                        calls.add(func.id)
            self.assertEqual(imports & banned_modules, set(), name)
            self.assertEqual(calls & banned_calls, set(), name)


# --------------------------------------------------------------------------- #
# espeak-ng system fallback (v0.21 — loader package absent)
# --------------------------------------------------------------------------- #


class EspeakFallbackTests(unittest.TestCase):

    def test_fallback_requires_an_existing_data_directory(self):
        from unittest import mock

        from companion.voice_output import EspeakSynthesizer
        with mock.patch("os.path.isdir", return_value=False):
            self.assertIsNone(EspeakSynthesizer._system_espeak_paths())

    def test_fallback_returns_library_and_data_when_present(self):
        from unittest import mock

        from companion.voice_output import EspeakSynthesizer
        with mock.patch("os.path.isdir", return_value=True), \
                mock.patch("ctypes.util.find_library",
                           return_value="libespeak-ng.so.1"):
            result = EspeakSynthesizer._system_espeak_paths()
        self.assertIsNotNone(result)
        library, data = result
        self.assertEqual(library, "libespeak-ng.so.1")
        self.assertTrue(data.endswith("espeak-ng-data"))

    def test_fallback_never_downloads_or_shells_out(self):
        from companion.voice_output import EspeakSynthesizer
        import inspect
        source = inspect.getsource(
            EspeakSynthesizer._system_espeak_paths)
        for banned in ("subprocess", "urllib", "requests", "pip ",
                       "curl", "wget", "os.system", "check_output"):
            self.assertNotIn(banned, source)

    def test_available_is_a_probe_with_honest_error_when_not(self):
        from companion.voice_output import CompanionVoice
        voice = CompanionVoice()
        available = voice.available        # probed on THIS host
        self.assertIsInstance(available, bool)
        if not available:
            self.assertTrue(voice.last_error)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
