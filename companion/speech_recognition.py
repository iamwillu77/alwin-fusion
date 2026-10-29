"""Speech recognition for the companion (v0.9.0) — local, lazy, honest.

Runs **faster-whisper** (a local CTranslate2 Whisper port) over the
in-memory PCM from :mod:`companion.voice_input`.  Design rules:

* **Nothing downloads, ever.** The engine loads with
  ``local_files_only=True``: a missing model raises
  :class:`RecognitionError` explaining exactly which model is absent
  and pointing at ``scripts/install_voice_models.py`` (the explicit,
  disk-space-reporting installer that lives *outside* the companion).
  A first-run surprise download is never acceptable.
* **Lazy everywhere.** Importing this module imports neither
  ``faster_whisper`` nor numpy and touches no device; the model is
  loaded on the first :meth:`recognize` call on a **worker thread** —
  recognition never runs on the GTK main loop
  (:meth:`SpeechRecognitionService.recognize_async`).
* **Never invents words.** Digital silence (and near-empty audio) is
  rejected *before* the model runs — Whisper happily hallucinates
  fluent text over silence. Segments the model itself marks as
  non-speech (``no_speech_prob`` ≥ :data:`NO_SPEECH_PROB`) are
  dropped, and what remains is returned verbatim (stripped only).
  An empty transcript is returned as ``text=""`` with
  ``no_speech=True`` — never filled with placeholder words.
* **Honest hints.** When the caller forces a decode language, the
  result's ``asr_hint`` is ``None``: a language the app chose itself
  is not evidence for detection.  Auto-decode reports the engine's own
  language + probability for the detection layer to weigh.
* **Cancellation** drops any in-flight result (generation counter) —
  a cancel that races a finished decode still discards the outcome.

Engine/model are injectable (``engine_factory``) so tests run
completely mocked; real-engine verification uses the same API.
"""

from __future__ import annotations

import os
import threading
from dataclasses import dataclass
from typing import Any, Callable, Optional, Tuple

#: Documented model sizes (closed set — the installer offers these).
MODEL_SIZES: Tuple[str, ...] = ("tiny", "base", "small", "medium")

#: Default: the smallest size that was honestly accurate on real
#: multilingual speech in this project's verification runs.
DEFAULT_MODEL_SIZE = "small"

#: Approximate download sizes in MiB (for the installer's disk-space
#: report; labelled approximate there — network manifests differ).
APPROX_MODEL_MIB = {
    "tiny": 75, "base": 145, "small": 484, "medium": 1530,
}

#: Drop model segments at/above this non-speech probability.
NO_SPEECH_PROB = 0.6

#: PCM shorter than this (seconds) is never sent to the model.
MIN_AUDIO_SECONDS = 0.12

#: Recognition is synchronous inside its worker; this bounds it so a
#: hung model cannot wedge the UI's "listening → thinking" transition.
DEFAULT_TIMEOUT_SECONDS = 120.0


class RecognitionError(Exception):
    """A recognition failure with a user-facing explanation."""


@dataclass(frozen=True)
class RecognitionResult:
    """What the recognizer honestly produced for one capture."""

    #: Recognized words, verbatim from the model ('' when no speech).
    text: str
    #: Engine's auto-detected language — None when we forced the
    #: decode language ourselves (self-chosen hints are no evidence).
    asr_hint: Optional[str]
    #: Engine's confidence for ``asr_hint`` (0.0 when unavailable).
    language_probability: float
    #: True when the audio contained no usable speech (never faked).
    no_speech: bool
    #: Approximate input duration in seconds.
    seconds: float
    #: The model size that produced this result.
    model_size: str

    @property
    def empty(self) -> bool:
        return not self.text.strip()


# --------------------------------------------------------------------------- #
# Model cache probing (status display only — no downloads here)
# --------------------------------------------------------------------------- #


def _hub_cache_dir() -> str:
    """Where faster-whisper would look for models (best effort)."""
    try:
        from huggingface_hub.constants import HF_HUB_CACHE

        return str(HF_HUB_CACHE)
    except Exception:  # noqa: BLE001 - default layout is the fallback
        return os.path.expanduser("~/.cache/huggingface/hub")


def model_installed(model_size: str = DEFAULT_MODEL_SIZE) -> bool:
    """True when the model's snapshot already exists locally.

    Pure filesystem check — no network, no import of the engine.
    """
    if model_size not in MODEL_SIZES:
        return False
    directory = os.path.join(
        _hub_cache_dir(), f"models--Systran--faster-whisper-{model_size}"
    )
    if not os.path.isdir(directory):
        return False
    # A real snapshot has blobs AND refs; an interrupted download
    # leaves one of them behind — require both.
    return (os.path.isdir(os.path.join(directory, "blobs"))
            and os.path.isdir(os.path.join(directory, "refs")))


