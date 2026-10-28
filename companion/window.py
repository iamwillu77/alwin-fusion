"""Companion window: the GTK4 view for the Alwin character (v0.7.0).

A lightweight, persistent window that shows the character, a speech
bubble, a minimal task status and a small control row.  It is a **thin
view** over the pure modules (states / adapter / presentation), exactly
like ``taskui.gtk_app`` is a thin view over ``taskui.presenter``:

* it never executes anything, never grants a permission, never approves
  a plan — approval stays in the trusted task window,
* all dynamic text (speech, status, settings) is inserted with
  ``set_text``/plain labels — never markup — so untrusted content can
  never impersonate UI chrome,
* task updates arrive from the manager's worker thread and are
  marshalled onto the GTK main loop with ``GLib.idle_add`` before they
  reach the controller/renderer ( GTK objects are main-thread-only ),
* the renderer is a leaf: if the character cannot be drawn (no display,
  missing bindings, an animation bug) the window degrades to a
  text-only companion and the task engine is untouched,
* the frame timer runs only while a state animates (idle/sleeping/
  resting draw once and stop), so an idle companion costs nothing,
* the window does not steal focus after startup and never blocks other
  applications; closing it hides the companion (the task engine, if
  hosted in the same application, keeps running).
"""

from __future__ import annotations

import sys
from typing import Callable, Optional

try:  # pragma: no cover - exercised only with a display
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError):  # pragma: no cover - headless fallback
    Gtk = None  # type: ignore[assignment]
    GLib = None  # type: ignore[assignment]

from companion import settings as settings_store
from companion import presentation
from companion.adapter import TaskEventAdapter
from companion.greeting import GreetingConfig, GreetingController
from companion.renderer import NullRenderer
from companion.states import (
    STATE_CELEBRATING,
    STATE_ERROR,
    STATE_GREETING,
    STATE_IDLE,
    STATE_WAKING_UP,
    CharacterController,
)
from companion.voice import NullVoice, voice_status

APP_ID = "org.alwin.fusion.companion"

#: Character area size (small: a companion, not a game window).
CHAR_WIDTH = 240
CHAR_HEIGHT = 250

#: Idle check cadence (very low frequency; no busy polling).
IDLE_CHECK_MS = 5000

#: Warm, Mac-inspired, original ALWIN palette (no external assets).
CSS = b"""
window { background: #f4f1ea; }
.companion-root { background: linear-gradient(to bottom, #f7f4ee, #efe9dd);
                  border-radius: 18px; }
.bubble { background: #ffffff; border-radius: 14px; padding: 8px 12px;
          color: #2e3440; font-size: 13px; }
.bubble-attention { background: #fff3df; color: #7a4a00; }
.bubble-positive { background: #e4f3e8; color: #17663a; }
.bubble-negative { background: #fbe3e4; color: #a4272d; }
.status-label { color: #5b6472; font-size: 11px; }
.subtitle-label { color: #6b7280; font-size: 11px; font-style: italic; }
.control-btn { padding: 3px 10px; border-radius: 10px; background: #e7e1d5;
               color: #3b4350; font-size: 12px; border: none; }
.control-btn:hover { background: #ddd5c6; }
.char-area { background: transparent; }
.settings-row { font-size: 12px; color: #2e3440; }
"""


def _label(text: str, css: str = "") -> "Gtk.Label":
    """A plain-text label.  Dynamic content is NEVER markup."""
    label = Gtk.Label(label=text or "")
    label.set_xalign(0.0)
    label.set_wrap(True)
    label.set_selectable(False)
    for name in css.split():
        label.add_css_class(name)
    return label


