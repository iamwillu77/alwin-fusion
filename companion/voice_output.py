"""Local text-to-speech for the companion (v0.13.0) — espeak-ng + Gst.

Implements the existing :class:`companion.voice.VoiceEngine` seam (the
window already accepts any VoiceEngine; ``NullVoice`` stays the
fallback when this engine cannot run).

How it speaks — all local, all offline:

* **Synthesis**: espeak-ng through ``ctypes``, loaded lazily via the
  ``espeakng-loader`` wheel (bundled library + voice data — no system
  package, no download at runtime).  The ctypes prototypes are set
  explicitly (argtypes/restype/callback) because a default-typed
  ``espeak_Synth`` call **segfaults** — verified the hard way.
* **Playback**: GStreamer ``appsrc → audioconvert → audioresample →
  alsasink`` fed from our own worker thread (GStreamer has no TTS
  elements on this system; the pipeline is created per utterance and
  torn down when it finishes, is stopped, or fails).
* **Voice per language**: the selector comes from the closed
  registry (:mod:`companion.language_registry` — e.g. ``tamil``,
  ``cmn``, ``French (France)``), never guessed here; an unknown
  language keeps the current voice and the status says so.

Honesty rules:

* ``available`` is *probed* (library + data + can it really load),
  never assumed; a missing espeak loader or audio device produces a
  clear status line, not a fake voice,
* ``speak`` returns ``True`` only when synthesis succeeded and
  playback was actually dispatched — synthesis failures return
  ``False`` immediately,
* ``on_done(success, error)`` reports the *completion truth*: a device
  failure mid-speech arrives as ``success=False`` so the window never
  claims it spoke when it did not,
* everything that takes time (synthesis + playback) runs on a daemon
  worker thread — the GTK main loop is never blocked,
* ``stop`` interrupts: the pending playback is discarded and
  ``on_done(False, "stopped")`` tells the caller it was cancelled.

Security: this is the **one** companion module allowed to import
``ctypes`` (there is no other way to drive a local TTS library);
``espeakng_loader`` and GStreamer are imported lazily inside methods,
so importing the module itself stays cheap and headless.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass
from typing import Any, Callable, Optional

from companion.language_registry import DEFAULT_LANGUAGE, get as get_language
from companion.voice import VoiceEngine

#: espeak-ng's synthesis rate (set by espeak_Initialize; documented
#: default — changed only if the library reports otherwise).
ESPEAK_DEFAULT_RATE = 22050

#: espeakRATE parameter id (words per minute control).
_ESPEAK_RATE_PARAM = 1

#: Bounds for the speech-rate setting (multiplier → wpm).
_RATE_MIN = 0.5
_RATE_MAX = 2.0
_BASE_WPM = 175

#: Final guard on what is ever synthesized/spoken (mirrors the
#: display-side bubble bound; speech is display text, verbatim).
MAX_SPEECH_CHARS = 600


class VoiceOutputError(Exception):
    """Synthesis/playback failure with a user-facing explanation."""


# --------------------------------------------------------------------------- #
# Synthesis (espeak-ng via ctypes)
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SynthesizedSpeech:
    """In-memory PCM from the synthesizer (never written to disk)."""

    #: S16LE mono PCM at ``sample_rate``.
    pcm: bytes
    #: Sample rate the synthesizer produced (espeak: 22050).
    sample_rate: int

    @property
    def seconds(self) -> float:
        return len(self.pcm) / (self.sample_rate * 2)


class EspeakSynthesizer:
    """ctypes wrapper around the espeak-ng library from espeakng-loader.

    The library is initialized once per process (guarded by a lock);
    every prototype is declared explicitly.  Thread-safe: synthesis is
    serialized by an internal lock (espeak state is global).
    """

    def __init__(self) -> None:
        self._lib = None
        self._init_rate = ESPEAK_DEFAULT_RATE
        self._lock = threading.RLock()
        self._samples: list = []
        self._callback = None
        self._load_error: Optional[str] = None

    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """True when the bundled library + voice data load (probed)."""
        return self._ensure_loaded() is not None

    @property
    def load_error(self) -> Optional[str]:
        """Why the synthesizer is unavailable (None when healthy)."""
        self._ensure_loaded()
        return self._load_error

    @property
    def sample_rate(self) -> int:
        return self._init_rate

    # ------------------------------------------------------------------ #

    def _ensure_loaded(self):
        """Load + initialize the library once; return it or None."""
        with self._lock:
            if self._lib is not None:
                return self._lib
            if self._load_error is not None:
                return None
            try:
                import ctypes

                from espeakng_loader import get_data_path, get_library_path
            except Exception as exc:  # noqa: BLE001 - honest absence
                self._load_error = (
                    "espeak-ng is not installed (the 'espeakng-loader' "
                    "package is missing). Install it with: pip install "
                    f"espeakng-loader — details: {exc}"
                )
                return None
            try:
                lib = ctypes.CDLL(str(get_library_path()))
                self._declare_prototypes(ctypes, lib)
                rate = lib.espeak_Initialize(
                    1,  # AUDIO_OUTPUT_SYNCHRONOUS: samples via callback
                    0,
                    str(get_data_path()).encode(),
                    0, 0,
                )
                if not rate:
                    raise RuntimeError("espeak_Initialize returned 0")
                self._init_rate = int(rate)
            except Exception as exc:  # noqa: BLE001
                self._load_error = (
                    "The espeak-ng library failed to initialize: "
                    f"{exc}"
                )
                return None
            self._lib = lib
            self._load_error = None
            return lib

    @staticmethod
    def _declare_prototypes(ctypes, lib) -> None:
        """Full prototypes — omitting these causes segfaults."""
        callback_type = ctypes.CFUNCTYPE(
            ctypes.c_int,
            ctypes.POINTER(ctypes.c_short),
            ctypes.c_int,
            ctypes.c_void_p,
        )
        lib.espeak_Initialize.restype = ctypes.c_int
        lib.espeak_Initialize.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_char_p,
            ctypes.c_int, ctypes.c_int,
        ]
        lib.espeak_SetSynthCallback.argtypes = [callback_type]
        lib.espeak_Synth.restype = ctypes.c_int
        lib.espeak_Synth.argtypes = [
            ctypes.c_void_p, ctypes.c_size_t, ctypes.c_uint,
            ctypes.c_uint, ctypes.c_uint, ctypes.c_int,
            ctypes.POINTER(ctypes.c_uint), ctypes.c_void_p,
        ]
        lib.espeak_Synchronize.restype = ctypes.c_int
        lib.espeak_Synchronize.argtypes = []
        lib.espeak_SetVoiceByName.restype = ctypes.c_int
        lib.espeak_SetVoiceByName.argtypes = [ctypes.c_char_p]
        lib.espeak_SetParameter.restype = ctypes.c_int
        lib.espeak_SetParameter.argtypes = [
            ctypes.c_int, ctypes.c_int, ctypes.c_int,
        ]
        # Keep the callback object alive for the library's lifetime.
        lib._alwin_callback_type = callback_type  # type: ignore[attr-defined]

    def synthesize(self, text: str, *, voice: str,
                   rate: float = 1.0) -> SynthesizedSpeech:
        """Synthesize *text* with *voice* (registry selector) to RAM.

        Raises :class:`VoiceOutputError` on a missing library, unknown
        voice or synthesis failure — callers surface it honestly.
        """
        clean = " ".join((text or "").split())
        if not clean:
            raise VoiceOutputError("There is no text to speak.")
        if len(clean) > MAX_SPEECH_CHARS:
            clean = clean[:MAX_SPEECH_CHARS]

        lib = self._ensure_loaded()
        if lib is None:
            raise VoiceOutputError(
                self._load_error or "Speech synthesis is unavailable."
            )
        import ctypes

        with self._lock:
            samples: list = []

            @lib._alwin_callback_type  # type: ignore[attr-defined]
            def _on_samples(wav, num_samples, _events):
                if wav and num_samples > 0:
                    samples.extend(wav[:num_samples])
                return 0

            # Keep the callback referenced for the duration of the call.
            self._callback = _on_samples
            lib.espeak_SetSynthCallback(_on_samples)

            voice_rc = lib.espeak_SetVoiceByName(voice.encode("utf-8"))
            if voice_rc != 0:
                self._callback = None
                raise VoiceOutputError(
                    f"The voice '{voice}' is not available in espeak-ng "
                    "for this language."
                )

            wpm = int(_BASE_WPM * max(_RATE_MIN, min(_RATE_MAX, rate)))
            lib.espeak_SetParameter(_ESPEAK_RATE_PARAM, wpm, 0)

            data = clean.encode("utf-8")
            rc = lib.espeak_Synth(
                data, len(data), 0, 0xFFFFFFFF, 0, 1, None, None
            )
            lib.espeak_Synchronize()
            self._callback = None
            if rc != 0:
                raise VoiceOutputError(
                    f"Speech synthesis failed (code {rc})."
                )
            if not samples:
                raise VoiceOutputError(
                    "Speech synthesis produced no audio."
                )
            pcm = b"".join(
                int(s).to_bytes(2, "little", signed=True) for s in samples
            )
        return SynthesizedSpeech(pcm, self._init_rate)


# --------------------------------------------------------------------------- #
# Playback (GStreamer, fed from the calling worker thread)
# --------------------------------------------------------------------------- #


def await_playback_end(bus: Any, total_seconds: float, stopped: Any,
                       eos_or_error: Any, second_ns: int,
                       slice_seconds: float = 0.2):
    """Wait for EOS/ERROR on *bus*, waking promptly on stop().

    Returns ``(outcome, message)`` with outcome in
    ``{"message", "stopped", "timeout"}``.

    v0.11 fix (measured on real hardware): a single
    ``timed_pop_filtered(total)`` blocks the FULL deadline after
    ``stop()`` NULLs the pipeline — the bus never delivers EOS for a
    destroyed pipeline, so ``play()`` returned ~9 s late and
    ``on_done`` (the UI's leave-speaking signal) arrived seconds after
    the user interrupted.  Slicing the wait keeps stop latency ≤
    ``slice_seconds`` without any extra threads.
    """
    end = time.monotonic() + max(0.0, total_seconds)
    while True:
        if stopped.is_set():
            return "stopped", None
        remaining = end - time.monotonic()
        if remaining <= 0:
            return "timeout", None
        wait = min(slice_seconds, remaining)
        message = bus.timed_pop_filtered(int(wait * second_ns),
                                         eos_or_error)
        if message is not None:
            return "message", message


class GStreamerPlayer:
    """Blocking playback of one PCM buffer; interruptible via stop().

    ``play`` runs entirely on the caller's (worker) thread: it pushes
    buffers into an ``appsrc → alsasink`` pipeline and waits on the
    bus for EOS/ERROR, so no GLib main-loop integration is needed and
    the GTK thread is never involved.
    """

    def __init__(self, speech: SynthesizedSpeech) -> None:
        self._speech = speech
        self._pipeline = None
        self._src = None
        self._bus = None
        self._stopped = threading.Event()

    def play(self) -> Optional[str]:
        """Play to the end; returns None on success, 'stopped' when
        :meth:`stop` interrupted, or an error message."""
        try:
            import gi

            gi.require_version("Gst", "1.0")
            from gi.repository import Gst
        except Exception as exc:  # noqa: BLE001
            return (
                "GStreamer is not available, so speech cannot be played "
                f"({exc})."
            )
        Gst.init(None)
        caps = (
            "audio/x-raw,format=S16LE,rate="
            f"{self._speech.sample_rate},channels=1,layout=interleaved"
        )
        try:
            pipeline = Gst.parse_launch(
                "appsrc name=src format=bytes block=true ! "
                "audioconvert ! audioresample ! "
                f"{caps} ! alsasink sync=false"
            )
        except Exception as exc:  # noqa: BLE001
            return f"The audio output pipeline failed: {exc}"
        src = pipeline.get_by_name("src")
        src.set_property(
            "caps", Gst.Caps.from_string(caps)
        )
        self._pipeline = pipeline
        self._src = src
        self._bus = pipeline.get_bus()

        if pipeline.set_state(Gst.State.PLAYING) == Gst.StateChangeReturn.FAILURE:
            pipeline.set_state(Gst.State.NULL)
            self._pipeline = None
            return "No audio output device could be opened."

        error: Optional[str] = None
        chunk = 8192  # bytes per push (~0.25 s at 22.05 kHz mono)
        data = self._speech.pcm
        for offset in range(0, len(data), chunk):
            if self._stopped.is_set():
                error = "stopped"
                break
            piece = data[offset:offset + chunk]
            buf = Gst.Buffer.new_allocate(None, len(piece), None)
            buf.fill(0, piece)
            result = src.emit("push-buffer", buf)
            if result != Gst.FlowReturn.OK:
                error = "The audio stream failed while playing."
                break
        else:
            # GstAppSrc's action signal is "end-of-stream" (what
            # gst_app_src_end_of_stream() exposes); "end-stream" does
            # not exist and raises, which real playback caught.
            src.emit("end-of-stream")
            # Wait for EOS or ERROR (bounded: buffer time + 5 s) —
            # sliced so stop() wakes us within ~200 ms (see
            # await_playback_end).
            outcome, message = await_playback_end(
                self._bus, self._speech.seconds + 5.0, self._stopped,
                Gst.MessageType.EOS | Gst.MessageType.ERROR, Gst.SECOND,
            )
            if outcome == "stopped":
                error = "stopped"
            elif outcome == "timeout":
                error = ("stopped" if self._stopped.is_set()
                         else "Speech playback timed out.")
            elif message.type == Gst.MessageType.ERROR:
                err, _debug = message.parse_error()
                error = f"Audio playback failed: {err.message}"

        try:
            pipeline.set_state(Gst.State.NULL)
        except Exception:  # noqa: BLE001 - teardown never raises
            pass
        self._pipeline = None
        self._src = None
        self._bus = None
        return error

    def stop(self) -> None:
        """Interrupt playback from any thread."""
        self._stopped.set()
        pipeline = self._pipeline
        if pipeline is not None:
            try:
                import gi

                gi.require_version("Gst", "1.0")
                from gi.repository import Gst

                pipeline.set_state(Gst.State.NULL)
            except Exception:  # noqa: BLE001
                pass


# --------------------------------------------------------------------------- #
# The VoiceEngine implementation
# --------------------------------------------------------------------------- #


class CompanionVoice(VoiceEngine):
    """Real local voice: espeak-ng synthesis + GStreamer playback.

    Construction never touches the library; ``available`` probes it on
    first access (thread-safe, cached — including a cached *failure*).

    Injectable seams for tests/headless:

    * ``synthesizer`` — anything with ``available``, ``synthesize``
      and ``sample_rate`` (defaults to :class:`EspeakSynthesizer`),
    * ``player_factory(speech) -> object`` with blocking ``play()`` and
      ``stop()`` (defaults to :class:`GStreamerPlayer`).
    """

    name = "espeak-ng"

    def __init__(
        self,
        *,
        synthesizer: Optional[Any] = None,
        player_factory: Optional[Callable[[SynthesizedSpeech], Any]] = None,
        language: Optional[str] = None,
        rate: float = 1.0,
        on_done: Optional[Callable[[bool, Optional[str]], None]] = None,
    ) -> None:
        self._synthesizer = synthesizer or EspeakSynthesizer()
        self._player_factory = player_factory or GStreamerPlayer
        self._language = language or DEFAULT_LANGUAGE
        self._rate = rate
        self._voice_id = ""  # "" = follow the registry voice per language
        self._on_done = on_done
        self._speaking = False
        self._generation = 0
        self._current_player: Optional[Any] = None
        self._worker: Optional[threading.Thread] = None
        # RLock: _finish() may be invoked while _run() already holds
        # the lock at a checkpoint (a plain Lock deadlocks there).
        self._lock = threading.RLock()
        self._last_error: Optional[str] = None

    # ------------------------------------------------------------------ #
    # VoiceEngine: capability (probed, honest)
    # ------------------------------------------------------------------ #

    @property
    def available(self) -> bool:
        """True only when the synthesizer really loads (probed)."""
        try:
            return bool(self._synthesizer.available)
        except Exception:  # noqa: BLE001 - never raise from a property
            return False

    @property
    def status(self) -> str:
        """One bounded line describing the voice situation (honest)."""
        spec = get_language(self._language)
        lang_name = spec.name if spec else self._language
        if not self.available:
            reason = getattr(self._synthesizer, "load_error", None)
            if reason:
                return f"Voice off — {reason}"[:200]
            return (
                "Voice off — no local speech engine could be loaded."
            )[:200]
        voice = self._voice_id or (spec.espeak if spec else "(default)")
        rate_note = f", rate ×{self._rate:.1f}" if self._rate != 1.0 else ""
        error = (
            f" — last error: {self._last_error}" if self._last_error else ""
        )
        return (
            f"espeak-ng ready for {lang_name} (voice '{voice}'{rate_note})"
            f"{error}"
        )[:200]

    # ------------------------------------------------------------------ #
    # Language / rate (settings apply live)
    # ------------------------------------------------------------------ #

    @property
    def language(self) -> str:
        return self._language

    def set_language(self, code: Optional[str]) -> bool:
        """Switch the speaking voice; False when the code is unknown
        (the current voice is kept — never silently swapped)."""
        spec = get_language(code)
        if spec is None:
            return False
        self._language = spec.code
        return True

    @property
    def rate(self) -> float:
        return self._rate

    def set_rate(self, rate: float) -> None:
        try:
            value = float(rate)
        except (TypeError, ValueError):
            return
        self._rate = max(_RATE_MIN, min(_RATE_MAX, value))

    @property
    def voice_id(self) -> str:
        """Pinned espeak voice ('' = automatic, one per language)."""
        return self._voice_id

    def set_voice_id(self, voice_id: Optional[str]) -> None:
        """Pin (or with ''/None, clear) the espeak voice selector.

        Purely a user preference: the value is bounded and never
        validated against the registry here — the synthesizer itself
        reports an unknown voice honestly at speak time instead of
        silently substituting another one.
        """
        clean = str(voice_id or "").strip()[:40]
        self._voice_id = clean

    @property
    def speaking(self) -> bool:
        """True while synthesis/playback is in flight."""
        return self._speaking

    @property
    def last_error(self) -> Optional[str]:
        return self._last_error

    def set_on_done(self, callback: Optional[Callable[[bool, Optional[str]], None]]) -> None:
        """Completion callback (success, error-or-None) — set by the
        window so speaking returns to idle/listening truthfully."""
        self._on_done = callback

    # ------------------------------------------------------------------ #
    # VoiceEngine: speak / stop
    # ------------------------------------------------------------------ #

    def speak(self, text: str, *,
              on_done: Optional[Callable[[bool, Optional[str]], None]] = None
              ) -> bool:
        """Speak *text* on a worker thread; True when dispatched.

        Returns ``False`` (nothing claimed) when the engine is
        unavailable, the text is empty, or dispatch fails.  The final
        truth — including device failures mid-speech — is reported
        through ``on_done`` (per-call callback, falling back to the
        instance one).
        """
        clean = " ".join((text or "").split())
        if not clean:
            return False
        if not self.available:
            return False
        if len(clean) > MAX_SPEECH_CHARS:
            clean = clean[:MAX_SPEECH_CHARS]

        spec = get_language(self._language)
        voice = self._voice_id or (spec.espeak if spec else "en-us")
        rate = self._rate
        callback = on_done or self._on_done

        # A new utterance invalidates the current one (never two at
        # once): workers compare their captured generation at every
        # checkpoint, so a superseded worker can neither play audio nor
        # mutate shared state — it only reports "stopped" to its own
        # callback, which always fires exactly once.
        with self._lock:
            old_player = self._current_player
            self._generation += 1
            generation = self._generation
            self._speaking = True
            self._last_error = None
        if old_player is not None:
            try:
                old_player.stop()
            except Exception:  # noqa: BLE001 - never raise here
                pass

        def _finish(success: bool, error: Optional[str],
                    gen: int) -> None:
            with self._lock:
                stale = gen != self._generation
                if not stale:
                    self._speaking = False
                    self._current_player = None
                    if error and error != "stopped":
                        self._last_error = error
            if callback is not None:
                try:
                    callback(success, error)
                except Exception:  # noqa: BLE001 - UI callback isolation
                    pass

        def _run() -> None:
            try:
                speech = self._synthesizer.synthesize(
                    clean, voice=voice, rate=rate
                )
            except Exception as exc:  # noqa: BLE001 - honest failure
                message = str(exc) or "Speech synthesis failed."
                _finish(False, message, generation)
                return
            with self._lock:
                if generation != self._generation:
                    _finish(False, "stopped", generation)
                    return
                try:
                    player = self._player_factory(speech)
                except Exception as exc:  # noqa: BLE001
                    _finish(False, f"Audio output failed: {exc}",
                            generation)
                    return
                self._current_player = player
            try:
                error = player.play()
            except Exception as exc:  # noqa: BLE001 - completion truth
                # A broken player must still complete the turn: the
                # caller's on_done is how the UI leaves `speaking`.
                error = f"Audio playback failed: {exc}"
            with self._lock:
                stale = generation != self._generation
            if stale or error == "stopped":
                _finish(False, "stopped", generation)
            elif error:
                _finish(False, error, generation)
            else:
                _finish(True, None, generation)

        worker = threading.Thread(target=_run, daemon=True,
                                  name="alwin-tts")
        self._worker = worker
        worker.start()
        return True

    def stop(self) -> None:
        """Interrupt current speech (no-op when idle).

        The in-flight worker is invalidated: it will report
        ``on_done(False, "stopped")`` when it reaches its next
        checkpoint, and no stale worker can start new audio afterwards.
        """
        with self._lock:
            if not self._speaking and self._current_player is None:
                return
            self._generation += 1
            self._speaking = False
            player = self._current_player
            self._current_player = None
        if player is not None:
            try:
                player.stop()
            except Exception:  # noqa: BLE001 - stop never raises
                pass


__all__ = [
    "CompanionVoice",
    "ESPEAK_DEFAULT_RATE",
    "EspeakSynthesizer",
    "GStreamerPlayer",
    "MAX_SPEECH_CHARS",
    "SynthesizedSpeech",
    "VoiceOutputError",
]
