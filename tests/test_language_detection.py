"""v0.9.0 language detection tests (deterministic, no hardware/network).

Covers the rules the voice pipeline depends on:

* all 15 supported codes are detected from real Unicode sentences,
* Tanglish (Tamil in Latin letters) is kept by the ASR hint and is
  honestly uncertain *without* one,
* short/ambiguous text never yields a confident guess,
* mixed-script text is uncertain (candidates only), never a coin flip,
* Urdu and Arabic are separated by alphabet inventory, not keywords,
* a stale ASR hint loses to a long unambiguous sentence,
* the preferred fallback language never overrides a clear detection,
* detection-off mode and missing-library degradation stay honest.

langdetect (a tiny pure-Python statistical model) is required for the
text-evidence cases; those tests skip when it is not installed, and a
dedicated test pins the degraded hint/script-only behaviour.
"""

from __future__ import annotations

import unittest

from companion.language_detection import (
    MIN_TEXT_CHARS,
    DetectionResult,
    LanguageDetectionService,
    detect_language,
)
from companion.language_registry import SUPPORTED_CODES

try:  # pragma: no cover - environment dependent
    import langdetect  # noqa: F401

    HAVE_LANGDETECT = True
except Exception:  # pragma: no cover
    HAVE_LANGDETECT = False


# One natural sentence per supported language (native script).
SAMPLES = {
    "en": "Hello there, how are you today? I would like to open my browser.",
    "ta": "வணக்கம், எப்படி இருக்கிறீர்கள்? என் உலாவியைத் திறக்க விரும்புகிறேன்.",
    "hi": "नमस्ते, आप कैसे हैं? मैं अपना ब्राउज़र खोलना चाहता हूँ।",
    "te": "మీరు ఎలా ఉన్నారు? నేను నా బ్రౌజర్ తెరవాలనుకుంటున్నాను.",
    "ml": "എന്റെ ബ്രൗസർ തുറക്കൂ. സുഖമാണോ ഇന്ന്?",
    "kn": "ನನ್ನ ಬ್ರೌಸರ್ ತೆರೆಯಿರಿ. ಹೇಗಿದ್ದೀರಿ ಇಂದು?",
    "ur": "میرا براؤزر کھولیں۔ آپ کیسے ہیں آج؟",
    "zh": "打开我的浏览器。你好吗？今天天气怎么样？",
    "fr": "Ouvre mon navigateur s'il te plaît. Comment allez-vous aujourd'hui?",
    "ru": "Открой мой браузер пожалуйста. Как у тебя дела сегодня?",
    "es": "Abre mi navegador por favor. ¿Cómo estás hoy amigo?",
    "ar": "افتح المتصفح من فضلك. كيف حالك اليوم يا صديقي?",
    "bn": "আমার ব্রাউজার খুলুন। আপনি কেমন আছেন?",
    "pt": "Abra meu navegador por favor. Como você está hoje meu amigo?",
    "id": "Tolong buka browser saya sekarang. Bagaimana kabarmu hari ini?",
}


class RegistryContractTests(unittest.TestCase):
    def test_samples_exist_for_every_supported_language(self):
        self.assertEqual(tuple(sorted(SAMPLES)), tuple(sorted(SUPPORTED_CODES)))

    def test_result_is_immutable_and_preserves_text(self):
        original = SAMPLES["en"]
        result = detect_language(original)
        self.assertIsInstance(result, DetectionResult)
        self.assertEqual(result.text, original)
        with self.assertRaises(Exception):
            result.text = "rewritten"  # frozen dataclass


@unittest.skipUnless(HAVE_LANGDETECT, "langdetect not installed")
class FifteenLanguageTests(unittest.TestCase):
    def test_all_fifteen_detected_from_text_alone(self):
        for want, text in SAMPLES.items():
            with self.subTest(lang=want):
                result = detect_language(text)
                self.assertEqual(result.code, want)
                self.assertFalse(result.uncertain)
                self.assertGreaterEqual(result.confidence, 0.9)
                self.assertIn(result.source, ("text", "script", "agree"))

    def test_detection_is_deterministic(self):
        first = detect_language(SAMPLES["ru"]).code
        for _ in range(3):
            self.assertEqual(detect_language(SAMPLES["ru"]).code, first)


