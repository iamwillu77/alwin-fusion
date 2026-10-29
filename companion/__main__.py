"""Entry point for the Alwin companion (v0.10.0).

Launch the companion window::

    python3 -m companion                 # companion character window
    python3 -m companion --no-greeting   # skip the startup greeting
    python3 -m companion --llm           # local Ollama for optional
                                         # spoken replies (still local)
    python3 -m taskui --companion        # task window + companion together

Useful flags::

    python3 -m companion --check   # headless self-check (no display):
                                   # state graph, adapter mapping, greeting,
                                   # assets, cairo rendering of all states,
                                   # the bedroom scene model + rendering,
                                   # and the v0.9 voice/language registry
    python3 -m companion --smoke   # scripted headless walkthrough of the
                                   # companion behaviour, including the
                                   # animated-world morning sequence (no
                                   # display; used by the manual smoke script)

Environment: ``ALWIN_HOME``, ``ALWIN_PERMITTED_ROOTS``,
``ALWIN_STATE_DIR`` (same as the CLI/task UI), plus the greeting
variables ``ALWIN_COMPANION_NAME`` and ``ALWIN_COMPANION_GREETING``.

Honesty notes (v0.9): microphone input, language-matched replies and
optional spoken output exist, but every engine is **opt-in and probed** —
capture needs a working microphone (push-to-talk, nothing recorded to
disk), recognition needs ``faster-whisper`` **plus** an explicitly
installed model (``scripts/install_voice_models.py``; nothing is ever
downloaded silently), speech needs ``espeakng-loader`` + an audio
device, and replies need ``--llm`` with a running local Ollama.  Any
missing piece degrades to an honest bubble, never a fake result.  No
automatic startup service is installed, and the companion never
plans/approves/executes anything itself.  The animated world is purely
visual: the scene can move Alwin around his room, but it never
executes, approves or modifies a task.
"""

from __future__ import annotations

import argparse
import sys
from typing import List, Optional

from ai_core import __version__

EXIT_OK = 0
EXIT_FAILURE = 1
EXIT_CONFIG = 2


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python3 -m companion",
        description=(
            "Alwin companion: a lightweight desktop character that "
            "reflects real task states with truthful animations."
        ),
    )
    parser.add_argument("--check", action="store_true",
                        help="headless self-check (no display); verifies "
                             "the state graph, adapter mapping, greeting "
                             "and 2D rendering, then exits")
    parser.add_argument("--smoke", action="store_true",
                        help="scripted headless walkthrough of the "
                             "companion behaviour (no display)")
    parser.add_argument("--no-greeting", action="store_true",
                        help="do not show the startup greeting this run")
    parser.add_argument("--llm", action="store_true",
                        help="attach the local Ollama client (used for the "
                             "manager's proposals when the task window "
                             "opens and for the companion's optional "
                             "spoken replies; still fully local)")
    parser.add_argument("--version", action="version",
                        version=f"alwin-companion {__version__}")
    return parser


# --------------------------------------------------------------------------- #
# Headless self-check
# --------------------------------------------------------------------------- #

