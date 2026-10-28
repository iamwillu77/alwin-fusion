"""v0.6 startup-greeting tests (headless, injected clock, temp files).

The greeting foundation must be configurable and honest:

* the name is configurable (env/settings) and **never hard-coded**,
* the greeting fires once per session — never again on a redraw,
* an optional once-per-day marker prevents repeats across sessions,
* quiet hours and a disabled/muted preference suppress it,
* malformed configuration degrades honestly (explicit error or
  documented default), never by guessing,
* the greeting is text: it never claims to speak or record audio.
"""

from __future__ import annotations

import tempfile
import unittest
from datetime import datetime, time as dtime
from pathlib import Path

from companion.greeting import (
    DEFAULT_NAME,
    DEFAULT_TEMPLATE,
    GreetingConfig,
    GreetingController,
    MAX_GREETING_CHARS,
    MAX_NAME_CHARS,
    NAME_ENV,
    greeting_text,
    in_quiet_hours,
    parse_quiet_hours,
    part_of_day,
    sanitize_name,
)
from companion.voice import NullVoice


def fixed_clock(year, month, day, hour, minute):
    return lambda: datetime(year, month, day, hour, minute)


class NameTests(unittest.TestCase):
    def test_default_name_is_generic_not_a_specific_user(self):
        self.assertEqual(DEFAULT_NAME, "friend")
        self.assertEqual(GreetingConfig().name, DEFAULT_NAME)

    def test_name_normalization(self):
        self.assertEqual(sanitize_name("  Wilson   Alwin "), "Wilson Alwin")
        self.assertEqual(sanitize_name("\t\n"), DEFAULT_NAME)
        self.assertEqual(sanitize_name(None), DEFAULT_NAME)
        self.assertEqual(sanitize_name(123), DEFAULT_NAME)
        self.assertEqual(sanitize_name(""), DEFAULT_NAME)

    def test_name_is_bounded(self):
        long_name = "x" * 500
        self.assertLessEqual(len(sanitize_name(long_name)), MAX_NAME_CHARS)

    def test_name_strips_control_characters(self):
        cleaned = sanitize_name("W\x00i\x1flson")
        self.assertTrue(cleaned.isprintable())
        self.assertNotIn("\x00", cleaned)

    def test_env_variable_name_is_used(self):
        config = GreetingConfig.from_env({NAME_ENV: "Ada"})
        self.assertEqual(config.name, "Ada")

    def test_no_env_falls_back_to_generic_name(self):
        config = GreetingConfig.from_env({})
        self.assertEqual(config.name, DEFAULT_NAME)


class TextTests(unittest.TestCase):
    def test_part_of_day(self):
        self.assertEqual(part_of_day(dtime(7, 30)), "Good morning!")
        self.assertEqual(part_of_day(dtime(11, 59)), "Good morning!")
        self.assertEqual(part_of_day(dtime(12, 0)), "Good afternoon!")
        self.assertEqual(part_of_day(dtime(17, 59)), "Good afternoon!")
        self.assertEqual(part_of_day(dtime(18, 0)), "Good evening!")
        self.assertEqual(part_of_day(dtime(23, 1)), "Good evening!")
        self.assertEqual(part_of_day(dtime(2, 1)), "Good evening!")

    def test_greeting_contains_name_and_part(self):
        text = greeting_text("Wilson", dtime(8, 0))
        self.assertIn("Wilson", text)
        self.assertIn("Good morning!", text)
        self.assertIn("What shall we do today?", text)

    def test_greeting_is_bounded(self):
        text = greeting_text("n" * 500, dtime(8, 0))
        self.assertLessEqual(len(text), MAX_GREETING_CHARS)

    def test_greeting_template_is_the_configurable_one(self):
        self.assertIn("{name}", DEFAULT_TEMPLATE)
        self.assertIn("{part}", DEFAULT_TEMPLATE)


class QuietHoursTests(unittest.TestCase):
    def test_parse_valid_range(self):
        quiet = parse_quiet_hours("22:30-07:15")
        self.assertEqual(quiet, (dtime(22, 30), dtime(7, 15)))

    def test_parse_empty_means_disabled(self):
        self.assertIsNone(parse_quiet_hours(""))
        self.assertIsNone(parse_quiet_hours("   "))

    def test_parse_malformed_raises(self):
        for spec in ("nonsense", "25:00-07:00", "10:00", "10:00-10:99",
                     "a-b"):
            with self.subTest(spec=spec):
                with self.assertRaises(ValueError):
                    parse_quiet_hours(spec)

    def test_inside_and_outside(self):
        quiet = parse_quiet_hours("23:00-07:00")
        self.assertTrue(in_quiet_hours(dtime(23, 30), quiet))
        self.assertTrue(in_quiet_hours(dtime(3, 0), quiet))
        self.assertTrue(in_quiet_hours(dtime(6, 59), quiet))
        self.assertFalse(in_quiet_hours(dtime(7, 0), quiet))
        self.assertFalse(in_quiet_hours(dtime(12, 0), quiet))

    def test_no_quiet_hours_is_never_quiet(self):
        self.assertFalse(in_quiet_hours(dtime(3, 0), None))

    def test_degenerate_window_is_never_quiet(self):
        self.assertFalse(in_quiet_hours(dtime(8, 0),
                                        (dtime(8, 0), dtime(8, 0))))