class HintInteractionTests(unittest.TestCase):
    def test_tanglish_kept_by_asr_hint(self):
        # Tamil spoken in Latin letters: the audio is the ground truth.
        result = detect_language(
            "naan panren, enna pannurathu?", asr_hint="ta", hint_confidence=0.9
        )
        self.assertEqual(result.code, "ta")
        self.assertEqual(result.source, "asr")
        self.assertFalse(result.uncertain)

    @unittest.skipUnless(HAVE_LANGDETECT, "langdetect not installed")
    def test_tanglish_without_hint_is_uncertain(self):
        result = detect_language("naan panren naan office poronnen")
        self.assertIsNone(result.code)
        self.assertTrue(result.uncertain)

    def test_short_text_with_hint_keeps_hint(self):
        result = detect_language("ok", asr_hint="zh", hint_confidence=0.95)
        self.assertEqual(result.code, "zh")
        self.assertFalse(result.uncertain)

    @unittest.skipUnless(HAVE_LANGDETECT, "langdetect not installed")
    def test_stale_hint_loses_to_long_unambiguous_sentence(self):
        result = detect_language(SAMPLES["hi"], asr_hint="en", hint_confidence=0.9)
        self.assertEqual(result.code, "hi")
        self.assertEqual(result.source, "text")

    def test_hint_is_alias_normalised(self):
        result = detect_language("ok", asr_hint="zh-CN", hint_confidence=0.9)
        self.assertEqual(result.code, "zh")

    def test_unsupported_hint_is_not_adopted(self):
        result = detect_language("hello there my friend", asr_hint="tl",
                                 hint_confidence=0.9)
        # 'tl' is unsupported: it must not become a supported claim.
        if result.source == "asr":
            self.assertNotEqual(result.code, "tl")


class AmbiguityTests(unittest.TestCase):
    def test_short_words_are_uncertain_without_hint(self):
        for text in ("hi", "ok", "yes"):
            with self.subTest(text=text):
                result = detect_language(text)
                self.assertTrue(result.uncertain)
                self.assertIsNone(result.code)

    def test_featureless_text_is_uncertain(self):
        result = detect_language("\U0001D15A\U0001D15B")  # musical notes
        self.assertTrue(result.uncertain)
        self.assertIsNone(result.code)

    def test_empty_text_is_uncertain(self):
        self.assertTrue(detect_language("").uncertain)
        self.assertTrue(detect_language("   \n\t").uncertain)

    @unittest.skipUnless(HAVE_LANGDETECT, "langdetect not installed")
    def test_mixed_script_text_is_uncertain_with_candidates(self):
        result = detect_language("hello வணக்கம் 你好 مرحبا")
        self.assertTrue(result.uncertain)
        self.assertIsNone(result.code)
        # Candidates come from the closed set only.
        for code in result.candidates:
            self.assertIn(code, SUPPORTED_CODES)

    @unittest.skipUnless(HAVE_LANGDETECT, "langdetect not installed")
    def test_mixed_script_with_hint_keeps_the_audio_hint(self):
        result = detect_language(
            "hello வணக்கம் 你好 مرحبا", asr_hint="hi", hint_confidence=0.9
        )
        self.assertEqual(result.code, "hi")
        self.assertFalse(result.uncertain)

    @unittest.skipUnless(HAVE_LANGDETECT, "langdetect not installed")
    def test_dominant_script_with_incidental_latin_still_detects(self):
        # Code-switched proper noun inside Han text is not "mixed".
        result = detect_language("打开我的浏览器然后继续OK工作和学习")
        self.assertEqual(result.code, "zh")
        self.assertFalse(result.uncertain)


