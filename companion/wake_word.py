"""Wake-word service for the Alwin companion (v0.11.0).

A **local, two-stage** wake-word detector with an explicit
microphone-ownership contract — hands-free "Hey Alwin" without giving
up any of the v0.9 privacy or lifecycle guarantees:

Stage 1 — **candidate gate**: a vosk keyword-spotting recognizer
(``vosk-model-small-en-us-0.15``, Apache 2.0) watches the microphone
stream cheaply.  Vosk's KWS grammar forces the keyword into its
hypotheses, so it fires *loosely* (measured: it fires on most speech)
— it is only a gate that keeps stage 2 from running constantly.

Stage 2 — **verification**: on a candidate, the trailing ``WINDOW``
seconds of audio (a bounded rolling buffer — never the whole session)
are decoded by the shared :class:`~companion.speech_recognition.
SpeechRecognitionService` engine with ``hotwords=<phrase>`` bias AND
**auto language detection**.  Acceptance requires BOTH the exact
phrase pattern (each configured word, any separator between) AND a
detected language of ``en`` — the auto-language requirement defeats
hotword-induced phrase hallucinations on foreign audio (measured: a
forced-English decode fabricated "Hey Alwin…" out of Bengali speech;
auto-detect decoded Bengali instead and rejected it).

Measured on 25 synthetic positives / all 149 FLEURS human-speech
fixtures (see README): 25/25 recall, 0/149 false accepts with the
production rule; the ``tiny`` model was tested and rejected as the
verification engine (17/25 recall, 23/149 false accepts).

Privacy contract (tested):

* audio flows through this module **in memory only** — a bounded
  queue (drop-oldest) and a bounded rolling window (~112 KB); nothing
  is ever written to disk (the package bans ``wave``/``audioop``
  package-wide and this module performs no file I/O at all),
* stage-2 transcripts are transient verification evidence: they are
  discarded with the window — never stored, never logged, and never
  sent anywhere (the wake path does not touch Ollama; only the user's
  *command* does, after a trigger),
* the detector stops itself on a trigger so the foreground
  conversation owns the microphone exclusively; the window restarts it
  after the turn ends (or after any recoverable failure).

Threading: the capture callback only enqueues chunks; a single
``alwin-wake`` worker feeds the detector, polls capture health and
runs verification, so the GTK thread and the GStreamer callback
thread never decode.  ``start()``/``stop()``/``release()`` are
idempotent; ``listening`` reports the *real* capture state (it never
claims to be listening after a capture failure).
"""

from __future__ import annotations

import os
import queue
import re
import threading
import time
from typing import Any, Callable, Optional

DEFAULT_PHRASE = "Hey Alwin"

#: The default acceptance pattern (any non-alphanumeric separator
#: between the words) — built dynamically for custom phrases.
DEFAULT_PHRASE_PATTERN = r"\bhey\b[^a-z0-9]+\balwin\b"

#: Consecutive verification errors tolerated before the service
#: reports an honest failure instead of degrading silently.
MAX_VERIFY_FAILURES = 3

#: Cooldown after a trigger before a *new* session may trigger again.
COOLDOWN_SECONDS = 2.5

#: Bounded chunk queue between the capture callback and the worker:
#: 64 x 200 ms = 12.8 s of audio worst case (far more than one
#: verification takes), 205 KB — drop-oldest if ever exceeded.
QUEUE_MAX_CHUNKS = 64
CHUNK_SAMPLES = 3200                 # 200 ms at 16 kHz mono S16
CHUNK_BYTES = CHUNK_SAMPLES * 2

#: Rolling verification window (matches the measured configuration).
WINDOW_SECONDS = 3.5
WINDOW_BYTES = int(WINDOW_SECONDS * 16000 * 2)   # 112 000 bytes

#: Stage-1 candidate floor (measured: true utterances score >= 93).
CANDIDATE_CONF_FLOOR = 50.0
MAX_CANDIDATES_PER_SESSION = 8       # budget per reset (rate is bounded
                                      # by VERIFY_MIN_INTERVAL below)
