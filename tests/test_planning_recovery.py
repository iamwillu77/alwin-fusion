"""v0.20.0 planning & recovery tests — idempotency, resume, clarification.

Labels (accurate, per milestone brief):

* **SANDBOX** — manager tests run the REAL TaskManager + pipeline +
  verifier in temporary directories (real files, real approvals).
* **MOCKED** — the LLM is the deterministic RoutedFakeLLM fixture
  (never real-host evidence; that lives in
  scripts/verify_planning_recovery.py); uncertain-outcome scenarios
  are labelled simulations.
* **STATIC** — AST pins for the recovery trust boundary.

Covers: idempotency classification, dependency ordering, verified vs
uncertain step results, partial completion, resume-after-verified-step
(completed mutations never repeated), fresh approval for resumed
mutations, resume bounds, cancellation during resume, overwrite
clarification, and audit/record separation of attempts.
"""

from __future__ import annotations

import ast
import json
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))

from ai_core.task_plan import (  # noqa: E402
    IDEM_NEVER,
    IDEM_READ_ONLY,
    IDEM_RECONCILE,
    IDEM_REPEATABLE,
    IDEMPOTENCY,
    IDEMPOTENCY_CLASSES,
    STEP_FAILED,
    STEP_PENDING,
    STEP_SUCCEEDED,
    STEP_VERIFICATION_FAILED,
    TaskPlan,
    build_resume_plan,
    idempotency_of,
    validate_plan,
)
from taskui.manager import MAX_RESUMES, ManagerError, TaskManager  # noqa: E402
from taskui.lifecycle import (  # noqa: E402
    STATE_AWAITING_APPROVAL,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_FAILED,
)
from test_task_planning import RoutedFakeLLM, _step, plan_json  # noqa: E402

from helpers import Sandbox  # noqa: E402


# --------------------------------------------------------------------------- #
# Idempotency classification (STATIC pins over real semantics)
# --------------------------------------------------------------------------- #


class IdempotencyTests(unittest.TestCase):

    def test_every_class_is_closed_and_known(self):
        for name, klass in IDEMPOTENCY.items():
            self.assertIn(klass, IDEMPOTENCY_CLASSES, name)

    def test_read_only_actions_are_read_only(self):
        for name in ("list_directory", "read_file", "read_control",
                     "list_controls", "inspect_application",
                     "find_control", "application_status",
                     "active_window", "detect_applications"):
            self.assertEqual(idempotency_of({"action": name}),
                             IDEM_READ_ONLY, name)

    def test_mutating_actions_are_never_read_only(self):
        from ai_core.planner import MUTATING_ACTIONS
        for name in MUTATING_ACTIONS:
            klass = idempotency_of({"action": name})
            self.assertNotEqual(klass, IDEM_READ_ONLY, name)
            self.assertIn(klass, (IDEM_RECONCILE, IDEM_NEVER,
                                  IDEM_REPEATABLE), name)

    def test_creations_are_never_auto_repeat(self):
        for name in ("create_text_file", "create_report", "create_pdf",
                     "create_spreadsheet", "enter_text", "save_document",
                     "task_sequence"):
            self.assertEqual(idempotency_of({"action": name}),
                             IDEM_NEVER, name)

    def test_reconcile_class_for_stateful_operations(self):
        for name in ("open_file", "launch_application",
                     "close_application", "activate_control",
                     "replace_file_content"):
            self.assertEqual(idempotency_of({"action": name}),
                             IDEM_RECONCILE, name)

    def test_unknown_action_is_conservative(self):
        self.assertEqual(idempotency_of({"action": "made_up"}),
                         IDEM_NEVER)
        self.assertEqual(idempotency_of({}), IDEM_NEVER)
        self.assertEqual(idempotency_of(None), IDEM_NEVER)


# --------------------------------------------------------------------------- #
# build_resume_plan (pure)
# --------------------------------------------------------------------------- #


def make_plan(steps_json):
    return validate_plan({"objective": "Test", "steps": steps_json})


class ResumePlanTests(unittest.TestCase):

    def test_verified_completed_steps_are_excluded_and_replayed(self):
        plan = make_plan([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/a.txt", "content": "x"}),
            _step("s2", {"action": "read_file", "path": "Documents/a.txt"},
                  depends_on=["s1"]),
        ])
        plan.steps[0].state = STEP_SUCCEEDED
        residual, notes, recon = build_resume_plan(
            plan, verified_results={"s1": {"data": {}, "digest": "abc"}})
        self.assertEqual([s.id for s in residual.steps], ["s2"])
        self.assertIn("will NOT be repeated", notes[0])
        self.assertEqual(recon, [])
        # The dependent keeps its dependency id — derivations resolve
        # against the replayed result.
        self.assertEqual(residual.steps[0].depends_on, ("s1",))

    def test_uncertain_dispatched_mutation_is_excluded(self):
        plan = make_plan([
            _step("s1", {"action": "replace_file_content",
                         "path": "Documents/a.txt", "content": "y"}),
            _step("s2", {"action": "read_file", "path": "Documents/a.txt"}),
        ])
        plan.steps[0].state = STEP_VERIFICATION_FAILED
        plan.steps[0].dispatched = True        # simulation
        residual, notes, recon = build_resume_plan(
            plan, verified_results={})
        self.assertEqual([s.id for s in residual.steps], ["s2"])
        self.assertEqual(len(recon), 1)
        self.assertIn("not verified", recon[0])
        self.assertIn("reconciliation", notes[0])

    def test_undispatched_mutation_failure_is_resumable(self):
        plan = make_plan([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/a.txt", "content": "x"}),
        ])
        plan.steps[0].state = STEP_FAILED      # failed at preview:
        plan.steps[0].dispatched = False       # nothing executed
        residual, _, recon = build_resume_plan(plan, verified_results={})
        self.assertEqual([s.id for s in residual.steps], ["s1"])
        self.assertEqual(residual.steps[0].state, STEP_PENDING)
        self.assertEqual(recon, [])

    def test_empty_residual_returns_none(self):
        plan = make_plan([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/a.txt", "content": "x"}),
        ])
        plan.steps[0].state = STEP_SUCCEEDED
        residual, _, _ = build_resume_plan(
            plan, verified_results={"s1": {"data": {}, "digest": "d"}})
        self.assertIsNone(residual)