def _run_check() -> int:
    from companion import states, renderer, presentation, settings
    from companion.adapter import TaskEventAdapter
    from companion.greeting import GreetingConfig, GreetingController
    from companion.voice import NullVoice, voice_status
    from companion.renderer2d import draw_character
    import cairo

    failures: List[str] = []

    def expect(condition: bool, what: str) -> None:
        if condition:
            print(f"  ok: {what}")
        else:
            failures.append(what)
            print(f"  FAIL: {what}")

    print("Companion self-check (headless)")
    expect(len(states.ALL_STATES) == 17, "17 character states defined")
    expect(len(states.TRANSITIONS) == 17, "every state has a transition set")
    expect(states.is_valid_transition("idle", "listening"),
           "idle -> listening is valid")
    expect(not states.is_valid_transition("sleeping", "working"),
           "sleeping -> working is correctly invalid")
    expect(states.FALLBACK_STATE in states.TRANSITIONS,
           "safe fallback state exists")

    # Renderer interface: headless renderer satisfies the contract.
    null = renderer.NullRenderer()
    expect(null.is_available, "null renderer available headless")
    null.show_state("thinking", expression="thoughtful")
    expect(null.state == "thinking", "null renderer accepts state changes")
    controller = states.CharacterController(null)
    controller.enter("waiting_for_approval")
    expect(null.state == "waiting_for_approval",
           "controller drives the renderer")
    try:
        controller.enter("nonsense-state")
        unknown_ok = True
    except Exception:  # noqa: BLE001 - entering an unknown state must never raise
        unknown_ok = False
    expect(unknown_ok, "unknown state request never raises")
    expect(controller.state in states.ALL_STATES,
           "character stayed in a known state after an unknown request")

    # Adapter mapping (approval + success + failure honesty).
    adapter = TaskEventAdapter(controller)

    class _Record:
        def __init__(self, state):
            self.state = state
            self.plan = {"action": "create_text_file", "summary": "s",
                         "target": "a.py"}
            self.steps = []
            self.task_id = "check"
            self.verification = None
            self.report_status = None
            self.error = None
            self.result_message = None

    for task_state, expected in (
        ("planning", "thinking"),
        ("awaiting_approval", "waiting_for_approval"),
        ("verifying", "verifying"),
        ("completed", "celebrating"),
        ("failed", "error"),
    ):
        adapter.on_task_update(_Record(task_state))
        expect(controller.state == expected,
               f"task {task_state} -> {expected}")

    # Greeting: configurable name, disabled path.
    greeting = GreetingController(GreetingConfig(enabled=False))
    expect(not greeting.should_greet(), "disabled greeting never fires")
    voice = NullVoice()
    expect(voice_status(voice)["available"] is False,
           "voice honestly reported unavailable")

    # Settings: closed schema sanitizes junk.
    clean = settings.sanitize({"bogus_key": 1, "greeting_enabled": "no"})
    expect("bogus_key" not in clean, "unknown settings keys are dropped")
    expect(clean["greeting_enabled"] is False,
           "string 'no' is sanitized to a bool")

    # Presentation: bounded, no approval affordance.
    view = presentation.bubble_view("idle", "x" * 500)
    expect(len(view["text"]) <= presentation.MAX_BUBBLE_CHARS,
           "speech bubbles are bounded")
    controls = presentation.controls_view(voice=voice)
    ids = {c["id"] for c in controls["controls"]}
    expect("approve" not in ids and "reject" not in ids,
           "companion exposes no approve/reject control")

    # 2D rendering: every state draws headless (placeholder artwork).
    drawn = 0
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 120, 130)
    for state in states.ALL_STATES:
        try:
            ctx = cairo.Context(surf)
            draw_character(ctx, 120, 130, state, t=0.5)
            drawn += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"drawing {state}: {exc}")
    expect(drawn == len(states.ALL_STATES),
           "all states render with the vector character")

    # --- v0.9 voice stack (headless: no device, no model, no network) ------ #
    from companion.language_registry import (
        LANGUAGES, SUPPORTED_CODES, choices_for_ui, supports_rtl,
    )
    from companion.language_detection import detect_language
    from companion.speech_recognition import MODEL_SIZES, model_status
    from companion.voice_output import CompanionVoice

    expect(len(LANGUAGES) == 15 and SUPPORTED_CODES == tuple(
        s.code for s in LANGUAGES),
        "closed registry defines exactly the 15 supported languages")
    expect(len(choices_for_ui()) == 15, "settings dropdown covers all 15")
    expect(supports_rtl("ur") and supports_rtl("ar")
           and not supports_rtl("en") and not supports_rtl("zh"),
           "right-to-left is limited to Urdu and Arabic")
    detection = detect_language(
        "வணக்கம், எப்படி இருக்கிறீர்கள்? என் உலாவியைத் திறக்க வேண்டும்."
    )
    if detection.code == "ta":
        expect(True, "language detection works headless (Tamil sample)")
    else:
        # Honest degradation: without langdetect only script/hint
        # evidence exists; the check reports it instead of failing.
        print("  note: text detection is degraded here "
              "(install langdetect for full detection)")
    expect(MODEL_SIZES == ("tiny", "base", "small", "medium"),
           "closed set of ASR model sizes")
    status = model_status("small")
    print(f"  note: speech model — {status['detail']}")
    voice_probe = CompanionVoice()
    print(f"  note: voice engine — {voice_probe.status}")
    expect(NullVoice().speak("hello") is False,
           "NullVoice never claims to have spoken")

    # --- v0.10 wake word (import + pattern + honest status, no I/O heavy)
    from companion.wake_word import (
        DEFAULT_PHRASE, phrase_pattern, wake_status,
    )
    import re as _re

    expect(DEFAULT_PHRASE == "Hey Alwin",
           "default wake phrase is 'Hey Alwin'")
    expect(_re.search(phrase_pattern(DEFAULT_PHRASE), "Hey, Alwin!",
                      _re.IGNORECASE) is not None,
           "acceptance pattern matches the phrase with punctuation")
    expect(_re.search(phrase_pattern(DEFAULT_PHRASE), "heyallwin",
                      _re.IGNORECASE) is None,
           "acceptance pattern requires word separation")
    wake_probe = wake_status(None)
    expect(isinstance(wake_probe.get("available"), bool)
           and len(str(wake_probe.get("detail", ""))) <= 300,
           "wake-word status is honest and bounded")
    print(f"  note: wake word — {wake_probe['detail']}")

    # v0.8 — the animated world: scene model, movement, and room render.
    from companion.scene import (
        NAMED_POSITIONS, SCENE_PHASES, SceneController, SceneTransform,
    )
    from companion import bedroom

    scene = SceneController()
    expect(scene.at_position("bed"), "scene starts asleep in bed")
    expect(bool(SCENE_PHASES), "scene phase vocabulary is defined")
    expect(set(NAMED_POSITIONS) >= {"bed", "beside_bed", "greeting", "desk"},
           "named positions exist")
    expect(scene.command("wake", now=0.0), "scene wake command works")
    expect(not scene.command("exec", now=0.0),
           "unknown scene command is refused (closed allowlist)")
    expect(not scene.command("move_to", position="moon", now=0.0),
           "unknown scene position is refused")
    try:
        rsurf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 480, 360)
        rctx = cairo.Context(rsurf)
        tr = SceneTransform.for_viewport(480, 360)
        view = scene.render_view(0.0)
        bedroom.draw_scene_back(rctx, view, 0.0, tr)
        bedroom.draw_scene_front(rctx, view, 0.0, tr)
        expect(True, "bedroom scene renders headless (cairo)")
    except Exception as exc:  # noqa: BLE001
        expect(False, f"bedroom scene renders headless ({exc})")

    if failures:
        print(f"CHECK FAILED ({len(failures)} problem(s))")
        return EXIT_FAILURE
    print("CHECK OK — companion core is healthy (headless).")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Headless scripted walkthrough
