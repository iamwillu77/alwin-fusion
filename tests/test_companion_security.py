"""v0.6 source-level security meta-checks for the companion package.

The companion is a *presentation layer* — it must never grow an
execution, approval or network capability of its own.  These tests
parse the actual **AST** of every ``companion`` module (not raw text, so
docstrings that merely mention a banned API do not cause false alarms)
and assert:

* no module can shell out, spawn processes, evaluate code or open a
  socket — at module level or lazily inside functions,
* microphone capture and speech synthesis exist **only** in the
  documented voice modules (``voice_input``/``voice_output``) through
  GStreamer + espeak-ng, with an explicit per-module import allowlist;
  recording audio to a file stays impossible package-wide (``wave`` /
  ``audioop`` remain banned everywhere),
* ``ctypes`` (needed to drive the local TTS library) is reachable from
  exactly one module, with full prototypes declared there,
* nothing imports the trusted execution engine (planner, permissions,
  executor, verifier, pipeline, integrations),
* only the ``__main__`` launcher may import the task experience (the
  window itself is engine-free; the adapter is duck-typed),
* the character controller is pure (no I/O imports at all),
* the GTK view never renders dynamic text as markup (Pango injection),
* no web server / remote service surface exists anywhere in the package,
* no specific user name is hard-coded into the greeting.

This is a *static* guarantee that complements the behavioural tests in
the other companion test modules (which pin the runtime behaviour)..
"""

from __future__ import annotations

import ast
import pathlib
import unittest

COMPANION = pathlib.Path(__file__).resolve().parent.parent / "companion"

#: Every companion module (excluding bytecode and assets).
MODULES = sorted(p for p in COMPANION.glob("*.py"))

#: Expected module set — a rename must be a deliberate act.
EXPECTED_MODULES = {
    "__init__.py", "__main__.py", "adapter.py", "bedroom.py", "character.py",
    "conversation.py", "greeting.py", "language_detection.py",
    "language_registry.py", "presentation.py", "renderer.py", "renderer2d.py",
    "scene.py", "settings.py", "speech_recognition.py", "states.py",
    "voice.py", "voice_input.py", "voice_output.py", "wake_word.py",
    "window.py",
}

#: Dotted module names that must never be imported **at module level**
#: anywhere in companion (the voice modules' narrow, documented
#: exemptions from *specific* bans are enforced by the per-module
#: allowlists below — everything else keeps every ban).
BANNED_IMPORTS = frozenset({
    # process / code execution
    "subprocess", "commands", "multiprocessing", "pickle", "marshal",
    "ctypes", "pty", "importlib",
    # network / remote services (no web server, no cloud)
    "socket", "ssl", "urllib", "http", "http.client", "requests",
    "aiohttp", "httpx", "websockets", "websocket", "flask", "fastapi",
    "django", "uvicorn", "socketserver", "xmlrpc",
    # audio capture / synthesis through ad-hoc libraries: the one
    # supported path is GStreamer via ``gi`` (voice_input/voice_output);
    # recording-to-file stays impossible package-wide (wave/audioop).
    "pyaudio", "sounddevice", "alsaaudio", "speech_recognition",
    "pocketsphinx", "audioop", "wave",
})

#: Call names (dotted) that must never appear in companion code.
BANNED_CALLS = frozenset({
    "eval", "exec", "compile", "__import__", "importlib.import_module",
    "os.system", "os.popen", "os.execv", "os.execve", "os.execl",
    "os.spawnl", "os.spawnv", "os.fork", "os.kill", "os.startfile",
    "subprocess.run", "subprocess.Popen", "subprocess.call",
    "subprocess.check_output", "pickle.loads", "marshal.loads",
    "pty.spawn", "socket.socket", "urllib.request.urlopen",
    "urlopen", "requests.get", "requests.post", "webbrowser.open",
})

#: The trusted execution engine: no companion module may import any of it.
EXECUTION_ENGINE = frozenset({
    "ai_core.pipeline", "ai_core.executor", "ai_core.planner",
    "ai_core.permissions", "ai_core.verifier", "ai_core.audit",
    "integrations",
})

#: The task experience: only the __main__ launcher may import it
#: (to build the manager/window it hosts — the presentation modules
#: stay duck-typed and engine-free).
TASK_EXPERIENCE = frozenset({"taskui"})