#: Minimum seconds between stage-2 verifications — bounds CPU in noisy
#: rooms even when candidates arrive constantly. The FIRST candidate
#: of a session always verifies immediately (last verify = -inf).
VERIFY_MIN_INTERVAL = 1.5

#: Default location of the Apache-2.0 vosk model (explicitly
#: installed by scripts/install_wake_word_model.py — never by the app).
DEFAULT_VOSK_MODEL_DIR = os.path.expanduser(
    "~/.cache/alwin-vosk/vosk-model-small-en-us-0.15"
)
VOSK_MODEL_ENV = "ALWIN_WAKE_MODEL_DIR"


class WakeWordError(Exception):
    """A wake-word failure with a user-facing explanation."""


def phrase_pattern(phrase: str) -> str:
    """The acceptance pattern for a configured phrase (word-bounded).

    Returned as a *string* (``re.search`` uses its own cache) because
    the package's meta-suite bans ``compile`` calls outright — the
    security pin wins over a micro-optimization.
    """
    words = [re.escape(w) for w in str(phrase).split() if w]
    if not words:
        words = [re.escape(w) for w in DEFAULT_PHRASE.split()]
    return r"\b" + r"[^a-z0-9]+".join(words) + r"\b"


# --------------------------------------------------------------------------- #
# Stage 1 + 2 detector (injectable backend seam)
# --------------------------------------------------------------------------- #