# --------------------------------------------------------------------------- #

def _run_smoke() -> int:
    import os
    import tempfile
    from pathlib import Path

    from companion import presentation, settings
    from companion.adapter import TaskEventAdapter
    from companion.greeting import (
        GreetingConfig, GreetingController, in_quiet_hours,
        parse_quiet_hours, sanitize_name,
    )
    from companion.renderer import NullRenderer
    from companion.renderer2d import AssetStore, draw_character
    from companion.states import ALL_STATES, CharacterController, STATE_IDLE
    from companion.voice import NullVoice, voice_status
    import cairo

    failures: List[str] = []

    def expect(condition: bool, what: str) -> None:
        if condition:
            print(f"  ok: {what}")
        else:
            failures.append(what)
            print(f"  FAIL: {what}")

    print("Companion smoke (headless, no window)")

    # --- state machine ---------------------------------------------------- #
    renderer_ = NullRenderer()
    controller = CharacterController(renderer_)
    expect(controller.state == STATE_IDLE, "starts calm and idle")
    expect(controller.enter("greeting") is False,
           "idle -> greeting is an invalid edge (rejected honestly)")
    expect(controller.state == STATE_IDLE,
           "invalid request from a calm state changes nothing")
    controller.force("sleeping", reason="startup")
    expect(controller.state == "sleeping", "force() drives the wake-up path")
    controller.force("waking_up")
    controller.enter("greeting")
    expect(controller.state == "greeting", "wake -> greeting reached")

    # --- truthful task mapping (happy path) ------------------------------- #
    adapter = TaskEventAdapter(controller, on_speech=lambda t: None)

    class R:
        state = ""
        plan = {"action": "create_text_file", "summary": "script",
                "target": "app.py"}
        steps = []
        task_id = "smoke-1"
        verification = None
        report_status = None
        error = None
        result_message = None

    happy = []
    for task_state in ("planning", "awaiting_approval", "approved",
                       "executing", "verifying", "completed"):
        rec = R()
        rec.state = task_state
        adapter.on_task_update(rec)
        happy.append((task_state, controller.state))
    expect(happy[-1] == ("completed", "celebrating"),
           "verified completion drives celebrating")
    expect(("awaiting_approval", "waiting_for_approval") in happy,
           "approval request maps to waiting_for_approval")
    expect(("executing", "coding") in happy,
           "code-file execution maps to coding (reliably identified)")

    # --- failure never celebrates ---------------------------------------- #
    controller.force(STATE_IDLE)
    renderer_.calls.clear()       # observe only the failure path below
    for task_state in ("planning", "executing", "failed"):
        rec = R()
        rec.state = task_state
        adapter.on_task_update(rec)
    expect(controller.state == "error", "failed task maps to error")
    expect(renderer_.state != "celebrating",
           "a failed task never rendered celebrating")
    seen = [c[1] for c in renderer_.calls if c[0] == "state"]
    expect("celebrating" not in seen, "celebrating never sent to renderer "
                                      "on the failure path")

    # --- renderer failure cannot affect presentation ---------------------- #
    def _boom(*_a, **_k):
        raise RuntimeError("animation exploded")

    renderer_.show_state = _boom
    rec = R()
    rec.state = "completed"
    adapter.on_task_update(rec)   # must not raise
    expect(bool(controller.renderer_errors),
           "renderer exceptions are captured, not raised")
    rec.state = "failed"
    adapter.on_task_update(rec)   # still working after the animation bug
    expect(controller.state == "error",
           "adapter keeps mapping after renderer exceptions")

    # --- greeting rules ---------------------------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        marker = Path(tmp) / "greeted.json"
        env = {"ALWIN_COMPANION_NAME": "Ada",
               "ALWIN_COMPANION_GREETING": "1"}
        config = GreetingConfig.from_env(env, marker_path=marker)
        greeting = GreetingController(config)
        expect(greeting.should_greet(), "greeting allowed on first check")
        expect("Ada" in greeting.text(), "greeting uses the given name")
        greeting.mark_greeted()
        expect(not greeting.should_greet(),
               "second check in the same session does not re-greet")
        greeting2 = GreetingController(config)   # new session, same day
        expect(not greeting2.should_greet(),
               "once-per-day marker prevents a second greeting")
        expect(marker.is_file(), "once-per-day marker written")

        quiet = GreetingController(GreetingConfig.from_env(
            {"ALWIN_COMPANION_NAME": "Ada",
             "ALWIN_COMPANION_QUIET_HOURS": "23:00-07:00"}))
        from datetime import datetime, time as dtime
        quiet._now = lambda: datetime(2026, 1, 1, 2, 30)  # 02:30
        expect(not quiet.should_greet(), "quiet hours suppress the greeting")

        disabled = GreetingController(GreetingConfig(enabled=False))
        expect(not disabled.should_greet(), "disabled preference honored")

        muted_env = {"ALWIN_COMPANION_GREETING": "off",
                     "ALWIN_COMPANION_NAME": "Ada"}
        expect(not GreetingConfig.from_env(muted_env).enabled,
               "'off' env value disables the greeting")
        expect(sanitize_name("  A   B  ") == "A B", "names are normalized")
        expect(sanitize_name("<b>x</b>") != "",
               "name survives sanitization as plain text")
        expect(in_quiet_hours(dtime(3, 0), parse_quiet_hours("23:00-07:00")),
               "quiet window wraps midnight")
        try:
            parse_quiet_hours("nonsense")
            malformed_ok = False
        except ValueError:
            malformed_ok = True
        expect(malformed_ok, "malformed quiet hours raise ValueError")

    # --- settings round trip ------------------------------------------------ #
    with tempfile.TemporaryDirectory() as tmp:
        path = settings.path_for(Path(tmp))
        settings.save(path, {"greeting_enabled": False, "junk": 1})
        loaded = settings.load(path)
        expect(loaded["greeting_enabled"] is False, "settings persist")
        expect("junk" not in loaded, "unknown settings keys never persist")
        mode = oct(path.stat().st_mode & 0o777)
        expect(mode == "0o600", f"settings file is owner-only ({mode})")
        path.write_text("{not json", encoding="utf-8")
        expect(settings.load(path) == settings.defaults(),
               "corrupt settings fall back to defaults")

    # --- assets: missing assets fall back --------------------------------- #
    with tempfile.TemporaryDirectory() as tmp:
        store = AssetStore(Path(tmp))
        expect(store.available_states() == [],
               "no assets on disk -> procedural rendering used")
        expect(store.texture_for("idle") is None,
               "missing asset resolves to None (fallback, never a crash)")

    # --- rendering: all states draw headless ------------------------------- #
    surf = cairo.ImageSurface(cairo.FORMAT_ARGB32, 160, 170)
    drawn = 0
    for state in ALL_STATES:
        try:
            ctx = cairo.Context(surf)
            draw_character(ctx, 160, 170, state, expression="neutral", t=1.0)
            drawn += 1
        except Exception as exc:  # noqa: BLE001
            failures.append(f"draw {state}: {exc}")
    expect(drawn == len(ALL_STATES), "vector character draws every state")

    # --- v0.8 animated world: morning sequence + scene-driven tasks -------- #
    from companion.scene import SceneController

    scene = SceneController()
    expect(scene.at_position("bed"), "scene: Alwin starts asleep in bed")
    scene.command("wake", now=0.0)
    expect(scene.phase == "bedroom_waking", "scene: wake begins")
    scene.command("get_up", now=0.5)
    for tt in (0.6, 1.0, 1.4, 1.6):
        scene.tick(tt)
    expect(scene.at_position("beside_bed"), "scene: got up beside the bed")
    scene.command("move_to", position="greeting", now=2.0)
    events = []
    for tt in (2.3, 2.7, 3.1):
        events.extend(scene.tick(tt))
    expect("arrived_at_greeting" in events, "scene: arrival event fired once")
    expect(scene.at_position("greeting"), "scene: at the greeting spot")
    scene.command("begin_coding", now=4.0)
    for tt in (4.5, 5.0, 5.5, 6.0):
        scene.tick(tt)
    expect(scene.phase == "coding", "scene: coding at the desk")
    # the scene never executes/approves: the allowlist is closed and
    # invalid destinations are refused without moving him.
    expect(not scene.command("rm", now=7.0),
           "scene: unknown command refused")
    expect(not scene.command("move_to", position="moon", now=7.0),
           "scene: bad position refused")

    # --- presentation honesty ------------------------------------------------ #
    expect(len(presentation.bubble_view("idle", "y" * 999)["text"])
           <= presentation.MAX_BUBBLE_CHARS, "bubble text is bounded")
    status_view = presentation.task_status_view(None)
    expect(status_view["present"] is False, "no task -> honest empty status")
    voice = NullVoice()
    expect(voice.speak("hello") is False,
           "the no-engine voice never claims to have spoken")
    expect(voice_status(voice)["available"] is False,
           "voice absence is reported honestly")

    if failures:
        print(f"SMOKE FAILED ({len(failures)} check(s))")
        return EXIT_FAILURE
    print("SMOKE OK — companion behaves correctly (headless).")
    return EXIT_OK


