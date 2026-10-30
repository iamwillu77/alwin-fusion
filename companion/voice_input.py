"""Microphone capture for the companion (v0.12.0) — memory only, honest.

Opens the default microphone through **GStreamer** (``autoaudiosrc`` →
16 kHz mono S16 PCM into an ``appsink``) and buffers samples *in RAM
only*: this module never writes audio to disk (no ``wave``, no
``audioop``, no files — the security meta-tests ban them package-wide)
and never uploads anything.

Lifecycle (all non-blocking on the caller's thread):

* :meth:`VoiceInputService.start` — open the device and begin
  buffering; raises :class:`VoiceInputError` with a human explanation
  when no microphone exists/is busy (never a silent failure),
* :meth:`VoiceInputService.stop` — close the device and return the
  captured PCM as an immutable :class:`CaptureResult` (seconds, peak,
  RMS — enough for the UI meter and for a silence pre-check),
* :meth:`VoiceInputService.cancel` — close and *discard* what was
  heard (user changed their mind; nothing is kept),
* :meth:`VoiceInputService.release` — final teardown for window
  close; after release the service refuses to start again (the UI
  creates a fresh service per window),
* :meth:`VoiceInputService.level` — live 0.0–1.0 loudness for the
  mic meter, computed from the rolling buffer.

The GStreamer import is **lazy**: importing this module (and the test
suite) never touches audio devices; only :meth:`start` does, and only
on the real backend.  Tests inject a fake backend through
``backend_factory`` — no hardware is ever required headless.

Sample format is fixed and documented: signed 16-bit little-endian,
16000 Hz, mono — exactly what the speech recognizer consumes.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

#: The one audio format this stack uses end-to-end.
SAMPLE_RATE = 16000
CHANNELS = 1
SAMPLE_WIDTH = 2  # bytes (S16)

#: A captured chunk from the backend (S16LE bytes).
ChunkCallback = Callable[[bytes], None]
ErrorCallback = Callable[[str], None]


class VoiceInputError(Exception):
    """A microphone failure with a user-facing explanation."""


@dataclass(frozen=True)
class CaptureResult:
    """What the microphone actually heard — RAM only, never persisted."""

    #: S16LE mono 16 kHz PCM (may be empty when nothing was captured).
    pcm: bytes
    #: Captured duration in seconds (0.0 for an empty capture).
    seconds: float
    #: Peak absolute amplitude, 0.0–1.0.
    peak: float
    #: RMS loudness, 0.0–1.0 (silence ≈ 0.0).
    rms: float
    #: True when the user cancelled instead of stopping normally.
    cancelled: bool = False

    @property
    def sample_count(self) -> int:
        return len(self.pcm) // SAMPLE_WIDTH

    @property
    def silent(self) -> bool:
        """True when there is effectively nothing to recognize."""
        return not self.pcm or self.peak <= 0.0

    @property
    def bytes_per_second(self) -> int:
        return SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH


def _levels(pcm: bytes) -> "tuple[float, float]":
    """(peak, rms) of S16LE audio, normalized to 0.0–1.0. Pure math."""
    count = len(pcm) // SAMPLE_WIDTH
    if count == 0:
        return 0.0, 0.0
    peak = 0
    total = 0
    # Sample every frame's first channel (mono: each frame is 2 bytes).
    for i in range(0, count * SAMPLE_WIDTH, SAMPLE_WIDTH):
        value = int.from_bytes(pcm[i:i + SAMPLE_WIDTH], "little",
                               signed=True)
        magnitude = -value if value < 0 else value
        if magnitude > peak:
            peak = magnitude
        total += value * value
    rms_sq = total // count
    return peak / 32768.0, (rms_sq ** 0.5) / 32768.0


class GStreamerBackend:
    """Captures 16 kHz mono PCM from the default microphone via Gst.

    Constructed lazily by :class:`VoiceInputService`; the GStreamer
    import happens in :meth:`open`, so merely importing this module
    costs nothing and requires no display/device.
    """

    def __init__(self) -> None:
        self._pipeline = None
        self._sink = None
        self._bus = None
        self._on_error: Optional[ErrorCallback] = None

    def open(self, on_sample: ChunkCallback,
             on_error: ErrorCallback) -> None:
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except Exception as exc:  # noqa: BLE001 - honest absence
            raise VoiceInputError(
                "GStreamer is not available on this system, so the "
                "microphone cannot be opened. Install it with your "
                "package manager (Debian/Ubuntu: 'sudo apt install "
                "gstreamer1.0-plugins-base gstreamer1.0-plugins-good')."
            ) from exc

        Gst.init(None)
        self._on_error = on_error
        pipeline_desc = (
            "autoaudiosrc ! audioconvert ! audioresample ! "
            f"audio/x-raw,format=S16LE,rate={SAMPLE_RATE},"
            f"channels={CHANNELS} ! "
            "appsink name=sink emit-signals=true sync=false "
            "max-buffers=16 drop=true"
        )
        try:
            pipeline = Gst.parse_launch(pipeline_desc)
        except Exception as exc:  # noqa: BLE001
            raise VoiceInputError(
                f"The microphone pipeline could not be created: {exc}"
            ) from exc

        sink = pipeline.get_by_name("sink")

        def _new_sample(_sink):
            sample = _sink.emit("pull-sample")
            if sample is None:
                return Gst.FlowReturn.OK
            buffer = sample.get_buffer()
            ok, info = buffer.map(Gst.MapFlags.READ)
            if ok:
                try:
                    on_sample(bytes(info.data))
                finally:
                    buffer.unmap(info)
            return Gst.FlowReturn.OK

        sink.connect("new-sample", _new_sample)

        change = pipeline.set_state(Gst.State.PLAYING)
        if change == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise VoiceInputError(
                "No microphone could be opened (the audio device is "
                "missing, already in use, or permission was denied)."
            )
        # Give the async state change a moment to fail loudly (two
        # seconds is how long GStreamer itself waits before deciding).
        result, _state, _pending = pipeline.get_state(2 * Gst.SECOND)
        if result == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            raise VoiceInputError(
                "No microphone could be opened (the audio device is "
                "missing, already in use, or permission was denied)."
            )

        self._pipeline = pipeline
        self._sink = sink
        self._bus = pipeline.get_bus()

    def poll_error(self) -> Optional[str]:
        """Non-blocking check for a device error after open."""
        if self._bus is None:
            return None
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except Exception:  # pragma: no cover - open() already needed it
            return None
        message = self._bus.timed_pop_filtered(
            0, Gst.MessageType.ERROR | Gst.MessageType.EOS
        )
        if message is None:
            return None
        if message.type == Gst.MessageType.ERROR:
            err, debug = message.parse_error()
            return f"{err.message} ({debug})" if debug else str(err)
        return "The microphone stream ended unexpectedly."

    def close(self) -> None:
        if self._pipeline is not None:
            try:
                import gi

                gi.require_version("Gst", "1.0")
                from gi.repository import Gst

                self._pipeline.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001 - teardown must not raise
                pass
        self._pipeline = None
        self._sink = None
        self._bus = None


class VoiceInputService:
    """Non-blocking microphone capture with an honest lifecycle.

    ``backend_factory`` injects a fake backend for tests; the default
    builds the real :class:`GStreamerBackend` only when ``start`` is
    called (no audio device is touched at construction time).
    """

    def __init__(self, *, backend_factory: Optional[Callable[[], Any]] = None,
                 on_sample: Optional[Callable[[bytes], None]] = None,
                 time_func: Callable[[], float] = time.monotonic) -> None:
        self._backend_factory = backend_factory or GStreamerBackend
        # v0.10.0: optional streaming observer (the wake-word service)
        # receives every chunk as it arrives — in addition to (never
        # instead of) the bounded in-memory capture buffer.  A raising
        # observer is isolated: capture itself never breaks.
        self._on_sample = on_sample
        self._time = time_func
        self._backend: Optional[Any] = None
        self._buffer = bytearray()
        self._lock = threading.Lock()
        self._started_at = 0.0
        self._last_error: Optional[str] = None
        self._released = False
        self._listening = False

    # ------------------------------------------------------------------ #
    # State
    # ------------------------------------------------------------------ #

    @property
    def listening(self) -> bool:
        """True while the microphone is open and buffering."""
        return self._listening

    @property
    def released(self) -> bool:
        """True after :meth:`release` (window teardown)."""
        return self._released

    @property
    def last_error(self) -> Optional[str]:
        """The most recent capture failure (None when healthy)."""
        return self._last_error

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def start(self) -> None:
        """Open the microphone and start buffering (non-blocking).

        Raises :class:`VoiceInputError` when no microphone can be
        opened — the caller shows the message, it is never swallowed.
        """
        if self._released:
            raise VoiceInputError(
                "The microphone was already released; open a new input "
                "service before recording again."
            )
        if self._listening:
            return  # already recording — start is idempotent

        backend = self._backend_factory()
        with self._lock:
            self._buffer = bytearray()
        self._last_error = None

        def _on_sample(chunk: bytes) -> None:
            with self._lock:
                self._buffer.extend(chunk)
            observer = self._on_sample
            if observer is not None:
                try:
                    observer(chunk)
                except Exception:  # noqa: BLE001 - observer isolation
                    pass

        def _on_error(message: str) -> None:
            self._last_error = message

        try:
            backend.open(_on_sample, _on_error)
        except VoiceInputError as exc:
            self._backend = None
            self._last_error = str(exc)
            raise
        except Exception as exc:  # noqa: BLE001 - normalize backend errors
            message = f"The microphone could not be opened: {exc}"
            self._last_error = message
            raise VoiceInputError(message) from exc

        self._backend = backend
        self._started_at = self._time()
        self._listening = True

    def stop(self) -> CaptureResult:
        """Close the mic and return what was heard (never raises).

        A backend error surfaced mid-capture is attached to
        :attr:`last_error`; the partial buffer is still returned so the
        caller decides (usually: nothing usable → say so).
        """
        if not self._listening:
            return CaptureResult(b"", 0.0, 0.0, 0.0, cancelled=False)
        backend = self._backend
        if backend is not None:
            poll = getattr(backend, "poll_error", None)
            if callable(poll):
                try:
                    error = poll()
                except Exception:  # noqa: BLE001
                    error = None
                if error:
                    self._last_error = error
            try:
                backend.close()
            except Exception as exc:  # noqa: BLE001
                self._last_error = f"Microphone shutdown failed: {exc}"
        self._listening = False
        self._backend = None
        return self._drain()

    def cancel(self) -> CaptureResult:
        """Close the mic and discard the audio (user backed out).

        The discarded buffer is dropped entirely — the caller receives
        an empty, ``cancelled`` result and can never read it back.
        """
        if not self._listening:
            return CaptureResult(b"", 0.0, 0.0, 0.0, cancelled=True)
        backend = self._backend
        if backend is not None:
            try:
                backend.close()
            except Exception:  # noqa: BLE001 - cancel never raises
                pass
        self._listening = False
        self._backend = None
        with self._lock:
            self._buffer = bytearray()  # discarded for good
        return CaptureResult(b"", 0.0, 0.0, 0.0, cancelled=True)

    def release(self) -> None:
        """Final teardown (window closed): stop, discard, refuse reuse."""
        if self._listening:
            self.cancel()
        self._released = True

    def __enter__(self) -> "VoiceInputService":
        self.start()
        return self

    def __exit__(self, *_exc) -> None:
        self.release()

    # ------------------------------------------------------------------ #
    # Live feedback
    # ------------------------------------------------------------------ #

    def level(self) -> float:
        """Current loudness 0.0–1.0 for the UI meter (0 when idle)."""
        if not self._listening:
            return 0.0
        with self._lock:
            recent = bytes(self._buffer[-SAMPLE_RATE // 5 * SAMPLE_WIDTH:])
        _peak, rms = _levels(recent)
        return rms

    def seconds_listening(self) -> float:
        """How long the mic has been open (0.0 when idle)."""
        if not self._listening:
            return 0.0
        return max(0.0, self._time() - self._started_at)

    # ------------------------------------------------------------------ #

    def _drain(self) -> CaptureResult:
        with self._lock:
            pcm = bytes(self._buffer)
            self._buffer = bytearray()
        seconds = (len(pcm) / (SAMPLE_RATE * CHANNELS * SAMPLE_WIDTH))
        peak, rms = _levels(pcm)
        return CaptureResult(pcm, seconds, peak, rms, cancelled=False)


__all__ = [
    "CaptureResult",
    "CHANNELS",
    "GStreamerBackend",
    "SAMPLE_RATE",
    "SAMPLE_WIDTH",
    "VoiceInputError",
    "VoiceInputService",
]