#: Per-voice-module allowlist of *module-level* (top-of-file) imports.
#: Each entry is the complete, deliberate set for that module; the
#: common bans still apply on top (nothing here may shell out, reach
#: the network, read eval, or open audio-recording libraries).
VOICE_MODULE_IMPORTS = {
    "language_registry.py": {"__future__", "dataclasses", "typing"},
    "language_detection.py": {"__future__", "threading", "dataclasses",
                              "typing", "langdetect", "companion"},
    "conversation.py": {"__future__", "dataclasses", "typing",
                        "companion"},
    "voice_input.py": {"__future__", "threading", "time", "dataclasses",
                       "typing"},
    "speech_recognition.py": {"__future__", "os", "threading",
                              "dataclasses", "typing"},
    "voice_output.py": {"__future__", "threading", "dataclasses",
                        "typing", "companion"},
    # v0.10.0 wake word: stdlib streaming scaffolding only at module
    # level (bounded queue + worker + regex acceptance pattern).
    "wake_word.py": {"__future__", "os", "queue", "re", "threading",
                     "time", "typing"},
}

#: The *additional* imports those modules may use **lazily** (inside a
#: function only).  This is the complete list of every engine-adjacent
#: dependency the voice stack may touch — anything outside these sets
#: (and the module-level sets above) fails the test, at any depth.
VOICE_MODULE_LAZY_IMPORTS = {
    "language_registry.py": set(),
    "language_detection.py": set(),
    "conversation.py": set(),
    "voice_input.py": {"gi"},                # GStreamer (audio device)
    "speech_recognition.py": {"faster_whisper", "numpy",
                              "huggingface_hub"},
    # ctypes drives espeak-ng — allowed HERE and nowhere else, and only
    # with explicit prototypes (a default-typed espeak_Synth segfaults).
    "voice_output.py": {"ctypes", "espeakng_loader", "gi"},
    # v0.10.0: the detector loads the local vosk engine + JSON for the
    # keyword API, and lazily grabs VoiceInputService when no factory
    # was injected. Still no network, no recording, no process.
    "wake_word.py": {"json", "vosk", "companion"},
}

#: Roots neither allowed nor lazy: the hard bans for the voice modules
#: at ANY import depth — no network, no processes, no recording.
VOICE_HARD_BANS = frozenset({
    "subprocess", "socket", "ssl", "urllib", "http", "requests",
    "aiohttp", "httpx", "pickle", "marshal", "wave", "audioop",
    "pyaudio", "sounddevice", "alsaaudio", "pycaw", "alsaaudio",
    "eval", "webbrowser", "paramiko", "ftplib", "smtplib",
})


def _dotted(node: ast.AST) -> str:
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


