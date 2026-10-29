"""Closed language registry for multilingual voice (v0.9.1).

One module owns *what* a supported language is everywhere in the voice
stack: its ISO code, display names, right-to-left flag, and the
**engine-specific codes** for each local engine.  Keeping this closed
and explicit is what lets the app be honest about capability:

* detection codes (what :mod:`companion.language_detection` may
  return) are the 15 ISO 639-1 codes of the milestone — nothing else
  is ever *supported*, even when a detector emits a variant
  (``zh-cn``, ``pt-br``, ``cmn`` …) — those are *normalised*, never
  invented,
* engine codes differ from ours per engine: espeak-ng speaks Chinese
  as ``cmn`` (ISO 639-3) and French as ``French (France)``; langdetect
  reports ``zh-cn`` for Chinese; faster-whisper uses our ISO codes
  as-is.  Resolving them belongs here, not scattered in call sites,
* "detected" and "supported" stay distinct: a detector may be unsure
  or name an unsupported language — the registry never pretends a
  missing capability exists (voice availability is *probed* by the
  engine modules and reported live, never stored as a claim here).

Pure Python (dataclasses + typing only), headless and dependency-free
so it is testable without GTK, audio or models.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Optional, Tuple

#: The milestone's ISO codes, in the documented order.
SUPPORTED_CODES: Tuple[str, ...] = (
    "en", "ta", "hi", "te", "ml", "kn", "ur", "zh",
    "fr", "ru", "es", "ar", "bn", "pt", "id",
)

#: UI/response language used when nothing else is clear.
DEFAULT_LANGUAGE = "en"


@dataclass(frozen=True)
class LanguageSpec:
    """Static facts about one supported language."""

    #: ISO 639-1 code (the canonical id everywhere in the app).
    code: str
    #: English display name (menus, status text).
    name: str
    #: Native display name (what the language calls itself).
    native: str
    #: True for right-to-left scripts (Urdu and Arabic only — Urdu is
    #: never treated as Arabic: they are separate languages here).
    rtl: bool
    #: Language code faster-whisper's multilingual model accepts.
    whisper: str
    #: espeak-ng voice selector (verified against the bundled voice
    #: data: name or identifier that resolves to that language).
    espeak: str
    #: Primary code :mod:`langdetect` reports for this language.
    langdetect: str
    #: Script families (keys used by language_detection) whose
    #: presence characterises this language's writing system.
    scripts: Tuple[str, ...]


LANGUAGES: Tuple[LanguageSpec, ...] = (
    LanguageSpec("en", "English", "English", False,
                 "en", "en-us", "en", ("latin",)),
    LanguageSpec("ta", "Tamil", "தமிழ்", False,
                 "ta", "tamil", "ta", ("tamil",)),
    LanguageSpec("hi", "Hindi", "हिन्दी", False,
                 "hi", "hindi", "hi", ("devanagari",)),
    LanguageSpec("te", "Telugu", "తెల౗గు", False,
                 "te", "telugu", "te", ("telugu",)),
    LanguageSpec("ml", "Malayalam", "മലയാളം", False,
                 "ml", "malayalam", "ml", ("malayalam",)),
    LanguageSpec("kn", "Kannada", "ಕನ್ನಡ", False,
                 "kn", "kannada", "kn", ("kannada",)),
    LanguageSpec("ur", "Urdu", "اردو", True,
                 "ur", "urdu", "ur", ("arabic",)),
    LanguageSpec("zh", "Chinese", "中文", False,
                 "zh", "cmn", "zh-cn", ("han",)),
    LanguageSpec("fr", "French", "Français", False,
                 "fr", "French (France)", "fr", ("latin",)),
    LanguageSpec("ru", "Russian", "Русский", False,
                 "ru", "russian", "ru", ("cyrillic",)),
    LanguageSpec("es", "Spanish", "Español", False,
                 "es", "Spanish (Spain)", "es", ("latin",)),
    LanguageSpec("ar", "Arabic", "العربية", True,
                 "ar", "arabic", "ar", ("arabic",)),
    LanguageSpec("bn", "Bengali", "বাংলা", False,
                 "bn", "bengali", "bn", ("bengali",)),
    LanguageSpec("pt", "Portuguese", "Português", False,
                 "pt", "Portuguese (Portugal)", "pt", ("latin",)),
    LanguageSpec("id", "Indonesian", "Bahasa Indonesia", False,
                 "id", "indonesian", "id", ("latin",)),
)

BY_CODE: Dict[str, LanguageSpec] = {spec.code: spec for spec in LANGUAGES}

#: Codes langdetect (or engines) emit that mean a supported language —
#: every alias resolves *into* the closed set; nothing is added by it.
_ALIASES: Dict[str, str] = {
    # Chinese variants -> zh (Simplified is the documented default).
    "zh-cn": "zh", "zh-tw": "zh", "zh-hans": "zh", "zh-hant": "zh",
    "zh-sg": "zh", "cmn": "zh", "yue": "zh", "zh-cn-gb": "zh",
    # Common regional variants -> the base supported language.
    "en-us": "en", "en-gb": "en", "en-au": "en",
    "pt-br": "pt", "pt-pt": "pt",
    "es-419": "es", "es-es": "es", "es-mx": "es",
    "fr-fr": "fr", "fr-be": "fr", "fr-ch": "fr",
    "ru-ru": "ru", "ar-eg": "ar", "ar-sa": "ar",
    "hi-in": "hi", "ta-in": "ta", "te-in": "te", "ml-in": "ml",
    "kn-in": "kn", "ur-pk": "ur", "bn-in": "bn", "id-id": "id",
    # langdetect's Chinese split and any underscore spelling.
    "zh_cn": "zh", "zh_tw": "zh",
}


def normalize(code: Optional[str]) -> Optional[str]:
    """Map a variant/alias onto a supported code (None if unsupported).

    ``"zh-CN"``, ``"zh_cn"``, ``"cmn"`` -> ``"zh"``; ``"tl"``, ``"de"``,
    ``None``, ``""`` -> ``None``.  Case-insensitive, never raises.
    """
    if not isinstance(code, str):
        return None
    key = code.strip().lower().replace("_", "-")
    if not key:
        return None
    if key in BY_CODE:
        return key
    alias = _ALIASES.get(key)
    if alias is not None:
        return alias
    # "xx-YY" regional spelling not listed above: try the base part.
    base = key.split("-", 1)[0]
    if base != key:
        if base in BY_CODE:
            return base
        return _ALIASES.get(base)
    return None


def get(code: Optional[str]) -> Optional[LanguageSpec]:
    """The spec for *code* (normalised), or None when unsupported."""
    resolved = normalize(code)
    if resolved is None:
        return None
    return BY_CODE.get(resolved)


def is_supported(code: Optional[str]) -> bool:
    """True when *code* names one of the 15 supported languages."""
    return normalize(code) is not None


def supports_rtl(code: Optional[str]) -> bool:
    """True only for Urdu and Arabic (both RTL, never conflated)."""
    spec = get(code)
    return bool(spec is not None and spec.rtl)


def supports_language(code: Optional[str]) -> bool:
    """Alias of :func:`is_supported` for call-site readability."""
    return is_supported(code)


def choices_for_ui() -> Tuple[Tuple[str, str], ...]:
    """(code, label) pairs for a settings dropdown: name + native + ISO code."""
    return tuple(
        (spec.code, f"{spec.name} ({spec.native}) · {spec.code}")
        for spec in LANGUAGES
    )


def detect_codes() -> Tuple[str, ...]:
    """Codes the detector may legitimately return (normalised set)."""
    return SUPPORTED_CODES


__all__ = [
    "BY_CODE",
    "DEFAULT_LANGUAGE",
    "LANGUAGES",
    "LanguageSpec",
    "SUPPORTED_CODES",
    "choices_for_ui",
    "detect_codes",
    "get",
    "is_supported",
    "normalize",
    "supports_language",
    "supports_rtl",
]
