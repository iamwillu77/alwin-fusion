"""v0.5 two-phase pipeline tests: preview() + run_previewed().

Covers the API the task experience is built on:

* preview never executes and never audits,
* run_previewed on the same preview matches run() exactly (parity),
* approval tokens bind approval to one exact plan digest,
* a bare ``True`` approval is not honoured,
* permissions are re-checked at execution time,
* progress callbacks fire truthfully.

Temporary sandboxes and mocks only.
"""

from __future__ import annotations

import unittest

from ai_core.pipeline import (
    STATUS_DRY_RUN,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    ApprovalToken,
    TaskPipeline,
    plan_digest,
)
from tests.helpers import Sandbox


class PlanDigestTests(unittest.TestCase):
    def test_digest_is_stable_and_key_order_independent(self):
        a = {"action": "create_directory", "path": "x", "name": "y"}
        b = {"name": "y", "path": "x", "action": "create_directory"}
        self.assertEqual(plan_digest(a), plan_digest(b))

    def test_digest_changes_when_plan_changes(self):
        a = {"action": "create_directory", "path": "x"}
        b = {"action": "create_directory", "path": "z"}
        self.assertNotEqual(plan_digest(a), plan_digest(b))

    def test_empty_action_has_empty_digest(self):
        self.assertEqual(plan_digest(None), "")
        self.assertEqual(plan_digest({}), "")

    def test_digest_is_bounded_hex(self):
        digest = plan_digest({"action": "a"})
        self.assertEqual(len(digest), 16)
        int(digest, 16)  # must be hex


class PreviewTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.pipeline = self.sandbox.pipeline()

    def test_preview_plans_a_read_only_action(self):
        preview = self.pipeline.preview("Show files and folders in my Documents.")
        self.assertTrue(preview.planned)
        self.assertFalse(preview.requires_approval)
        self.assertEqual(preview.action["action"], "list_directory")
        self.assertTrue(preview.plan_digest)

    def test_preview_marks_mutating_action_as_needing_approval(self):
        preview = self.pipeline.preview(
            "Create a folder named Preview in Documents."
        )
        self.assertTrue(preview.planned)
        self.assertTrue(preview.requires_approval)

    def test_preview_never_executes(self):
        preview = self.pipeline.preview(
            "Create a folder named Preview in Documents."
        )
        self.assertFalse(self.sandbox.doc_path("Preview").exists())

    def test_preview_is_not_audited(self):
        preview = self.pipeline.preview("Show files and folders in my Documents.")
        audit = self.sandbox.config.audit_path
        if audit.exists():
            content = audit.read_text(encoding="utf-8")
            self.assertNotIn(preview.report.task_id, content)

    def test_preview_of_unsupported_reports_status(self):
        preview = self.pipeline.preview("Fly me to the moon.")
        self.assertFalse(preview.planned)
        self.assertEqual(preview.status, STATUS_UNSUPPORTED)

    def test_preview_carries_the_report_it_will_reuse(self):
        preview = self.pipeline.preview("Show files and folders in my Documents.")
        report = self.pipeline.run_previewed(preview, approval=False)
        self.assertIs(report, preview.report)
        self.assertEqual(report.task_id, preview.report.task_id)


class ParityTests(unittest.TestCase):
    """Two-phase (preview+run_previewed) == one-phase run()."""

    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)

    def _two_phase(self, command, *, approve, dry_run=False):
        pipeline = self.sandbox.pipeline(approve=approve)
        preview = pipeline.preview(command, dry_run=dry_run)
        return pipeline.run_previewed(
            preview, approval=ApprovalToken(preview.plan_digest)
            if approve else False
        )

    def test_read_only_parity(self):
        command = "Show files and folders in my Documents."
        one_phase = self.sandbox.pipeline().run(command)
        two_phase = self._two_phase(command, approve=False)
        self.assertEqual(one_phase.status, two_phase.status)
        self.assertEqual(one_phase.action, two_phase.action)

    def test_approved_mutating_parity(self):
        # Separate sandboxes so the two paths don't collide on the
        # never-overwrites rule.
        one_box = Sandbox()
        self.addCleanup(one_box.cleanup)
        two_box = Sandbox()
        self.addCleanup(two_box.cleanup)
        command = "Create a folder named Parity in Documents."
        one_phase = one_box.pipeline(approve=True).run(command)
        preview = two_box.pipeline(approve=True).preview(command)
        two_phase = two_box.pipeline(approve=True).run_previewed(
            preview, approval=ApprovalToken(preview.plan_digest)
        )
        self.assertEqual(one_phase.status, STATUS_SUCCESS)
        self.assertEqual(two_phase.status, STATUS_SUCCESS)
        self.assertEqual(one_phase.verification["status"],
                         two_phase.verification["status"])

    def test_rejected_parity(self):
        command = "Create a folder named Parity in Documents."
        one_phase = self.sandbox.pipeline(approve=False).run(command)
        two_phase = self._two_phase(command, approve=False)
        self.assertEqual(one_phase.status, STATUS_REJECTED)
        self.assertEqual(two_phase.status, STATUS_REJECTED)
        self.assertEqual(one_phase.approval, two_phase.approval)

    def test_dry_run_parity(self):
        command = "Create a folder named Parity in Documents."
        one_phase = self.sandbox.pipeline().run(command, dry_run=True)
        two_phase = self._two_phase(command, approve=False, dry_run=True)
        self.assertEqual(one_phase.status, STATUS_DRY_RUN)
        self.assertEqual(two_phase.status, STATUS_DRY_RUN)
        self.assertFalse(self.sandbox.doc_path("Parity").exists())

    def test_unsupported_parity_is_audited_in_both_paths(self):
        command = "Compose a symphony."
        one_phase = self.sandbox.pipeline().run(command)
        preview = self.sandbox.pipeline().preview(command)
        two_phase = self.sandbox.pipeline().run_previewed(preview)
        self.assertEqual(one_phase.status, STATUS_UNSUPPORTED)
        self.assertEqual(two_phase.status, STATUS_UNSUPPORTED)
        audit = self.sandbox.config.audit_path.read_text(encoding="utf-8")
        self.assertIn(one_phase.task_id, audit)
        self.assertIn(two_phase.task_id, audit)


class ApprovalTokenTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.addCleanup(self.sandbox.cleanup)
        self.pipeline = self.sandbox.pipeline()

    def test_matching_token_executes(self):
        preview = self.pipeline.preview(
            "Create a folder named Token in Documents."
        )
        report = self.pipeline.run_previewed(
            preview, approval=ApprovalToken(preview.plan_digest)
        )
        self.assertEqual(report.status, STATUS_SUCCESS)
        self.assertTrue(self.sandbox.doc_path("Token").is_dir())

    def test_wrong_digest_is_rejected_and_nothing_executes(self):
        preview = self.pipeline.preview(
            "Create a folder named Token in Documents."
        )
        report = self.pipeline.run_previewed(
            preview, approval=ApprovalToken("0000000000000000")
        )
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertEqual(report.approval, "invalidated")
        self.assertIn("plan changed", report.error.lower())
        self.assertFalse(self.sandbox.doc_path("Token").exists())

    def test_token_for_one_action_cannot_approve_another(self):
        preview_a = self.pipeline.preview(
            "Create a folder named Alpha in Documents."
        )
        preview_b = self.sandbox.pipeline().preview(
            "Create a folder named Beta in Documents."
        )
        report = self.pipeline.run_previewed(
            preview_b, approval=ApprovalToken(preview_a.plan_digest)
        )
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("Beta").exists())

    def test_reused_token_on_changed_preview_is_rejected(self):
        # Approve plan A, then re-preview (world changed -> new action
        # dict with different digest), and try to reuse the old token.
        preview_a = self.pipeline.preview(
            "Create a folder named Reuse in Documents."
        )
        token = ApprovalToken(preview_a.plan_digest)
        preview_b = self.pipeline.preview(
            "Create a file named other.txt in Documents containing hi."
        )
        report = self.pipeline.run_previewed(preview_b, approval=token)
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("other.txt").exists())

    def test_bare_true_is_not_honoured(self):
        # A bare True falls through to the interactive approver, whose
        # default says "no": approval must be bound to a plan, never
        # granted to "whatever comes next".
        pipeline = self.sandbox.pipeline(approve=False)
        preview = pipeline.preview(
            "Create a folder named Bare in Documents."
        )
        report = pipeline.run_previewed(preview, approval=True)
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("Bare").exists())

    def test_bare_true_cannot_bypass_a_rejecting_approver(self):
        # Even when the interactive approver would say yes, a bare True
        # is not an ApprovalToken; it is routed through the approver.
        # With no approver wired, the default is "no".
        pipeline = TaskPipeline(self.sandbox.config)
        preview = pipeline.preview(
            "Create a folder named Bare2 in Documents."
        )
        report = pipeline.run_previewed(preview, approval=True)
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("Bare2").exists())

    def test_false_rejects_without_touching_disk(self):
        preview = self.pipeline.preview(
            "Create a folder named No in Documents."
        )
        report = self.pipeline.run_previewed(preview, approval=False)
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertEqual(report.approval, "rejected")
        self.assertFalse(self.sandbox.doc_path("No").exists())


class PermissionRecheckTests(unittest.TestCase):
    def test_permission_revoked_after_preview_blocks_execution(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        pipeline = sandbox.pipeline()
        preview = pipeline.preview(
            "Create a folder named Revoked in Documents."
        )
        self.assertTrue(preview.planned)

        # The world changed: the checker now denies everything.
        from ai_core.permissions import PermissionDecision

        class DenyEverything:
            home = sandbox.config.home

            def check_action(self, action):
                return PermissionDecision(
                    allowed=False,
                    operation=str(action.get("action", "")),
                    requested=str(action.get("path", "")),
                    resolved=None,
                    reason="roots changed under you",
                )

        pipeline.checker = DenyEverything()
        report = pipeline.run_previewed(
            preview, approval=ApprovalToken(preview.plan_digest)
        )
        self.assertEqual(report.status, "denied")
        self.assertIn("roots changed", report.error)
        self.assertFalse(sandbox.doc_path("Revoked").exists())


class ProgressTests(unittest.TestCase):
    def test_progress_reports_executing_then_verifying(self):
        sandbox = Sandbox()
        self.addCleanup(sandbox.cleanup)
        pipeline = sandbox.pipeline()
        seen = []
        report = pipeline.run(
            "Show files and folders in my Documents.",
            progress=seen.append,
        )
        self.assertEqual(report.status, STATUS_SUCCESS)
        self.assertEqual(seen, ["executing", "verifying"])


if __name__ == "__main__":
    unittest.main()
