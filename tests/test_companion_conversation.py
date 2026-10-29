"""v0.9.0 conversation pipeline tests (fakes only — no engine, no LLM).

Pins the promises the voice feature rests on:

* typed and spoken input share **one** submission path — the exact
  text handed to the manager, through ``manager.submit()``,
* intake rules are identical to the task window's (parity check
  against ``taskui.presenter.validate_submission``),
* transcripts are untrusted text: nothing about voice input changes
  validation, permissions or approval,
* the reply prompt is a single template carrying the language
  instruction — replies are never per-language assistants,
* replies are display/TTS text only: never fed back to the manager,
* every failure mode (no engine, busy, refusal, LLM error, replies
  disabled) degrades honestly instead of faking an outcome.
"""

from __future__ import annotations

import unittest

from companion.conversation import (
    MAX_REQUEST_CHARS,
    REPLY_LANGUAGE_INSTRUCTION,
    ConversationService,
    TurnResult,
    validate_intake,
)
from companion.language_detection import DetectionResult, LanguageDetectionService


class FakeManager:
    """Duck-typed TaskManager: records submissions, mimics busy/raise."""

    def __init__(self, *, busy=False, raise_error=None):
        self.busy = busy
        self.raise_error = raise_error
        self.submissions = []

    def submit(self, command, *, dry_run=False):
        if self.raise_error is not None:
            raise self.raise_error
        self.submissions.append(command)
        return f"record:{len(self.submissions)}"


class FakeLLM:
    """Duck-typed Ollama client: records prompts, can fail on demand."""

    def __init__(self, response="நல்லது.", raise_error=None, non_string=False):
        self.response = response
        self.raise_error = raise_error
        self.non_string = non_string
        self.calls = []

    def generate(self, prompt, *, system=None, **kwargs):
        self.calls.append({"prompt": prompt, "system": system})
        if self.raise_error is not None:
            raise self.raise_error
        if self.non_string:
            return {"text": "not a string"}
        return self.response


TAMIL = "வணக்கம், என் உலாவியைத் திறக்க வேண்டும்."
ENGLISH = "Please open my browser and show me the weather."


class IntakeParityTests(unittest.TestCase):
    """The mirrored rules must equal the task window's real rules."""

    def test_matches_taskui_presenter_for_every_case(self):
        from taskui.presenter import validate_submission as real

        cases = [
            (None, False), ("", False), ("   ", False),
            ("hello", False), ("x" * (MAX_REQUEST_CHARS + 1), False),
            ("x" * MAX_REQUEST_CHARS, False),
            ("hello", True), ("", True),
        ]
        for text, busy in cases:
            with self.subTest(text=(text or "")[:20], busy=busy):
                self.assertEqual(
                    validate_intake(text, busy=busy),
                    real(text, busy=busy),
                )

    def test_refusal_messages_are_present_and_actionable(self):
        self.assertIn("already running", validate_intake("x", busy=True))
        self.assertTrue(validate_intake("", busy=False))
        self.assertIn(str(MAX_REQUEST_CHARS),
                      validate_intake("x" * 3000, busy=False))