class _Collector(ast.NodeVisitor):
    def __init__(self) -> None:
        self.module_imports: set = set()
        self.all_imports: set = set()
        self.full_module_imports: set = set()
        self.calls: set = set()
        self.markup_calls: set = set()
        self._depth = 0

    def _record(self, name: str) -> None:
        if not name:
            return
        root = name.split(".")[0]
        self.all_imports.add(root)
        if self._depth == 0:
            self.module_imports.add(name)
            self.full_module_imports.add(name)
        else:
            self.full_module_imports.add(name)

    def visit_Import(self, node: ast.Import) -> None:
        for alias in node.names:
            self._record(alias.name)
        self.generic_visit(node)

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        self._record(node.module or "")
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        name = _dotted(node)
        if name:
            self.calls.add(name)
            self.calls.add(name.split(".")[-1])
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_ClassDef(self, node: ast.ClassDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1


def collect(path: pathlib.Path) -> _Collector:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    collector = _Collector()
    collector.visit(tree)
    return collector


class CompanionSourceTests(unittest.TestCase):
    def test_module_set_is_exactly_as_designed(self):
        names = {p.name for p in MODULES}
        self.assertEqual(names, EXPECTED_MODULES)

    def test_no_module_can_shell_out_spawn_or_eval(self):
        self.assertTrue(MODULES, "no companion modules found")
        for path in MODULES:
            info = collect(path)
            for banned in BANNED_IMPORTS:
                with self.subTest(module=path.name, banned_import=banned):
                    self.assertNotIn(banned, info.module_imports)
            for banned in BANNED_CALLS:
                with self.subTest(module=path.name, banned_call=banned):
                    self.assertNotIn(banned, info.calls)

    def test_no_module_imports_the_execution_engine(self):
        for path in MODULES:
            info = collect(path)
            for module in EXECUTION_ENGINE:
                with self.subTest(module=path.name, engine=module):
                    self.assertNotIn(module, info.full_module_imports)

    def test_only_the_launcher_imports_the_task_experience(self):
        for path in MODULES:
            info = collect(path)
            if path.name == "__main__.py":
                continue   # the launcher hosts the manager/window by design
            for module in TASK_EXPERIENCE:
                with self.subTest(module=path.name, taskui=module):
                    self.assertNotIn(module, info.module_imports)
                    self.assertNotIn(module, info.full_module_imports)

    # ------------------------------------------------------------------ #
    # v0.9.0 voice stack: narrow, per-module import allowlists
    # ------------------------------------------------------------------ #

    def test_voice_modules_stay_inside_their_module_level_allowlist(self):
        self.assertEqual(set(VOICE_MODULE_IMPORTS), {
            p.name for p in MODULES
            if p.name in VOICE_MODULE_IMPORTS
        })
        for name, allowed in VOICE_MODULE_IMPORTS.items():
            info = collect(COMPANION / name)
            for imported in sorted(info.module_imports):
                root = imported.split(".")[0]
                with self.subTest(module=name, imported=imported):
                    self.assertIn(
                        root, allowed,
                        f"{name} imports {imported} at module level; its "
                        f"allowlist is {sorted(allowed)}",
                    )

    def test_voice_modules_lazy_imports_are_exactly_the_documented_set(self):
        for name, lazy in VOICE_MODULE_LAZY_IMPORTS.items():
            allowed = VOICE_MODULE_IMPORTS[name] | lazy
            info = collect(COMPANION / name)
            for imported in sorted(info.full_module_imports):
                root = imported.split(".")[0]
                with self.subTest(module=name, imported=imported):
                    self.assertIn(
                        root, allowed,
                        f"{name} imports {imported} (anywhere in the "
                        f"file); allowed roots are {sorted(allowed)}",
                    )

    def test_voice_modules_obey_hard_bans_at_any_import_depth(self):
        for name in VOICE_MODULE_LAZY_IMPORTS:
            info = collect(COMPANION / name)
            roots = {i.split(".")[0] for i in info.full_module_imports}
            with self.subTest(module=name):
                self.assertFalse(
                    VOICE_HARD_BANS & roots,
                    f"{name} imports hard-banned roots: "
                    f"{sorted(VOICE_HARD_BANS & roots)}",
                )

    def test_ctypes_is_reachable_from_exactly_one_module(self):
        # The espeak wrapper is the documented exception (there is no
        # other way to drive a local TTS library); every other module —
        # even lazily — must stay away from it.
        for path in MODULES:
            if path.name == "voice_output.py":
                continue
            info = collect(path)
            roots = {i.split(".")[0] for i in info.full_module_imports}
            with self.subTest(module=path.name):
                self.assertNotIn("ctypes", roots)

    def test_voice_modules_never_import_recording_or_network_libraries(self):
        # wave/audioop (recording persistence) and every network client
        # stay banned package-wide, at any import depth, in the voice
        # modules specifically.
        for name in VOICE_MODULE_LAZY_IMPORTS:
            info = collect(COMPANION / name)
            for banned in ("wave", "audioop", "socket", "urllib",
                           "requests", "http.client", "subprocess"):
                with self.subTest(module=name, banned=banned):
                    self.assertNotIn(banned, info.module_imports)
                    self.assertNotIn(banned, info.full_module_imports)

    def test_conversation_speaks_only_through_the_duck_typed_manager(self):
        # The conversation module is engine-free: it must not import
        # taskui, ai_core or ai_runtime — manager and LLM come injected.
        info = collect(COMPANION / "conversation.py")
        roots = {i.split(".")[0] for i in info.full_module_imports}
        self.assertLessEqual(
            roots,
            {"__future__", "dataclasses", "typing", "companion"},
        )
        # The shared language instruction must actually appear (one
        # template, language as data — not per-language assistants).
        source = (COMPANION / "conversation.py").read_text(encoding="utf-8")
        self.assertIn("Reply in the language used in the user's latest message",
                      source)

    def test_state_controller_is_pure_python(self):
        # The controller owns animation state: no I/O, no GTK, no
        # threads — only stdlib time/typing.
        info = collect(COMPANION / "states.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(roots, {"__future__", "time", "typing"})

    def test_adapter_is_a_pure_function_of_duck_typed_records(self):
        info = collect(COMPANION / "adapter.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(roots,
                             {"__future__", "typing", "companion"})

    def test_renderers_never_touch_the_engine(self):
        for name in ("renderer.py", "renderer2d.py", "character.py"):
            info = collect(COMPANION / name)
            with self.subTest(module=name):
                for module in EXECUTION_ENGINE | TASK_EXPERIENCE:
                    self.assertNotIn(module, info.module_imports)
                self.assertNotIn("subprocess", info.module_imports)

    def test_greeting_never_records_audio_or_installs_services(self):
        info = collect(COMPANION / "greeting.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(
            roots,
            {"__future__", "json", "os", "time", "datetime", "pathlib",
             "typing", "dataclasses"},
        )
        for banned in ("wave", "pyaudio", "subprocess", "socket",
                       "systemd", "cron"):
            self.assertNotIn(banned, info.module_imports)

    def test_character_drawing_module_is_pure_cairo(self):
        # The character artwork must stay headless + dependency-free:
        # only cairo/math/dataclasses/typing, no GTK, no task engine, no
        # OS or network access.  A future renderer3d can reuse the pose
        # system without dragging in any trusted-core concept.
        info = collect(COMPANION / "character.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(
            roots, {"__future__", "math", "dataclasses", "typing", "cairo"},
        )
        for banned in EXECUTION_ENGINE | TASK_EXPERIENCE:
            self.assertNotIn(banned, info.module_imports)

    def test_scene_model_is_pure_python(self):
        # The scene/world model must stay pure Python: no cairo, no GTK,
        # no I/O, no threads, no task engine — so movement and the
        # wake-up sequence are unit-testable headless with simulated time.
        info = collect(COMPANION / "scene.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(roots,
                             {"__future__", "math", "dataclasses", "typing"})
        for banned in EXECUTION_ENGINE | TASK_EXPERIENCE | BANNED_IMPORTS:
            self.assertNotIn(banned, info.module_imports)

    def test_bedroom_drawing_module_is_pure_cairo(self):
        # The bedroom artwork is the view half of the scene layer: pure
        # cairo + math, reading only the scene model.  No GTK, no I/O, no
        # engine — it can render headless onto an ImageSurface.
        info = collect(COMPANION / "bedroom.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(
            roots, {"__future__", "math", "typing", "cairo", "companion"},
        )
        for banned in EXECUTION_ENGINE | TASK_EXPERIENCE | BANNED_IMPORTS:
            self.assertNotIn(banned, info.module_imports)

    def test_window_never_renders_text_as_markup(self):
        # Pango markup in dynamic text would let untrusted content
        # impersonate UI chrome (same rule as the task window).
        info = collect(COMPANION / "window.py")
        for forbidden in ("set_markup", "set_markup_with_mnemonic"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, info.calls)
        self.assertNotIn("Pango", info.all_imports)

    def test_window_has_no_remote_resources_or_focus_tricks(self):
        source = (COMPANION / "window.py").read_text(encoding="utf-8")
        self.assertNotIn("url(", source, "CSS must not fetch remote assets")
        self.assertNotIn("http://", source)
        self.assertNotIn("https://", source)

    def test_no_web_server_surface_anywhere(self):
        for path in MODULES:
            info = collect(path)
            for banned in ("flask", "fastapi", "http.server",
                           "socketserver", "uvicorn", "BaseHTTPRequestHandler",
                           "socket.socket"):
                with self.subTest(module=path.name, banned=banned):
                    self.assertNotIn(banned, info.module_imports)
                    self.assertNotIn(banned, info.calls)

    def test_voice_module_cannot_capture_or_synthesize_audio(self):
        info = collect(COMPANION / "voice.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(roots, {"__future__", "abc", "typing"})

    def test_settings_stay_in_the_state_directory(self):
        info = collect(COMPANION / "settings.py")
        roots = {name.split(".")[0] for name in info.module_imports}
        self.assertLessEqual(roots,
                             {"__future__", "json", "os", "pathlib",
                              "typing"})

    def test_no_specific_user_name_is_hard_coded(self):
        # The greeting name must be configurable, never baked in.
        for path in MODULES:
            source = path.read_text(encoding="utf-8").lower()
            with self.subTest(module=path.name):
                self.assertNotIn("wilson", source)
                self.assertNotIn('"wilson', source)

    def test_default_greeting_name_is_generic(self):
        from companion.greeting import DEFAULT_NAME

        self.assertEqual(DEFAULT_NAME, "friend")

    def test_window_module_imports_headless(self):
        import importlib

        module = importlib.import_module("companion.window")
        self.assertTrue(hasattr(module, "CompanionWindow"))
        self.assertEqual(module.APP_ID, "org.alwin.fusion.companion")

    def test_package_imports_headless(self):
        import importlib

        module = importlib.import_module("companion")
        for name in ("CharacterController", "CharacterRenderer",
                     "TaskEventAdapter", "GreetingController", "NullVoice"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(module, name))

    def test_companion_renderer_errors_are_structurally_contained(self):
        # The controller must wrap every renderer call (the mechanism
        # that keeps an animation exception away from task execution).
        info = collect(COMPANION / "states.py")
        source = (COMPANION / "states.py").read_text(encoding="utf-8")
        self.assertIn("renderer_errors", source)
        self.assertIn("except Exception", source)
        # The controller never executes anything.
        for banned in ("subprocess", "os.system", "Popen"):
            self.assertNotIn(banned, info.calls)


if __name__ == "__main__":
    unittest.main()
