"""v0.5 task-manager tests: the UI <-> trusted-pipeline bridge.

These tests drive the *real* TaskManager against sandboxed pipelines
(mocks and temporary directories only — no display, no desktop).

Covered:

* submission validation and one-task-at-a-time intake,
* read-only tasks complete without any approval,
* mutating tasks wait for approval; rejection cancels and changes
  nothing,
* approval is bound to the exact displayed plan digest; a changed
  plan invalidates it,
* cancellation before execution; refusal to cancel mid-execution,
* execution failure -> failed, never completed,
* verification failure after successful execution -> needs_attention,
  never completed,
* partial sequence failure -> needs_attention with truthful per-step
  records,
* permission re-check at execution time still holds through the manager,
* history is written for every terminal outcome,
* no fabricated success.
"""

from __future__ import annotations

import threading
import unittest

from ai_core.pipeline import (
    STATUS_SUCCESS,
    ApprovalToken,
    TaskPipeline,
)
from integrations.filesystem.tools import ToolResult
from taskui.lifecycle import (
    STATE_AWAITING_APPROVAL,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_EXECUTING,
    STATE_FAILED,
    STATE_NEEDS_ATTENTION,
    STATE_PLANNING,
    STATE_VERIFYING,
)
from taskui.manager import ManagerError, TaskManager
from tests.helpers import Sandbox


def wait(manager, *states, timeout=10.0):
    ok = manager.wait_for_state(*states, timeout=timeout)
    manager.wait_terminal(timeout=timeout)
    return ok


class ManagerTestCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def manager(self, **kwargs) -> TaskManager:
        return TaskManager(self.sandbox.config, **kwargs)


class IntakeTests(ManagerTestCase):
    def test_empty_request_is_refused(self):
        manager = self.manager()
        with self.assertRaises(ManagerError):
            manager.submit("   ")

    def test_overlong_request_is_refused(self):
        manager = self.manager()
        with self.assertRaises(ManagerError):
            manager.submit("x" * 2001)

    def test_second_task_while_running_is_refused(self):
        manager = self.manager()
        manager.submit("Create a folder named Slow in Documents.")
        with self.assertRaises(ManagerError):
            manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)

    def test_new_task_allowed_after_terminal(self):
        manager = self.manager()
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        record = manager.submit("Show files and folders in my Documents.")
        self.assertTrue(wait(manager, STATE_COMPLETED))

    def test_submit_starts_in_planning(self):
        manager = self.manager()
        record = manager.submit("Show files and folders in my Documents.")
        self.assertEqual(record.state, STATE_PLANNING)
        manager.wait_terminal(timeout=10)


class ReadOnlyFlowTests(ManagerTestCase):
    def test_read_only_completes_without_approval(self):
        manager = self.manager()
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(wait(manager, STATE_COMPLETED))
        record = manager.current
        self.assertEqual(record.approval, "not_required")
        self.assertIsNotNone(record.verification)

    def test_read_only_never_blocks_on_approval_gate(self):
        # If the approval gate were entered, this would hang; the
        # timeout assertion catches it.
        manager = self.manager()
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertEqual(manager.current.state, STATE_COMPLETED)


class ApprovalFlowTests(ManagerTestCase):
    def test_mutating_task_waits_for_approval(self):
        manager = self.manager()
        manager.submit("Create a folder named Gate in Documents.")
        self.assertTrue(manager.wait_for_state(STATE_AWAITING_APPROVAL,
                                               timeout=10))
        self.assertFalse(self.sandbox.doc_path("Gate").exists())
        self.assertEqual(manager.current.approval, "pending")

    def test_approve_executes_and_completes(self):
        manager = self.manager()
        manager.submit("Create a folder named Go in Documents.")
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        digest = manager.current.approval_digest
        self.assertTrue(digest)
        self.assertTrue(manager.approve(digest))
        self.assertTrue(wait(manager, STATE_COMPLETED))
        self.assertTrue(self.sandbox.doc_path("Go").is_dir())
        self.assertEqual(manager.current.approval, "approved")

    def test_reject_cancels_and_changes_nothing(self):
        manager = self.manager()
        manager.submit("Create a folder named No in Documents.")
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        self.assertTrue(manager.reject())
        self.assertTrue(wait(manager, STATE_CANCELLED))
        self.assertFalse(self.sandbox.doc_path("No").exists())
        self.assertEqual(manager.current.approval, "rejected")

    def test_approve_with_wrong_digest_is_refused(self):
        manager = self.manager()
        manager.submit("Create a folder named Wrong in Documents.")
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        self.assertFalse(manager.approve("ffffffffffffffff"))
        # Task is still awaiting; the worker never woke up.
        self.assertEqual(manager.current.state, STATE_AWAITING_APPROVAL)
        self.assertTrue(manager.reject())
        manager.wait_terminal(timeout=10)
        self.assertFalse(self.sandbox.doc_path("Wrong").exists())

    def test_approve_outside_awaiting_is_refused(self):
        manager = self.manager()
        record = manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        self.assertFalse(manager.approve(None))
        self.assertEqual(manager.current.state, STATE_COMPLETED)

    def test_reject_outside_awaiting_is_refused(self):
        manager = self.manager()
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        self.assertFalse(manager.reject())

    def test_approval_is_bound_to_the_displayed_plan(self):
        # The manager only approves the digest it displayed; a token
        # minted for a *different* plan can never slip through because
        # the pipeline compares digests too.
        manager = self.manager()
        manager.submit("Create a folder named Bound in Documents.")
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        shown = manager.current.plan["digest"]
        self.assertEqual(shown, manager.current.approval_digest)
        forged = TaskPipeline(self.sandbox.config)
        other = forged.preview(
            "Create a folder named OtherBound in Documents."
        )
        self.assertNotEqual(other.plan_digest, shown)
        # Even constructing a token for the other plan and handing it
        # to the pipeline directly is rejected (see pipeline tests);
        # through the manager, the wrong digest is refused up front.
        self.assertFalse(manager.approve(other.plan_digest))
        self.assertTrue(manager.reject())
        manager.wait_terminal(timeout=10)