class CompanionWindow:
    """The persistent companion window (character + bubble + controls)."""

    def __init__(
        self,
        manager=None,
        *,
        config=None,
        status=None,
        voice=None,
        application=None,
        open_task_window: Optional[Callable[[], None]] = None,
        greeting_config: Optional[GreetingConfig] = None,
        renderer=None,
    ) -> None:
        """Create (but do not show) the companion.

        ``manager`` (a ``taskui.manager.TaskManager``) is optional: with
        one, the character mirrors real task states through a
        read-only observer; without one, the companion still runs
        (greeting + idle) — useful for the headless smoke and tests.
        ``open_task_window`` is a host callback that opens the trusted
        task interface (the companion never embeds approval itself).
        """
        if Gtk is None:  # pragma: no cover - defensive
            raise RuntimeError(
                "GTK4 (PyGObject with Gtk 4.0) is required for the "
                "companion window."
            )
        self.manager = manager
        self.config = config
        self.status = status
        self.voice = voice or NullVoice()
        self.open_task_window = open_task_window

        # ---- settings (validated, local) -------------------------------- #
        self._settings_path = None
        self._settings = settings_store.defaults()
        if config is not None:
            try:
                self._settings_path = settings_store.path_for(
                    config.state_dir
                )
                self._settings = settings_store.load(self._settings_path)
            except Exception:  # noqa: BLE001 - settings never break startup
                self._settings = settings_store.defaults()

        # ---- character stack -------------------------------------------- #
        # Renderer: the 2D one by default; a null renderer when GTK
        # drawing is unavailable (the window then degrades honestly).
        if renderer is None:
            try:
                from companion.renderer2d import Character2DRenderer

                renderer = Character2DRenderer()
            except Exception:  # noqa: BLE001 - degrade, never crash
                renderer = NullRenderer()
        self.renderer = renderer
        self.controller = CharacterController(renderer)
        self.adapter = TaskEventAdapter(
            self.controller, on_speech=self._queue_speech
        )

        # ---- greeting ---------------------------------------------------- #
        if greeting_config is None:
            greeting_config = GreetingConfig(
                enabled=bool(self._settings.get("greeting_enabled", True)),
                name=str(self._settings.get("greeting_name", "") or ""),
            )
            if config is not None:
                import os

                env_name = os.environ.get("ALWIN_COMPANION_NAME")
                if env_name:
                    greeting_config.name = env_name
        self._greeting = GreetingController(greeting_config)
        self._greeted_this_window = False

        # ---- GTK objects ------------------------------------------------- #
        self.app = application or Gtk.Application(application_id=APP_ID)
        self._owns_app = application is None
        self.window: Optional["Gtk.ApplicationWindow"] = None
        self._auto_return_id: Optional[int] = None
        self._idle_id: Optional[int] = None
        self._greet_id: Optional[int] = None
        self._bubble_text = ""
        self._bubble_tone = presentation.TONE_CALM
        self._closing = False
        # Widgets exist only after _build(); guarded accessors check them.
        self._bubble = None
        self._status_label = None
        self._subtitle_label = None
        self._popover = None
        self._task_btn = None

        if self._owns_app:
            self.app.connect("activate", self._on_activate)

    # ------------------------------------------------------------------ #
    # Window construction
    # ------------------------------------------------------------------ #

    def open(self, app=None) -> None:
        """Build/show the companion window on *app* (or its own app).

        Used when the companion is hosted by another application (e.g.
        ``python3 -m taskui --companion``): the host calls ``open(app)``
        after its own activation.  Calling it twice is harmless — the
        existing window is simply presented again (this also re-shows a
        hidden companion, which is how "show" works after "hide").
        """
        target = app or self.app
        if self.window is None:
            self._build(target)
        self.window.present()

    def run(self) -> int:
        """Run the GTK main loop (own-app mode)."""
        code = self.app.run(None)
        return int(code) if code is not None else 0

    def _on_activate(self, app) -> None:  # pragma: no cover - needs display
        # Guard against re-activation (e.g. a second launch while the
        # GApplication is already running): present the existing window
        # instead of rebuilding a duplicate one.
        if self.window is None:
            self._build(app)
        self.window.present()
        self._sync_animation()

    def _build(self, app) -> None:  # pragma: no cover - needs display
        provider = Gtk.CssProvider()
        provider.load_from_data(CSS)
        display = app.get_default_for_display() if hasattr(
            app, "get_default_for_display"
        ) else None
        if display is not None:
            Gtk.StyleContext.add_provider_for_display(
                display, provider, Gtk.STYLE_PROVIDER_PRIORITY_APPLICATION
            )

        window = Gtk.ApplicationWindow(application=app)
        window.set_title("Alwin")
        window.set_default_size(CHAR_WIDTH + 40, CHAR_HEIGHT + 170)
        window.set_resizable(True)
        window.set_hide_on_close(True)  # closing hides; engine keeps running
        window.connect("close-request", self._on_close_request)
        # Stop the frame timer whenever the window is hidden (minimised,
        # unmapped or closed-to-tray) so nothing redraws off-screen.
        window.connect("map", self._on_map)
        window.connect("unmap", self._on_unmap)
        self.window = window

        root = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        root.set_margin_top(10)
        root.set_margin_bottom(10)
        root.set_margin_start(12)
        root.set_margin_end(12)
        root.add_css_class("companion-root")
        window.set_child(root)

        # ---- speech bubble ---------------------------------------------- #
        self._bubble = _label("", "bubble")
        self._bubble.set_justify(Gtk.Justification.CENTER)
        self._bubble.set_wrap_mode(2)  # word-char
        root.append(self._bubble)

        # ---- character --------------------------------------------------- #
        area = Gtk.DrawingArea()
        area.set_content_width(CHAR_WIDTH)
        area.set_content_height(CHAR_HEIGHT)
        area.add_css_class("char-area")
        try:
            self.renderer.attach(area)
        except Exception:  # noqa: BLE001 - degrade to text-only
            self.renderer = NullRenderer()
            self.controller.renderer = self.renderer
        root.append(area)

        # ---- status + subtitle ------------------------------------------- #
        self._status_label = _label("No task yet", "status-label")
        self._status_label.set_justify(Gtk.Justification.CENTER)
        root.append(self._status_label)
        self._subtitle_label = _label("", "subtitle-label")
        self._subtitle_label.set_justify(Gtk.Justification.CENTER)
        root.append(self._subtitle_label)

        # ---- controls ----------------------------------------------------- #
        controls = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=6)
        controls.set_halign(Gtk.Align.CENTER)
        self._task_btn = Gtk.Button(label="Open task window")
        self._task_btn.add_css_class("control-btn")
        self._task_btn.connect("clicked", self._on_open_task)
        controls.append(self._task_btn)
        self._settings_btn = Gtk.Button(label="Settings")
        self._settings_btn.add_css_class("control-btn")
        self._settings_btn.connect("clicked", self._on_settings)
        controls.append(self._settings_btn)
        hide_btn = Gtk.Button(label="Hide")
        hide_btn.add_css_class("control-btn")
        hide_btn.connect("clicked", self._on_hide)
        controls.append(hide_btn)
        root.append(controls)

        # ---- settings popover -------------------------------------------- #
        self._popover = self._build_settings_popover()

        # ---- wiring ------------------------------------------------------- #
        self.controller.on_change = self._on_state_change
        if self.manager is not None:
            self.manager.add_observer(self._on_record_from_worker)
        try:
            self.renderer.start()
        except Exception:  # noqa: BLE001 - renderer bugs never propagate
            pass

        self._refresh_status()
        self._show_bubble("", presentation.TONE_CALM)
        self._start_idle_check()
        self._maybe_greet()

    # ------------------------------------------------------------------ #
    # Greeting (once per window; honours config/quiet hours)
    # ------------------------------------------------------------------ #

    def _maybe_greet(self) -> None:
        if self._greeted_this_window:
            return  # never re-greet on redraw/re-build
        if not self._greeting.should_greet():
            return
        self._greeted_this_window = True
        self._greeting.mark_greeted()
        text = self._greeting.text()

        # Sequence: wake up (stretch + yawn) -> pause -> greet + wave.
        # The greeting state auto-returns to idle via the existing
        # AUTO_RETURN_DELAYS timer; every timer id is tracked so the
        # window teardown can cancel them.
        def _wake() -> bool:
            self._greet_id = None
            if self._closing:
                return False
            self.controller.force(STATE_WAKING_UP, reason="startup")
            return False

        def _greet() -> bool:
            self._greet_id = None
            if self._closing:
                return False
            self.controller.enter(STATE_GREETING, reason="greeting")
            self._show_bubble(text, presentation.TONE_POSITIVE)
            return False

        def _wake_then_greet() -> bool:
            self._greet_id = None
            if self._closing:
                return False
            self.controller.force(STATE_WAKING_UP, reason="startup")
            self._greet_id = GLib.timeout_add(1300, _greet)
            return False

        self._greet_id = GLib.timeout_add(650, _wake_then_greet)

    # ------------------------------------------------------------------ #
    # Task updates (worker thread -> GTK main loop)
    # ------------------------------------------------------------------ #

    def _on_record_from_worker(self, record) -> None:
        """Manager observer (worker thread): marshal, then map."""
        GLib.idle_add(self._on_record_main, record)

    def _on_record_main(self, record) -> bool:
        if self._closing:
            return False
        # The user is interacting with a task: wake a resting character.
        if self.controller.state in ("sleeping", "resting"):
            self.controller.force(STATE_IDLE, reason="task activity")
        self.adapter.on_task_update(record)
        self._refresh_status(record)
        return False

    def _queue_speech(self, text: str) -> None:
        """Adapter speech (may be any thread): marshal to the UI."""
        GLib.idle_add(self._show_bubble, text, None)

    # ------------------------------------------------------------------ #
    # Rendering hooks
    # ------------------------------------------------------------------ #

    def _on_state_change(self, state: str, expression: str, reason: str) -> None:
        # Controller callbacks run on the thread that entered the state
        # (the GTK main loop, via the marshalled observer).  Schedule
        # the auto-return for transient outcome states.
        self._schedule_auto_return(state)
        if self._subtitle_label is not None:
            self._subtitle_label.set_text(
                presentation.subtitle_view(self.controller.snapshot())
            )

    def _schedule_auto_return(self, state: str) -> None:
        if self._auto_return_id is not None:
            GLib.source_remove(self._auto_return_id)
            self._auto_return_id = None
        delay = self.controller.auto_return_delay()
        if delay is None or state not in (STATE_CELEBRATING, STATE_ERROR,
                                          STATE_GREETING, STATE_WAKING_UP):
            return

        def _back() -> bool:
            self._auto_return_id = None
            if self.controller.state == state:  # only if still there
                self.controller.enter(STATE_IDLE, reason="calm return")
                if state in (STATE_CELEBRATING, STATE_ERROR):
                    self._show_bubble("", presentation.TONE_CALM)
            return False

        self._auto_return_id = GLib.timeout_add(int(delay * 1000), _back)

    def _show_bubble(self, text: str, tone: Optional[str]) -> bool:
        view = presentation.bubble_view(
            self.controller.state, text, tone=tone
        )
        self._bubble_text = view["text"]
        self._bubble_tone = view["tone"]
        if self._bubble is not None:
            self._bubble.set_text(self._bubble_text)
            for css in ("bubble", "bubble-attention", "bubble-positive",
                        "bubble-negative"):
                self._bubble.remove_css_class(css)
            self._bubble.add_css_class("bubble")
            tone_class = {
                presentation.TONE_ATTENTION: "bubble-attention",
                presentation.TONE_POSITIVE: "bubble-positive",
                presentation.TONE_NEGATIVE: "bubble-negative",
            }.get(self._bubble_tone)
            if tone_class:
                self._bubble.add_css_class(tone_class)
            self._bubble.set_visible(bool(self._bubble_text))
        return False

    def _refresh_status(self, record=None) -> None:
        if self._status_label is None:
            return
        if record is None and self.manager is not None:
            record = self.manager.current
        view = presentation.task_status_view(record)
        parts = [view["label"]]
        if view["verification"]:
            parts.append(view["verification"])
        self._status_label.set_text("  ·  ".join(p for p in parts if p))

    # ------------------------------------------------------------------ #
    # Idle -> resting (very low frequency)
    # ------------------------------------------------------------------ #

    def _start_idle_check(self) -> None:
        if self._idle_id is not None:
            return

        def _check() -> bool:
            if self.controller.state == STATE_IDLE:
                self.adapter.on_idle(self.controller.seconds_in_state())
            return True

        self._idle_id = GLib.timeout_add(IDLE_CHECK_MS, _check)

    # ------------------------------------------------------------------ #
    # Controls
    # ------------------------------------------------------------------ #

    def _on_open_task(self, *_args) -> None:  # pragma: no cover - display
        if self.open_task_window is not None:
            try:
                self.open_task_window()
            except Exception:  # noqa: BLE001 - a host bug shows, not crashes
                self._show_bubble(
                    "I couldn't open the task window just now.",
                    presentation.TONE_NEGATIVE,
                )

    def _on_hide(self, *_args) -> None:  # pragma: no cover - display
        if self.window is not None:
            self.window.hide()

    # ------------------------------------------------------------------ #
    # Animation sync with window visibility (no redraws while hidden)
    # ------------------------------------------------------------------ #

    def _on_map(self, *_args) -> None:  # pragma: no cover - display
        self._sync_animation()

    def _on_unmap(self, *_args) -> None:  # pragma: no cover - display
        # Hidden: pause the frame timer so nothing animates off-screen.
        try:
            self.renderer.stop()
        except Exception:  # noqa: BLE001 - renderer bugs never propagate
            pass

    def _sync_animation(self) -> None:
        """Start/stop the renderer timer to match the window's visibility."""
        visible = self.window is not None and self.window.get_mapped()
        try:
            if visible:
                self.renderer.start()
            else:
                self.renderer.stop()
        except Exception:  # noqa: BLE001 - renderer bugs never propagate
            pass

    def _on_settings(self, *_args) -> None:  # pragma: no cover - display
        if self._popover is not None:
            self._popover.popup()

    def _build_settings_popover(self):  # pragma: no cover - needs display
        popover = Gtk.Popover()
        box = Gtk.Box(orientation=Gtk.Orientation.VERTICAL, spacing=6)
        box.set_margin_top(8)
        box.set_margin_bottom(8)
        box.set_margin_start(10)
        box.set_margin_end(10)
        popover.set_child(box)

        def _switch(key: str, label: str):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("settings-row")
            row.append(_label(label))
            sw = Gtk.Switch()
            sw.active = bool(self._settings.get(key, True))
            sw.set_halign(Gtk.Align.END)
            sw.set_hexpand(True)

            def _toggled(_widget, _pspec, k=key):
                self._settings[k] = sw.active
                self._apply_settings()

            sw.connect("notify::active", _toggled)
            row.append(sw)
            box.append(row)

        _switch("greeting_enabled", "Startup greeting")
        _switch("show_subtitles", "Show subtitles")
        _switch("always_on_top", "Always on top (best effort)")

        mute_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=8)
        mute_row.add_css_class("settings-row")
        info = voice_status(self.voice)
        mute_row.append(_label(f"{info['label']} — placeholder"))
        box.append(mute_row)

        note = _label("Voice is a placeholder until a local speech "
                      "engine is connected. Nothing is spoken now.",
                      "subtitle-label")
        note.set_wrap(True)
        box.append(note)
        return popover

    def _apply_settings(self) -> None:
        # Greeting toggle affects the *next* launch; the current
        # window has already greeted (or not) for this session.
        if self._settings_path is not None:
            try:
                settings_store.save(self._settings_path, self._settings)
            except settings_store.SettingsError:
                pass  # surfaced by the store; never crash the window
        if self._subtitle_label is not None:
            self._subtitle_label.set_visible(
                bool(self._settings.get("show_subtitles", True))
            )

    # ------------------------------------------------------------------ #
    # Lifecycle
    # ------------------------------------------------------------------ #

    def _on_close_request(self, *_args) -> bool:  # pragma: no cover
        # Hide instead of destroying: the companion is persistent and
        # the task engine (if hosted here) keeps running.
        if self._owns_app:
            # Own-app mode: closing the last window quits the app.
            self._teardown()
            return False
        self.window.hide()
        return True  # stop the default destroy

    def _teardown(self) -> None:
        self._closing = True
        if self._auto_return_id is not None:
            GLib.source_remove(self._auto_return_id)
            self._auto_return_id = None
        if self._idle_id is not None:
            GLib.source_remove(self._idle_id)
            self._idle_id = None
        if self._greet_id is not None:
            GLib.source_remove(self._greet_id)
            self._greet_id = None
        if self.manager is not None:
            try:
                self.manager.remove_observer(self._on_record_from_worker)
            except Exception:  # noqa: BLE001 - teardown never raises
                pass
        try:
            self.renderer.stop()
        except Exception:  # noqa: BLE001
            pass
        try:
            self.renderer.detach()
        except Exception:  # noqa: BLE001
            pass


def run_companion(manager=None, *, config=None, status=None,
                  open_task_window: Optional[Callable[[], None]] = None,
                  voice=None) -> int:  # pragma: no cover - needs display
    """Create the companion window and run the GTK main loop."""
    companion = CompanionWindow(
        manager, config=config, status=status, voice=voice,
        open_task_window=open_task_window,
    )
    return companion.run()


__all__ = [
    "CHAR_HEIGHT",
    "CHAR_WIDTH",
    "CompanionWindow",
    "run_companion",
]
