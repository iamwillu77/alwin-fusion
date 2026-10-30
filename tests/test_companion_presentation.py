"""v0.6 companion presentation tests: bubbles, status, controls, settings, voice.

The presentation layer is the pure view-model behind the companion
window (no GTK, no threads, no I/O), tested headless like the task
presenter.  Trust rules pinned here:

* every rendered string is bounded (untrusted text can never flood or
  impersonate UI chrome) and the view interprets nothing as markup,
* the minimal task status shows only curated fields — never file
  content, never raw error internals, never a plan digest,
* the companion exposes **no approve/reject affordance** (approval
  lives only in the trusted task window),
* voice is honestly reported as unavailable; nothing pretends to speak,
* settings are a closed, sanitized, cosmetic-only schema.
"""

from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from companion import settings as settings_store
from companion import presentation
from companion.voice import NullVoice, VoiceEngine, voice_status


class FakeRecord:
    def __init__(self, state="completed", *, plan=None, verification=None,
                 state_label=None):
        self.state = state
        self.plan = plan or {"summary": "Create a folder named Demo",
                             "action": "create_directory"}
        self.verification = verification
        self.state_label = state_label or state.replace("_", " ")
        self.steps = []
        self.task_id = "p-1"
        self.report_status = None
        self.error = None
        self.result_message = None


class BubbleTests(unittest.TestCase):
    def test_empty_speech_hides_the_bubble(self):
        view = presentation.bubble_view("idle", "")
        self.assertEqual(view["text"], "")
        self.assertFalse(view["visible"])

    def test_speech_shows_the_bubble(self):
        view = presentation.bubble_view("thinking", "Let me think…")
        self.assertTrue(view["visible"])
        self.assertEqual(view["text"], "Let me think…")

    def test_long_speech_is_bounded(self):
        view = presentation.bubble_view("idle", "x" * 10_000)
        self.assertLessEqual(len(view["text"]),
                             presentation.MAX_BUBBLE_CHARS)
        self.assertTrue(view["text"].endswith("…"))

    def test_whitespace_and_newlines_are_normalized(self):
        view = presentation.bubble_view("idle", " line\none \t two ")
        self.assertEqual(view["text"], "line one two")

    def test_non_string_speech_is_dropped(self):
        for bad in (None, 42, ["a"], {"t": "x"}):
            with self.subTest(bad=bad):
                view = presentation.bubble_view("idle", bad)
                self.assertEqual(view["text"], "")
                self.assertFalse(view["visible"])

    def test_markup_in_speech_stays_plain_text(self):
        view = presentation.bubble_view("idle", "<b>bold?</b> & <script>")
        self.assertEqual(view["text"], "<b>bold?</b> & <script>")

    def test_tones_follow_the_state(self):
        self.assertEqual(presentation.bubble_view("waiting_for_approval", "x")["tone"],
                         presentation.TONE_ATTENTION)
        self.assertEqual(presentation.bubble_view("celebrating", "x")["tone"],
                         presentation.TONE_POSITIVE)
        self.assertEqual(presentation.bubble_view("error", "x")["tone"],
                         presentation.TONE_NEGATIVE)
        self.assertEqual(presentation.bubble_view("idle", "x")["tone"],
                         presentation.TONE_CALM)

    def test_explicit_tone_overrides_the_state_default(self):
        view = presentation.bubble_view("idle", "x",
                                        tone=presentation.TONE_ATTENTION)
        self.assertEqual(view["tone"], presentation.TONE_ATTENTION)


class SubtitleTests(unittest.TestCase):
    def test_subtitle_is_an_accessible_text_alternative(self):
        controller_snapshot = {
            "state": "waiting_for_approval",
            "state_label": "waiting for your approval",
            "reason": "plan needs approval",
            "expression": "hopeful",
            "animated": True,
            "auto_return_delay": None,
            "renderer_errors": [],
        }
        text = presentation.subtitle_view(controller_snapshot)
        self.assertIn("Alwin", text)
        self.assertIn("waiting for your approval", text)

    def test_subtitle_is_bounded(self):
        text = presentation.subtitle_view(
            {"state_label": "z" * 500, "reason": ""}
        )
        self.assertLessEqual(len(text), presentation.MAX_SUBTITLE_CHARS)

    def test_subtitle_survives_a_missing_snapshot(self):
        text = presentation.subtitle_view({})
        self.assertIsInstance(text, str)


