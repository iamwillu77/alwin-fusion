"""v0.22.0 release-readiness tests — integration, security, lifecycle.

Labels (accurate, per the milestone brief):

* **SANDBOX** — real components (dispatcher, planner, PermissionChecker,
  memory/scheduler stores, TaskManager flows) run against temporary
  homes; the user's real state_dir is never touched.
* **MOCKED** — the deterministic plan-proposal fixture for manager
  flows (never real-model or hardware evidence).
* **STATIC** — source/AST pins for the release contracts (version
  source of truth, closed execution boundary, dependency inventory,
  health-check exit contract, no-approval scheduler).

This file deliberately covers the CROSS-MODULE invariants the
per-subsystem suites do not already pin: dependency inventory,
version consistency, the single-execution-path boundary, state-dir
lifecycle across BOTH databases, missing-optional-dependency honesty,
and skipped-steps-never-completed.
"""

from __future__ import annotations

import ast
import importlib.util
import pathlib
import re
import subprocess
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from ai_core import __version__  # noqa: E402
from ai_core.planner import (  # noqa: E402
    ACTION_SCHEMA,
    MUTATING_ACTIONS,
    PlanValidationError,
    validate_action,
)

from helpers import Sandbox  # noqa: E402

# stdlib + first-party roots (anything else in ai_core/taskui = third
# party and must be justified).
_STDLIB = {
    "argparse", "ast", "base64", "collections", "contextlib", "csv",
    "ctypes", "dataclasses", "datetime", "enum", "functools", "glob",
    "hashlib", "http", "importlib", "inspect", "io", "json", "math",
    "os", "pathlib", "platform", "queue", "random", "re", "shutil",
    "signal", "socket", "sqlite3", "ssl", "string", "struct",
    "subprocess", "sys", "tempfile", "textwrap", "threading", "time",
    "traceback", "types", "typing", "unicodedata", "urllib", "uuid",
    "wave", "xml", "zipfile", "zlib", "zoneinfo",
}
_FIRST_PARTY = {"ai_core", "ai_runtime", "companion", "taskui",
                "integrations", "tests", "scripts"}


