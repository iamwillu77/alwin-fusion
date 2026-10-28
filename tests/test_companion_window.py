"""v0.6 companion-window tests (headless).

The GTK view itself needs a display, but — like ``taskui.gtk_app`` — it
must *import* and *construct* headless so the whole companion stack can
be tested without a window.  These tests cover the view's wiring that
does not require a running main loop:

* headless import (GTK is guarded, never required),
* construction with the null renderer (no display, no timers),
* controller/adapter/greeting/settings wiring,
* teardown removes observers and timers so the companion can close
  without touching the task engine,
* after teardown, late worker-thread records are ignored,
* CSS palette stays light and self-contained (no remote resources).
"""

from __future__ import annotations

import unittest

from companion.renderer import NullRenderer

try:
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import Gtk
except (ImportError, ValueError):  # pragma: no cover - headless machine
    Gtk = None


class HeadlessImportTests(unittest.TestCase):
    def test_window_module_imports_without_a_display(self):
        import companion.window as window_module

        self.assertTrue(hasattr(window_module, "CompanionWindow"))
        self.assertTrue(hasattr(window_module, "run_companion"))

    def test_constants(self):
        import companion.window as window_module

        self.assertEqual(window_module.APP_ID,
                         "org.alwin.fusion.companion")
        self.assertGreater(window_module.CHAR_WIDTH, 0)
        self.assertGreater(window_module.CHAR_HEIGHT, 0)
        # The idle check must be *low* frequency (no busy polling).
        self.assertGreaterEqual(window_module.IDLE_CHECK_MS, 1000)
        # The frame timer must stay modest on a small machine.
        from companion.renderer2d import FRAME_INTERVAL_MS

        self.assertGreaterEqual(FRAME_INTERVAL_MS, 50)

    def test_css_is_light_and_self_contained(self):
        import companion.window as window_module

        css = window_module.CSS.decode() if isinstance(
            window_module.CSS, bytes) else window_module.CSS
        self.assertNotIn("url(", css, "no external CSS resources")
        self.assertNotIn("http", css, "no remote CSS")
        # Warm, light background — not a black-background-only design.
        self.assertIn("#f4f1ea", css)
        self.assertIn("background", css)


class ConstructionTests(unittest.TestCase):
    def build(self, **kwargs):
        if Gtk is None:  # pragma: no cover - headless machine
            self.skipTest("GTK4 not available")
        from companion.window import CompanionWindow

        try:
            return CompanionWindow(renderer=NullRenderer(), **kwargs)
        except Exception as exc:  # pragma: no cover - environment-specific
            self.skipTest(f"cannot construct GTK objects headless: {exc}")

    def test_construction_needs_no_display_and_shows_nothing(self):
        from companion.voice import NullVoice

        companion = self.build()
        self.assertIsNone(companion.window, "nothing is shown before open()")
        self.assertEqual(companion.controller.state, "idle")
        self.assertIs(companion.adapter.controller, companion.controller)
        self.assertIsInstance(companion.voice, NullVoice)

    def test_own_application_and_honest_defaults(self):
        companion = self.build()
        self.assertTrue(companion._owns_app)
        self.assertEqual(companion.app.get_application_id(),
                         "org.alwin.fusion.companion")
        self.assertFalse(companion._greeted_this_window,
                         "no greeting happens before the window is built")

    def test_observer_is_only_registered_when_the_window_builds(self):
        companion = self.build()
        # open() wires the observer; construction alone subscribes nothing.
        self.assertFalse(getattr(companion, "_closing", False))
        self.assertIsNone(companion.manager)

    def test_late_records_after_teardown_are_ignored(self):
        companion = self.build()

        class _Record:
            state = "completed"
            plan = {"action": "create_report", "summary": "s",
                    "target": "n.odt"}
            steps = []
            task_id = "t"
            verification = None
            report_status = None
            error = None
            result_message = None

        companion._teardown()
        companion._on_record_main(_Record())     # must be a no-op
        self.assertEqual(companion.controller.state, "idle")

    def test_speech_before_build_is_queued_not_crashing(self):
        companion = self.build()
        # _show_bubble is guarded: no widget yet, no exception.
        self.assertFalse(companion._show_bubble("hi", None))


if __name__ == "__main__":
    unittest.main()