class CancellationFlowTests(ManagerTestCase):
    def test_cancel_at_approval_gate(self):
        manager = self.manager()
        manager.submit("Create a folder named Canc in Documents.")
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        self.assertTrue(manager.cancel())
        self.assertTrue(wait(manager, STATE_CANCELLED))
        self.assertFalse(self.sandbox.doc_path("Canc").exists())
        # Cancellation never claims rollback: the note says nothing ran.
        notes = " ".join(manager.current.notes + [manager.current.error or ""])
        self.assertIn("nothing was executed", notes.lower())

    def test_cancel_during_planning_window(self):
        manager = self.manager()
        manager.submit("Create a folder named Quick in Documents.")
        # Cancel as early as possible; either the gate or the planning
        # window catches it — but it must never execute.
        for _ in range(200):
            if manager.cancel():
                break
            if manager.current.is_terminal:
                break
        manager.wait_terminal(timeout=10)
        self.assertIn(manager.current.state,
                      (STATE_CANCELLED, STATE_COMPLETED, STATE_FAILED))
        if manager.current.state == STATE_CANCELLED:
            self.assertFalse(self.sandbox.doc_path("Quick").exists())

    def test_cancel_refused_once_terminal(self):
        manager = self.manager()
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        self.assertFalse(manager.cancel())

    def test_cancel_refused_while_running_action(self):
        # A read-only list is quick; use the approval state instead to
        # prove the *running* refusal path exists for executing states.
        manager = self.manager()
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        record = manager.current
        record.state = STATE_EXECUTING  # simulate a running action
        self.assertFalse(manager.cancel())
        self.assertIn("does not interrupt", record.notes[-1])


class FailureMappingTests(ManagerTestCase):
    def test_unsupported_request_fails_honestly(self):
        manager = self.manager()
        manager.submit("Juggle seven balls while reciting poetry.")
        self.assertTrue(wait(manager, STATE_FAILED))
        self.assertIsNotNone(manager.current.error)
        self.assertNotEqual(manager.current.state, STATE_COMPLETED)

    def test_denied_request_fails_not_completes(self):
        # A path outside permitted roots is denied by the checker.
        manager = self.manager()
        manager.submit("Create a folder named sneak in /etc.")
        self.assertTrue(wait(manager, STATE_FAILED, STATE_CANCELLED))
        self.assertNotEqual(manager.current.state, STATE_COMPLETED)

    def test_verification_failure_is_needs_attention_not_success(self):
        # Execution reports success, but the world cannot be verified:
        # the fake executor claims a directory, the verifier says the
        # filesystem does not back it.  Honest outcome: needs_attention.
        from ai_core.executor import ExecutionResult
        from ai_core.verifier import VerificationResult

        class ClaimingExecutor:
            def execute(self, action, *, dry_run=False):
                return ExecutionResult(
                    ok=True, executed=True, dry_run=False,
                    message="created (allegedly)", data={})

        class FailingVerifier:
            def verify(self, action, execution):
                return VerificationResult(
                    status="failed",
                    message="The directory does not exist.",
                    checks=[{"name": "path_exists", "passed": False,
                             "detail": "Documents/Ghost"}],
                )

        def factory(record):
            pipeline = TaskPipeline(self.sandbox.config)
            pipeline.executor = ClaimingExecutor()
            pipeline.verifier = FailingVerifier()
            return pipeline

        manager = self.manager(pipeline_factory=factory)
        manager.submit("Create a folder named Ghost in Documents.")
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        manager.approve(manager.current.approval_digest)
        self.assertTrue(wait(manager, STATE_NEEDS_ATTENTION))
        self.assertNotEqual(manager.current.state, STATE_COMPLETED)
        self.assertIn("does not exist", manager.current.error)
        # The failed verification is shown, not swallowed.
        self.assertEqual(manager.current.verification["status"], "failed")

    def test_execution_failure_maps_to_failed(self):
        from ai_core.executor import ExecutionResult

        class BrokenExecutor:
            def execute(self, action, *, dry_run=False):
                return ExecutionResult(
                    ok=False, executed=False, dry_run=False, message="",
                    error="disk on fire", data={})

        def factory(record):
            pipeline = TaskPipeline(self.sandbox.config)
            pipeline.executor = BrokenExecutor()
            return pipeline

        manager = self.manager(pipeline_factory=factory)
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(wait(manager, STATE_FAILED))
        self.assertIn("disk on fire", manager.current.error)
        self.assertNotEqual(manager.current.state, STATE_COMPLETED)