class SubmissionPathTests(unittest.TestCase):
    def test_submission_reaches_the_manager_with_the_exact_text(self):
        manager = FakeManager()
        service = ConversationService(manager=manager)
        result = service.submit(TAMIL, source="voice", asr_hint="ta",
                                hint_confidence=0.95)
        self.assertTrue(result.ok)
        self.assertIsNone(result.error)
        # Verbatim: recognition output is not rewritten or "cleaned".
        self.assertEqual(manager.submissions, [TAMIL])
        self.assertEqual(result.record, "record:1")
        self.assertEqual(result.source, "voice")

    def test_typed_and_voice_paths_are_indistinguishable_to_manager(self):
        manager = FakeManager()
        service = ConversationService(manager=manager)
        service.submit("open the browser", source="typed")
        service.submit("open the browser", source="voice", asr_hint="en")
        self.assertEqual(manager.submissions,
                         ["open the browser", "open the browser"])

    def test_language_is_detected_before_submission(self):
        manager = FakeManager()
        service = ConversationService(manager=manager)
        result = service.submit(TAMIL, source="voice")
        self.assertEqual(result.language, "ta")
        self.assertFalse(result.detection.uncertain)

    def test_uncertain_detection_is_reported_not_guessed(self):
        manager = FakeManager()
        service = ConversationService(manager=manager)
        result = service.submit("ok", source="voice")
        # The task still runs (the words were clear enough to submit),
        # but no language claim is invented.
        self.assertTrue(result.ok)
        self.assertIsNone(result.language)
        self.assertTrue(result.uncertain_language)

    def test_busy_manager_refuses_without_submitting(self):
        manager = FakeManager(busy=True)
        service = ConversationService(manager=manager)
        result = service.submit("do something")
        self.assertFalse(result.ok)
        self.assertIn("already running", result.error)
        self.assertEqual(manager.submissions, [])

    def test_empty_and_oversized_never_reach_the_manager(self):
        manager = FakeManager()
        service = ConversationService(manager=manager)
        for bad in (None, "", "   ", "x" * (MAX_REQUEST_CHARS + 1)):
            result = service.submit(bad)
            self.assertFalse(result.ok, repr(bad))
        self.assertEqual(manager.submissions, [])

    def test_no_manager_fails_honestly(self):
        service = ConversationService(manager=None)
        result = service.submit(ENGLISH, source="voice")
        self.assertFalse(result.ok)
        self.assertIn("task engine", result.error)
        self.assertFalse(service.engine_available)
        # Detection still ran: the caller knows what was said.
        self.assertEqual(result.language, "en")

    def test_manager_refusal_is_surfaced_verbatim(self):
        manager = FakeManager(raise_error=RuntimeError("A task is already running; wait for it to finish."))
        service = ConversationService(manager=manager)
        result = service.submit("do something")
        self.assertFalse(result.ok)
        self.assertIn("already running", result.error)

    def test_manager_exception_never_escapes(self):
        manager = FakeManager(raise_error=ValueError("boom"))
        service = ConversationService(manager=manager)
        result = service.submit("do something")
        self.assertIsInstance(result, TurnResult)
        self.assertEqual(result.error, "boom")


class DetectionFailureTests(unittest.TestCase):
    def test_detector_exception_degrades_to_honest_uncertainty(self):
        class ExplodingDetector(LanguageDetectionService):
            def detect(self, *args, **kwargs):
                raise RuntimeError("detector exploded")

        service = ConversationService(
            manager=FakeManager(), detector=ExplodingDetector()
        )
        result = service.submit(TAMIL, source="voice")
        self.assertTrue(result.ok)  # the task still runs
        self.assertTrue(result.detection.uncertain)
        self.assertIsNone(result.language)
        self.assertEqual(len(service.manager.submissions), 1)