def _imports(path: pathlib.Path) -> set:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            found.update(a.name for a in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            found.add(node.module)
    return found


# --------------------------------------------------------------------------- #
# Version source of truth (STATIC + one REAL invocation)
# --------------------------------------------------------------------------- #


class VersionSourceTests(unittest.TestCase):

    def test_all_documentation_agrees_with_the_single_source(self):
        readme = (ROOT / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Version **{__version__}**", readme)
        for doc in ("docs/ARCHITECTURE.md", "docs/SECURITY.md"):
            text = (ROOT / doc).read_text(encoding="utf-8")
            self.assertIn(f"Version: {__version__}", text, doc)

    def test_entry_points_import_the_single_source(self):
        for module in ("companion/__main__.py", "taskui/__main__.py",
                       "ai_core/cli.py"):
            source = (ROOT / module).read_text(encoding="utf-8")
            self.assertIn("from ai_core import __version__", source,
                          module)

    def test_cli_version_prints_the_single_source(self):
        proc = subprocess.run(
            [sys.executable, "-m", "companion", "--version"],
            cwd=ROOT, capture_output=True, text=True, timeout=60)
        self.assertEqual(proc.returncode, 0, proc.stderr[:200])
        self.assertIn(__version__, proc.stdout)


# --------------------------------------------------------------------------- #
# Execution & approval boundary (SANDBOX dispatcher + STATIC pins)
# --------------------------------------------------------------------------- #


class ExecutionBoundaryTests(unittest.TestCase):

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def dispatcher(self):
        from ai_core.permissions import PermissionChecker
        from integrations import build_default_dispatcher
        return build_default_dispatcher(
            PermissionChecker(self.sandbox.config))

    def test_every_registered_action_is_in_the_closed_registry(self):
        names = set(self.dispatcher().names)
        self.assertEqual(names - set(ACTION_SCHEMA), set(),
                         "an executable action exists outside the "
                         "closed action registry")

    def test_every_mutating_registered_action_requires_approval(self):
        for name in sorted(MUTATING_ACTIONS & set(self.dispatcher().names)):
            spec = self.dispatcher().spec(name)
            self.assertTrue(spec.requires_approval, name)
            self.assertTrue(spec.mutates, name)

    def test_read_only_registered_actions_never_require_approval(self):
        for name in sorted(set(self.dispatcher().names) - MUTATING_ACTIONS):
            spec = self.dispatcher().spec(name)
            self.assertFalse(spec.requires_approval, name)

    def test_unknown_action_is_rejected_at_validation(self):
        with self.assertRaises(PlanValidationError):
            validate_action({"action": "run_shell_command",
                             "command": "rm -rf /"})

    def test_dispatch_call_sites_are_exactly_the_two_reviewed_paths(self):
        # Exactly two call sites exist, both reviewed: the executor
        # (the pipeline's single entry) and the sequence runner's
        # per-step re-dispatch through the SAME wired dispatcher (with
        # its own permission re-check — see the check_action call in
        # sequence.py).  Nothing else in the tree may dispatch.
        hits = []
        for pattern in ("ai_core", "taskui", "companion",
                        "integrations"):
            for path in (ROOT / pattern).rglob("*.py"):
                if "__pycache__" in str(path):
                    continue
                for i, line in enumerate(
                        path.read_text(encoding="utf-8").splitlines(),
                        start=1):
                    if ".dispatch(" in line and "def dispatch" not in line:
                        hits.append(f"{path.name}:{i}")
        self.assertEqual(sorted(hits),
                         ["executor.py:83", "sequence.py:213"], hits)
        sequence_src = (ROOT / "integrations" / "automation" /
                        "sequence.py").read_text(encoding="utf-8")
        self.assertIn("check_action", sequence_src)  # per-step gate

    def test_scheduler_module_cannot_approve(self):
        source = (ROOT / "ai_core" / "scheduler.py").read_text(
            encoding="utf-8")
        self.assertNotIn("def approve", source)
        self.assertNotIn("ApprovalToken", source)
        # The manager gate still requires the awaiting state.
        manager_src = (ROOT / "taskui" / "manager.py").read_text(
            encoding="utf-8")
        self.assertIn("STATE_AWAITING_APPROVAL", manager_src)


# --------------------------------------------------------------------------- #
# State-dir lifecycle across BOTH databases (SANDBOX)
# --------------------------------------------------------------------------- #


class StateDirLifecycleTests(unittest.TestCase):

    def test_fresh_state_dir_creates_both_dbs_owner_only(self):
        import stat
        import tempfile
        from ai_core.jobstore import JobStore
        from companion.memory import MemoryStore
        root = pathlib.Path(tempfile.mkdtemp(prefix="relstate-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root,
                                                             True))
        mem = MemoryStore.open_for(root)
        jobs = JobStore.open_for(root)
        self.addCleanup(mem.close)
        self.addCleanup(jobs.close)
        for name in ("memory.db", "scheduler.db"):
            mode = stat.S_IMODE((root / name).stat().st_mode)
            self.assertEqual(mode, 0o600, name)
        self.assertEqual(stat.S_IMODE(root.stat().st_mode), 0o700)

    def test_reopening_existing_dbs_preserves_rows(self):
        import tempfile
        from ai_core import schedule
        from ai_core.jobstore import JobStore
        from companion.memory import MemoryStore
        root = pathlib.Path(tempfile.mkdtemp(prefix="relstate-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root,
                                                             True))
        mem = MemoryStore.open_for(root)
        mem.create("I prefer compact mode", category="preference",
                   provenance="explicit_request")
        jobs = JobStore.open_for(root)
        sched = schedule.make_schedule("daily", hour=9, tzid="UTC")
        jobs.add(schedule.make_job("Keep me", "x", job_type="reminder",
                                   schedule=sched, now=1_800_000_000))
        mem.close()
        jobs.close()
        mem2 = MemoryStore.open_for(root)     # restart with existing DBs
        jobs2 = JobStore.open_for(root)
        self.addCleanup(mem2.close)
        self.addCleanup(jobs2.close)
        self.assertEqual(mem2.count(), 1)
        self.assertEqual(len(jobs2.list_jobs()), 1)

    def test_corrupt_scheduler_db_is_refused_not_recreated(self):
        import tempfile
        from ai_core.jobstore import JobStore, JobStoreError
        root = pathlib.Path(tempfile.mkdtemp(prefix="relstate-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root,
                                                             True))
        (root / "scheduler.db").write_bytes(b"garbage, not sqlite")
        with self.assertRaises(JobStoreError):
            JobStore.open_for(root)
        # Refused, not silently recreated: the file is untouched.
        self.assertEqual((root / "scheduler.db").read_bytes(),
                         b"garbage, not sqlite")

    def test_memory_clear_never_touches_the_scheduler_db(self):
        import tempfile
        from ai_core import schedule
        from ai_core.jobstore import JobStore
        from companion.memory import MemoryStore
        root = pathlib.Path(tempfile.mkdtemp(prefix="relstate-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root,
                                                             True))
        mem = MemoryStore.open_for(root)
        mem.create("a fact", category="general",
                   provenance="explicit_request")
        jobs = JobStore.open_for(root)
        jobs.add(schedule.make_job("Keep", "x", job_type="reminder",
                                   schedule=schedule.make_schedule(
                                       "daily", hour=9, tzid="UTC"),
                                   now=1_800_000_000))
        mem.clear()
        self.assertEqual(mem.count(), 0)
        self.assertEqual(len(jobs.list_jobs()), 1)
        mem.close()
        jobs.close()

    def test_closed_scheduler_refuses_new_work_honestly(self):
        import tempfile
        from ai_core.jobstore import JobStore, JobStoreError
        from ai_core.scheduler import Scheduler, TickReport
        root = pathlib.Path(tempfile.mkdtemp(prefix="relstate-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(root,
                                                             True))
        store = JobStore.open_for(root)
        clock = lambda: 1_800_000_000  # noqa: E731
        scheduler = Scheduler(store, clock=clock)
        scheduler.close()
        report = scheduler.tick()
        self.assertTrue(report.errors)      # honest refusal
        store.close()
        with self.assertRaises(JobStoreError):
            store.list_jobs()
        del TickReport


# --------------------------------------------------------------------------- #
# Dependency inventory (STATIC pins)
# --------------------------------------------------------------------------- #


class DependencyInventoryTests(unittest.TestCase):

    def test_requirements_stay_optional_documentation_deps_only(self):
        lines = [line.strip() for line in
                 (ROOT / "requirements.txt").read_text(
                     encoding="utf-8").splitlines()]
        packages = [line.split("#")[0].strip() for line in lines
                    if line and not line.startswith("#")
                    and line.split("#")[0].strip()]
        names = {p.split("==")[0].split(">=")[0].lower()
                 for p in packages}
        self.assertEqual(names, {"openpyxl", "reportlab"})

    def test_ai_core_imports_only_stdlib_and_first_party(self):
        third = set()
        for path in (ROOT / "ai_core").glob("*.py"):
            for module in _imports(path):
                root = module.split(".")[0]
                if root not in _STDLIB and root not in _FIRST_PARTY \
                        and root != "__future__":
                    third.add((root, path.name))
        self.assertEqual(third, set())

    def test_taskui_third_party_is_gtk_only(self):
        third = set()
        for path in (ROOT / "taskui").glob("*.py"):
            for module in _imports(path):
                root = module.split(".")[0]
                if root not in _STDLIB and root not in _FIRST_PARTY \
                        and root != "__future__":
                    third.add(root)
        self.assertLessEqual(third, {"gi"}, third)


# --------------------------------------------------------------------------- #
# Missing optional dependencies → honest bounded errors (SANDBOX)
# --------------------------------------------------------------------------- #


class OptionalDependencyTests(unittest.TestCase):

    def test_missing_asr_package_yields_an_honest_error(self):
        if importlib.util.find_spec("faster_whisper") is not None:
            self.skipTest("faster_whisper is installed on this host "
                          "(the missing-package branch cannot run)")
        from companion.speech_recognition import (
            RecognitionError, SpeechRecognitionService)
        service = SpeechRecognitionService(model_size="tiny")
        with self.assertRaises(RecognitionError) as ctx:
            service._ensure_engine()
        self.assertIn("not installed", str(ctx.exception))
        # The error is bounded and actionable, never a crash.
        self.assertLess(len(str(ctx.exception)), 300)

    def test_missing_document_deps_report_actionably(self):
        from ai_core import capabilities
        caps = capabilities.discover_capabilities(
            llm_available=False)
        if importlib.util.find_spec("openpyxl") is None:
            self.assertFalse(caps["documents"]["create_spreadsheet"])
        if importlib.util.find_spec("reportlab") is None:
            self.assertFalse(caps["documents"]["create_pdf"])
        # Either way the capability map is well-formed booleans.
        self.assertIsInstance(caps["documents"]["create_spreadsheet"],
                              bool)


# --------------------------------------------------------------------------- #
# Health-check exit contract (STATIC + REAL run)
# --------------------------------------------------------------------------- #


class HealthCheckContractTests(unittest.TestCase):

    def test_companion_check_fails_the_exit_code_on_any_failure(self):
        source = (ROOT / "companion" / "__main__.py").read_text(
            encoding="utf-8")
        self.assertIn('print(f"CHECK FAILED ({len(failures)} problem(s))")',
                      source)
        self.assertIn("return EXIT_FAILURE", source)
        self.assertIn("print(\"CHECK OK — companion core is healthy",
                      source.replace("print(f", "print("))

    def test_companion_check_passes_for_real(self):
        proc = subprocess.run(
            [sys.executable, "-m", "companion", "--check"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout[-500:])
        self.assertIn("CHECK OK", proc.stdout)
        self.assertNotIn("  FAIL:", proc.stdout)

    def test_taskui_check_reports_version_from_single_source(self):
        proc = subprocess.run(
            [sys.executable, "-m", "taskui", "--check"],
            cwd=ROOT, capture_output=True, text=True, timeout=120)
        self.assertEqual(proc.returncode, 0, proc.stdout[-500:])
        self.assertIn(__version__, proc.stdout)


# --------------------------------------------------------------------------- #
# Cross-module invariant gaps (SANDBOX + MOCKED proposals)
# --------------------------------------------------------------------------- #


class CrossModuleInvariantTests(unittest.TestCase):
    """Invariants the per-subsystem suites do not pin end-to-end."""

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def test_skipped_dependent_steps_are_never_reported_completed(self):
        from taskui.manager import TaskManager
        from test_task_planning import RoutedFakeLLM, _step, plan_json
        llm = RoutedFakeLLM()
        # s1 fails on a missing file (read-only), s2 depends on s1.
        llm.proposal_response = plan_json([
            _step("s1", {"action": "read_file",
                         "path": "Documents/does-not-exist.txt"}),
            _step("s2", {"action": "read_file",
                         "path": "Documents/does-not-exist.txt"},
                  depends_on=["s1"]),
        ], objective="Read a missing file twice")
        manager = TaskManager(self.sandbox.config, use_llm=True,
                              llm_client=llm)
        record = manager.submit(
            "Analyze the CSV files in my Documents folder and summarize "
            "my monthly expenses")
        self.assertTrue(manager.wait_terminal(timeout=15))
        statuses = [s["status"] for s in record.steps]
        self.assertEqual(statuses[0], "failed")
        self.assertEqual(statuses[1], "skipped")
        self.assertNotIn("succeeded", statuses)
        self.assertNotIn("completed", statuses)

    def test_voice_cancellation_cannot_surface_a_stale_result(self):
        # Deterministic race (barrier-controlled engine): the decode
        # finishes ONLY after cancel() has bumped the generation, so a
        # late result must be suppressed, not delivered.
        import threading
        import time as time_mod

        from companion.speech_recognition import SpeechRecognitionService
        gate = threading.Event()

        class SlowEngine:
            def transcribe(self, audio, **_kw):
                gate.wait(timeout=5.0)
                raise RuntimeError("must never reach the caller")

        service = SpeechRecognitionService(
            engine_factory=lambda _size: SlowEngine())
        delivered = []
        started = service.recognize_async(
            b"\x01\x40" * 16000,             # 1 s, non-silent
            on_result=lambda r: delivered.append(("result", r)),
            on_error=lambda e: delivered.append(("error", e)),
        )
        self.assertTrue(started)
        service.cancel()                      # generation bump FIRST
        gate.set()                            # then the decode finishes
        time_mod.sleep(0.4)
        self.assertEqual(delivered, [])       # stale outcome suppressed


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
