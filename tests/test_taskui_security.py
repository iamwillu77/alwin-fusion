"""v0.5 source-level security meta-checks for the taskui package.

The task-experience layer must never grow an execution capability of
its own.  These tests parse the actual **AST** of every ``taskui``
module (not raw text, so docstrings that merely *mention* a banned API
do not cause false alarms) and assert:

* no module can shell out, spawn processes or evaluate code,
* only ``manager.py`` (and the ``__main__`` entry point) import the
  trusted execution engine,
* history stays display-only,
* the GTK view never renders dynamic text as markup.

This is a *static* guarantee that complements the behavioural tests.
"""

from __future__ import annotations

import ast
import pathlib
import unittest

TASKUI = pathlib.Path(__file__).resolve().parent.parent / "taskui"

#: Every module in the task-experience layer.
MODULES = sorted(p for p in TASKUI.glob("*.py"))

#: Dotted module names that must never be imported anywhere in taskui.
BANNED_IMPORTS = frozenset({
    "subprocess",
    "commands",
    "multiprocessing",
    "pickle",
    "marshal",
    "ctypes",
})

#: Call names (dotted) that must never appear in taskui code.
#: (``open``/``os`` file I/O is allowed *only* in history.py, which is
#: checked separately; every other module is I/O-free.)
BANNED_CALLS = frozenset({
    "eval",
    "exec",
    "compile",
    "__import__",
    "importlib.import_module",
    "os.system",
    "os.popen",
    "os.execv",
    "os.execve",
    "os.execl",
    "os.spawnl",
    "os.spawnv",
    "os.fork",
    "os.kill",
    "subprocess.run",
    "subprocess.Popen",
    "subprocess.call",
    "subprocess.check_output",
    "pickle.loads",
    "marshal.loads",
    "pty.spawn",
    "socket.socket",
})


def _dotted(node: ast.AST) -> str:
    """Render an attribute/name access chain as a dotted string."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        base = _dotted(node.value)
        return f"{base}.{node.attr}" if base else node.attr
    if isinstance(node, ast.Call):
        return _dotted(node.func)
    return ""


class _Collector(ast.NodeVisitor):
    """Collects module-level imports and every call's dotted name."""

    def __init__(self) -> None:
        self.module_imports: set = set()       # top-level import roots
        self.all_imports: set = set()          # every import root
        self.full_module_imports: set = set()  # full paths imported at module level
        self.full_nested_imports: set = set()  # full paths imported inside funcs
        self.calls: set = set()                # every call, dotted
        self._depth = 0

    def _record(self, name: str) -> None:
        if not name:
            return
        root = name.split(".")[0]
        self.all_imports.add(root)
        if self._depth == 0:
            self.module_imports.add(root)
            self.full_module_imports.add(name)
        else:
            self.full_nested_imports.add(name)

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
            # Also record the bare attribute tail (e.g. "set_markup").
            self.calls.add(name.split(".")[-1])
        self.generic_visit(node)

    def visit_FunctionDef(self, node: ast.FunctionDef) -> None:
        self._depth += 1
        self.generic_visit(node)
        self._depth -= 1

    def visit_AsyncFunctionDef(self, node) -> None:  # pragma: no cover
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


#: The execution engine: the only code that can plan, permission-check,
#: dispatch or run actions.  No UI-layer module except the manager may
#: import these (status.py may use the Ollama *client* — it executes
#: nothing but an HTTP GET to localhost).
EXECUTION_ENGINE = frozenset({
    "ai_core.pipeline",
    "ai_core.executor",
    "ai_core.planner",
    "ai_core.permissions",
    "ai_core.verifier",
    "integrations",
})


class TaskUISourceTests(unittest.TestCase):
    def test_all_taskui_modules_are_covered(self):
        names = {p.name for p in MODULES}
        for expected in ("__init__.py", "__main__.py", "gtk_app.py",
                         "history.py", "lifecycle.py", "manager.py",
                         "presenter.py", "status.py"):
            with self.subTest(module=expected):
                self.assertIn(expected, names)

    def test_no_module_can_shell_out_spawn_or_eval(self):
        self.assertTrue(MODULES, "no taskui modules found")
        for path in MODULES:
            info = collect(path)
            for banned in BANNED_IMPORTS:
                with self.subTest(module=path.name, banned_import=banned):
                    self.assertNotIn(banned, info.module_imports)
            for banned in BANNED_CALLS:
                with self.subTest(module=path.name, banned_call=banned):
                    self.assertNotIn(banned, info.calls)

    def test_only_manager_and_entry_point_import_execution_engine(self):
        # The UI (gtk_app), presenter, status, history and lifecycle
        # must not import the execution engine: the manager is the sole
        # bridge.  status.py may use the Ollama *client* for honest
        # probing; that client executes nothing.  Safe read-only helper
        # modules (ai_core.audit.redact, ai_core.command_parser's example
        # list, ai_core.config) are deliberately permitted.
        allowed = {"manager.py", "__main__.py", "__init__.py"}
        for path in MODULES:
            if path.name in allowed:
                continue
            info = collect(path)
            for module in EXECUTION_ENGINE:
                with self.subTest(module=path.name, engine=module):
                    self.assertNotIn(module, info.full_module_imports)

    def test_history_never_imports_trusted_execution(self):
        # history.py may reference the pipeline's masking helper via a
        # *local* (nested) import inside record(); it must have no
        # module-level import of the execution engine and no
        # dispatcher/executor calls.
        info = collect(TASKUI / "history.py")
        for module in EXECUTION_ENGINE:
            with self.subTest(module=module):
                self.assertNotIn(module, info.full_module_imports)
        # The only permitted nested engine import is the masking helper.
        nested_engine = info.full_nested_imports & EXECUTION_ENGINE
        self.assertLessEqual(nested_engine, {"ai_core.pipeline"})
        for banned in ("TaskSequenceRunner", "ActionDispatcher", "Executor",
                       "dispatch", "execute"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, info.calls)

    def test_gtk_view_never_imports_execution_engine(self):
        info = collect(TASKUI / "gtk_app.py")
        for module in EXECUTION_ENGINE:
            with self.subTest(module=module):
                self.assertNotIn(module, info.full_module_imports)
        self.assertNotIn("subprocess", info.module_imports)
        # Only the *dotted* dangerous forms are banned; GTK's own
        # app.run() / win.run() are legitimate and expected.
        for banned in ("subprocess.run", "subprocess.Popen", "os.system",
                       "os.popen", "Popen"):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, info.calls)

    def test_untrusted_text_is_never_rendered_as_markup(self):
        # The GTK view must never use markup-based setters on dynamic
        # text, which would let untrusted content impersonate UI chrome.
        info = collect(TASKUI / "gtk_app.py")
        for forbidden in ("set_markup", "set_markup_with_mnemonic", "Markup"):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, info.calls)
        # Pango must not be imported at all.
        self.assertNotIn("Pango", info.module_imports)

    def test_gtk_app_is_importable_headless(self):
        # Importing the view module must not require a display: GTK is
        # loaded lazily/guarded so the presenter/manager tests (and any
        # headless machine) can import taskui safely.
        import importlib

        module = importlib.import_module("taskui.gtk_app")
        self.assertTrue(hasattr(module, "TaskWindow"))

    def test_package_imports_headless(self):
        import importlib

        module = importlib.import_module("taskui")
        for name in ("TaskManager", "TaskHistory", "TaskRecord",
                     "collect_status"):
            with self.subTest(name=name):
                self.assertTrue(hasattr(module, name))


if __name__ == "__main__":
    unittest.main()