class ReplyTests(unittest.TestCase):
    def test_reply_carries_the_shared_language_instruction(self):
        llm = FakeLLM(response="உலாவி திறக்கப்படுகிறது.")
        service = ConversationService(manager=FakeManager(), llm_client=llm,
                                      replies_enabled=True)
        service.submit(TAMIL, source="voice", want_reply=True)
        self.assertEqual(len(llm.calls), 1)
        system = llm.calls[0]["system"]
        self.assertIn(REPLY_LANGUAGE_INSTRUCTION, system)
        self.assertIn("Tamil", system)
        self.assertIn("(ta)", system)
        # The prompt receives the user's words unchanged.
        self.assertEqual(llm.calls[0]["prompt"], TAMIL)

    def test_reply_prompt_is_one_template_not_per_language_assistants(self):
        llm = FakeLLM()
        service = ConversationService(manager=FakeManager(), llm_client=llm,
                                      replies_enabled=True)
        for text, lang in ((TAMIL, "ta"), (ENGLISH, "en")):
            llm.calls.clear()
            service.reply(text, lang)
            base = llm.calls[0]["system"].split(" The detected language")[0]
            self.assertIn(REPLY_LANGUAGE_INSTRUCTION, base)
        # Same base template for both languages.
        llm.calls.clear()
        service.reply(TAMIL, "ta")
        first = llm.calls[0]["system"].split(" The detected language")[0]
        service.reply(ENGLISH, "en")
        second = llm.calls[1]["system"].split(" The detected language")[0]
        self.assertEqual(first, second)

    def test_unknown_language_prompt_makes_no_detection_claim(self):
        llm = FakeLLM()
        service = ConversationService(manager=FakeManager(), llm_client=llm,
                                      replies_enabled=True)
        service.reply("ok", None)
        system = llm.calls[0]["system"]
        self.assertIn(REPLY_LANGUAGE_INSTRUCTION, system)
        self.assertNotIn("The detected language of this turn", system)

    def test_reply_disabled_returns_none_without_calling_the_llm(self):
        llm = FakeLLM()
        service = ConversationService(manager=FakeManager(), llm_client=llm,
                                      replies_enabled=False)
        result = service.submit(ENGLISH, want_reply=True)
        self.assertIsNone(result.reply)
        self.assertEqual(llm.calls, [])

    def test_no_llm_returns_none_even_when_enabled(self):
        service = ConversationService(manager=FakeManager(),
                                      replies_enabled=True)
        self.assertFalse(service.reply_available)
        result = service.submit(ENGLISH, want_reply=True)
        self.assertIsNone(result.reply)
        self.assertTrue(result.ok)  # the task itself is unaffected

    def test_llm_failure_returns_none_and_keeps_the_turn_ok(self):
        llm = FakeLLM(raise_error=RuntimeError("model unavailable"))
        service = ConversationService(manager=FakeManager(), llm_client=llm,
                                      replies_enabled=True)
        result = service.submit(ENGLISH, want_reply=True)
        self.assertTrue(result.ok)
        self.assertIsNone(result.reply)

    def test_non_string_or_empty_llm_output_is_rejected(self):
        for llm in (FakeLLM(non_string=True), FakeLLM(response="   \n ")):
            service = ConversationService(manager=FakeManager(),
                                          llm_client=llm, replies_enabled=True)
            self.assertIsNone(service.reply("hello", "en"))


