"""Tests for the verification stage.

Covers: real success verification, verification failure when execution
only *claims* success, execution failures, dry-run skipping, content
mismatch and explain/list/read verification rules.
"""

from __future__ import annotations

import unittest

from ai_core.executor import ExecutionResult, Executor
from ai_core.permissions import PermissionChecker
from ai_core.planner import validate_action
from ai_core.verifier import (
    STATUS_FAILED,
    STATUS_PASSED,
    STATUS_SKIPPED,
    Verifier,
)
from integrations import ActionDispatcher, build_default_dispatcher
from integrations.filesystem.tools import ToolResult

from helpers import Sandbox


class VerifierTestCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()
        self.checker = PermissionChecker(self.sandbox.config)
        self.dispatcher = build_default_dispatcher(self.checker)
        self.executor = Executor(self.dispatcher, self.checker)
        self.verifier = Verifier(self.checker)

    def tearDown(self):
        self.sandbox.cleanup()


class TestVerificationSuccess(VerifierTestCase):
    def test_directory_creation_verified(self):
        action = validate_action(
            {"action": "create_directory", "path": "Documents/Real"}
        )
        execution = self.executor.execute(action, dry_run=False)
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_PASSED)
        self.assertTrue(result.verified)
        names = [c["name"] for c in result.checks]
        self.assertIn("path_exists", names)
        self.assertIn("is_directory", names)
        self.assertIn("not_a_symlink", names)

    def test_text_file_creation_verified_with_content(self):
        action = validate_action(
            {
                "action": "create_text_file",
                "path": "Documents/ok.txt",
                "content": "payload",
            }
        )
        execution = self.executor.execute(action, dry_run=False)
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_PASSED)
        self.assertIn("content_matches", [c["name"] for c in result.checks])

    def test_list_directory_verified(self):
        self.sandbox.doc_path("x.txt").write_text("1")
        action = validate_action({"action": "list_directory", "path": "Documents"})
        execution = self.executor.execute(action, dry_run=False)
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_PASSED)

    def test_read_file_verified(self):
        self.sandbox.doc_path("r.txt").write_text("readable")
        action = validate_action({"action": "read_file", "path": "Documents/r.txt"})
        execution = self.executor.execute(action, dry_run=False)
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_PASSED)

    def test_explain_verified(self):
        action = validate_action(
            {
                "action": "explain",
                "target": {"action": "create_directory", "path": "Documents/E"},
            }
        )
        execution = self.executor.execute(action, dry_run=False)
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_PASSED)


class TestVerificationFailure(VerifierTestCase):
    def test_claimed_success_without_real_work_fails_verification(self):
        """A handler that lies about success must not pass verification."""

        lying_dispatcher = ActionDispatcher()
        lying_dispatcher.register(
            "create_directory",
            lambda action, *, dry_run=False: ToolResult(
                ok=True,
                message=f"Created directory {action['path']}",
                changed=True,
                data={"path": action["path"]},
            ),
            description="pretend",
            mutates=True,
            requires_approval=True,
        )
        executor = Executor(lying_dispatcher, self.checker)
        action = validate_action(
            {"action": "create_directory", "path": "Documents/Liar"}
        )
        execution = executor.execute(action, dry_run=False)
        self.assertTrue(execution.ok)  # the handler claims success...
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)  # ...verification disagrees
        self.assertFalse(self.sandbox.doc_path("Liar").exists())
        self.assertIn("path_exists", [c["name"] for c in result.checks])

    def test_execution_failure_cannot_be_verified(self):
        action = validate_action(
            {"action": "create_directory", "path": "Documents/Bad"}
        )
        execution = ExecutionResult(
            ok=False,
            executed=False,
            dry_run=False,
            message="",
            error="Could not create directory: boom",
        )
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("cannot be verified", result.message)

    def test_file_deleted_after_execution_fails_verification(self):
        action = validate_action(
            {
                "action": "create_text_file",
                "path": "Documents/vanish.txt",
                "content": "here",
            }
        )
        execution = self.executor.execute(action, dry_run=False)
        self.assertTrue(execution.ok)
        # Simulate the world changing between execution and verification.
        self.sandbox.doc_path("vanish.txt").unlink()
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)

    def test_content_drift_fails_verification(self):
        action = validate_action(
            {"action": "create_text_file", "path": "Documents/d.txt", "content": "a"}
        )
        execution = self.executor.execute(action, dry_run=False)
        self.sandbox.doc_path("d.txt").write_text("b")  # drift after the fact
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("content_matches", [c["name"] for c in result.checks])

    def test_read_content_mismatch_fails(self):
        self.sandbox.doc_path("m.txt").write_text("original")
        action = validate_action({"action": "read_file", "path": "Documents/m.txt"})
        execution = self.executor.execute(action, dry_run=False)
        execution.data["content"] = "tampered"
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)

    def test_list_entry_mismatch_fails(self):
        self.sandbox.doc_path("one.txt").write_text("1")
        action = validate_action({"action": "list_directory", "path": "Documents"})
        execution = self.executor.execute(action, dry_run=False)
        execution.data["entries"] = [{"name": "ghost", "type": "file"}]
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)

    def test_unknown_action_has_no_verification_rule(self):
        action = {"action": "mystery_action", "path": "Documents"}
        execution = ExecutionResult(
            ok=True, executed=True, dry_run=False, message="done"
        )
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)

    def test_verification_error_is_a_failure_not_success(self):
        action = validate_action({"action": "create_directory", "path": "Documents/X"})
        execution = self.executor.execute(action, dry_run=False)
        # Break the checker so verification itself raises internally.
        self.checker.roots = ()
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("error", result.message.lower())


class TestDryRunVerification(VerifierTestCase):
    def test_dry_run_is_skipped_not_passed(self):
        action = validate_action(
            {"action": "create_directory", "path": "Documents/Dry"}
        )
        execution = self.executor.execute(action, dry_run=True)
        result = self.verifier.verify(action, execution)
        self.assertEqual(result.status, STATUS_SKIPPED)
        self.assertFalse(result.verified)
        self.assertFalse(self.sandbox.doc_path("Dry").exists())


if __name__ == "__main__":
    unittest.main()