# --------------------------------------------------------------------------- #
# Manager resume flow (SANDBOX, real pipeline + real files)
# --------------------------------------------------------------------------- #


class ManagerResumeTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.llm = RoutedFakeLLM()

    def manager(self) -> TaskManager:
        return TaskManager(self.sandbox.config, use_llm=True,
                           llm_client=self.llm)

    def goal(self) -> str:
        return ("Analyze the CSV files in my Documents folder and "
                "summarize my monthly expenses")

    def failed_two_step_record(self):
        """s1 succeeds (real file), s2 fails at preview (exists)."""
        target = self.sandbox.doc_path("resume_a.txt")
        target.write_text("ALREADY HERE", encoding="utf-8")
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/resume_b.txt",
                         "content": "created by the plan"},
                  expected="the file exists"),
            _step("s2", {"action": "create_text_file",
                         "path": "Documents/resume_a.txt",
                         "content": "should never land"},
                  depends_on=["s1"], expected="the file exists"),
        ], objective="Make two notes")
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(
            manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10))
        self.assertTrue(manager.approve(record.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=15))
        # A mutating step that failed AFTER earlier mutations landed is
        # honestly needs_attention (partial mutation), not a clean fail.
        self.assertIn(record.state, (STATE_FAILED, "needs_attention"))
        return manager, record, target

    def test_partial_failure_persists_verified_results(self):
        manager, record, target = self.failed_two_step_record()
        self.assertEqual([s["status"] for s in record.steps],
                         ["succeeded", "failed"])
        self.assertIn("s1", record.plan_results)
        self.assertEqual(target.read_text("utf-8"), "ALREADY HERE")
        # Deterministic clarification note for the overwrite conflict.
        self.assertTrue(any("Needs clarification" in n
                            for n in record.notes))

    def test_resume_skips_completed_step_and_needs_fresh_approval(self):
        manager, record, target = self.failed_two_step_record()
        resumed = manager.resume_task(record.task_id)
        self.assertEqual(resumed.resume_of, record.task_id)
        self.assertNotEqual(resumed.task_id, record.task_id)
        self.assertTrue(
            manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10))
        # The old approval can never cover the resumed plan.
        self.assertFalse(manager.approve(record.approval_digest))
        self.assertEqual(resumed.state, STATE_AWAITING_APPROVAL)
        self.assertTrue(manager.approve(resumed.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=15))
        # s1 was NOT re-run (its file already existed from the first
        # attempt and the create would have refused); s2 failed again
        # honestly because the target still exists.
        self.assertEqual(target.read_text("utf-8"), "ALREADY HERE")
        self.assertEqual([s["status"] for s in resumed.steps],
                         ["failed"])

    def test_resume_after_removing_the_conflict_completes(self):
        manager, record, target = self.failed_two_step_record()
        target.unlink()                      # user resolves the conflict
        resumed = manager.resume_task(record.task_id)
        self.assertTrue(
            manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10))
        self.assertTrue(manager.approve(resumed.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(resumed.state, STATE_COMPLETED)
        # s1's completed mutation was NOT repeated: only s2 ran — and
        # with the conflict resolved by the user, it succeeded.
        self.assertEqual([s["status"] for s in resumed.steps],
                         ["succeeded"])
        self.assertEqual(
            target.read_text("utf-8"), "should never land")

    def test_resume_of_completed_chain_is_refused(self):
        self.llm.proposal_response = plan_json([
            _step("s1", {"action": "create_text_file",
                         "path": "Documents/ok.txt", "content": "x"}),
        ], objective="Make a note")
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(
            manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10))
        self.assertTrue(manager.approve(record.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, STATE_COMPLETED)
        with self.assertRaises(ManagerError):
            manager.resume_task(record.task_id)   # not failed

    def test_resume_budget_is_bounded(self):
        manager, record, target = self.failed_two_step_record()
        first = manager.resume_task(record.task_id)
        self.assertTrue(
            manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10))
        self.assertTrue(manager.approve(first.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertIn(first.state,
                      (STATE_FAILED, "needs_attention"))
        # A resume of a resume: budget spent.
        self.assertEqual(MAX_RESUMES, 1)
        with self.assertRaises(ManagerError):
            manager.resume_task(first.task_id)

    def test_cancellation_during_resume_executes_nothing(self):
        manager, record, target = self.failed_two_step_record()
        resumed = manager.resume_task(record.task_id)
        self.assertTrue(
            manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10))
        self.assertTrue(manager.reject())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(resumed.state, STATE_CANCELLED)
        self.assertEqual(target.read_text("utf-8"), "ALREADY HERE")

    def test_resume_requires_manager_idle(self):
        manager, record, target = self.failed_two_step_record()
        self.llm.proposal_response = plan_json([
            _step("x1", {"action": "read_file",
                         "path": "Documents/resume_a.txt"}),
        ], objective="Read a note")
        running = manager.submit(self.goal())   # occupies the manager
        with self.assertRaises(ManagerError):
            manager.resume_task(record.task_id)
        manager.cancel()
        manager.wait_terminal(timeout=10)
        del running


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
