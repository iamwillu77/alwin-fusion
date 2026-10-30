"""Companion window: the GTK4 view for the Alwin character (v0.13.0).

A lightweight, persistent window that shows the character, a speech
bubble, a minimal task status and a small control row.  It is a **thin
view** over the pure modules (states / adapter / presentation / voice
services), exactly like ``taskui.gtk_app`` is a thin view over
``taskui.presenter``:

* it never executes anything, never grants a permission, never approves
  a plan — approval stays in the trusted task window,
* all dynamic text (speech, status, settings, recognized transcripts)
  is inserted with ``set_text``/plain labels — never markup — so
  untrusted content can never impersonate UI chrome,
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

v0.9 voice workflow (push-to-talk — one mic button cycles the turn):

1. **Mic** (only from idle/greeting, and only when ``mic_enabled``)
   opens the microphone through :class:`companion.voice_input.
   VoiceInputService` and shows ``listening`` with a live level meter
   (100 ms timer, only while the window is up and the mic is open),
2. pressing **Mic** again stops the capture: silence is refused with an
   honest message *before* any model runs, otherwise the state moves to
   ``thinking`` and recognition runs on the ``alwin-asr`` worker — the
   GTK main loop never blocks on it,
3. pressing **Mic** during recognition (or while a reply is being
   generated) **cancels the turn**: the in-flight result is discarded
   by generation counter and can never be delivered late,
4. the recognized transcript is shown as plain text and handed to
   :class:`companion.conversation.ConversationService` — the *same*
   validate → detect → ``manager.submit()`` path typed input uses
   (same allowlist, same permissions, same approval; transcripts are
   untrusted text, nothing more),
5. uncertain language detection produces an honest "I couldn't tell"
   bubble (the user is asked — detection never guesses in the UI),
6. with **spoken replies** enabled, the optional LLM acknowledgement is
   generated on the ``alwin-reply`` worker and spoken through the
   ``alwin-tts`` worker: the character enters ``speaking`` (the v0.9
   state) and returns to idle when the voice engine reports completion
   truth — success, failure or user-cancel alike,
7. a failed/absent engine (no model, no microphone, no espeak, no audio
   device) always degrades to a bubble explaining what is missing —
   the window never fakes speech, capture or recognition.
"""

from __future__ import annotations

import sys
import threading
import time
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
from ai_core.command_parser import EXIT, UNSUPPORTED, parse_command
from companion.conversation import ConversationService
from companion.greeting import GreetingConfig, GreetingController
from companion.language_registry import LANGUAGES, choices_for_ui
from companion.renderer import NullRenderer
from companion.scene import SceneController
from companion.speech_recognition import MODEL_SIZES, SpeechRecognitionService
from companion.states import (
    STATE_CELEBRATING,
    STATE_CODING,
    STATE_ERROR,
    STATE_GREETING,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_RESTING,
    STATE_SPEAKING,
    STATE_THINKING,
    STATE_WAITING_FOR_APPROVAL,
    STATE_WAKING_UP,
    STATE_WALKING,
    STATE_WRITING,
    STATE_SLEEPING,
    CharacterController,
    is_valid_transition,
)
from companion.voice import voice_status
from companion.voice_input import VoiceInputService
from companion.voice_output import CompanionVoice
from companion.wake_word import WakeWordService, wake_status

APP_ID = "org.alwin.fusion.companion"

#: Character area size.  v0.8: wide enough for the bedroom scene (which
#: scales uniformly to whatever box it is given — this is just the
#: default the window asks for).
CHAR_WIDTH = 520
CHAR_HEIGHT = 380

#: Idle check cadence (very low frequency; no busy polling).
IDLE_CHECK_MS = 5000

#: v0.9 microphone level-meter cadence (only runs while listening).
LEVEL_METER_MS = 100