class TwoStageDetector:
    """vosk KWS candidate gate + shared-engine hotword verification.

    ``recognizer`` is the window's existing
    :class:`~companion.speech_recognition.SpeechRecognitionService`
    (duck-typed: only ``decode_window`` and ``available`` are used) so
    exactly one Whisper model exists in RAM.  ``vosk_model_dir`` is
    injectable for tests; the default comes from ``ALWIN_WAKE_MODEL_DIR``
    or the documented cache path.
    """

    def __init__(
        self,
        *,
        recognizer: Any,
        phrase: str = DEFAULT_PHRASE,
        vosk_model_dir: Optional[str] = None,
        vosk_factory: Optional[Callable[[str], Any]] = None,
        verify_interval: float = VERIFY_MIN_INTERVAL,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.recognizer = recognizer
        self.phrase = str(phrase or DEFAULT_PHRASE).strip()[:40] \
            or DEFAULT_PHRASE
        self._pattern = phrase_pattern(self.phrase)
        self._verify_interval = verify_interval
        self._clock = clock
        self._last_verify_at = float("-inf")
        self._model_dir = (
            vosk_model_dir
            or os.environ.get(VOSK_MODEL_ENV)
            or DEFAULT_VOSK_MODEL_DIR
        )
        # Test seam: a factory returning the KWS recognizer skips the
        # package/model checks (fakes never touch vosk or disk).
        self._vosk_factory = vosk_factory
        self._window = bytearray()
        self._kws: Any = None
        self._vosk_model: Any = None
        self._candidates = 0
        self._verify_failures = 0
        self._load()

    # -- construction ------------------------------------------------------ #

    def _load(self) -> None:
        if not getattr(self.recognizer, "available", False):
            raise WakeWordError(
                "Wake-word verification needs the speech model: "
                "install it with scripts/install_voice_models.py."
            )
        if self._vosk_factory is not None:
            try:
                self._kws = self._vosk_factory(self._kws_keyword())
                return
            except WakeWordError:
                raise
            except Exception as exc:  # noqa: BLE001
                raise WakeWordError(
                    f"The wake-word engine could not start: {exc}"
                ) from exc
        if not os.path.isdir(self._model_dir):
            raise WakeWordError(
                "The wake-word model is not installed. Install it "
                "explicitly with: python3 "
                "scripts/install_wake_word_model.py (it reports the "
                "size first; Alwin never downloads models by themselves)."
            )
        try:
            import json

            from vosk import KaldiRecognizer, Model, SetLogLevel

            SetLogLevel(-1)
            self._vosk_model = Model(self._model_dir)
            # JSON array form (a bare string is rejected by the API).
            # LOWERCASE: vosk's vocabulary lookup is case-sensitive and
            # a capitalized keyword silently drops every word (measured
            # warnings: "Ignoring word missing in vocabulary: 'Hey'"),
            # which degenerates the grammar into match-anything.
            self._kws = KaldiRecognizer(
                self._vosk_model, 16000,
                json.dumps([self._kws_keyword()]),
            )
            self._kws.SetMaxAlternatives(3)
        except WakeWordError:
            raise
        except Exception as exc:  # noqa: BLE001 - honest failure
            raise WakeWordError(
                f"The wake-word engine could not start: {exc}"
            ) from exc

    def _kws_keyword(self) -> str:
        """The stage-1 keyword spelling (vosk vocabulary terms)."""
        return " ".join(self.phrase.lower().split())

    # -- streaming --------------------------------------------------------- #

    def feed(self, pcm: bytes) -> bool:
        """Consume one chunk; True when the wake phrase is VERIFIED.

        Bounded by construction: the rolling window never exceeds
        ``WINDOW_BYTES``; stage-2 verification runs at most once per
        ``verify_interval`` (CPU bound in noisy rooms) and a session
        resets its per-session candidate budget instead of going
        deaf.  Candidates require the KWS result to actually *contain*
        the configured words — silence produces empty-text results
        with high confidence (measured) and must never trigger a
        verification.
        """
        self._window.extend(pcm)
        overflow = len(self._window) - WINDOW_BYTES
        if overflow > 0:
            del self._window[:overflow]

        if not self._kws.AcceptWaveform(pcm):
            return False
        import json as _json

        try:
            data = _json.loads(self._kws.Result() or "{}")
        except Exception:  # noqa: BLE001 - never crash the worker
            data = {}
        alternatives = data.get("alternatives") or []
        required = set(self._kws_keyword_words())
        best = -1e9
        for alt in alternatives:
            words = set(str(alt.get("text", "")).lower().split())
            if not required or not required <= words:
                continue                  # empty/garbage text: not a hit
            best = max(best, float(alt.get("confidence", -1e9)))
        if best < CANDIDATE_CONF_FLOOR:
            return False
        now = self._clock()
        if now - self._last_verify_at < self._verify_interval:
            return False                  # rate limit (CPU guard)
        if self._candidates >= MAX_CANDIDATES_PER_SESSION:
            # Bounded per reset — reset instead of going permanently
            # deaf, the interval above keeps the rate bounded.
            self._candidates = 0
        self._candidates += 1
        self._last_verify_at = now
        # Fresh KWS state so one utterance cannot re-fire endlessly.
        self._kws.Reset()
        return self._verify()

    def _kws_keyword_words(self):
        return self._kws_keyword().split()

    def _verify(self) -> bool:
        window = bytes(self._window)
        try:
            decode = self.recognizer.decode_window(
                window, hotwords=self.phrase
            )
        except Exception:  # noqa: BLE001
            # Verification failure is NEVER a trigger; but repeated
            # failures are reported instead of degrading silently.
            self._verify_failures += 1
            if self._verify_failures >= MAX_VERIFY_FAILURES:
                raise WakeWordError(
                    "Wake-word verification keeps failing — check the "
                    "speech model (python3 -m companion --check)."
                ) from None
            return False
        self._verify_failures = 0
        text = decode.text or ""
        if not re.search(self._pattern, text, re.IGNORECASE):
            return False
        return (decode.language or "") == "en"

    def reset(self) -> None:
        """Clear streaming state (cooldown / new session)."""
        self._window.clear()
        self._candidates = 0
        self._verify_failures = 0
        self._last_verify_at = float("-inf")
        if self._kws is not None:
            self._kws.Reset()

    def close(self) -> None:
        self._window.clear()
        self._kws = None
        self._vosk_model = None


# --------------------------------------------------------------------------- #
# The service
# --------------------------------------------------------------------------- #


class WakeWordService:
    """Owns the microphone *only* while monitoring; never concurrently.

    The capture callback enqueues bounded chunks; a single worker
    thread (``alwin-wake``) opens the input, loads the detector,
    feeds it and polls capture health.  On a verified trigger the
    service fires ``on_trigger`` (on the worker thread) and **stops
    itself**, releasing the microphone for the foreground conversation;
    the window restarts it after the turn.  Every failure path records
    ``last_error``, reports through ``on_error``, releases the capture
    and leaves ``listening`` False.
    """

    def __init__(
        self,
        *,
        input_factory: Optional[Callable[[], Any]] = None,
        detector_factory: Optional[Callable[[str], Any]] = None,
        recognizer: Any = None,
        on_trigger: Optional[Callable[[str], None]] = None,
        on_error: Optional[Callable[[str], None]] = None,
        phrase: str = DEFAULT_PHRASE,
        cooldown: float = COOLDOWN_SECONDS,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        if input_factory is None:
            from companion.voice_input import VoiceInputService

            input_factory = VoiceInputService
        self.recognizer = recognizer
        self._input_factory = input_factory
        self._detector_factory = detector_factory
        self._on_trigger = on_trigger
        self._on_error = on_error
        self.phrase = str(phrase or DEFAULT_PHRASE).strip()[:40] \
            or DEFAULT_PHRASE
        self._cooldown = cooldown
        self._clock = clock

        self._lock = threading.Lock()
        self._worker: Optional[threading.Thread] = None
        self._stop_flag = threading.Event()
        self._chunks: "queue.Queue[bytes]" = queue.Queue(
            maxsize=QUEUE_MAX_CHUNKS
        )
        self._input: Any = None
        self._detector: Any = None
        self._running = False
        self._released = False
        self._last_error: Optional[str] = None
        self._triggers = 0
        self._last_trigger_at = float("-inf")

    # -- state ------------------------------------------------------------- #

    @property
    def listening(self) -> bool:
        """True only while capture is really open (never after a failure)."""
        with self._lock:
            if not self._running:
                return False
            input_service = self._input
        return bool(input_service is not None
                    and getattr(input_service, "listening", False))

    @property
    def running(self) -> bool:
        """True from start() until a trigger/stop/release ends the session."""
        with self._lock:
            return self._running

    @property
    def released(self) -> bool:
        with self._lock:
            return self._released

    @property
    def last_error(self) -> Optional[str]:
        with self._lock:
            return self._last_error

    @property
    def triggers(self) -> int:
        with self._lock:
            return self._triggers

    def level(self) -> float:
        """Live input level (0.0 when not monitoring)."""
        with self._lock:
            input_service = self._input
        if input_service is None:
            return 0.0
        try:
            return float(input_service.level())
        except Exception:  # noqa: BLE001 - cosmetic only
            return 0.0

    # -- lifecycle --------------------------------------------------------- #

    def start(self) -> bool:
        """Begin monitoring (idempotent). Returns False when refused.

        Never blocks the caller: device/model work happens on the
        worker; failures arrive through ``on_error`` + ``last_error``.
        """
        with self._lock:
            if self._released:
                self._last_error = (
                    "The wake-word service was released; create a new "
                    "one before listening again."
                )
                return False
            if self._running:
                return True            # idempotent — one session only
            self._running = True
            self._stop_flag.clear()
            self._chunks = queue.Queue(maxsize=QUEUE_MAX_CHUNKS)
            self._last_error = None
            worker = threading.Thread(
                target=self._run, daemon=True, name="alwin-wake"
            )
            self._worker = worker
        worker.start()
        return True

    def stop(self) -> None:
        """Stop monitoring and discard buffered audio (idempotent)."""
        with self._lock:
            was_running = self._running
            self._running = False
            self._stop_flag.set()
            worker = self._worker
            input_service = self._input
            detector = self._detector
            self._input = None
            self._detector = None
        if not was_running:
            return
        # Discard everything captured: cancel() drops PCM for good.
        if input_service is not None:
            try:
                input_service.cancel()
            except Exception:  # noqa: BLE001 - stop never raises
                pass
        if detector is not None:
            try:
                detector.close()
            except Exception:  # noqa: BLE001
                pass
        self._drain_chunks()
        if (worker is not None and worker.is_alive()
                and worker is not threading.current_thread()):
            worker.join(timeout=1.0)

    def release(self) -> None:
        """Stop for good (window teardown); start() afterwards refuses."""
        self.stop()
        with self._lock:
            self._released = True

    def wait_stopped(self, timeout: float = 10.0) -> bool:
        """Best-effort join of the worker (clean-shutdown helper).

        The worker is a daemon, but a thread still running vosk/whisper
        C code while the interpreter finalizes can crash the process
        (observed: exit 139 in the verification driver).  Call this
        *after* :meth:`release` in scripts/launchers that are about to
        exit; it never blocks longer than *timeout* and returns whether
        the worker actually finished.
        """
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            with self._lock:
                worker = self._worker
            if worker is None or not worker.is_alive():
                return True
            if worker is threading.current_thread():
                return True
            worker.join(timeout=0.2)
        return False

    def _drain_chunks(self) -> None:
        while True:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                break

    # -- worker ------------------------------------------------------------ #

    def _fail(self, message: str,
              input_service: Any = None,
              detector: Any = None) -> None:
        """Record the failure, release every resource, notify (once)."""
        with self._lock:
            first = self._last_error is None
            self._last_error = message
            self._running = False
            owned_input = input_service if input_service is not None \
                else self._input
            owned_detector = detector if detector is not None \
                else self._detector
            self._input = None
            self._detector = None
        self._stop_flag.set()
        if owned_input is not None:
            try:
                owned_input.cancel()
            except Exception:  # noqa: BLE001
                pass
        if owned_detector is not None:
            try:
                owned_detector.close()
            except Exception:  # noqa: BLE001
                pass
        self._drain_chunks()
        if first:
            callback = self._on_error
            if callback is not None:
                try:
                    callback(message)
                except Exception:  # noqa: BLE001 - UI callback isolation
                    pass

    def _run(self) -> None:
        # --- open capture ------------------------------------------------ #
        try:
            input_service = self._input_factory(
                on_sample=self._enqueue,
            )
        except TypeError:
            # Fakes/old factories without the kwarg: fall back.
            try:
                input_service = self._input_factory()
            except Exception as exc:  # noqa: BLE001
                self._fail(f"The wake-word microphone could not open: "
                           f"{exc}")
                return
        except Exception as exc:  # noqa: BLE001
            self._fail(f"The wake-word microphone could not open: {exc}")
            return

        try:
            input_service.start()
        except Exception as exc:  # noqa: BLE001 - device errors surface
            self._fail(str(exc) or "The wake-word microphone failed.",
                       input_service=input_service)
            return
        with self._lock:
            if not self._running:      # stop() raced the warm-up
                self._input = input_service
                self._stop_flag.set()
            else:
                self._input = input_service
        if self._stop_flag.is_set():
            try:
                input_service.cancel()
            except Exception:  # noqa: BLE001
                pass
            return

        # --- load detector (heavy: model reads happen here, off GTK) ----- #
        try:
            if self._detector_factory is not None:
                detector = self._detector_factory(self.phrase)
            elif self.recognizer is not None:
                detector = TwoStageDetector(
                    recognizer=self.recognizer, phrase=self.phrase
                )
            else:
                raise WakeWordError(
                    "No speech recognizer was attached to the "
                    "wake-word service."
                )
        except Exception as exc:  # noqa: BLE001 (incl. WakeWordError)
            self._fail(str(exc), input_service=input_service)
            return
        with self._lock:
            if not self._running:      # stop() raced the model load
                self._detector = detector
                self._stop_flag.set()
            else:
                self._detector = detector
        if self._stop_flag.is_set():
            try:
                detector.close()
            except Exception:  # noqa: BLE001
                pass
            try:
                input_service.cancel()
            except Exception:  # noqa: BLE001
                pass
            return

        # --- monitoring loop --------------------------------------------- #
        while not self._stop_flag.is_set():
            # Honest device-health poll (removal/disconnect surfaces as
            # last_error on the input service).
            input_error = getattr(input_service, "last_error", None)
            if input_error:
                self._fail(f"The wake-word microphone failed: "
                           f"{input_error}")
                return
            try:
                chunk = self._chunks.get(timeout=0.25)
            except queue.Empty:
                continue
            if self._stop_flag.is_set():
                break
            try:
                triggered = detector.feed(chunk)
            except WakeWordError as exc:
                self._fail(str(exc))
                return
            except Exception as exc:  # noqa: BLE001 - never die silently
                self._fail(f"The wake-word engine failed: {exc}")
                return
            if self._stop_flag.is_set():
                # stop() arrived while verifying: no late trigger.
                break
            if not triggered:
                continue
            now = self._clock()
            with self._lock:
                if now - self._last_trigger_at < self._cooldown:
                    detector.reset()
                    continue
                self._last_trigger_at = now
                self._triggers += 1
                self._running = False   # conversation owns the mic now
            self._stop_flag.set()
            # Release the microphone BEFORE the callback so the window
            # can open capture immediately (no duplicate sessions).
            try:
                input_service.cancel()
            except Exception:  # noqa: BLE001
                pass
            try:
                detector.close()
            except Exception:  # noqa: BLE001
                pass
            with self._lock:
                self._input = None
                self._detector = None
            self._drain_chunks()
            callback = self._on_trigger
            if callback is not None:
                try:
                    callback(self.phrase)
                except Exception:  # noqa: BLE001 - UI callback isolation
                    pass
            return

        with self._lock:
            if self._running and self._stop_flag.is_set():
                self._running = False

    def _enqueue(self, chunk: bytes) -> None:
        """Capture-callback hook: bounded enqueue, drop-oldest."""
        try:
            self._chunks.put_nowait(chunk)
        except queue.Full:
            try:
                self._chunks.get_nowait()
            except queue.Empty:
                pass
            try:
                self._chunks.put_nowait(chunk)
            except queue.Full:
                pass


def wake_status(recognizer: Any, *, phrase: str = DEFAULT_PHRASE) -> dict:
    """Honest capability report for Settings / ``--check``.

    Import + filesystem probes only — never loads a model, never
    claims more than was actually checked.
    """
    try:
        import vosk  # noqa: F401
    except Exception:  # noqa: BLE001
        return {
            "available": False,
            "detail": "The vosk package is not installed "
                      "(pip install vosk).",
        }
    model_dir = (os.environ.get(VOSK_MODEL_ENV)
                 or DEFAULT_VOSK_MODEL_DIR)
    if not os.path.isdir(model_dir):
        return {
            "available": False,
            "detail": "The wake-word model is not installed. Run: "
                      "python3 scripts/install_wake_word_model.py",
        }
    if recognizer is not None and not getattr(
            recognizer, "available", True
    ):
        return {
            "available": False,
            "detail": "Wake-word verification needs the speech model "
                      "(scripts/install_voice_models.py).",
        }
    return {
        "available": True,
        "detail": f"Ready: two-stage detector for “{phrase}” "
                  "(local vosk gate + speech-model verification).",
    }


__all__ = [
    "CANDIDATE_CONF_FLOOR",
    "COOLDOWN_SECONDS",
    "DEFAULT_PHRASE",
    "DEFAULT_PHRASE_PATTERN",
    "DEFAULT_VOSK_MODEL_DIR",
    "MAX_CANDIDATES_PER_SESSION",
    "MAX_VERIFY_FAILURES",
    "QUEUE_MAX_CHUNKS",
    "VOSK_MODEL_ENV",
    "WINDOW_BYTES",
    "TwoStageDetector",
    "WakeWordError",
    "WakeWordService",
    "phrase_pattern",
    "wake_status",
    "VERIFY_MIN_INTERVAL",
]