def model_status(model_size: str = DEFAULT_MODEL_SIZE) -> dict:
    """Bounded status dict for Settings (never claims a download)."""
    installed = model_installed(model_size)
    if model_size not in MODEL_SIZES:
        return {
            "model": model_size,
            "installed": False,
            "detail": f"Unknown model '{model_size}' (choose "
                      f"{', '.join(MODEL_SIZES)}).",
        }
    if installed:
        detail = (f"Speech model '{model_size}' is installed locally "
                  f"(~{APPROX_MODEL_MIB[model_size]} MiB). Recognition "
                  "runs offline.")
    else:
        detail = (f"Speech model '{model_size}' is NOT installed. "
                  "Install it explicitly with: python3 scripts/"
                  "install_voice_models.py — Alwin never downloads "
                  "models on its own.")
    return {
        "model": model_size,
        "installed": installed,
        "detail": detail[:300],
    }


# --------------------------------------------------------------------------- #
# Service
# --------------------------------------------------------------------------- #


class SpeechRecognitionService:
    """Turns captured PCM into text on a worker thread.

    ``engine_factory(model_size) -> engine`` must return something
    with ``.transcribe(audio, language=..., beam_size=...) ->
    (segments, info)`` — the real ``faster_whisper.WhisperModel`` or a
    test fake.  ``audio`` is a float32 numpy array at 16 kHz (the
    format faster-whisper documents); conversion happens here, in
    memory.
    """

    def __init__(
        self,
        *,
        model_size: str = DEFAULT_MODEL_SIZE,
        engine_factory: Optional[Callable[[str], Any]] = None,
        sample_rate: int = 16000,
    ) -> None:
        if model_size not in MODEL_SIZES:
            raise ValueError(
                f"Unknown model size '{model_size}' "
                f"(choose {', '.join(MODEL_SIZES)})."
            )
        self.model_size = model_size
        self.sample_rate = sample_rate
        self._engine_factory = engine_factory
        self._engine: Optional[Any] = None
        self._engine_lock = threading.Lock()
        self._generation = 0
        self._generation_lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._busy = False

    # ------------------------------------------------------------------ #
    # Status
    # ------------------------------------------------------------------ #

    @property
    def busy(self) -> bool:
        """True while a recognition is running on the worker."""
        return self._busy

    @property
    def available(self) -> bool:
        """True when the engine import *and* the model exist locally.

        Import-checked (cheap) and cache-checked — never a guess.
        """
        if self._engine_factory is None:
            try:
                import faster_whisper  # noqa: F401
            except Exception:  # noqa: BLE001
                return False
        return model_installed(self.model_size)

    def status(self) -> dict:
        """Bounded status line for the settings/voice indicators."""
        base = model_status(self.model_size)
        if self._engine_factory is None:
            try:
                import faster_whisper  # noqa: F401
                engine_ok = True
            except Exception:  # noqa: BLE001
                engine_ok = False
            if not engine_ok:
                base["installed"] = False
                base["detail"] = (
                    "The speech-recognition package is not installed. "
                    "Install it with: pip install faster-whisper "
                    "(or use scripts/install_voice_models.py)."
                )[:300]
        return base

    # ------------------------------------------------------------------ #
    # Recognition
    # ------------------------------------------------------------------ #

    def recognize(
        self,
        pcm: bytes,
        *,
        language: Optional[str] = None,
    ) -> RecognitionResult:
        """Synchronous recognition (blocks): call it on a worker thread.

        Raises :class:`RecognitionError` for missing engine/model or a
        broken decode; returns an empty ``no_speech`` result for
        silence — silence is not an error, it is just *nothing heard*.
        """
        seconds = len(pcm) / (self.sample_rate * 2)
        if seconds < MIN_AUDIO_SECONDS or not any(pcm):
            return RecognitionResult(
                "", None, 0.0, True, seconds, self.model_size
            )

        audio = self._to_float32(pcm)
        engine = self._ensure_engine()

        try:
            segments, info = engine.transcribe(
                audio,
                language=language,
                beam_size=1,
            )
        except TypeError:
            # Fake/alternate engines may not accept beam_size; the real
            # one does. Retry with the minimal documented call shape.
            try:
                segments, info = engine.transcribe(audio, language=language)
            except Exception as exc:  # noqa: BLE001
                raise RecognitionError(
                    f"Speech recognition failed: {exc}"
                ) from exc
        except Exception as exc:  # noqa: BLE001
            raise RecognitionError(
                f"Speech recognition failed: {exc}"
            ) from exc

        parts = []
        for segment in segments:
            probability = float(getattr(segment, "no_speech_prob", 0.0) or 0.0)
            if probability >= NO_SPEECH_PROB:
                continue
            text = str(getattr(segment, "text", "") or "").strip()
            if text:
                parts.append(text)

        text = " ".join(parts).strip()
        detected = str(getattr(info, "language", "") or "") or None
        probability = float(getattr(info, "language_probability", 0.0) or 0.0)
        # Forcing the language ourselves is not detection evidence.
        asr_hint = None if language else detected
        return RecognitionResult(
            text,
            asr_hint,
            0.0 if asr_hint is None else probability,
            not text,
            seconds,
            self.model_size,
        )

    def recognize_async(
        self,
        pcm: bytes,
        *,
        language: Optional[str] = None,
        on_result: Optional[Callable[[RecognitionResult], None]] = None,
        on_error: Optional[Callable[[Exception], None]] = None,
        on_busy: Optional[Callable[[], None]] = None,
    ) -> bool:
        """Recognize on a daemon worker; returns False when already busy.

        Callbacks fire on the **worker thread** (wrap them in
        ``GLib.idle_add`` before touching GTK).  A :meth:`cancel`
        issued before the worker finishes suppresses every callback —
        the racing result is dropped, not delivered late.
        """
        with self._generation_lock:
            if self._busy:
                return False
            self._busy = True
            self._generation += 1
            generation = self._generation

        def _run() -> None:
            try:
                if on_busy is not None:
                    on_busy()
                result = self.recognize(pcm, language=language)
                with self._generation_lock:
                    stale = generation != self._generation
                if not stale:
                    if on_result is not None:
                        on_result(result)
            except Exception as exc:  # noqa: BLE001 - report, never raise
                with self._generation_lock:
                    stale = generation != self._generation
                if not stale and on_error is not None:
                    on_error(exc)
            finally:
                with self._generation_lock:
                    if generation == self._generation:
                        self._busy = False

        worker = threading.Thread(target=_run, daemon=True,
                                  name="alwin-asr")
        self._worker = worker
        worker.start()
        return True

    def cancel(self) -> None:
        """Drop any in-flight recognition (results are never delivered).

        The decode itself runs to completion inside the model — there
        is no safe mid-inference abort — but its outcome is discarded,
        which is what "cancelled" must mean to the caller.
        """
        with self._generation_lock:
            self._generation += 1
            self._busy = False

    # ------------------------------------------------------------------ #

    def _to_float32(self, pcm: bytes):
        """S16LE bytes → float32 [-1, 1] array (numpy, lazily)."""
        try:
            import numpy as np
        except Exception as exc:  # noqa: BLE001
            raise RecognitionError(
                "numpy is required for speech recognition but is not "
                "installed."
            ) from exc
        samples = np.frombuffer(pcm, dtype="<i2")
        return samples.astype(np.float32) / 32768.0

    def _ensure_engine(self) -> Any:
        if self._engine is not None:
            return self._engine
        with self._engine_lock:
            if self._engine is not None:
                return self._engine
            self._engine = self._build_engine()
            return self._engine

    def _build_engine(self) -> Any:
        if self._engine_factory is not None:
            try:
                return self._engine_factory(self.model_size)
            except RecognitionError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise RecognitionError(
                    f"Could not create the speech engine: {exc}"
                ) from exc

        try:
            from faster_whisper import WhisperModel
        except Exception as exc:  # noqa: BLE001
            raise RecognitionError(
                "Speech recognition is not installed (the 'faster-whisper' "
                "package is missing). Install it with: pip install "
                "faster-whisper — Alwin does not install it automatically."
            ) from exc

        if not model_installed(self.model_size):
            raise RecognitionError(
                f"The speech model '{self.model_size}' is not downloaded "
                f"(~{APPROX_MODEL_MIB[self.model_size]} MiB). Install it "
                "explicitly with: python3 scripts/install_voice_models.py "
                "— no download was attempted."
            )

        try:
            # local_files_only: a missing file can never trigger a
            # network fetch, even if the cache probe above was stale.
            return WhisperModel(
                self.model_size,
                device="cpu",
                compute_type="int8",
                local_files_only=True,
            )
        except Exception as exc:  # noqa: BLE001
            raise RecognitionError(
                f"The speech model '{self.model_size}' could not be "
                f"loaded: {exc}. Reinstall it with: python3 scripts/"
                "install_voice_models.py"
            ) from exc


__all__ = [
    "APPROX_MODEL_MIB",
    "DEFAULT_MODEL_SIZE",
    "MIN_AUDIO_SECONDS",
    "MODEL_SIZES",
    "NO_SPEECH_PROB",
    "RecognitionError",
    "RecognitionResult",
    "SpeechRecognitionService",
    "model_installed",
    "model_status",
]