# --------------------------------------------------------------------------- #
# Window mode
# --------------------------------------------------------------------------- #

def _run_window(no_greeting: bool, use_llm: bool = False) -> int:  # pragma: no cover - needs display
    from ai_core.config import ConfigError, load_config
    from taskui.manager import TaskManager
    from taskui.status import collect_status

    from companion.window import CompanionWindow, Gtk
    from companion.greeting import GreetingConfig

    if Gtk is None:
        print(
            "GTK4 (PyGObject with Gtk 4.0) is not available on this "
            "system, so the companion window cannot open.\n"
            "Install it with your package manager (Debian/Ubuntu: "
            "'sudo apt install python3-gi gir1.2-gtk-4.0') or run the "
            "headless check:  python3 -m companion --check",
            file=sys.stderr,
        )
        return EXIT_FAILURE

    try:
        config = load_config()
    except ConfigError as exc:
        print(f"Configuration error: {exc}", file=sys.stderr)
        return EXIT_CONFIG

    status = collect_status(config.ollama_base_url, config.ollama_model)
    greeting_config = GreetingConfig(enabled=not no_greeting)

    # v0.9: the task engine is built eagerly (not on first window
    # open) so microphone *and* typed requests can be submitted right
    # away — constructing a manager starts no task; it only makes the
    # single trusted submission path available.  ``--llm`` attaches
    # the local Ollama client for proposals and optional replies.
    llm_client = None
    if use_llm:
        from ai_runtime.ollama_client import OllamaClient

        llm_client = OllamaClient(
            base_url=config.ollama_base_url,
            model=config.ollama_model,
            timeout=config.ollama_timeout,
        )
    manager = TaskManager(config, use_llm=use_llm, llm_client=llm_client)

    companion = CompanionWindow(
        manager,
        config=config,
        status=status,
        llm_client=llm_client,
        greeting_config=greeting_config,
    )

    # "Open task window" builds the trusted task UI lazily *in this same
    # process/loop* — one application, two windows, one engine (the
    # engine above is shared, never a second one).
    holder = {}

    def open_task_window() -> None:
        if "task" not in holder:
            from taskui.gtk_app import TaskWindow

            holder["task"] = TaskWindow(
                manager, status=status, application=companion.app
            )
        holder["task"].open(companion.app)

    companion.open_task_window = open_task_window
    return companion.run()


def main(argv: Optional[List[str]] = None) -> int:
    args = _build_parser().parse_args(argv)
    if args.check:
        return _run_check()
    if args.smoke:
        return _run_smoke()
    return _run_window(no_greeting=args.no_greeting, use_llm=args.llm)


if __name__ == "__main__":
    raise SystemExit(main())
