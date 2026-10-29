"""v0.9.0 microphone capture tests (fake backend — no hardware).

Pins the capture contract the UI depends on:

* start/stop/cancel/release lifecycle with honest errors,
* captured audio is returned **in memory only** (RAM bytes — nothing
  here can write a file: no wave/audioop anywhere in the module),
* silence is detectable before ASR ever sees the buffer,
* the level meter reports loudness while listening and 0 when idle,
* a backend failure surfaces as a user-facing message, never a crash,
* after ``release`` the service refuses to record again (teardown).

The real GStreamer backend is exercised separately during hardware
verification; unit tests inject a fake through ``backend_factory``.
"""

from __future__ import annotations

import struct
import unittest

from companion.voice_input import (
    CaptureResult,
    SAMPLE_RATE,
    VoiceInputError,
    VoiceInputService,
    _levels,
)


def pcm_tone(seconds: float, amplitude: int = 8000,
              rate: int = SAMPLE_RATE) -> bytes:
    """Deterministic non-silent S16LE audio (a square-ish wave)."""
    n = int(seconds * rate)
    return b"".join(
        struct.pack("<h", amplitude if (i // 40) % 2 == 0 else -amplitude)
        for i in range(n)
    )


class FakeBackend:
    """Injectable stand-in for the GStreamer capture pipeline."""

    instances = []

    def __init__(self, *, fail_open=None, error_on_poll=None,
                 chunks=None):
        self.fail_open = fail_open
        self.error_on_poll = error_on_poll
        self.chunks = chunks or []
        self.opened = False
        self.closed = False
        self.on_sample = None
        FakeBackend.instances.append(self)

    def open(self, on_sample, on_error):
        if self.fail_open is not None:
            raise VoiceInputError(self.fail_open)
        self.opened = True
        self.on_sample = on_sample

    def poll_error(self):
        return self.error_on_poll

    def close(self):
        self.closed = True

    def feed(self, data: bytes) -> None:
        assert self.opened and not self.closed
        self.on_sample(data)


def make_service(**backend_kwargs) -> "tuple[VoiceInputService, list]":
    created = []

    def factory():
        backend = FakeBackend(**backend_kwargs)
        created.append(backend)
        return backend

    return VoiceInputService(backend_factory=factory), created


class LifecycleTests(unittest.TestCase):
    def test_stop_returns_captured_pcm_in_memory(self):
        service, backends = make_service()
        service.start()
        payload = pcm_tone(0.5)
        backends[0].feed(payload)
        result = service.stop()
        self.assertIsInstance(result, CaptureResult)
        self.assertEqual(result.pcm, payload)
        self.assertGreater(result.seconds, 0.4)
        self.assertLessEqual(result.seconds, 0.51)
        self.assertFalse(result.cancelled)
        self.assertFalse(service.listening)

    def test_start_is_idempotent_while_listening(self):
        service, backends = make_service()
        service.start()
        service.start()
        self.assertEqual(len(backends), 1)
        self.assertTrue(service.listening)
        service.release()

    def test_cancel_discards_what_was_heard(self):
        service, backends = make_service()
        service.start()
        backends[0].feed(pcm_tone(1.0))
        result = service.cancel()
        self.assertTrue(result.cancelled)
        self.assertEqual(result.pcm, b"")  # nothing kept
        self.assertTrue(backends[0].closed)
        # The cancelled audio is gone for good (drained).
        self.assertEqual(service.stop().pcm, b"")

    def test_stop_without_start_is_a_safe_noop(self):
        service, _ = make_service()
        result = service.stop()
        self.assertEqual(result.pcm, b"")
        self.assertFalse(service.listening)

    def test_release_stops_and_refuses_reuse(self):
        service, backends = make_service()
        service.start()
        service.release()
        self.assertTrue(backends[0].closed)
        self.assertTrue(service.released)
        with self.assertRaises(VoiceInputError):
            service.start()

    def test_context_manager_releases(self):
        service, backends = make_service()
        with service as svc:
            self.assertTrue(svc.listening)
        self.assertTrue(service.released)
        self.assertTrue(backends[0].closed)

    def test_backend_open_failure_is_a_user_facing_error(self):
        service, _ = make_service(
            fail_open="No microphone could be opened (device busy)."
        )
        with self.assertRaises(VoiceInputError) as ctx:
            service.start()
        self.assertIn("No microphone", str(ctx.exception))
        self.assertFalse(service.listening)
        self.assertIn("No microphone", service.last_error)
        # And it is retryable: a later start can still succeed? The
        # service must not be poisoned by one failure.
        self.assertFalse(service.released)


class MeterTests(unittest.TestCase):
    def test_level_is_zero_when_idle(self):
        service, _ = make_service()
        self.assertEqual(service.level(), 0.0)
        service.start()
        self.assertEqual(service.level(), 0.0)  # nothing heard yet
        service.release()

    def test_level_reflects_fed_audio(self):
        service, backends = make_service()
        service.start()
        backends[0].feed(pcm_tone(0.3, amplitude=12000))
        level = service.level()
        self.assertGreater(level, 0.1)
        self.assertLessEqual(level, 1.0)
        service.release()

    def test_seconds_listening_uses_the_injected_clock(self):
        now = [100.0]
        service = VoiceInputService(
            backend_factory=lambda: FakeBackend(),
            time_func=lambda: now[0],
        )
        service.start()
        now[0] = 103.5
        self.assertAlmostEqual(service.seconds_listening(), 3.5)
        service.release()
        self.assertEqual(service.seconds_listening(), 0.0)


class SilenceTests(unittest.TestCase):
    def test_silent_capture_is_flagged_for_the_caller(self):
        service, backends = make_service()
        service.start()
        backends[0].feed(b"\x00\x00" * SAMPLE_RATE)  # pure digital silence
        result = service.stop()
        self.assertTrue(result.silent)
        self.assertEqual(result.peak, 0.0)
        self.assertEqual(result.rms, 0.0)
        self.assertEqual(result.sample_count, SAMPLE_RATE)

    def test_tone_capture_is_not_silent(self):
        service, backends = make_service()
        service.start()
        backends[0].feed(pcm_tone(0.2))
        result = service.stop()
        self.assertFalse(result.silent)
        self.assertGreater(result.peak, 0.1)
        self.assertGreater(result.rms, 0.05)

    def test_empty_capture_is_silent(self):
        service, _ = make_service()
        service.start()
        result = service.stop()
        self.assertTrue(result.silent)
        self.assertEqual(result.seconds, 0.0)

    def test_levels_math_is_pure_and_bounded(self):
        peak, rms = _levels(pcm_tone(0.1, amplitude=32767))
        self.assertAlmostEqual(peak, 32767 / 32768, places=3)
        self.assertGreater(rms, 0.9)
        self.assertEqual(_levels(b""), (0.0, 0.0))
        self.assertEqual(_levels(b"\x00\x00"), (0.0, 0.0))


class ErrorSurfaceTests(unittest.TestCase):
    def test_mid_stream_device_error_surfaces_on_stop(self):
        service, backends = make_service(error_on_poll="Device unplugged")
        service.start()
        backends[0].feed(pcm_tone(0.2))
        service.stop()
        self.assertEqual(service.last_error, "Device unplugged")

    def test_unexpected_backend_exception_is_normalized(self):
        class BrokenBackend(FakeBackend):
            def open(self, on_sample, on_error):
                raise RuntimeError("driver exploded")

        service = VoiceInputService(backend_factory=BrokenBackend)
        with self.assertRaises(VoiceInputError) as ctx:
            service.start()
        self.assertIn("driver exploded", str(ctx.exception))
        self.assertFalse(service.listening)

    def test_error_callback_from_backend_is_recorded(self):
        class ErrorBackend(FakeBackend):
            def open(self, on_sample, on_error):
                super().open(on_sample, on_error)
                on_error("permission denied by portal")

        service = VoiceInputService(backend_factory=ErrorBackend)
        service.start()
        self.assertEqual(service.last_error, "permission denied by portal")
        service.release()


class MemoryOnlyTests(unittest.TestCase):
    def test_module_never_touches_audio_files(self):
        import pathlib

        from companion import voice_input as module

        source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
        for banned in ("import wave", "import audioop", "wave.open",
                       ".wav", "soundfile", "sf.write"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_captures_are_plain_bytes(self):
        service, backends = make_service()
        service.start()
        backends[0].feed(pcm_tone(0.1))
        result = service.stop()
        self.assertIsInstance(result.pcm, bytes)
        # Frozen dataclass: results are values, not mutable handles.
        with self.assertRaises(Exception):
            result.pcm = b""


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