class ArabicScriptTests(unittest.TestCase):
    def test_urdu_and_arabic_are_separated(self):
        ur = detect_language(SAMPLES["ur"])
        ar = detect_language(SAMPLES["ar"])
        self.assertEqual(ur.code, "ur")
        self.assertEqual(ar.code, "ar")

    def test_rtl_flags_match(self):
        from companion.language_registry import supports_rtl

        self.assertTrue(supports_rtl(detect_language(SAMPLES["ur"]).code))
        self.assertTrue(supports_rtl(detect_language(SAMPLES["ar"]).code))


class PreferenceTests(unittest.TestCase):
    @unittest.skipUnless(HAVE_LANGDETECT, "langdetect not installed")
    def test_preference_never_overrides_clear_detection(self):
        service = LanguageDetectionService(preferred="en")
        result = service.detect(SAMPLES["ta"])
        self.assertEqual(result.code, "ta")
        self.assertNotEqual(result.source, "preferred")

    def test_preference_only_labels_the_residue_honestly(self):
        service = LanguageDetectionService(preferred="fr")
        result = service.detect("ok")
        if result.source == "preferred":
            self.assertTrue(result.uncertain)
            self.assertEqual(result.code, "fr")
        else:
            self.assertTrue(result.uncertain)
            self.assertIsNone(result.code)

    def test_no_preference_means_no_invented_code(self):
        result = detect_language("ok")
        self.assertIsNone(result.code)
        self.assertTrue(result.uncertain)


class ConfigurationTests(unittest.TestCase):
    def test_auto_off_returns_preference_without_detecting(self):
        service = LanguageDetectionService(auto=False, preferred="ru")
        # Even a clear Tamil sentence obeys the disabled detection —
        # this mode is "use my fixed language", not "detect".
        result = service.detect(SAMPLES["ta"])
        self.assertEqual(result.code, "ru")
        self.assertEqual(result.source, "preferred")

    def test_auto_off_with_engine_hint_keeps_the_hint(self):
        service = LanguageDetectionService(auto=False, preferred="en")
        result = service.detect("ok", asr_hint="zh", hint_confidence=0.9)
        self.assertEqual(result.code, "zh")
        self.assertEqual(result.source, "asr")

    def test_auto_off_without_anything_is_honest(self):
        service = LanguageDetectionService(auto=False)
        result = service.detect("hello there my friend")
        self.assertTrue(result.uncertain)
        self.assertIsNone(result.code)

    def test_configure_applies_live(self):
        service = LanguageDetectionService(auto=True)
        service.configure(auto=False, preferred="es")
        self.assertFalse(service.auto)
        self.assertEqual(service.preferred, "es")
        service.configure(auto=True)
        self.assertTrue(service.auto)

    def test_configure_normalises_the_preference(self):
        service = LanguageDetectionService()
        service.configure(preferred="pt-BR")
        self.assertEqual(service.preferred, "pt")

    def test_short_text_threshold_is_a_documented_constant(self):
        self.assertEqual(MIN_TEXT_CHARS, 15)

    def test_text_available_flag_is_exposed(self):
        service = LanguageDetectionService()
        self.assertEqual(service.text_available, HAVE_LANGDETECT)


class DegradedModeTests(unittest.TestCase):
    """Behaviour when langdetect is missing: hint/script evidence only."""

    def test_result_reports_library_availability(self):
        service = LanguageDetectionService()
        result = service.detect(SAMPLES["en"])
        self.assertEqual(result.text_available, service.text_available)

    @unittest.skipIf(HAVE_LANGDETECT, "only meaningful without langdetect")
    def test_script_evidence_still_works(self):  # pragma: no cover
        result = detect_language(SAMPLES["ta"])
        self.assertEqual(result.code, "ta")

    @unittest.skipIf(HAVE_LANGDETECT, "only meaningful without langdetect")
    def test_latin_text_without_hint_is_uncertain(self):  # pragma: no cover
        result = detect_language(SAMPLES["en"])
        self.assertTrue(result.uncertain)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