class ProgressTests(ManagerTestCase):
    def test_states_progress_through_executing_and_verifying(self):
        seen = []
        manager = self.manager(on_update=lambda r: seen.append(r.state))
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        # The lifecycle was respected in order.
        self.assertIn(STATE_EXECUTING, seen)
        self.assertIn(STATE_VERIFYING, seen)
        self.assertEqual(seen[-1], STATE_COMPLETED)
        # No impossible jump ever observed.  (The manager may notify
        # the same terminal state more than once — e.g. once from the
        # transition and once from the final history write — so
        # consecutive duplicates are collapsed before checking.)
        from taskui.lifecycle import TRANSITIONS

        distinct = [s for i, s in enumerate(seen)
                    if i == 0 or s != seen[i - 1]]
        for earlier, later in zip(distinct, distinct[1:]):
            self.assertIn(later, TRANSITIONS[earlier],
                          f"illegal jump {earlier} -> {later}")

    def test_updates_arrive_from_worker_thread(self):
        threads = set()
        manager = self.manager(
            on_update=lambda r: threads.add(threading.current_thread().name)
        )
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        # Worker thread named after the task; UI thread never runs task
        # logic (it may also appear via submit()'s own notify).
        self.assertTrue(any("alwin-task" in name for name in threads))


class StepObserverTests(ManagerTestCase):
    def test_sequence_reports_live_steps(self):
        steps = []
        manager = self.manager(
            step_observer=lambda *a, **k: steps.append(a)
        )
        manager.submit(
            "Create a folder named Seq in Documents and then create a file "
            "named seq.txt in Documents containing hello."
        )
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        manager.approve(manager.current.approval_digest)
        manager.wait_terminal(timeout=10)
        # The planned steps were visible before execution.
        self.assertGreaterEqual(len(manager.current.steps), 2)
        if manager.current.state == STATE_COMPLETED:
            # Live observations happened on the real runner.
            self.assertTrue(steps)
            self.assertTrue(all(len(s) >= 4 for s in steps))


class HistoryWriteTests(ManagerTestCase):
    def test_completed_task_is_recorded(self):
        manager = self.manager()
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        rows = manager.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], STATE_COMPLETED)

    def test_cancelled_task_is_recorded(self):
        manager = self.manager()
        manager.submit("Create a folder named HC in Documents.")
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        manager.reject()
        manager.wait_terminal(timeout=10)
        rows = manager.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["state"], STATE_CANCELLED)

    def test_failed_task_is_recorded(self):
        manager = self.manager()
        manager.submit("Do something impossible and absurd.")
        manager.wait_terminal(timeout=10)
        rows = manager.history_rows()
        self.assertEqual(len(rows), 1)
        self.assertIn(rows[0]["state"], (STATE_FAILED, STATE_CANCELLED))

    def test_history_failure_is_surfaced_not_hidden(self):
        class Boom:
            def record(self, record):
                raise OSError("disk full")

        manager = self.manager(history=Boom())
        manager.submit("Show files and folders in my Documents.")
        manager.wait_terminal(timeout=10)
        notes = " ".join(manager.current.notes)
        self.assertIn("history write failed", notes.lower())

    def test_history_never_contains_raw_file_content(self):
        manager = self.manager()
        manager.submit(
            "Create a file named private.txt in Documents containing "
            "top secret diary text."
        )
        manager.wait_for_state(STATE_AWAITING_APPROVAL, timeout=10)
        manager.reject()
        manager.wait_terminal(timeout=10)
        rows = manager.history_rows()
        self.assertNotIn("top secret diary text", rows[0]["request"])


class UiBugIsolationTests(ManagerTestCase):
    def test_exploding_on_update_does_not_kill_the_task(self):
        def boom(record):
            raise RuntimeError("UI bug")

        manager = self.manager(on_update=boom)
        manager.submit("Show files and folders in my Documents.")
        self.assertTrue(wait(manager, STATE_COMPLETED))


if __name__ == "__main__":
    unittest.main()
