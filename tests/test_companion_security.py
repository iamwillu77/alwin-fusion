"""v0.6 source-level security meta-checks for the companion package.

The companion is a *presentation layer* — it must never grow an
execution, approval or network capability of its own.  These tests
parse the actual **AST** of every ``companion`` module (not raw text, so
docstrings that merely mention a banned API do not cause false alarms)
and assert:

* no module can shell out, spawn processes, evaluate code, open a
  socket or reach for a microphone/audio device,
* nothing imports the trusted execution engine (planner, permissions,
  executor, verifier, pipeline, integrations),
* only the ``__main__`` launcher may import the task experience (the
  window itself is engine-free; the adapter is duck-typed),
* the character controller is pure (no I/O imports at all),
* the GTK view never renders dynamic text as markup (Pango injection),
* no web server / remote service surface exists anywhere in the package,
* no specific user name is hard-coded into the greeting.

This is a *static* guarantee that complements the behavioural tests in
the other companion test modules.
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
    "greeting.py", "presentation.py", "renderer.py", "renderer2d.py",
    "scene.py", "settings.py", "states.py", "voice.py", "window.py",
}

#: Dotted module names that must never be imported anywhere in companion.
BANNED_IMPORTS = frozenset({
    # process / code execution
    "subprocess", "commands", "multiprocessing", "pickle", "marshal",
    "ctypes", "pty", "importlib",
    # network / remote services (no web server, no cloud)
    "socket", "ssl", "urllib", "http", "http.client", "requests",
    "aiohttp", "httpx", "websockets", "websocket", "flask", "fastapi",
    "django", "uvicorn", "socketserver", "xmlrpc",
    # audio capture / synthesis (no microphone, no fake voice engine)
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