class ReplyReasonTests(unittest.TestCase):
    """v0.9.1: every reply failure carries an honest, bounded reason."""

    def service(self, *, enabled=True, llm=None):
        return ConversationService(manager=FakeManager(), llm_client=llm,
                                   replies_enabled=enabled)

    def test_success_has_no_reason(self):
        service = self.service(llm=FakeLLM(response="Sure — opening it."))
        reply, reason = service.reply_with_reason("open it", "en")
        self.assertEqual(reply, "Sure — opening it.")
        self.assertIsNone(reason)

    def test_disabled_replies_report_the_setting(self):
        service = self.service(enabled=False, llm=FakeLLM())
        reply, reason = service.reply_with_reason("hi", "en")
        self.assertIsNone(reply)
        self.assertIn("Settings", reason)

    def test_missing_client_points_at_the_flag(self):
        service = self.service(llm=None)
        service.set_replies_enabled(True)
        reply, reason = service.reply_with_reason("hi", "en")
        self.assertIsNone(reply)
        self.assertIn("--llm", reason)

    def test_generation_error_is_named_not_hidden(self):
        service = self.service(
            llm=FakeLLM(raise_error=RuntimeError("connection refused"))
        )
        reply, reason = service.reply_with_reason("hi", "en")
        self.assertIsNone(reply)
        self.assertIn("failed", reason)
        self.assertIn("connection refused", reason)

    def test_empty_and_unusable_responses_are_distinguished(self):
        empty = self.service(llm=FakeLLM(response="  \n "))
        self.assertIn("empty", empty.reply_with_reason("hi", "en")[1])
        junk = self.service(llm=FakeLLM(non_string=True))
        self.assertIn("unusable", junk.reply_with_reason("hi", "en")[1])

    def test_reasons_are_bounded(self):
        huge = "x" * 5000
        service = self.service(
            llm=FakeLLM(raise_error=RuntimeError(huge))
        )
        _reply, reason = service.reply_with_reason("hi", "en")
        self.assertLessEqual(len(reason), 260)

    def test_credentials_never_survive_into_the_reason(self):
        # The configured Ollama URL may carry userinfo; a future client
        # may carry a bearer token. Both must be scrubbed before the
        # reason can reach a bubble or a log.
        cases = [
            ("error at http://user:secret@localhost:11434: refused",
             ["secret", "user:"], ["***@localhost"]),
            ("request failed: Authorization: Bearer abc123.def456",
             ["abc123.def456"], ["[redacted]"]),
            ("rejected: token: sk-1234567890",
             ["sk-1234567890"], ["[redacted]"]),
        ]
        for raw, must_not, must_have in cases:
            service = self.service(
                llm=FakeLLM(raise_error=RuntimeError(raw))
            )
            reply, reason = service.reply_with_reason("hi", "en")
            self.assertIsNone(reply)
            for needle in must_not:
                self.assertNotIn(needle, reason, raw)
            for needle in must_have:
                self.assertIn(needle, reason, raw)

    def test_ordinary_error_text_passes_through_unmangled(self):
        service = self.service(
            llm=FakeLLM(raise_error=RuntimeError("connection refused"))
        )
        _reply, reason = service.reply_with_reason("hi", "en")
        self.assertIn("connection refused", reason)

    def test_reply_text_is_never_submitted_to_the_manager(self):
        manager = FakeManager()
        llm = FakeLLM(response="Sure, opening it now.")
        service = ConversationService(manager=manager, llm_client=llm,
                                      replies_enabled=True)
        result = service.submit(ENGLISH, source="voice", want_reply=True)
        self.assertEqual(result.reply, "Sure, opening it now.")
        # Exactly one submission — the user's words, never the reply.
        self.assertEqual(manager.submissions, [ENGLISH])

    def test_runaway_reply_is_bounded(self):
        llm = FakeLLM(response="word " * 500)
        service = ConversationService(manager=FakeManager(), llm_client=llm,
                                      replies_enabled=True)
        reply = service.reply("hello", "en")
        self.assertLessEqual(len(reply), 600)

    def test_replies_toggle_applies_live(self):
        llm = FakeLLM()
        service = ConversationService(manager=FakeManager(), llm_client=llm,
                                      replies_enabled=True)
        service.set_replies_enabled(False)
        self.assertFalse(service.reply_available)
        service.set_replies_enabled(True)
        self.assertTrue(service.reply_available)
        # LLM removal also applies live.
        service.set_llm_client(None)
        self.assertFalse(service.reply_available)


class ConfigurationTests(unittest.TestCase):
    def test_language_settings_forward_to_the_detector(self):
        service = ConversationService(
            detector=LanguageDetectionService(preferred="en")
        )
        service.configure_language(auto=False, preferred="pt-BR")
        self.assertFalse(service.detector.auto)
        self.assertEqual(service.detector.preferred, "pt")
        # auto off: the preference decides (a deliberate user choice).
        result = service.detect(TAMIL)
        self.assertEqual(result.code, "pt")
        self.assertEqual(result.source, "preferred")

    def test_clear_detection_wins_over_preference(self):
        service = ConversationService(detector=LanguageDetectionService(
            preferred="en"))
        result = service.detect(TAMIL)
        self.assertEqual(result.code, "ta")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