class TaskStatusTests(unittest.TestCase):
    def test_no_task_is_honest(self):
        view = presentation.task_status_view(None)
        self.assertFalse(view["present"])
        self.assertEqual(view["label"], "No task yet")
        self.assertEqual(view["summary"], "")

    def test_state_and_summary_shown(self):
        view = presentation.task_status_view(FakeRecord())
        self.assertTrue(view["present"])
        self.assertEqual(view["label"], "completed")
        self.assertIn("folder", view["summary"])

    def test_verification_verdict_shown(self):
        verified = presentation.task_status_view(
            FakeRecord(verification={"status": "passed"})
        )
        self.assertEqual(verified["verification"], "Verified")
        self.assertEqual(verified["tone"], presentation.TONE_POSITIVE)

        unverified = presentation.task_status_view(
            FakeRecord(verification={"status": "failed"})
        )
        self.assertEqual(unverified["verification"], "Not verified")
        self.assertEqual(unverified["tone"], presentation.TONE_NEGATIVE)

        pending = presentation.task_status_view(FakeRecord())
        self.assertEqual(pending["verification"], "")
        self.assertEqual(pending["tone"], presentation.TONE_CALM)

    def test_status_never_exposes_digest_content_or_error_text(self):
        record = FakeRecord(
            plan={"summary": "Create a folder named Demo",
                  "action": "create_directory",
                  "digest": "sha256:abcdef0123456789"}
        )
        record.error = "Traceback: secret-token-12345 /home/user/.ssh/id"
        record.result_message = "FULL_DOCUMENT_BODY"
        view = presentation.task_status_view(record)
        rendered = " ".join(str(v) for v in view.values())
        self.assertNotIn("sha256", rendered)
        self.assertNotIn("secret-token", rendered)
        self.assertNotIn("FULL_DOCUMENT_BODY", rendered)
        self.assertNotIn(".ssh", rendered)

    def test_summary_is_bounded(self):
        record = FakeRecord(plan={"summary": "s" * 5000})
        view = presentation.task_status_view(record)
        self.assertLessEqual(len(view["summary"]),
                             presentation.MAX_STATUS_CHARS)

    def test_verification_as_non_dict_is_tolerated(self):
        record = FakeRecord(verification="passed")
        view = presentation.task_status_view(record)
        self.assertEqual(view["verification"], "")


class ControlsTests(unittest.TestCase):
    def test_required_controls_exist(self):
        controls = presentation.controls_view()
        ids = {c["id"] for c in controls["controls"]}
        self.assertEqual(ids, {"open_task_window", "hide", "settings"})

    def test_no_approval_affordance_in_the_companion(self):
        controls = presentation.controls_view()
        ids = {c["id"] for c in controls["controls"]}
        for forbidden in ("approve", "reject", "confirm", "grant",
                          "run", "execute"):
            self.assertNotIn(forbidden, ids)

    def test_every_control_has_a_label_and_tooltip(self):
        for control in presentation.controls_view()["controls"]:
            with self.subTest(control=control["id"]):
                self.assertTrue(control["label"].strip())
                self.assertTrue(control["tooltip"].strip())

    def test_voice_status_is_honestly_unavailable(self):
        controls = presentation.controls_view(voice=NullVoice())
        self.assertFalse(controls["voice"]["available"])
        self.assertEqual(controls["voice"]["label"], "Voice off")
        self.assertIn("no speech engine", controls["voice"]["detail"])

    def test_always_on_top_defaults_to_off(self):
        controls = presentation.controls_view()
        self.assertFalse(controls["always_on_top"])
        self.assertTrue(presentation.controls_view(
            always_on_top=True)["always_on_top"])


