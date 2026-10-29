"""Shared conversation pipeline for typed and spoken input (v0.9.1).

One service takes *any* user utterance — typed in the task window or
recognized from the microphone — and drives it through exactly the
same path:

1. **intake validation** mirroring the manager's own rules (empty /
   over-long / busy — the manager re-validates anyway; this is only so
   the UI can explain the refusal immediately),
2. **language detection** on the original, untouched text (ASR hint +
   text + script — see :mod:`companion.language_detection`),
3. **submission** through ``manager.submit()`` — the single trusted
   choke point every request already goes through.  Recognition output
   is *untrusted text* here, nothing more: same allowlist, same
   permissions, same approval flow as a typed request,
4. an **optional short reply** from the local LLM, generated with a
   language instruction (never a per-language assistant: one prompt
   template, the language filled in from the detected turn language).

Trust and safety rules pinned by tests:

* the companion never imports the task experience or the execution
  engine — the manager and the LLM client are **duck-typed** and
  injected (this module stays importable headless with fakes),
* the LLM reply is **display/speech text only**: it is never submitted
  back to the manager, never executed, never rendered as markup,
* reply generation failures are swallowed into ``reply=None`` — a
  failed acknowledgement is never faked, and the task submission
  itself stands regardless,
* detection never raises: an uncertain result is reported honestly so
  the caller can ask the user instead of guessing,
* this module performs no I/O of its own: no network, no files, no
  audio.  Callers run :meth:`ConversationService.submit` **off the GTK
  main loop** (it can block on the local LLM for a few seconds).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Optional, Tuple

from companion.language_detection import DetectionResult, LanguageDetectionService
from companion.language_registry import SUPPORTED_CODES, get

#: Mirror of the manager's intake ceiling (the manager re-checks).
MAX_REQUEST_CHARS = 2000

#: The language instruction every reply prompt carries (single shared
#: template — languages are data, not separate assistants).
REPLY_LANGUAGE_INSTRUCTION = (
    "Reply in the language used in the user's latest message."
)

_REPLY_BASE = (
    "You are Alwin, a friendly local desktop assistant. "
    + REPLY_LANGUAGE_INSTRUCTION
    + " Keep the reply to one short plain sentence. "
    "Preserve technical names exactly as written: file names, paths, "
    "commands, application names, URLs and code identifiers must not be "
    "translated or reworded. Do not add greetings, markdown or quotes. "
    "If the language is uncertain, reply in the same language as the "
    "user's message rather than switching to another."
)


@dataclass(frozen=True)
class TurnResult:
    """Outcome of one conversation turn — success and failures alike."""

    #: True only when the request reached the task manager.
    ok: bool
    #: Human-readable intake/submission error when ``ok`` is False.
    error: Optional[str]
    #: Resolved supported language for this turn (None when unknown).
    language: Optional[str]
    #: The full detection outcome (confidence, uncertainty, candidates).
    detection: DetectionResult
    #: Duck-typed TaskRecord returned by the manager (trusted output).
    record: Optional[Any] = None
    #: Optional short LLM acknowledgement (untrusted display/TTS text).
    reply: Optional[str] = None
    #: How the input arrived ("typed", "voice", ... for audit/UI).
    source: str = "typed"

    @property
    def uncertain_language(self) -> bool:
        """True when the caller should ask the user which language."""
        return self.detection.uncertain


def validate_intake(text: Optional[str], *, busy: bool) -> Optional[str]:
    """Return an intake error, or ``None`` when the request may be sent.

    Mirrors ``taskui.presenter.validate_submission`` (which itself
    mirrors the manager) without importing the task experience — the
    companion stays engine-free by design.
    """
    if busy:
        return (
            "A task is already running — wait for it to finish before "
            "sending another request."
        )
    clean = (text or "").strip()
    if not clean:
        return "Type what you want to do, then press Submit."
    if len(clean) > MAX_REQUEST_CHARS:
        return f"The request is too long ({MAX_REQUEST_CHARS} characters maximum)."
    return None


class ConversationService:
    """Turns utterances into task submissions, in the user's language.

    Parameters (all duck-typed on purpose):

    * ``manager`` — anything with ``busy`` and ``submit(text)`` (the
      real ``taskui.manager.TaskManager``); ``None`` means no engine is
      attached and every submission fails honestly,
    * ``detector`` — a :class:`LanguageDetectionService` (created with
      sensible defaults when omitted),
    * ``llm_client`` — anything with ``generate(prompt=..., system=...)``
      (the local Ollama client); ``None`` disables replies entirely,
    * ``replies_enabled`` — the user's spoken-replies setting; toggled
      live from settings, independent of whether an LLM exists.
    """

    def __init__(
        self,
        *,
        manager: Any = None,
        detector: Optional[LanguageDetectionService] = None,
        llm_client: Any = None,
        replies_enabled: bool = False,
    ) -> None:
        self.manager = manager
        self.detector = detector or LanguageDetectionService()
        self.llm_client = llm_client
        self.replies_enabled = bool(replies_enabled)

    # ------------------------------------------------------------------ #
    # Configuration (settings apply live)
    # ------------------------------------------------------------------ #

    def set_replies_enabled(self, enabled: bool) -> None:
        self.replies_enabled = bool(enabled)

    def set_llm_client(self, llm_client: Any) -> None:
        self.llm_client = llm_client

    def configure_language(self, *, auto: Optional[bool] = None,
                           preferred: Optional[str] = None) -> None:
        self.detector.configure(auto=auto, preferred=preferred)

    @property
    def engine_available(self) -> bool:
        """True when a task manager is attached (nothing is assumed)."""
        return self.manager is not None

    @property
    def reply_available(self) -> bool:
        """True only when replies are both enabled *and* possible."""
        return self.replies_enabled and self.llm_client is not None

    # ------------------------------------------------------------------ #
    # Detection
    # ------------------------------------------------------------------ #

    def detect(
        self,
        text: str,
        *,
        asr_hint: Optional[str] = None,
        hint_confidence: float = 0.0,
    ) -> DetectionResult:
        """Detect the turn language; never raises, never invents."""
        try:
            return self.detector.detect(
                text, asr_hint=asr_hint, hint_confidence=hint_confidence
            )
        except Exception:  # noqa: BLE001 - detection must not break a turn
            original = text if isinstance(text, str) else ""
            return DetectionResult(
                None, "none", 0.0, True, original, (), False
            )

    # ------------------------------------------------------------------ #
    # Submission
    # ------------------------------------------------------------------ #

    def submit(
        self,
        text: Optional[str],
        *,
        source: str = "typed",
        asr_hint: Optional[str] = None,
        hint_confidence: float = 0.0,
        want_reply: bool = False,
    ) -> TurnResult:
        """Validate, detect language, and submit through the manager.

        Blocking (the optional reply calls the local LLM): run it on a
        worker thread, never on the GTK main loop.
        """
        busy = bool(getattr(self.manager, "busy", False))
        error = validate_intake(text, busy=busy)
        clean = (text or "").strip()

        # Detection runs even for refused intake? No — only meaningful
        # requests are detected; a refusal is reported on its own.
        if error is not None:
            return TurnResult(
                False, error, None,
                DetectionResult(None, "none", 0.0, True, clean or "", (), True),
                None, None, source,
            )

        detection = self.detect(clean, asr_hint=asr_hint,
                                hint_confidence=hint_confidence)
        language = detection.resolved

        if self.manager is None:
            return TurnResult(
                False,
                "The task engine is not open — voice and typed requests "
                "both need the task window to run.",
                language, detection, None, None, source,
            )

        try:
            record = self.manager.submit(clean)
        except Exception as exc:  # noqa: BLE001 - surface manager refusals
            return TurnResult(
                False, str(exc) or "The request could not be sent.",
                language, detection, None, None, source,
            )

        reply = None
        if want_reply:
            reply = self.reply(clean, language)

        return TurnResult(
            True, None, language, detection, record, reply, source
        )

    # ------------------------------------------------------------------ #
    # Optional reply (display / speech text ONLY — never executed)
    # ------------------------------------------------------------------ #

    def reply(self, text: str, language: Optional[str]) -> Optional[str]:
        """A short acknowledgement in *language*, or None (never fake).

        Failure, missing LLM, or replies-disabled all return ``None``:
        the caller shows/says nothing rather than an English fallback
        the user did not ask for.  Use :meth:`reply_with_reason` when
        the caller wants to *explain* an honest failure to the user.
        """
        return self.reply_with_reason(text, language)[0]

    def reply_with_reason(
        self, text: str, language: Optional[str]
    ) -> Tuple[Optional[str], Optional[str]]:
        """Like :meth:`reply`, plus a short honest failure reason.

        Returns ``(reply, None)`` on success and ``(None, reason)`` on
        failure, where *reason* is a bounded, user-presentable
        explanation (``"replies are turned off in Settings"``,
        ``"no local AI model is attached (start with --llm)"``,
        ``"the local AI model failed: …"``,
        ``"the local AI model returned an empty response"``).  The
        reason is diagnostic text for the UI — never a fabricated
        reply, and never used when a real reply exists.
        """
        if not self.replies_enabled:
            return None, "replies are turned off in Settings"
        if self.llm_client is None:
            return None, ("no local AI model is attached "
                          "(start Alwin with --llm)")
        spec = get(language)
        system = _system_prompt(spec)
        try:
            out = self.llm_client.generate(
                prompt=text,
                system=system,
            )
        except Exception as exc:  # noqa: BLE001 - honest absence, no crash
            detail = str(exc).strip()[:160] or exc.__class__.__name__
            return None, f"the local AI model failed: {detail}"
        if not isinstance(out, str):
            return None, ("the local AI model returned an unusable "
                          "response")
        reply = " ".join(out.split())
        if not reply:
            return None, ("the local AI model returned an empty "
                          "response")
        # Bound what we will ever display/speak (mirrors the speech
        # bubble limit): a runaway generation is truncated, not shown.
        return reply[:600], None


def _system_prompt(spec) -> str:
    """The single shared reply template, language filled in as data."""
    if spec is None:
        return _REPLY_BASE
    return (
        f"{_REPLY_BASE} The detected language of this turn is "
        f"{spec.name} ({spec.code}); write the reply in {spec.name}."
    )


def reply_uses_supported_language(language: Optional[str]) -> bool:
    """Whether *language* names one of the closed 15 (helper for UI)."""
    return language in SUPPORTED_CODES


__all__ = [
    "MAX_REQUEST_CHARS",
    "REPLY_LANGUAGE_INSTRUCTION",
    "ConversationService",
    "TurnResult",
    "reply_uses_supported_language",
    "validate_intake",
]
