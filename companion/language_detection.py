"""Language detection for recognized/typed text (v0.9.1).

Combines three signals — never a fixed keyword list:

1. the **ASR hint** (the recognition engine's own language metadata
   for the audio it just heard),
2. **text detection** (:mod:`langdetect`, a statistical multilingual
   detector, seeded for determinism),
3. a deterministic **Unicode script profile** (Tamil text is Tamil
   script, Bengali is Bengali script … — writing systems, not
   keywords), which also separates the two Arabic-script languages
   (Urdu vs Arabic) with langdetect's statistical call as tie-break.

Rules (all tested):

* the original recognized text is always preserved untouched,
* confident agreement (hint + text, or a unique supported script) is
  returned with ``uncertain=False``,
* short/ambiguous text keeps the **recognition hint** when one exists
  (the audio is the ground truth for what was spoken — this is what
  makes Tanglish work: Tamil spoken in Latin letters is still Tamil),
* without a reliable signal the result is honestly ``uncertain=True``
  with no invented code (or only ranked candidates), so the caller can
  ask the user to repeat or choose — detection never confidently
  assigns a wrong language,
* a preferred language is used **only** when nothing was clearly
  detected; a clear detection always wins over the preference.

Pure Python + langdetect (a local statistical model, no network).
langdetect is optional: when it is not installed the service degrades
to hint/script signals only and says so in ``text_available``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass
from typing import List, Optional, Tuple

from companion.language_registry import normalize

# langdetect is seeded once so results are deterministic in tests and
# runs (its default bootstrap mode is random).
try:  # pragma: no cover - exercised by tests when the lib is present
    import langdetect as _langdetect

    _langdetect.DetectorFactory.seed = 20260915
    _LANGDETECT = True
except Exception:  # noqa: BLE001 - optional dependency
    _langdetect = None
    _LANGDETECT = False

#: Below this many characters, statistical text detection on Latin
#: script is guesswork ("ok", "yes", "hi" all "detect" confidently as
#: unrelated languages) — we refuse to pretend otherwise.
MIN_TEXT_CHARS = 15

#: Minimum langdetect probability to treat text evidence as confident.
MIN_TEXT_PROB = 0.90

#: Script coverage of a mixed-script text before it counts as mixed.
MIXED_SHARE = 0.30

# Unicode script families we care about, as (name, low, high) code
# point ranges (decoded literals — parsed as ordinals, never as text).
def _range(name: str, *pairs: str) -> tuple:
    return (name, tuple((ord(a), ord(b)) for a, b in pairs))


_SCRIPT_RANGES: Tuple[tuple, ...] = (
    _range("devanagari", ("\u0900", "\u097F")),
    _range("bengali", ("\u0980", "\u09FF")),
    _range("tamil", ("\u0B80", "\u0BFF")),
    _range("telugu", ("\u0C00", "\u0C7F")),
    _range("malayalam", ("\u0D00", "\u0D7F")),
    _range("kannada", ("\u0D80", "\u0DFF")),
    _range("arabic", ("\u0600", "\u06FF"), ("\u0750", "\u077F"),
           ("\u08A0", "\u08FF")),
    _range("cyrillic", ("\u0400", "\u04FF")),
    _range("han", ("\u3400", "\u4DBF"), ("\u4E00", "\u9FFF"),
           ("\uF900", "\uFAFF")),
    _range("latin", ("A", "Z"), ("a", "z"), ("\u00C0", "\u024F")),
)

#: Characters that mark **Urdu** rather than Arabic within the shared
#: Arabic script (Persian/Urdu additions).  This is an alphabet
#: inventory — writing-system characteristics, not keyword matching.
_URDU_MARKERS = set("\u0679\u0688\u0691\u0698\u06A9\u06AF\u06BE\u06C1\u06D2\u06D5\u06CC")
_ARABIC_MARKERS = set("\u0640\u064B\u064C\u064D\u064E\u064F\u0650\u0651\u0652")

#: script family -> the one supported language that uses it (unique
#: within the closed registry; Latin is deliberately absent).
_SCRIPT_LANGUAGE = {
    "devanagari": "hi",
    "bengali": "bn",
    "tamil": "ta",
    "telugu": "te",
    "malayalam": "ml",
    "kannada": "kn",
    "cyrillic": "ru",
    "han": "zh",
}


@dataclass(frozen=True)
class DetectionResult:
    """What the detector concluded — and how sure it honestly is."""

    #: Supported code, or None when nothing reliable was found.
    code: Optional[str]
    #: How it was decided: "asr" | "text" | "script" | "agree" |
    #: "preferred" | "none".
    source: str
    #: 0.0–1.0 confidence in ``code`` (0 when code is None).
    confidence: float
    #: True when the user should be asked to repeat/choose — the UI
    #: must not present an uncertain answer as fact.
    uncertain: bool
    #: The original text, preserved verbatim (never rewritten).
    text: str
    #: Ranked alternatives when uncertain (supported codes only).
    candidates: Tuple[str, ...] = ()
    #: False when the statistical text detector is not installed.
    text_available: bool = True

    @property
    def resolved(self) -> Optional[str]:
        """The supported code (alias-normalised); convenience view."""
        return normalize(self.code)


def _script_profile(text: str) -> Tuple[List[Tuple[str, int]], int]:
    """Dominant script families in *text* (most frequent first) and the
    total number of alphabetic characters considered."""
    counts = {name: 0 for name, _ranges in _SCRIPT_RANGES}
    total = 0
    for ch in text:
        if not ch.isalpha():
            continue
        total += 1
        point = ord(ch)
        for name, ranges in _SCRIPT_RANGES:
            if any(lo <= point <= hi for lo, hi in ranges):
                counts[name] += 1
                break
    ranked = sorted(counts.items(), key=lambda kv: kv[1], reverse=True)
    return [(name, n) for name, n in ranked if n > 0], total


def _arabic_flavour(text: str) -> Optional[str]:
    """'ur' or 'ar' from alphabet inventory, or None when unclear."""
    ur = sum(1 for ch in text if ch in _URDU_MARKERS)
    ar = sum(1 for ch in text if ch in _ARABIC_MARKERS)
    if ur > 0 and ur >= max(1, ar // 6):
        return "ur"
    if ar > 0 or ur > 0:
        return "ar"
    return None


class LanguageDetectionService:
    """Detects the language of recognized text (thread-safe, pure).

    ``auto`` off (a user setting) means detection is disabled: the
    preferred language (if any) is returned as ``source="preferred"``
    — still never overriding a hint, because no detection is attempted
    at all in that mode.
    """

    def __init__(self, *, auto: bool = True,
                 preferred: Optional[str] = None) -> None:
        self.auto = bool(auto)
        self.preferred = normalize(preferred)
        self._lock = threading.Lock()

    # ------------------------------------------------------------------ #
    # Configuration (settings apply live)
    # ------------------------------------------------------------------ #

    def configure(self, *, auto: Optional[bool] = None,
                  preferred: Optional[str] = None) -> None:
        with self._lock:
            if auto is not None:
                self.auto = bool(auto)
            if preferred is not None:
                self.preferred = normalize(preferred)

    @property
    def text_available(self) -> bool:
        """True when the statistical text detector is installed."""
        return _LANGDETECT

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
        """Detect the language of *text*.

        ``asr_hint`` is the recognition engine's language for the audio
        (only pass it when the engine auto-detected it — a hint the app
        forced itself is not evidence).  ``hint_confidence`` (0–1) is
        the engine's own probability for that hint.
        """
        original = text if isinstance(text, str) else ""
        clean = " ".join(original.split())

        if not self.auto:
            # Detection disabled by setting: preference decides, and an
            # engine hint still outranks it (no clear detection exists).
            hint = normalize(asr_hint)
            if hint:
                return DetectionResult(hint, "asr", max(hint_confidence, 0.5),
                                       False, original, (), self.text_available)
            if self.preferred:
                return DetectionResult(self.preferred, "preferred", 0.6,
                                       False, original, (), self.text_available)
            return DetectionResult(None, "none", 0.0, True, original, (),
                                   self.text_available)

        hint = normalize(asr_hint)
        if not clean:
            # Nothing to analyze: keep the hint if we have one.
            if hint:
                return DetectionResult(hint, "asr",
                                       max(hint_confidence, 0.5), False,
                                       original, (), self.text_available)
            return DetectionResult(None, "none", 0.0, True, original, (),
                                   self.text_available)

        # ---- statistical text detection (local, seeded) ---------------- #
        text_code: Optional[str] = None
        text_prob = 0.0
        top: List[Tuple[str, float]] = []
        if _LANGDETECT and len(clean) >= MIN_TEXT_CHARS:
            try:
                with self._lock:
                    probs = _langdetect.detect_langs(clean)
                if probs:
                    best = probs[0]
                    text_code = normalize(best.lang)
                    text_prob = float(best.prob)
                    top = [(str(p.lang), float(p.prob)) for p in probs]
            except Exception:  # noqa: BLE001 - no features / garbage text
                text_code, text_prob, top = None, 0.0, []
        text_strong = (
            text_code is not None
            and text_prob >= MIN_TEXT_PROB
            and len(clean) >= MIN_TEXT_CHARS
        )

        # ---- deterministic script profile ----------------------------- #
        profile, alpha_total = _script_profile(clean)
        dominant = profile[0] if profile else (None, 0)
        dominant_name, dominant_n = dominant
        mixed = bool(
            profile
            and alpha_total > 0
            and len(profile) > 1
            and profile[1][1] >= 2
            and (
                profile[1][1] >= alpha_total * MIXED_SHARE
                # or a near-even second script (a 25%-of-sentence Latin
                # word inside Tamil text is still mixed writing) …
                or profile[1][1] >= profile[0][1] * 0.5
            )
        )

        script_code: Optional[str] = None
        if not mixed and dominant_name == "arabic":
            script_code = _arabic_flavour(clean)
            if script_code is None and text_strong:
                script_code = text_code
        elif not mixed and dominant_name in _SCRIPT_LANGUAGE:
            script_code = _SCRIPT_LANGUAGE[dominant_name]

        # ---- combine the signals --------------------------------------- #
        # 0. Genuinely mixed scripts with no engine hint: several
        #    writing systems share the sentence — claiming one language
        #    would be a guess (langdetect still "confidently" picks one
        #    of them for mixed input, so its confidence is not trusted
        #    here).  Candidates only; the caller asks the user.
        if mixed and not hint:
            cands = _candidates(top, text_code, preferred=self.preferred)
            script_cands = tuple(
                _SCRIPT_LANGUAGE[name]
                for name, _n in profile
                if name in _SCRIPT_LANGUAGE
                and _SCRIPT_LANGUAGE[name] not in cands
            )
            ranked = tuple(cands) + script_cands
            if ranked:
                return DetectionResult(None, "none", 0.0, True, original,
                                       ranked[:3], self.text_available)
            if self.preferred:
                return DetectionResult(self.preferred, "preferred", 0.3,
                                       True, original, (), self.text_available)
            return DetectionResult(None, "none", 0.0, True, original, (),
                                   self.text_available)

        # 1. Hint and strong text evidence agree: confident.
        if hint and text_strong and hint == text_code:
            return DetectionResult(hint, "agree", min(1.0, text_prob),
                                   False, original, (), True)

        # 2. Strong text evidence, no contradicting confident hint:
        #    the sentence itself decides (but on mixed-script input the
        #    statistical guess is not evidence against the audio).
        if text_strong:
            if (hint and hint != text_code and len(clean) >= 30
                    and not mixed):
                # A long, unambiguous sentence in another language beats
                # a stale hint (the user switched languages).
                return DetectionResult(text_code, "text", text_prob,
                                       False, original, (), True)
            if not hint or hint == text_code:
                return DetectionResult(text_code, "text", text_prob,
                                       False, original, (), True)

        # 3. Unique supported script (Tamil text is Tamil, …): confident
        #    unless a *confident* hint disagrees outright.
        if script_code:
            if hint and hint != script_code and hint_confidence >= 0.8:
                return DetectionResult(hint, "asr",
                                       max(hint_confidence, 0.7), False,
                                       original, (), self.text_available)
            return DetectionResult(script_code, "script", 0.9, False,
                                   original, (), self.text_available)

        # 4. Only an engine hint (short text, Latin script, Tanglish…):
        #    the audio is the ground truth — keep it, honestly labelled.
        if hint:
            return DetectionResult(hint, "asr", max(hint_confidence, 0.6),
                                   False, original, (), self.text_available)

        # 5. No hint, inconclusive text: be honest — candidates only.
        candidates = _candidates(top, text_code, preferred=self.preferred)
        if candidates:
            return DetectionResult(None, "none", 0.0, True, original,
                                   tuple(candidates), self.text_available)
        # Last resort: the preference (never a "detected" claim).
        if self.preferred:
            return DetectionResult(self.preferred, "preferred", 0.3, True,
                                   original, (), self.text_available)
        return DetectionResult(None, "none", 0.0, True, original, (),
                               self.text_available)


def _candidates(top: List[Tuple[str, float]], text_code: Optional[str],
                *, preferred: Optional[str]) -> Tuple[str, ...]:
    """Ranked *supported* candidates from detector output (best-first)."""
    out: List[str] = []
    for raw, _prob in top:
        code = normalize(raw)
        if code and code not in out:
            out.append(code)
    if text_code and text_code not in out:
        out.insert(0, text_code)
    if preferred and preferred not in out:
        out.append(preferred)
    return tuple(out[:3])


def detect_language(
    text: str,
    *,
    asr_hint: Optional[str] = None,
    hint_confidence: float = 0.0,
    auto: bool = True,
    preferred: Optional[str] = None,
) -> DetectionResult:
    """Module-level convenience (a one-shot detection, same rules)."""
    return LanguageDetectionService(auto=auto, preferred=preferred).detect(
        text, asr_hint=asr_hint, hint_confidence=hint_confidence
    )


__all__ = [
    "DetectionResult",
    "LanguageDetectionService",
    "MIN_TEXT_CHARS",
    "MIN_TEXT_PROB",
    "detect_language",
]