class SettingsTests(unittest.TestCase):
    def test_schema_is_closed_and_cosmetic_only(self):
        self.assertEqual(
            set(settings_store.SCHEMA),
            {"greeting_enabled", "greeting_name", "quiet_hours",
             "always_on_top", "show_subtitles", "reduced_motion",
             # v0.9 voice & language
             "mic_enabled", "mic_level_meter", "language_auto",
             "language_preferred", "spoken_responses", "speech_rate",
             "voice_id", "asr_model", "wake_word_enabled",
             # v0.10 wake word
             "wake_phrase",
             # v0.11 conversation context
             "context_enabled", "context_turns",
             # v0.15 persistent memory (cosmetic switch + explicit
             # scope label — a tampered file can at worst point
             # retrieval at the wrong label; it can never grant a
             # permission or enable execution, see SECURITY.md)
             "memory_enabled", "memory_project"},
        )
        allowed_prefixes = ("greeting", "quiet", "always", "show_subtitles",
                            "reduced", "mic", "language", "spoken",
                            "speech", "voice", "asr", "wake", "context",
                            "memory")
        for key in settings_store.SCHEMA:
            self.assertTrue(key.startswith(allowed_prefixes),
                            f"{key} is not a cosmetic preference")
            # bool/str only: no numbers, no nested objects — a tampered
            # file can never smuggle anything structured in.
            self.assertIn(settings_store.SCHEMA[key][0], (bool, str), key)

    def test_defaults(self):
        defaults = settings_store.defaults()
        self.assertTrue(defaults["greeting_enabled"])
        self.assertFalse(defaults["always_on_top"])
        self.assertTrue(defaults["show_subtitles"])
        self.assertFalse(defaults["reduced_motion"])

    def test_unknown_keys_are_dropped(self):
        clean = settings_store.sanitize({"execute_without_approval": True})
        self.assertNotIn("execute_without_approval", clean)

    def test_type_mismatches_fall_back_to_defaults(self):
        clean = settings_store.sanitize({"greeting_enabled": ["not", "bool"],
                                         "greeting_name": 123,
                                         "show_subtitles": {"a": 1}})
        self.assertIs(clean["greeting_enabled"], True)   # default preserved
        self.assertEqual(clean["greeting_name"], "")     # default preserved
        self.assertIs(clean["show_subtitles"], True)     # default preserved

    def test_bool_values_are_coerced_not_trusted(self):
        # Strings and numbers are accepted as booleans; anything else
        # is rejected above.  A tampered file can at worst change a
        # cosmetic preference.
        self.assertIs(settings_store.sanitize(
            {"greeting_enabled": "no"})["greeting_enabled"], False)
        self.assertIs(settings_store.sanitize(
            {"greeting_enabled": "true"})["greeting_enabled"], True)
        self.assertIs(settings_store.sanitize(
            {"always_on_top": 7})["always_on_top"], True)

    def test_roundtrip_and_permissions(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = settings_store.path_for(Path(tmp))
            self.assertEqual(path.name, "companion_settings.json")
            settings_store.save(path, {"greeting_enabled": False,
                                       "show_subtitles": False})
            self.assertEqual(oct(path.stat().st_mode & 0o777), "0o600")
            loaded = settings_store.load(path)
            self.assertIs(loaded["greeting_enabled"], False)
            self.assertIs(loaded["show_subtitles"], False)

    def test_missing_file_yields_defaults(self):
        loaded = settings_store.load(Path("/not/here/settings.json"))
        self.assertEqual(loaded, settings_store.defaults())

    def test_corrupt_file_yields_defaults(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = settings_store.path_for(Path(tmp))
            path.write_text("{{{ not json", encoding="utf-8")
            self.assertEqual(settings_store.load(path),
                             settings_store.defaults())

    def test_settings_never_influence_execution(self):
        # The trusted core never reads this file; the companion never
        # imports the execution engine (see test_companion_security).
        import ast
        import pathlib

        source = (pathlib.Path(__file__).resolve().parent.parent
                  / "companion" / "settings.py").read_text(encoding="utf-8")
        imports = set()
        for node in ast.walk(ast.parse(source)):
            if isinstance(node, ast.Import):
                imports.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imports.add(node.module.split(".")[0])
        self.assertLessEqual(imports,
                             {"__future__", "json", "os", "pathlib", "typing"})


class VoiceTests(unittest.TestCase):
    def test_null_voice_is_honest(self):
        voice = NullVoice()
        self.assertFalse(voice.available)
        self.assertFalse(voice.speak("Hi"))
        self.assertEqual(voice.stop(), None)
        self.assertIsInstance(voice, VoiceEngine)

    def test_voice_status_with_none_engine(self):
        view = voice_status(None)
        self.assertFalse(view["available"])
        self.assertEqual(view["label"], "Voice off")

    def test_voice_status_for_a_future_engine(self):
        class FutureVoice(VoiceEngine):
            name = "local-tts"

            @property
            def available(self):
                return True

            @property
            def status(self):
                return "engine connected"

            def speak(self, text):
                return True

            def stop(self):
                return None

        view = voice_status(FutureVoice())
        self.assertTrue(view["available"])
        self.assertIn("local-tts", view["label"])

    def test_voice_status_is_bounded(self):
        class LongVoice(VoiceEngine):
            name = "x" * 300

            @property
            def available(self):
                return True

            @property
            def status(self):
                return "s" * 1000

            def speak(self, text):
                return False

            def stop(self):
                return None

        view = voice_status(LongVoice())
        self.assertLessEqual(len(view["detail"]), 200)


class SettingsViewTests(unittest.TestCase):
    def test_view_bounds_and_types(self):
        view = presentation.settings_view(
            {"greeting_name": "n" * 200, "quiet_hours": "q" * 100,
             "greeting_enabled": 1, "unknown_key": "x"}
        )
        self.assertLessEqual(len(view["greeting_name"]), 40)
        self.assertLessEqual(len(view["quiet_hours"]), 20)
        self.assertIs(view["greeting_enabled"], True)
        self.assertNotIn("unknown_key", view)

    def test_view_with_no_settings(self):
        view = presentation.settings_view({})
        self.assertIs(view["greeting_enabled"], True)
        self.assertEqual(view["greeting_name"], "")


if __name__ == "__main__":
    unittest.main()
