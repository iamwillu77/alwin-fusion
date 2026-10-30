"""v0.19.0 reliability tests — capabilities, adapters, workflow recovery.

Labels (accurate, per milestone brief):

* **SANDBOX** — pipeline/tool tests run the real PermissionChecker,
  dispatcher, executor and verifier in temporary directories.
* **MOCKED** — application behavior (buses, processes, controls,
  write failures) is simulated with the existing fake modules; these
  are NEVER presented as real-host evidence (that lives in
  scripts/verify_reliability.py).
* **STATIC** — AST/source pins for the diagnostic-redaction and
  no-retry guarantees.

Covers: capability facets (available/unavailable/blocked/unknown),
transient probe failures, tier honesty, adapter failure categories,
partial workflow completion, cancellation, verification failure,
uncertain outcomes, bounded cleanup, and diagnostic redaction.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from automation_fakes import (  # noqa: E402
    AutomationSandbox,
    FakeAtspiModule,
    FakeNode,
)
from ai_core.capabilities import (  # noqa: E402
    FACET_AVAILABLE,
    FACET_BLOCKED,
    FACET_UNAVAILABLE,
    FACET_UNKNOWN,
    application_readiness,
    application_tiers,
)
from ai_core.pipeline import (  # noqa: E402
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
)
from ai_core.planner import validate_action  # noqa: E402
from integrations.apps.registry import BUILTIN_REGISTRY  # noqa: E402
from integrations.automation.base import (  # noqa: E402
    FAILURE_CATEGORIES,
    ControlLocator,
    failure_data,
)
from integrations.automation.gnome_text_editor import (  # noqa: E402
    GnomeTextEditorAdapter,
)
from integrations.automation.libreoffice import LibreOfficeAdapter  # noqa: E402

from helpers import Sandbox  # noqa: E402


# --------------------------------------------------------------------------- #
# Capability discovery (SANDBOX over injected probes — MOCKED probes)
# --------------------------------------------------------------------------- #


class CapabilityFacetTests(unittest.TestCase):

    def definition(self, app_id="libreoffice-writer"):
        return BUILTIN_REGISTRY.get(app_id)

    def test_installed_alone_never_yields_interact(self):
        # The concrete overstatement bug v0.19 fixes: bindings +
        # registry used to be enough for 'interact' with no install
        # check at all.
        tiers = application_tiers(
            which=lambda _n: None,            # nothing installed
            automation_bindings=True,
        )
        self.assertNotIn("interact",
                         tiers["libreoffice-writer"]["tiers"])
        self.assertFalse(tiers["libreoffice-writer"]["installed"])

    def test_readiness_running_but_absent_from_bus_is_blocked(self):
        # The generic 'libreoffice' entry carries the reviewed bus
        # identity ("soffice"/"soffice.bin") — the exact reference-host
        # blocked case.
        readiness = application_readiness(
            self.definition("libreoffice"),
            bindings=True,
            which=lambda _n: "/usr/bin/soffice",
            # /proc says the process IS running...
            running_probe=lambda _d: True,
            # ...and the bus answers but never lists it (the real
            # reference-host LibreOffice situation).
            bus_probe=lambda _d: ["gnome-shell", "ptyxis"],
        )
        self.assertEqual(readiness["installed"], FACET_AVAILABLE)
        self.assertIs(readiness["running"], True)
        self.assertEqual(readiness["a11y"], FACET_BLOCKED)
        self.assertEqual(readiness["control"], "open_only")
        self.assertNotIn("interact", readiness["tiers"])

    def test_readiness_listed_on_bus_is_available(self):
        readiness = application_readiness(
            self.definition("gnome-text-editor"),
            bindings=True,
            which=lambda _n: "/usr/bin/gnome-text-editor",
            running_probe=lambda _d: True,
            bus_probe=lambda _d: ["gnome-text-editor"],
        )
        self.assertEqual(readiness["a11y"], FACET_AVAILABLE)
        self.assertEqual(readiness["control"], "interact")
        self.assertIn("interact", readiness["tiers"])

    def test_transient_probe_failure_is_unknown_not_unavailable(self):
        def exploding_probe(_d):
            raise RuntimeError("bus hiccup")

        readiness = application_readiness(
            self.definition(),
            bindings=True,
            which=lambda _n: "/usr/bin/soffice",
            running_probe=exploding_probe,
            bus_probe=exploding_probe,
        )
        self.assertIsNone(readiness["running"])
        self.assertEqual(readiness["a11y"], FACET_UNKNOWN)
        self.assertEqual(readiness["control"], "unknown")

    def test_not_running_app_has_unknown_a11y_not_blocked(self):
        readiness = application_readiness(
            self.definition(),
            bindings=True,
            which=lambda _n: "/usr/bin/soffice",
            running_probe=lambda _d: False,
            bus_probe=lambda _d: [],
        )
        self.assertEqual(readiness["a11y"], FACET_UNKNOWN)
        self.assertEqual(readiness["control"], "unknown")

    def test_without_bindings_a11y_is_unavailable(self):
        readiness = application_readiness(
            self.definition(),
            bindings=False,
            which=lambda _n: "/usr/bin/soffice",
            running_probe=lambda _d: True,
            bus_probe=lambda _d: ["libreoffice"],
        )
        self.assertEqual(readiness["a11y"], FACET_UNAVAILABLE)

    def test_detection_failure_is_unknown_install(self):
        def broken_which(_n):
            raise OSError("procfs gone")

        readiness = application_readiness(
            self.definition(),
            bindings=True,
            which=broken_which,
        )
        self.assertEqual(readiness["installed"], FACET_UNKNOWN)


# --------------------------------------------------------------------------- #
# Adapter failure categories (MOCKED trees)
# --------------------------------------------------------------------------- #


def editor_tree():
    app = FakeNode("application", "gnome-text-editor")
    window = FakeNode("frame", "test.txt - Text Editor")
    button = FakeNode("button", "New Tab", actions=("click",))
    window.add(button)
    app.add(window)
    return app


class AdapterCategoryTests(unittest.TestCase):

    def tools_for(self, app=None, running="gnome-text-edit"):
        import tempfile
        from ai_core.config import load_config
        from ai_core.permissions import PermissionChecker
        from integrations.automation.atspi_backend import AtspiSession
        from integrations.automation.tools import AutomationTools
        root = pathlib.Path(tempfile.mkdtemp(prefix="rel-test-"))
        config = load_config(home=root, roots=["Documents"],
                             state_dir=root / "state")
        module = FakeAtspiModule(apps=[app] if app is not None else [])
        proc = None
        comm = None
        if running:
            proc = str(root / "proc")
            pathlib.Path(proc).mkdir()
            (pathlib.Path(proc) / "42").mkdir()
            (pathlib.Path(proc) / "42" / "comm").write_text(
                running + "\n", encoding="utf-8")
            comm = {running}
        return AutomationTools(
            PermissionChecker(config), session=AtspiSession(module),
            which=lambda _n: "/usr/bin/x", proc_root=proc,
            comm_names=comm)

    def test_unsupported_capability_carries_category(self):
        tools = self.tools_for(editor_tree())
        result = tools.read_control("gnome-text-editor",
                                    {"role": "label", "name": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("category"), "unsupported")

    def test_unavailable_adapter_carries_category(self):
        tools = self.tools_for(editor_tree())
        result = tools.read_control("nautilus",
                                    {"role": "button", "name": "x"})
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("category"), "unsupported")

    def test_missing_app_is_category_and_unknown_state(self):
        tools = self.tools_for(None, running=None)
        result = tools.inspect("gnome-text-editor")
        # read-only inspect reports an honest state, never a crash
        self.assertIn(result.data.get("snapshot", {}).get("state"),
                      ("installed_not_running", "failed_or_unknown"))

    def test_ambiguous_carries_category(self):
        app = editor_tree()
        app.children[0].add(FakeNode("button", "New Tab",
                                     actions=("click",)))
        tools = self.tools_for(app)
        result = tools.find_control("gnome-text-editor",
                                    {"role": "button", "name": "New Tab"})
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("category"), "ambiguous")

    def test_defunct_carries_stale_category(self):
        from automation_fakes import FakeState
        app = editor_tree()
        button = app.children[0].children[0]
        button._states.append(FakeState("defunct"))
        tools = self.tools_for(app)
        adapter = GnomeTextEditorAdapter("gnome-text-editor",
                                         session=tools.session)
        result = adapter.find_control(
            ControlLocator(role="button", name="New Tab"))
        self.assertFalse(result.ok)
        self.assertEqual(result.data.get("category"), "stale_target")

    def test_failure_data_rejects_unknown_categories(self):
        payload = failure_data("find_control", "made-up-category")
        self.assertEqual(payload["category"], "unknown")

    def test_failure_data_is_bounded_and_content_free(self):
        payload = failure_data("find_control", "ambiguous",
                               reason="x" * 500, matches=3)
        self.assertLessEqual(len(payload["reason"]), 200)
        self.assertEqual(payload["matches"], 3)
        self.assertEqual(set(payload) <=
                         {"capability", "category", "unsupported",
                          "reason", "state", "matches"}, True)


# --------------------------------------------------------------------------- #
# Workflow recovery (SANDBOX pipeline + MOCKED failures)
# --------------------------------------------------------------------------- #


class WorkflowRecoveryTests(unittest.TestCase):

    def setUp(self):
        self.sandbox = Sandbox()

    def tearDown(self):
        self.sandbox.cleanup()

    def run_action(self, action, *, approve=True):
        from ai_core.pipeline import ApprovalToken
        pipe = self.sandbox.pipeline(approve=approve)
        preview = pipe.preview_action(action)
        if preview.requires_approval:
            token = (ApprovalToken(digest=preview.plan_digest)
                     if approve else False)
            return pipe.run_previewed(preview, approval=token)
        return pipe.run_previewed(preview)

    def test_partial_completion_is_explicit(self):
        self.seed = self.sandbox.doc_path("partial.txt")
        self.seed.write_text("original", encoding="utf-8")
        report = self.run_action({
            "action": "task_sequence",
            "steps": [
                {"action": "replace_file_content",
                 "path": "Documents/partial.txt", "content": "step one"},
                {"action": "read_file", "path": "Documents/missing.txt"},
            ],
        })
        self.assertEqual(report.status, STATUS_FAILED)
        partial = report.execution.data["partial"]
        self.assertEqual(partial["completed"], 1)
        self.assertEqual(partial["total"], 2)
        self.assertEqual(partial["failed_step"], 2)
        self.assertTrue(partial["changed"])       # step one DID land
        self.assertIn("DID change state", report.error)
        self.assertIn("1 of 2 completed", report.error)
        self.assertTrue(partial["next_action"])

    def test_partial_failure_never_claims_no_effects(self):
        target = self.sandbox.doc_path("effects.txt")
        target.write_text("original", encoding="utf-8")
        report = self.run_action({
            "action": "task_sequence",
            "steps": [
                {"action": "replace_file_content",
                 "path": "Documents/effects.txt", "content": "changed"},
                {"action": "read_file", "path": "Documents/nope.txt"},
            ],
        })
        self.assertEqual(target.read_text("utf-8"), "changed")
        self.assertNotEqual(report.status, STATUS_SUCCESS)
        self.assertIn("DID change state", report.error)

    def test_remaining_steps_are_skipped_with_reason(self):
        self.sandbox.doc_path("s.txt").write_text("x", encoding="utf-8")
        report = self.run_action({
            "action": "task_sequence",
            "steps": [
                {"action": "read_file", "path": "Documents/missing.txt"},
                {"action": "read_file", "path": "Documents/s.txt"},
            ],
        })
        statuses = [s["status"] for s in report.execution.data["steps"]]
        self.assertEqual(statuses, ["failed", "skipped"])
        self.assertIn("not executed", report.execution.data["steps"][1]["error"])

    def test_uncertain_mutation_is_not_retried(self):
        target = self.sandbox.doc_path("once.txt")
        target.write_text("old", encoding="utf-8")
        calls = []

        def failing(src, dst):
            calls.append(dst)
            raise OSError(5, "I/O error")

        with mock.patch("integrations.filesystem.tools.os.replace",
                        side_effect=failing):
            self.run_action({"action": "replace_file_content",
                             "path": "Documents/once.txt",
                             "content": "new"})
        self.assertEqual(len(calls), 1)
        self.assertEqual(target.read_text("utf-8"), "old")

    def test_denied_step_executes_nothing_for_that_step(self):
        report = self.run_action({
            "action": "task_sequence",
            "steps": [
                {"action": "read_file", "path": "/etc/hostname"},
                {"action": "create_text_file",
                 "path": "Documents/after-denied.txt", "content": "x"},
            ],
        })
        # The out-of-roots step may be denied at the plan level (whole
        # sequence rejected before execution) or at the step level —
        # either way NOTHING may execute and the later step never runs.
        self.assertNotEqual(report.status, STATUS_SUCCESS)
        steps = report.execution.data.get("steps") if report.execution \
            else None
        if steps:
            self.assertIn(steps[0]["status"], ("denied", "failed"))
            self.assertEqual(steps[1]["status"], "skipped")
        self.assertFalse(
            self.sandbox.doc_path("after-denied.txt").exists())

    def test_cancellation_at_gate_leaves_step_unexecuted(self):
        preview = self.sandbox.pipeline().preview_action(
            {"action": "replace_file_content",
             "path": "Documents/cancel.txt", "content": "x"})
        result = self.sandbox.pipeline().run_previewed(preview,
                                                       approval=False)
        self.assertEqual(result.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("cancel.txt").exists())

    def test_step_category_flows_into_partial_block(self):
        self.sandbox.doc_path("c.txt").write_text("x", encoding="utf-8")
        report = self.run_action({
            "action": "task_sequence",
            "steps": [
                {"action": "replace_file_content",
                 "path": "Documents/c.txt", "content": "y"},
                {"action": "replace_file_content",
                 "path": "Documents/ghost.txt", "content": "z"},
            ],
        })
        partial = report.execution.data["partial"]
        # The replace tool's honest failure has no a11y category —
        # the block falls back to 'unknown' rather than inventing one.
        self.assertIn(partial["category"], FAILURE_CATEGORIES)


# --------------------------------------------------------------------------- #
# Planner unknown-vs-unavailable (STATIC/SANDBOX)
# --------------------------------------------------------------------------- #


class PlannerBehaviorTests(unittest.TestCase):

    def test_closed_action_registry_is_unchanged_by_v019(self):
        # No new actions were introduced in v0.19 — reliability only.
        for payload in (
            {"action": "application_status", "application":
             "libreoffice-writer"},
            {"action": "list_controls", "application":
             "gnome-text-editor"},
            {"action": "replace_file_content", "path":
             "Documents/x.txt", "content": "y"},
        ):
            self.assertTrue(validate_action(payload))


# --------------------------------------------------------------------------- #
# Diagnostic redaction (STATIC)
# --------------------------------------------------------------------------- #


class DiagnosticRedactionTests(unittest.TestCase):

    def test_failure_data_never_carries_arbitrary_keys(self):
        source = (ROOT / "integrations" / "automation" /
                  "base.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, ast.FunctionDef)
                  and n.name == "failure_data")
        segment = ast.get_source_segment(source, fn)
        # only the whitelisted keys may be copied from `extra`
        self.assertIn('for key in ("unsupported", "reason", "state", '
                      '"matches")', segment)

    def test_sequence_partial_block_is_fixed_strings(self):
        source = (ROOT / "integrations" / "automation" /
                  "sequence.py").read_text(encoding="utf-8")
        fn_start = source.index('data["partial"] = {')
        fn_end = source.index("}", fn_start)
        segment = source[fn_start:fn_end]
        for banned in ("content", "command", "description"):
            self.assertNotIn(banned, segment)


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