class ConfigTests(unittest.TestCase):
    def test_greeting_enabled_env_toggle(self):
        for value, expected in (("1", True), ("true", True), ("yes", True),
                                ("0", False), ("false", False),
                                ("off", False), ("no", False)):
            with self.subTest(value=value):
                config = GreetingConfig.from_env(
                    {"ALWIN_COMPANION_GREETING": value}
                )
                self.assertEqual(config.enabled, expected)

    def test_no_env_defaults_to_enabled(self):
        self.assertTrue(GreetingConfig.from_env({}).enabled)

    def test_quiet_hours_env_parsing(self):
        config = GreetingConfig.from_env(
            {"ALWIN_COMPANION_QUIET_HOURS": "22:00-07:00"}
        )
        self.assertEqual(config.quiet_hours, (dtime(22, 0), dtime(7, 0)))

    def test_malformed_quiet_hours_env_degrades_to_no_quiet_hours(self):
        config = GreetingConfig.from_env(
            {"ALWIN_COMPANION_QUIET_HOURS": "banana"}
        )
        self.assertIsNone(config.quiet_hours)
        self.assertTrue(config.enabled)  # greeting still works normally


class ControllerTests(unittest.TestCase):
    def make(self, *, env=None, tmp=None, now=None, **overrides):
        config = GreetingConfig.from_env(env or {}, **overrides)
        if tmp is not None:
            config.marker_path = Path(tmp) / "greeted.json"
        return GreetingController(config, now_fn=now or fixed_clock(2026, 1, 1, 8, 0))

    def test_first_check_greets(self):
        with tempfile.TemporaryDirectory() as tmp:
            greeting = self.make(tmp=tmp)
            self.assertTrue(greeting.should_greet())
            self.assertEqual(greeting.last_decision, "will greet")

    def test_session_dedup_no_re_greet_on_redraw(self):
        with tempfile.TemporaryDirectory() as tmp:
            greeting = self.make(tmp=tmp)
            self.assertTrue(greeting.should_greet())
            greeting.mark_greeted()
            for _ in range(5):  # every redraw asks again
                self.assertFalse(greeting.should_greet())
            self.assertEqual(greeting.last_decision,
                             "already greeted this session")

    def test_once_per_day_marker_prevents_second_session(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make(tmp=tmp)
            self.assertTrue(first.should_greet())
            first.mark_greeted()

            # A brand-new session (same day) reads the marker.
            second = self.make(tmp=tmp)
            self.assertFalse(second.should_greet())
            self.assertEqual(second.last_decision, "already greeted today")

            # A new day: the marker no longer applies.
            third = self.make(tmp=tmp, now=fixed_clock(2026, 1, 2, 8, 0))
            self.assertTrue(third.should_greet())

    def test_once_per_day_can_be_disabled(self):
        with tempfile.TemporaryDirectory() as tmp:
            first = self.make(tmp=tmp, once_per_day=False)
            first.mark_greeted()
            second = self.make(tmp=tmp, once_per_day=False)
            self.assertTrue(second.should_greet())

    def test_marker_is_owner_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            greeting = self.make(tmp=tmp)
            greeting.mark_greeted()
            marker = greeting.config.marker_path
            self.assertTrue(marker.is_file())
            self.assertEqual(oct(marker.stat().st_mode & 0o777), "0o600")

    def test_unwritable_marker_never_raises(self):
        greeting = GreetingController(
            GreetingConfig(marker_path=Path("/proc/definitely/not/writable"))
        )
        greeting.mark_greeted()   # best-effort: must not raise
        self.assertTrue(greeting._session_greeted)

    def test_disabled_preference_suppresses(self):
        greeting = self.make(env={"ALWIN_COMPANION_GREETING": "off"})
        self.assertFalse(greeting.should_greet())
        self.assertEqual(greeting.last_decision, "disabled by configuration")

    def test_quiet_hours_suppresses(self):
        greeting = self.make(
            env={"ALWIN_COMPANION_QUIET_HOURS": "23:00-07:00"},
            now=fixed_clock(2026, 1, 1, 2, 30),
        )
        self.assertFalse(greeting.should_greet())
        self.assertEqual(greeting.last_decision, "inside quiet hours")

    def test_outside_quiet_hours_greets(self):
        greeting = self.make(
            env={"ALWIN_COMPANION_QUIET_HOURS": "23:00-07:00"},
            now=fixed_clock(2026, 1, 1, 9, 0),
        )
        self.assertTrue(greeting.should_greet())

    def test_text_is_always_safe_to_show(self):
        greeting = self.make(env={"ALWIN_COMPANION_NAME": "Wilson"})
        text = greeting.text()
        self.assertIn("Wilson", text)
        self.assertLessEqual(len(text), MAX_GREETING_CHARS)
        self.assertTrue(text.isprintable())

    def test_text_uses_current_part_of_day(self):
        evening = self.make(now=fixed_clock(2026, 1, 1, 21, 0))
        self.assertIn("Good evening!", evening.text())

    def test_no_mic_no_speech_claim_anywhere_in_the_flow(self):
        # The greeting is text; the voice engine honestly reports off.
        voice = NullVoice()
        self.assertFalse(voice.available)
        self.assertFalse(voice.speak("Good morning!"))


if __name__ == "__main__":
    unittest.main()