#: Push-to-talk button labels per turn phase (plain text, no markup).
MIC_LABEL_OFF = "Mic"
MIC_LABEL_LISTENING = "Stop & send"

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
.settings-heading { font-size: 12px; font-weight: 600; color: #2e3440;
                    margin-top: 4px; }
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
        llm_client=None,
        conversation=None,
        voice_input=None,
        recognizer=None,
        wake_service=None,
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

        v0.9 voice services (every one is optional and injectable for
        tests/headless use; the defaults are lazy and touch **no**
        device, model or library until actually used):

        * ``voice`` — a :class:`companion.voice.VoiceEngine` for spoken
          replies; defaults to the real local espeak engine, which
          probes its own availability honestly (``NullVoice`` still
          works and means "text only"),
        * ``conversation`` — the shared typed/voice pipeline (defaults
          to one wrapping *manager*),
        * ``llm_client`` — duck-typed local LLM client for optional
          replies (None = no replies, reported honestly),
        * ``voice_input`` — microphone capture service,
        * ``recognizer`` — speech-recognition service (model size comes
          from the ``asr_model`` setting).
        """
        if Gtk is None:  # pragma: no cover - defensive
            raise RuntimeError(
                "GTK4 (PyGObject with Gtk 4.0) is required for the "
                "companion window."
            )
        self.manager = manager
        self.config = config
        self.status = status
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

        # ---- v0.9 voice stack (lazy, injectable, honest) ----------------- #
        # Construction order matters: settings first (they configure the
        # services), and nothing here opens a device, loads a model or
        # touches espeak — those happen on first *use*, off this thread.
        self.voice = voice if voice is not None else CompanionVoice()
        self.conversation = conversation or ConversationService(
            manager=manager,
            llm_client=llm_client,
            replies_enabled=bool(self._settings.get("spoken_responses",
                                                    False)),
            # v0.11 routing: the trusted deterministic parser decides
            # ACTION (full task pipeline) vs CONVERSATIONAL (reply only).
            # The conversation module stays engine-free (injected).
            is_action=self._is_action_text,
        )
        self.voice_input = voice_input or VoiceInputService()
        requested_model = str(self._settings.get("asr_model", "small"))
        model_size = (requested_model if requested_model in MODEL_SIZES
                      else "small")
        self.recognizer = recognizer or SpeechRecognitionService(
            model_size=model_size
        )
        # v0.10.0 hands-free wake word: cheap to construct (device and
        # model work happen on its worker when monitoring starts);
        # shares the recognizer so only ONE Whisper model exists in RAM.
        # Injected instances (tests) are honoured as-is.
        self.wake_service = wake_service or WakeWordService(
            recognizer=self.recognizer,
            phrase=str(self._settings.get("wake_phrase",
                                          "Hey Alwin") or "Hey Alwin"),
            on_trigger=lambda phrase: GLib.idle_add(
                self._on_wake_triggered, phrase
            ),
            on_error=lambda message: GLib.idle_add(
                self._on_wake_error, message
            ),
        )
        # One voice turn at a time: phase + generation counter.  The
        # counter invalidates any in-flight recognition/reply result, so
        # a cancelled turn can never be delivered late.
        self._voice_phase = "off"  # off|wake|listening|recognizing|replying|speaking
        self._turn_serial = 0
        self._meter_id: Optional[int] = None
        self._configure_services()

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

        # ---- animated world (v0.8) --------------------------------------- #
        # A renderer-independent scene model (pure Python).  The renderer
        # draws it and advances it; it never executes, approves or
        # modifies a task — it is only ever a place Alwin stands.
        self.scene = SceneController(
            reduced_motion=bool(self._settings.get("reduced_motion", False))
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
        # v0.8 — morning wake-up sequence bookkeeping.  ``_morning_active``
        # is True only while the sleep -> wake -> walk -> greet chain runs;
        # it suppresses the ordinary scene drive so the two never fight.
        self._morning_active = False
        self._morning_text = ""
        # Widgets exist only after _build(); guarded accessors check them.
        self._bubble = None
        self._status_label = None
        self._subtitle_label = None
        self._popover = None
        self._task_btn = None
        self._mic_btn = None
        self._level_bar = None

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
        # v0.8: hand the bedroom scene to the renderer (when it supports
        # one) so Alwin is drawn *in a room* at his world position.  A
        # renderer without set_scene (the null one) degrades to text-only.
        if hasattr(self.renderer, "set_scene"):
            try:
                self.renderer.set_scene(self.scene, self._on_scene_event)
            except Exception:  # noqa: BLE001 - never break window build
                pass
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
        # v0.9 push-to-talk: one button cycles listen -> recognize ->
        # (submit) -> speak, and cancels at any point of the cycle.
        self._mic_btn = Gtk.Button(label=MIC_LABEL_OFF)
        self._mic_btn.add_css_class("control-btn")
        self._mic_btn.set_tooltip_text(
            "Push to talk: click to start listening, click again to send."
        )
        self._mic_btn.connect("clicked", self._on_mic)
        self._mic_btn.set_sensitive(
            bool(self._settings.get("mic_enabled", True))
        )
        controls.append(self._mic_btn)
        # Live input-level feedback (only shown while actually listening).
        self._level_bar = Gtk.LevelBar()
        self._level_bar.set_min_value(0.0)
        self._level_bar.set_max_value(1.0)
        self._level_bar.set_value(0.0)
        self._level_bar.set_mode(Gtk.LevelBarMode.CONTINUOUS)
        self._level_bar.set_size_request(110, -1)
        self._level_bar.set_visible(False)
        controls.append(self._level_bar)
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
            # Greeting disabled or quiet hours: skip the wake-up sequence
            # but still make the room "ready" — Alwin stands at the
            # greeting spot, idle, instead of staying in bed.
            self._silent_ready()
            return
        self._greeted_this_window = True
        self._greeting.mark_greeted()
        self._morning_text = self._greeting.text()
        self._start_morning_sequence()

    def _silent_ready(self) -> None:
        """No greeting (disabled/quiet): place Alwin up, idle, ready."""
        self._scene_command("place", position="greeting")
        self.controller.force(STATE_IDLE, reason="ready")
        self._maybe_resume_wake()

    # ------------------------------------------------------------------ #
    # Morning wake-up sequence (v0.8): sleep -> wake -> get up -> walk ->
    # greet.  Every stage is a real scene command advanced by elapsed
    # time; the walk ends on an arrival event, with a timer safety net so
    # the chain never dead-ends if no frame is drawn.  Reduced motion
    # collapses the whole thing to a near-instant placement.
    # ------------------------------------------------------------------ #

    def _start_morning_sequence(self) -> None:
        self._morning_active = True
        # Begin in bed, pre-dawn, with Alwin asleep.
        self._scene_command("reset")
        self.controller.force(STATE_SLEEPING, reason="startup")

        if self.scene.reduced_motion:
            # Reduced motion: no long walk — place him at the greeting
            # spot and greet after a short beat.
            def _greet() -> bool:
                self._greet_id = None
                if self._closing:
                    return False
                self._scene_command("place", position="greeting")
                self._finish_morning_greeting()
                return False

            self._greet_id = GLib.timeout_add(500, _greet)
            return

        def _wake() -> bool:
            self._greet_id = None
            if self._closing:
                return False
            self._scene_command("wake")
            self.controller.force(STATE_WAKING_UP, reason="startup")
            self._greet_id = GLib.timeout_add(1500, _get_up)
            return False

        def _get_up() -> bool:
            self._greet_id = None
            if self._closing:
                return False
            self._scene_command("get_up")
            self._greet_id = GLib.timeout_add(1300, _walk)
            return False

        def _walk() -> bool:
            self._greet_id = None
            if self._closing:
                return False
            self._scene_command("move_to", position="greeting")
            self.controller.enter(STATE_WALKING, reason="morning walk")
            # Arrival normally finishes the greeting via _on_scene_event;
            # this timer is a safety net so the chain always resolves.
            self._greet_id = GLib.timeout_add(3600, _arrive)
            return False

        def _arrive() -> bool:
            self._greet_id = None
            if self._closing:
                return False
            self._finish_morning_greeting()
            return False

        self._greet_id = GLib.timeout_add(2500, _wake)

    def _finish_morning_greeting(self) -> None:
        self._cancel_morning()
        if self._closing:
            return
        self.controller.enter(STATE_GREETING, reason="greeting")
        self._show_bubble(self._morning_text, presentation.TONE_POSITIVE)
        self._maybe_resume_wake()

    def _cancel_morning(self) -> None:
        if self._greet_id is not None:
            GLib.source_remove(self._greet_id)
            self._greet_id = None
        self._morning_active = False

    def _now(self) -> float:
        # The scene uses the same monotonic clock the renderer ticks on,
        # so a command's start time lines up with the next frame.
        return time.monotonic()

    def _scene_command(self, name: str, *, position: Optional[str] = None) -> bool:
        """Run one allowlisted scene command; scene bugs never propagate."""
        try:
            return self.scene.command(name, position=position, now=self._now())
        except Exception:  # noqa: BLE001 - the scene is cosmetic only
            return False

    # ------------------------------------------------------------------ #
    # Scene <-> character-state wiring (v0.8)
    # ------------------------------------------------------------------ #

    def _drive_scene_for_state(self, state: str) -> None:
        """Map a *character* state to the matching scene command.

        The scene never decides anything on its own — it only reflects
        where Alwin should be for the current task stage.  Writing/coding
        walk him to the desk; thinking returns him to the greeting spot;
        an approval/failure/celebration stops the work animation and sets
        an honest visual state; idle walks him back from the desk.  While
        the morning sequence runs, this is suppressed so the two systems
        never fight over the same character.
        """
        if self._morning_active:
            return
        cmd = {
            STATE_WRITING: "begin_writing",
            STATE_CODING: "begin_coding",
            STATE_THINKING: "begin_thinking",
            STATE_WAITING_FOR_APPROVAL: "begin_waiting",
            STATE_ERROR: "begin_error",
            STATE_CELEBRATING: "begin_celebrating",
            STATE_IDLE: "return_idle",
        }.get(state)
        if cmd is None:
            return
        self._scene_command(cmd)

    def _on_scene_event(self, event: str) -> None:
        """Arrival events from the renderer's scene tick (main thread)."""
        if self._closing:
            return
        if event == "arrived_at_greeting" and self._morning_active:
            self._finish_morning_greeting()
        # Desk arrivals are handled by the scene itself (the writing/
        # coding phase begins on arrival), so nothing else to do here.

    # ------------------------------------------------------------------ #
    # Task updates (worker thread -> GTK main loop)
    # ------------------------------------------------------------------ #

    def _on_record_from_worker(self, record) -> None:
        """Manager observer (worker thread): marshal, then map."""
        GLib.idle_add(self._on_record_main, record)

    def _on_record_main(self, record) -> bool:
        if self._closing:
            return False
        # A real task arrived.  If the morning wake-up sequence is still
        # running, cancel it cleanly — the task must never wait on a
        # visual flourish.  The scene snaps coherent (Alwin is up) via
        # the drive below / _ensure_up inside the scene commands.
        self._cancel_morning()
        # The user is interacting with a task: wake a resting character.
        if self.controller.state in ("sleeping", "resting"):
            self.controller.force(STATE_IDLE, reason="task activity")
        self.adapter.on_task_update(record)
        self._refresh_status(record)
        # v0.10: a finished (or calm) task is a resume point for
        # wake-word monitoring; the helper itself refuses unless the
        # character is calm, monitoring is off and the setting is on.
        self._maybe_resume_wake()
        return False

    def _queue_speech(self, text: str) -> None:
        """Adapter speech (may be any thread): marshal to the UI."""
        GLib.idle_add(self._show_bubble, text, None)

    # ------------------------------------------------------------------ #
    # v0.9 voice turn cycle (push-to-talk)
    #
    # One button drives the whole cycle; every phase transition is
    # explicit (``_voice_phase``) so a double click, a cancellation or
    # a late worker result can never be mistaken for a new turn.  The
    # GTK main loop never blocks: recognition, reply generation and
    # speech all run on their own workers, and their results are
    # marshalled back through ``GLib.idle_add`` with the turn's serial
    # number — a stale (cancelled) result is dropped, not delivered.
    # ------------------------------------------------------------------ #

    #: Turn phases (mirrors the character states they drive).
    PHASE_OFF = "off"
    PHASE_LISTENING = "listening"
    PHASE_RECOGNIZING = "recognizing"
    PHASE_REPLYING = "replying"
    PHASE_SPEAKING = "speaking"
    PHASE_WAKE = "wake"          # v0.10.0: monitoring for the phrase

    # ------------------------------------------------------------------ #
    # v0.10.0 wake-word monitoring (opt-in, off by default)
    #
    # Lifecycle: (disabled) -> WAKE_LISTENING --phrase--> ack ->
    # command capture (the existing PHASE_LISTENING turn cycle) ->
    # PHASE_REPLYING/PHASE_SPEAKING -> WAKE_LISTENING again.  The
    # service owns the microphone EXCLUSIVELY while monitoring and
    # stops itself on a trigger, so a capture session never overlaps;
    # every resume goes through _maybe_resume_wake, which refuses
    # unless it is calm, not closing and the setting is on.
    # ------------------------------------------------------------------ #

    def _is_action_text(self, text: str) -> bool:
        """Trusted routing predicate for ConversationService (v0.11).

        The deterministic parser — the SAME validator the task pipeline
        starts from — decides whether an utterance is a desktop action.
        v0.12 adds a deterministic *task-goal gate*: an unsupported
        sentence that clearly asks for a desktop task (file/report/
        analysis wording) is routed to the manager, where the planning
        service either structures it into a validated plan, asks a
        clarifying question, or honestly declines (never executes).
        Parser-supported requests keep the unchanged validate →
        permission → approval → verification path; everything else is
        conversational. The predicate fails CLOSED (routing errors
        treat the text as an action).
        """
        try:
            intent = parse_command(text).intent
        except Exception:  # noqa: BLE001 - never crash a turn
            return True
        if intent not in (UNSUPPORTED, EXIT):
            return True
        # Unsupported → is this a *task goal* worth planning? (Cheap,
        # deterministic, English — same language basis as the parser.)
        try:
            from ai_core.task_planning import looks_like_task_goal

            return looks_like_task_goal(text)
        except Exception:  # noqa: BLE001 - fail CLOSED to planning
            return True

    def _wake_enabled(self) -> bool:
        return bool(self._settings.get("wake_word_enabled", False))

    def _wake_calm(self) -> bool:
        """The only states in which monitoring may (re)start."""
        return (
            self.controller.state in (STATE_IDLE, STATE_GREETING,
                                      STATE_RESTING)
            and not self._morning_active
        )

    def _begin_wake(self, *, announce: bool = True) -> None:
        """Start (or restart) monitoring; honest about every failure.

        ``announce=False`` (automatic resumes after turns/failures)
        shows only the meter indicator so the turn's own last bubble —
        a reply, an honest error, a silence refusal — is not clobbered;
        explicit user enablement always explains itself.
        """
        if self._closing or not self._wake_enabled():
            return
        if self._voice_phase != self.PHASE_OFF:
            return                      # a turn owns the audio pipeline
        if not self._wake_calm():
            return
        if self.wake_service.running:
            self._voice_phase = self.PHASE_WAKE
            if announce:
                self._show_wake_indicator()
            else:
                self._start_level_meter()
            return                      # already monitoring (idempotent)
        if not self.wake_service.start():
            error = self.wake_service.last_error or (
                "Wake-word listening could not start."
            )
            self._show_bubble(error[:300], presentation.TONE_NEGATIVE)
            return
        self._voice_phase = self.PHASE_WAKE
        if announce:
            self._show_wake_indicator()
        else:
            self._start_level_meter()

    def _show_wake_indicator(self) -> None:
        """Visible 'listening for the phrase' state (meter + bubble)."""
        self._show_bubble(
            f"Listening for “{self.wake_service.phrase}” — say it, or "
            "tap Mic to talk now.",
            presentation.TONE_CALM,
        )
        self._start_level_meter()

    def _end_wake(self) -> None:
        """Stop monitoring now (immediate stop control / takeover)."""
        try:
            self.wake_service.stop()   # idempotent; discards buffered audio
        except Exception:  # noqa: BLE001 - stop never raises
            pass
        if self._voice_phase == self.PHASE_WAKE:
            self._voice_phase = self.PHASE_OFF
            if self.voice_input.listening:
                self._start_level_meter()   # capture's own meter
            else:
                self._stop_level_meter()

    def _maybe_resume_wake(self) -> None:
        """Return to monitoring after a turn, a failure or a cancel."""
        if (self._closing
                or self._voice_phase != self.PHASE_OFF
                or not self._wake_enabled()
                or not self._wake_calm()):
            return
        self._begin_wake(announce=False)

    def _on_wake_triggered(self, phrase: str) -> None:
        """Verified phrase (marshalled from the wake worker)."""
        if self._closing or self._voice_phase != self.PHASE_WAKE:
            return                      # stale trigger: state moved on
        # The service stopped itself first: the microphone is free.
        self._voice_phase = self.PHASE_OFF
        if not self._wake_calm():
            # Busy with something visible: no ack, no capture; the
            # normal calm hooks will resume monitoring later.
            return
        # Acknowledge, then listen for the COMMAND — the wake phrase
        # itself is never the user's request (it belongs to the
        # monitoring session, which has already ended).
        self._show_bubble(
            "Yes? I'm listening.", presentation.TONE_CALM
        )
        self._start_listening()

    def _on_wake_error(self, message: str) -> None:
        """Honest wake failure (marshalled); never auto-retry-storms."""
        if self._closing:
            return
        if self._voice_phase == self.PHASE_WAKE:
            self._voice_phase = self.PHASE_OFF
            self._stop_level_meter()
        self._show_bubble(str(message)[:300],
                          presentation.TONE_NEGATIVE)
        # Deliberately NO immediate resume: a dead device would loop.
        # Recovery hooks (turn end, window map, settings toggle) retry.

    def _configure_services(self) -> None:
        """Apply the persisted voice/language settings to the services.

        Called at construction (services exist, no widgets yet) and
        from ``_apply_settings`` on every settings change; all of it is
        live reconfiguration, never a restart, and never raises.
        """
        try:
            self.conversation.configure_language(
                auto=bool(self._settings.get("language_auto", True)),
                preferred=str(self._settings.get("language_preferred", "")
                              or ""),
            )
            self.conversation.set_replies_enabled(
                bool(self._settings.get("spoken_responses", False))
            )
        except Exception:  # noqa: BLE001 - cosmetic settings only
            pass
        try:
            # v0.11: bounded in-memory context (live; never persisted).
            self.conversation.configure_context(
                enabled=bool(self._settings.get("context_enabled", True)),
                turns=str(self._settings.get("context_turns", "8") or "8"),
            )
        except Exception:  # noqa: BLE001 - cosmetic settings only
            pass
        try:
            rate = float(self._settings.get("speech_rate", "1.0"))
        except (TypeError, ValueError):
            rate = 1.0
        setter = getattr(self.voice, "set_rate", None)
        if callable(setter):
            try:
                setter(rate)
            except Exception:  # noqa: BLE001
                pass
        setter = getattr(self.voice, "set_voice_id", None)
        if callable(setter):
            try:
                setter(str(self._settings.get("voice_id", "") or ""))
            except Exception:  # noqa: BLE001
                pass

    # ---- the button ------------------------------------------------------ #

    def _on_mic(self, *_args) -> None:
        """Push-to-talk: start / stop-and-send / cancel / stop-speech."""
        if self._closing:
            return
        if self.voice_input.listening:
            self._finish_capture()
            return
        if self._voice_phase in (self.PHASE_RECOGNIZING,
                                 self.PHASE_REPLYING):
            self._cancel_turn()
            return
        if getattr(self.voice, "speaking", False) or \
                self._voice_phase == self.PHASE_SPEAKING:
            self._stop_speech()
            return
        if self._voice_phase == self.PHASE_WAKE:
            # Push-to-talk overrides monitoring: pause the wake session
            # (its mic is released) and take over with capture — one
            # audio owner at a time, never two sessions.
            self._end_wake()
            self._start_listening()
            return
        if not bool(self._settings.get("mic_enabled", True)):
            self._show_bubble(
                "The microphone is turned off — turn it on in Settings.",
                presentation.TONE_ATTENTION,
            )
            return
        if (self.controller.state in (STATE_IDLE, STATE_GREETING,
                                      STATE_RESTING)
                and not self._morning_active
                and self._voice_phase == self.PHASE_OFF):
            self._start_listening()
            return
        self._show_bubble(
            "I'm busy right now — tap the mic when I'm idle.",
            presentation.TONE_ATTENTION,
        )

    def _start_listening(self) -> None:
        """Open the microphone (never on the GTK thread's budget: the
        backend open is non-blocking; failures are honest bubbles)."""
        try:
            self.voice_input.start()
        except Exception as exc:  # noqa: BLE001 - device errors surface
            self._voice_phase = self.PHASE_OFF
            self._show_bubble(
                str(exc) or "The microphone could not be opened.",
                presentation.TONE_NEGATIVE,
            )
            return
        self._turn_serial += 1
        self._voice_phase = self.PHASE_LISTENING
        self.controller.enter(STATE_LISTENING, reason="microphone")
        self._set_mic_button(listening=True)
        self._start_level_meter()

    def _finish_capture(self) -> None:
        """Stop the mic; silence is refused *before* any model runs."""
        self._stop_level_meter()
        self._set_mic_button(listening=False)
        capture = self.voice_input.stop()
        if capture.silent or capture.cancelled:
            self._voice_phase = self.PHASE_OFF
            self._show_bubble(
                "I didn't hear anything — tap the mic, speak, then tap "
                "again to send.",
                presentation.TONE_ATTENTION,
            )
            self._return_to_calm()
            return
        self._voice_phase = self.PHASE_RECOGNIZING
        self.controller.enter(STATE_THINKING, reason="recognizing speech")
        # Language forcing only when auto-detection is off (the user's
        # deliberate choice); with auto-detection on the engine decodes
        # freely and reports its own guess as a hint for detection.
        forced = None
        if not bool(self._settings.get("language_auto", True)):
            forced = str(self._settings.get("language_preferred", "")
                         or "") or None
        serial = self._turn_serial
        started = self.recognizer.recognize_async(
            capture.pcm,
            language=forced,
            on_result=lambda result: GLib.idle_add(
                self._on_recognition, result, serial
            ),
            on_error=lambda exc: GLib.idle_add(
                self._on_recognition_error, exc, serial
            ),
        )
        if not started:
            self._voice_phase = self.PHASE_OFF
            self._show_bubble(
                "A recognition is already running.",
                presentation.TONE_ATTENTION,
            )
            self._return_to_calm()

    def _on_recognition(self, result, serial: int) -> bool:
        """A recognition result arrived (marshalled from the worker)."""
        if (self._closing or serial != self._turn_serial
                or self._voice_phase != self.PHASE_RECOGNIZING):
            return False  # cancelled/superseded turn: the result is dropped
        if result.no_speech or not result.text.strip():
            self._voice_phase = self.PHASE_OFF
            self._show_bubble(
                "I couldn't hear any words — tap the mic and try again.",
                presentation.TONE_ATTENTION,
            )
            self._return_to_calm()
            return False
        # Show exactly what was recognized (plain text, verbatim) before
        # the shared pipeline takes over with its own status lines.
        self._show_bubble(result.text.strip(), presentation.TONE_CALM)
        self._submit_voice_turn(result)
        return False

    def _on_recognition_error(self, exc, serial: int) -> bool:
        """Recognition failed (missing model, broken decode) — honest."""
        if (self._closing or serial != self._turn_serial
                or self._voice_phase != self.PHASE_RECOGNIZING):
            return False
        self._voice_phase = self.PHASE_OFF
        message = str(exc) or "Speech recognition failed."
        self._show_bubble(message[:300], presentation.TONE_NEGATIVE)
        self._return_to_calm()
        return False

    def _submit_voice_turn(self, result) -> None:
        """Shared pipeline: validate -> detect -> ``manager.submit()``.

        Exactly the path typed input uses — transcripts are untrusted
        text here, nothing more (same allowlist, same permissions,
        same approval).  The optional reply runs on its own worker so
        the main loop never waits on the local LLM.
        """
        want_reply = (
            bool(self._settings.get("spoken_responses", False))
            and bool(getattr(self.voice, "available", False))
        )
        try:
            turn = self.conversation.submit(
                result.text.strip(),
                source="voice",
                asr_hint=result.asr_hint,
                hint_confidence=result.language_probability,
                want_reply=False,   # the reply is generated off-thread below
            )
        except Exception as exc:  # noqa: BLE001 - never crash the UI
            self._voice_phase = self.PHASE_OFF
            self._show_bubble(str(exc)[:300], presentation.TONE_NEGATIVE)
            self._return_to_calm()
            return

        if not turn.ok:
            self._voice_phase = self.PHASE_OFF
            self._show_bubble(turn.error or "The request could not be "
                              "sent.", presentation.TONE_NEGATIVE)
            self._return_to_calm()
            return

        if turn.uncertain_language:
            # Detection is uncertain: ask the user instead of guessing.
            # (For actions the task still runs — the words were clear;
            # only the language claim is refused.)
            self._show_bubble(
                "I couldn't tell which language that was — set your "
                "fallback language in Settings, or say it again.",
                presentation.TONE_ATTENTION,
            )

        if turn.record is None:
            # v0.11 conversational turn: no manager record exists, so
            # nothing will drive the character through task states.
            if not want_reply:
                self._voice_phase = self.PHASE_OFF
                self._return_to_calm()
                return
            # With a reply pending, stay in `thinking` until it is
            # generated (reply/speaking paths resolve the state).
            self._voice_phase = self.PHASE_REPLYING
            language = turn.language
            serial = self._turn_serial
            threading.Thread(
                target=self._reply_worker,
                args=(result.text.strip(), language, serial,
                      turn.record is not None),
                daemon=True,
                name="alwin-reply",
            ).start()
            return

        if not want_reply:
            # The manager's record drives the character from here (the
            # observer was notified synchronously inside submit()).
            self._voice_phase = self.PHASE_OFF
            return

        self._voice_phase = self.PHASE_REPLYING
        language = turn.language
        serial = self._turn_serial
        threading.Thread(
            target=self._reply_worker,
            args=(result.text.strip(), language, serial,
                      turn.record is not None),
            daemon=True,
            name="alwin-reply",
        ).start()

    def _reply_worker(self, text: str, language, serial: int,
                      sent: bool) -> None:
        """Generate the optional reply off the GTK thread, then marshal."""
        reply, reason = self.conversation.reply_with_reason(
            text, language, action=sent
        )
        GLib.idle_add(self._on_reply_ready, reply, reason, language,
                      serial, sent)

    def _on_reply_ready(self, reply, reason, language, serial: int,
                        sent: bool) -> bool:
        """A reply (or its honest failure) arrived on the main loop."""
        if (self._closing or serial != self._turn_serial
                or self._voice_phase != self.PHASE_REPLYING):
            return False  # cancelled turn: no speech, nothing claimed
        if not reply:
            # No reply: the local model failed, returned nothing, or is
            # not attached.  Say so honestly (v0.9.1) instead of going
            # quiet — and only claim "still sent" when a task actually
            # was submitted (v0.11: conversational turns never were).
            self._voice_phase = self.PHASE_OFF
            detail = str(reason or "no reply was generated")[:300]
            tail = (". Your request was still sent." if sent
                    else "")
            self._show_bubble(
                f"I couldn't write a reply ({detail}){tail}",
                presentation.TONE_ATTENTION,
            )
            self._return_to_calm()
            return False
        self._show_bubble(reply, presentation.TONE_CALM)
        # v0.11: the reply reached the UI — confirm it into the session
        # context (serial already validated; failures/cancels never get
        # here, so history only holds replies the user actually saw).
        try:
            self.conversation.record_reply(reply)
        except Exception:  # noqa: BLE001 - context is cosmetic-only
            pass
        set_language = getattr(self.voice, "set_language", None)
        if language and callable(set_language):
            try:
                set_language(language)  # False = keep current voice (honest)
            except Exception:  # noqa: BLE001
                pass
        current = self._turn_serial
        try:
            dispatched = bool(self.voice.speak(
                reply,
                on_done=lambda ok, err, s=current:
                    self._on_speech_done(ok, err, s),
            ))
        except Exception:  # noqa: BLE001 - a broken engine must not crash
            dispatched = False
        if not dispatched:
            # Engine unavailable right now: the text stays visible, no
            # speaking state is entered, nothing is claimed as spoken.
            self._voice_phase = self.PHASE_OFF
            self._return_to_calm()
            return False
        self._voice_phase = self.PHASE_SPEAKING
        self._enter_speaking()
        return False

    def _enter_speaking(self) -> None:
        """Enter ``speaking`` only where the transition graph allows it.

        A reply can finish while Alwin is already walking/working on
        the submitted task; when the graph has no edge for the current
        state the audio still plays and the bubble still shows the
        text — the animation is simply skipped instead of forcing an
        invalid state.
        """
        state = self.controller.state
        if is_valid_transition(state, STATE_SPEAKING):
            self.controller.enter(STATE_SPEAKING, reason="voice reply")

    def _on_speech_done(self, success: bool, error: Optional[str],
                        serial: Optional[int] = None) -> None:
        """VoiceEngine completion callback (TTS worker): marshal.

        v0.11: a completion carrying a STALE turn serial (the turn was
        cancelled/superseded after dispatch) is dropped here — the
        engine already released its audio resources, and touching the
        UI could clobber the newer turn's state.
        """
        if serial is not None and serial != self._turn_serial:
            return
        GLib.idle_add(self._speech_finished, bool(success), error)

    def _speech_finished(self, success: bool, error: Optional[str]) -> bool:
        """The speaking turn's end: return to idle, honestly."""
        if self._closing:
            return False
        if self._voice_phase == self.PHASE_SPEAKING:
            self._voice_phase = self.PHASE_OFF
        if error and error != "stopped":
            self._show_bubble(
                f"Speech failed: {error}"[:300],
                presentation.TONE_NEGATIVE,
            )
        if self.controller.state == STATE_SPEAKING:
            # Success, failure or user-cancel alike: the speaking state
            # always resolves calmly (the 120 s auto-return in states
            # is only the safety net for a lost callback).
            self.controller.enter(STATE_IDLE, reason="reply finished")
        self._maybe_resume_wake()
        return False

    def _stop_speech(self) -> None:
        """User pressed the mic while Alwin was talking: interrupt."""
        stop = getattr(self.voice, "stop", None)
        if callable(stop):
            try:
                stop()
            except Exception:  # noqa: BLE001 - stop never raises
                pass
        # Completion arrives via on_done(False, "stopped"); the phase is
        # cleared there so a racing mic click cannot start a new turn
        # while the old worker is still unwinding.

    def _cancel_turn(self) -> None:
        """Cancel capture/recognition/reply (mic pressed mid-turn)."""
        self._turn_serial += 1          # invalidates in-flight results
        # v0.11: a full-turn cancel also interrupts speech — the engine
        # releases its audio resources, and its (now stale) completion
        # is dropped by the serial guard instead of disturbing the UI.
        stop_speech = getattr(self.voice, "stop", None)
        if callable(stop_speech):
            try:
                stop_speech()
            except Exception:  # noqa: BLE001 - cancel never raises
                pass
        if self.voice_input.listening:
            try:
                self.voice_input.cancel()   # discard what was heard
            except Exception:  # noqa: BLE001 - cancel never raises
                pass
        try:
            self.recognizer.cancel()
        except Exception:  # noqa: BLE001
            pass
        was_announced = self._voice_phase in (self.PHASE_RECOGNIZING,
                                              self.PHASE_REPLYING)
        self._voice_phase = self.PHASE_OFF
        self._stop_level_meter()
        self._set_mic_button(listening=False)
        if self.controller.state in (STATE_LISTENING, STATE_THINKING,
                                     STATE_SPEAKING):
            self.controller.enter(STATE_IDLE, reason="cancelled")
        if was_announced:
            self._show_bubble("Okay — cancelled.", presentation.TONE_CALM)
        self._maybe_resume_wake()

    def _return_to_calm(self) -> None:
        """Back to idle from the voice-only states (never from a task)."""
        if self.controller.state in (STATE_LISTENING, STATE_THINKING):
            self.controller.enter(STATE_IDLE, reason="turn over")
        self._maybe_resume_wake()

    # ---- push-to-talk button + level meter ------------------------------- #

    def _set_mic_button(self, *, listening: bool) -> None:
        if self._mic_btn is None:
            return
        self._mic_btn.set_label(MIC_LABEL_LISTENING
                                if listening else MIC_LABEL_OFF)

    def _meter_active(self) -> bool:
        """The meter reads whichever input currently owns the audio."""
        if self._voice_phase == self.PHASE_WAKE:
            return bool(self.wake_service.running)
        return bool(self.voice_input.listening)

    def _meter_value(self) -> float:
        if self._voice_phase == self.PHASE_WAKE:
            return float(self.wake_service.level())
        return float(self.voice_input.level())

    def _start_level_meter(self) -> None:
        """Live input level (100 ms) while the mic is open — opt-out."""
        if (self._level_bar is None
                or not bool(self._settings.get("mic_level_meter", True))):
            return
        if self._meter_id is not None:
            return
        self._level_bar.set_visible(True)

        def _tick() -> bool:
            if self._closing or not self._meter_active():
                self._meter_id = None
                self._hide_level_meter()
                return False
            try:
                self._level_bar.set_value(self._meter_value())
            except Exception:  # noqa: BLE001 - cosmetic feedback only
                pass
            return True

        self._meter_id = GLib.timeout_add(LEVEL_METER_MS, _tick)

    def _stop_level_meter(self) -> None:
        if self._meter_id is not None:
            try:
                GLib.source_remove(self._meter_id)
            except Exception:  # noqa: BLE001 - already gone is fine
                pass
            self._meter_id = None
        self._hide_level_meter()

    def _hide_level_meter(self) -> None:
        if self._level_bar is None:
            return
        self._level_bar.set_value(0.0)
        self._level_bar.set_visible(False)

    # ------------------------------------------------------------------ #
    # Rendering hooks
    # ------------------------------------------------------------------ #

    def _on_state_change(self, state: str, expression: str, reason: str) -> None:
        # Controller callbacks run on the thread that entered the state
        # (the GTK main loop, via the marshalled observer).  Schedule
        # the auto-return for transient outcome states, and let the
        # scene reflect where Alwin should be for this stage.
        self._schedule_auto_return(state)
        self._drive_scene_for_state(state)
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
                                          STATE_GREETING, STATE_WAKING_UP,
                                          # v0.9: safety net only — the
                                          # voice engine's completion
                                          # callback normally ends the
                                          # speaking state promptly.
                                          STATE_SPEAKING):
            return

        def _back() -> bool:
            self._auto_return_id = None
            if self.controller.state == state:  # only if still there
                if state == STATE_SPEAKING:
                    # Lost completion callback safety net: clear the
                    # turn phase too, so the mic button is not stuck.
                    self._voice_phase = self.PHASE_OFF
                    self._set_mic_button(listening=False)
                self.controller.enter(STATE_IDLE, reason="calm return")
                if state in (STATE_CELEBRATING, STATE_ERROR):
                    self._show_bubble("", presentation.TONE_CALM)
                self._maybe_resume_wake()
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
        # v0.10: monitoring keeps running while hidden (documented), so
        # restoring the window restores its visible indicator; if
        # monitoring stopped while hidden, this is also a recovery hook.
        if self._voice_phase == self.PHASE_WAKE:
            self._start_level_meter()
        else:
            self._maybe_resume_wake()

    def _on_unmap(self, *_args) -> None:  # pragma: no cover - display
        # Hidden: pause the frame timer so nothing animates off-screen.
        try:
            self.renderer.stop()
        except Exception:  # noqa: BLE001 - renderer bugs never propagate
            pass
        # v0.9: the level meter must not tick while hidden either.
        self._stop_level_meter()

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

        def _switch(key: str, label: str, default: bool = True):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("settings-row")
            row.append(_label(label))
            sw = Gtk.Switch()
            sw.active = bool(self._settings.get(key, default))
            sw.set_halign(Gtk.Align.END)
            sw.set_hexpand(True)

            def _toggled(_widget, _pspec, k=key):
                self._settings[k] = sw.active
                self._apply_settings()

            sw.connect("notify::active", _toggled)
            row.append(sw)
            box.append(row)
            return sw

        def _dropdown(title: str, labels, index: int, on_change):
            row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL, spacing=8)
            row.add_css_class("settings-row")
            row.append(_label(title))
            combo = Gtk.DropDown.new(Gtk.StringList.new(list(labels)))
            combo.set_selected(max(0, min(int(index), len(labels) - 1)))
            combo.set_halign(Gtk.Align.END)
            combo.set_hexpand(True)

            def _changed(_combo, _pspec, cb=on_change, choices=labels):
                cb(choices[_combo.get_selected()])

            combo.connect("notify::selected", _changed)
            row.append(combo)
            box.append(row)
            return combo

        _switch("greeting_enabled", "Startup greeting")
        _switch("show_subtitles", "Show subtitles")
        _switch("always_on_top", "Always on top (best effort)")
        _switch("reduced_motion", "Reduced motion")

        # ---- v0.9 microphone, language & spoken replies ------------------ #
        heading = _label("Voice & language", "settings-heading")
        box.append(heading)
        _switch("mic_enabled", "Microphone button", default=True)
        _switch("mic_level_meter", "Input level meter", default=True)
        _switch("language_auto", "Detect language automatically",
                default=True)

        # Preferred fallback language: used only when detection is
        # unclear — it never overrides a clear result (the detector
        # enforces that; this is pure data for it).
        language_choices = choices_for_ui()   # (code, label) x15
        language_labels = ["No preference (ask me)"] + [
            label for _code, label in language_choices
        ]
        preferred = str(self._settings.get("language_preferred", "") or "")
        language_index = 0
        for i, (code, _label_text) in enumerate(language_choices, start=1):
            if code == preferred:
                language_index = i
        _dropdown("Fallback language", language_labels, language_index,
                  lambda chosen: self._set_language_preference(
                      chosen, language_choices))

        _switch("spoken_responses", "Spoken replies", default=False)

        # Reply speed (stored as text — the schema is bool/str only).
        rate_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                           spacing=8)
        rate_row.add_css_class("settings-row")
        rate_row.append(_label("Reply speed"))
        try:
            rate_value = float(self._settings.get("speech_rate", "1.0"))
        except (TypeError, ValueError):
            rate_value = 1.0
        rate_value = max(0.5, min(2.0, rate_value))
        rate_spin = Gtk.SpinButton.new_with_range(0.5, 2.0, 0.1)
        rate_spin.set_value(rate_value)
        rate_spin.set_digits(2)
        rate_spin.set_halign(Gtk.Align.END)

        def _rate_changed(_spin):
            self._settings["speech_rate"] = f"{rate_spin.get_value():.2f}"
            self._apply_settings()

        rate_spin.connect("value-changed", _rate_changed)
        rate_row.append(rate_spin)
        box.append(rate_row)

        # Speaking voice: "automatic" (the registry's voice for the
        # detected language) or one pinned espeak voice.
        voice_labels = ["Automatic (matches the language)"] + sorted(
            {spec.espeak for spec in LANGUAGES}
        )
        voice_id = str(self._settings.get("voice_id", "") or "")
        try:
            voice_index = voice_labels.index(voice_id)
        except ValueError:
            voice_index = 0
        _dropdown("Voice", voice_labels, voice_index,
                  lambda chosen: self._set_voice_choice(
                      chosen, voice_labels))

        # ---- v0.10 wake word (opt-in; OFF unless the user chooses) ------ #
        heading_wake = _label("Wake word", "settings-heading")
        box.append(heading_wake)
        _switch("wake_word_enabled", "Hands-free “Hey Alwin” listening",
                default=False)

        # Phrase entry: the two-stage detector genuinely supports
        # arbitrary phrases (vosk keyword + hotword bias + pattern are
        # all derived from this value). Press Enter to apply.
        phrase_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                             spacing=8)
        phrase_row.add_css_class("settings-row")
        phrase_row.append(_label("Phrase"))
        phrase_entry = Gtk.Entry()
        phrase_entry.set_max_length(40)
        phrase_entry.set_text(
            str(self._settings.get("wake_phrase", "Hey Alwin")
                or "Hey Alwin")
        )
        phrase_entry.set_placeholder_text("Hey Alwin")
        phrase_entry.set_halign(Gtk.Align.END)
        phrase_entry.set_hexpand(True)
        phrase_entry.set_width_chars(14)

        def _phrase_activate(entry_widget):
            value = (entry_widget.get_text() or "").strip()[:40]
            if not value:
                value = "Hey Alwin"
                entry_widget.set_text(value)
            self._settings["wake_phrase"] = value
            self._apply_settings()

        phrase_entry.connect("activate", _phrase_activate)
        phrase_row.append(phrase_entry)
        box.append(phrase_row)

        # Honest status: is the two-stage detector actually usable here?
        try:
            wake_detail = wake_status(
                self.recognizer,
                phrase=str(self._settings.get("wake_phrase",
                                              "Hey Alwin")),
            )["detail"]
        except Exception:  # noqa: BLE001 - status is diagnostic only
            wake_detail = "Wake-word status is unavailable."
        wake_note = _label(str(wake_detail), "subtitle-label")
        wake_note.set_wrap(True)
        box.append(wake_note)

        # ---- v0.11 conversation context (in-memory only) ---------------- #
        heading_conv = _label("Conversation", "settings-heading")
        box.append(heading_conv)
        _switch("context_enabled", "Remember this conversation",
                default=True)

        context_labels = ["2", "4", "8", "12", "20"]
        current_turns = str(self._settings.get("context_turns", "8")
                            or "8")
        try:
            ctx_index = context_labels.index(current_turns)
        except ValueError:
            ctx_index = context_labels.index("8")
        _dropdown("Recent turns kept", context_labels, ctx_index,
                  lambda chosen: self._set_context_turns(chosen))

        clear_row = Gtk.Box(orientation=Gtk.Orientation.HORIZONTAL,
                            spacing=8)
        clear_row.add_css_class("settings-row")
        clear_row.append(_label("Session"))
        clear_btn = Gtk.Button(label="Clear conversation")
        clear_btn.add_css_class("control-btn")
        clear_btn.connect("clicked", self._on_clear_context)
        clear_row.append(clear_btn)
        box.append(clear_row)

        try:
            kept = len(self.conversation.context)
        except Exception:  # noqa: BLE001 - diagnostic only
            kept = 0
        ctx_note = _label(
            f"Context is in memory only ({kept} recent turns) — never "
            "written to disk, never logged, and discarded when Alwin "
            "closes. Clearing drops it immediately.",
            "subtitle-label",
        )
        ctx_note.set_wrap(True)
        box.append(ctx_note)

        # Honest status lines: speech model, voice engine, privacy.
        try:
            model_detail = str(self.recognizer.status().get("detail", ""))
        except Exception:  # noqa: BLE001 - status is diagnostic only
            model_detail = "Speech model status is unavailable."
        model_note = _label(model_detail, "subtitle-label")
        model_note.set_wrap(True)
        box.append(model_note)

        info = voice_status(self.voice)
        engine_note = _label(f"{info['label']} — {info['detail']}",
                             "subtitle-label")
        engine_note.set_wrap(True)
        box.append(engine_note)

        privacy = _label(
            "Everything stays on this device: microphone audio is never "
            "saved or uploaded, and transcripts follow the same approval "
            "rules as typed requests.",
            "subtitle-label",
        )
        privacy.set_wrap(True)
        box.append(privacy)
        return popover

    def _set_language_preference(self, chosen_label: str, choices) -> None:
        """Dropdown handler: map the chosen label back to its code."""
        code = ""
        for candidate, label in choices:
            if label == chosen_label:
                code = candidate
                break
        self._settings["language_preferred"] = code
        self._apply_settings()

    def _set_context_turns(self, chosen: str) -> None:
        """Dropdown handler for the context budget (2..20 exchanges)."""
        value = str(chosen or "8").strip()
        if value not in ("2", "4", "8", "12", "20"):
            value = "8"
        self._settings["context_turns"] = value
        self._apply_settings()

    def _on_clear_context(self, *_args) -> None:
        """Explicit clear-conversation control (immediate, in-memory)."""
        try:
            dropped = self.conversation.clear_context()
        except Exception:  # noqa: BLE001 - control must never crash
            dropped = 0
        self._show_bubble(
            f"Conversation cleared ({dropped} earlier turns dropped).",
            presentation.TONE_CALM,
        )

    def _set_voice_choice(self, chosen_label: str, labels) -> None:
        """Dropdown handler: '' (first entry) means automatic."""
        self._settings["voice_id"] = (
            "" if chosen_label == labels[0] else chosen_label[:40]
        )
        self._apply_settings()

    def _apply_settings(self) -> None:
        # Greeting toggle affects the *next* launch; the current
        # window has already greeted (or not) for this session.
        if self._settings_path is not None:
            try:
                settings_store.save(self._settings_path, self._settings)
            except settings_store.SettingsError:
                pass  # surfaced by the store; never crash the window
        # Reduced motion takes effect immediately on the scene (calmer,
        # near-instant transitions for the wake-up and walks).
        try:
            self.scene.reduced_motion = bool(
                self._settings.get("reduced_motion", False)
            )
        except Exception:  # noqa: BLE001 - cosmetic only
            pass
        if self._subtitle_label is not None:
            self._subtitle_label.set_visible(
                bool(self._settings.get("show_subtitles", True))
            )
        # v0.9: voice/language settings apply live to the services
        # (detection mode, reply toggle, speech rate, pinned voice).
        self._configure_services()
        # The mic button obeys its setting immediately — including
        # turning it off *while* listening (capture is discarded, the
        # character returns to idle; nothing keeps recording).
        if self._mic_btn is not None:
            self._mic_btn.set_sensitive(
                bool(self._settings.get("mic_enabled", True))
            )
        if (not bool(self._settings.get("mic_enabled", True))
                and (self.voice_input.listening
                     or self._voice_phase != self.PHASE_OFF)):
            self._cancel_turn()
        # The level meter is opt-out live: hiding it while running
        # stops the timer on its next tick (the tick checks visibility
        # through the listening flag; here we stop it outright).
        if not bool(self._settings.get("mic_level_meter", True)):
            self._stop_level_meter()
        # v0.10: wake-word enable/disable and phrase apply LIVE — the
        # enable switch doubles as the immediate stop control.
        self._apply_wake_settings()

    def _apply_wake_settings(self) -> None:
        """Enable / stop / re-phrase monitoring without a restart."""
        phrase = (
            str(self._settings.get("wake_phrase", "Hey Alwin") or "")
            .strip()[:40] or "Hey Alwin"
        )
        if phrase != self.wake_service.phrase:
            # The phrase feeds both detector stages: apply it only
            # between sessions (stop first, restart below if still on).
            if (self.wake_service.running
                    or self._voice_phase == self.PHASE_WAKE):
                self._end_wake()
            if not self.wake_service.running:
                try:
                    self.wake_service.phrase = phrase
                except Exception:  # noqa: BLE001 - cosmetic setting
                    pass
        if self._wake_enabled():
            # Explicit settings change: announce the state once (the
            # switch is the user's own action, so this is the moment
            # to say what it did); stays idempotent while running.
            self._begin_wake(announce=True)
        else:
            # Turning the switch off is the immediate stop control.
            self._end_wake()

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
        # v0.9: end any voice turn first — in-flight recognition or
        # speech must never outlive the window, and nothing captured
        # is kept (release() discards it).
        self._turn_serial += 1
        self._stop_level_meter()
        for action in (self.recognizer.cancel, self.voice_input.release,
                       getattr(self.voice, "stop", None),
                       getattr(self.wake_service, "release", None)):
            if action is None:
                continue
            try:
                action()
            except Exception:  # noqa: BLE001 - teardown never raises
                pass
        self._voice_phase = self.PHASE_OFF
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
                  voice=None, llm_client=None) -> int:  # pragma: no cover - needs display
    """Create the companion window and run the GTK main loop."""
    companion = CompanionWindow(
        manager, config=config, status=status, voice=voice,
        llm_client=llm_client,
        open_task_window=open_task_window,
    )
    return companion.run()


__all__ = [
    "CHAR_HEIGHT",
    "CHAR_WIDTH",
    "CompanionWindow",
    "run_companion",
]
