"""v0.9.0 closed language registry tests.

The registry is the single source of truth for *which* languages the
voice stack may claim.  These tests pin:

* exactly the milestone's 15 ISO codes, in order, with unique specs,
* alias/variant normalisation resolves **into** the closed set and
  never invents a language (``tl``, ``de``, ``ja`` stay unsupported),
* Urdu and Arabic are distinct languages; only those two are RTL,
* every spec carries the engine-specific codes (whisper / espeak /
  langdetect) it needs, non-empty,
* UI choices cover every language exactly once.
"""

from __future__ import annotations

import unittest

from companion.language_registry import (
    BY_CODE,
    DEFAULT_LANGUAGE,
    LANGUAGES,
    SUPPORTED_CODES,
    choices_for_ui,
    detect_codes,
    get,
    is_supported,
    normalize,
    supports_rtl,
)

EXPECTED = (
    "en", "ta", "hi", "te", "ml", "kn", "ur", "zh",
    "fr", "ru", "es", "ar", "bn", "pt", "id",
)


class RegistryShapeTests(unittest.TestCase):
    def test_exactly_the_fifteen_milestone_codes(self):
        self.assertEqual(SUPPORTED_CODES, EXPECTED)
        self.assertEqual(tuple(s.code for s in LANGUAGES), EXPECTED)

    def test_default_language_is_english(self):
        self.assertEqual(DEFAULT_LANGUAGE, "en")
        self.assertIn(DEFAULT_LANGUAGE, BY_CODE)

    def test_specs_are_unique_and_complete(self):
        self.assertEqual(len(BY_CODE), 15)
        for spec in LANGUAGES:
            self.assertTrue(spec.name, spec.code)
            self.assertTrue(spec.native, spec.code)
            self.assertTrue(spec.whisper, spec.code)
            self.assertTrue(spec.espeak, spec.code)
            self.assertTrue(spec.langdetect, spec.code)
            self.assertTrue(spec.scripts, spec.code)

    def test_detect_codes_are_the_supported_set(self):
        self.assertEqual(detect_codes(), SUPPORTED_CODES)

    def test_ui_choices_cover_every_language_once(self):
        choices = choices_for_ui()
        self.assertEqual(len(choices), 15)
        self.assertEqual(tuple(c for c, _ in choices), EXPECTED)
        for code, label in choices:
            self.assertIn(code, label)


class NormalisationTests(unittest.TestCase):
    def test_canonical_codes_pass_through(self):
        for code in EXPECTED:
            self.assertEqual(normalize(code), code)

    def test_case_and_separator_variants(self):
        self.assertEqual(normalize("EN"), "en")
        self.assertEqual(normalize("  Ta "), "ta")
        self.assertEqual(normalize("zh_CN"), "zh")
        self.assertEqual(normalize("zh-CN"), "zh")
        self.assertEqual(normalize("zh-Hant"), "zh")

    def test_engine_dialects_resolve_into_the_set(self):
        # langdetect's Chinese output, espeak's ISO 639-3 Mandarin.
        self.assertEqual(normalize("zh-cn"), "zh")
        self.assertEqual(normalize("cmn"), "zh")
        # Regional variants.
        self.assertEqual(normalize("pt-BR"), "pt")
        self.assertEqual(normalize("es-419"), "es")
        self.assertEqual(normalize("en-US"), "en")
        self.assertEqual(normalize("ur-PK"), "ur")

    def test_unsupported_languages_are_never_invented(self):
        for code in ("tl", "de", "ja", "ko", "tr", "sw", "fi", "it", "cy"):
            self.assertIsNone(normalize(code), code)
            self.assertFalse(is_supported(code), code)

    def test_junk_is_none(self):
        for junk in (None, "", "   ", 42, b"en", "xx"):
            self.assertIsNone(normalize(junk))


class RtlAndLookupTests(unittest.TestCase):
    def test_only_urdu_and_arabic_are_rtl(self):
        rtl = [c for c in EXPECTED if supports_rtl(c)]
        self.assertEqual(rtl, ["ur", "ar"])

    def test_urdu_is_not_arabic(self):
        urdu = get("ur")
        arabic = get("ar")
        self.assertIsNotNone(urdu)
        self.assertIsNotNone(arabic)
        self.assertNotEqual(urdu.espeak, arabic.espeak)
        self.assertNotEqual(urdu.code, arabic.code)

    def test_get_normalises_before_lookup(self):
        self.assertEqual(get("zh-cn").code, "zh")
        self.assertIsNone(get("klingon"))


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
