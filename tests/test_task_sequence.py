"""v0.4 multi-step task-sequence tests (mocks and temporary directories only).

A sequence is parsed only when *every* part is a real command (which is
what keeps v0.1 content containing the word "then" working), planned
step by step through the same validator, permission-checked step by
step, approved once as a whole, executed with stop-on-failure semantics,
and re-verified step by step: a partial run is never reported as full
success.
"""

from __future__ import annotations

import unittest

from ai_core.command_parser import (
    CREATE_TEXT_FILE,
    TASK_SEQUENCE,
    parse_command,
)
from ai_core.planner import PlanValidationError, describe_action, plan_from_intent, validate_action
from ai_core.pipeline import (
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
)
from ai_core.verifier import STATUS_PASSED
from integrations.automation.sequence import (
    STATUS_DENIED,
    STATUS_SKIPPED,
    TaskSequenceRunner,
)
from tests.automation_fakes import AutomationSandbox, LyingAdapter

FOLDER_THEN_FILE = (
    "Create a folder named Batch in Documents then create a file named "
    "log.txt in Documents containing done"
)


# --------------------------------------------------------------------------- #
# Parsing: sequences are only sequences when every step is a command
# --------------------------------------------------------------------------- #


class TestSequenceParsing(unittest.TestCase):
    def test_two_step_sequence(self):
        intent = parse_command(FOLDER_THEN_FILE)
        self.assertEqual(intent.intent, TASK_SEQUENCE)
        self.assertEqual(len(intent.slots["commands"]), 2)

    def test_and_then_separator_and_three_steps(self):
        intent = parse_command(
            "Create a folder named A in Documents and then create a folder "
            "named B in Documents and then list files in Documents"
        )
        self.assertEqual(intent.intent, TASK_SEQUENCE)
        self.assertEqual(len(intent.slots["commands"]), 3)

    def test_automation_steps_are_sequenceable(self):
        intent = parse_command(
            "Inspect the state of LibreOffice Writer then find the Save "
            "button in LibreOffice Writer"
        )
        self.assertEqual(intent.intent, TASK_SEQUENCE)

    def test_then_inside_file_content_stays_v_0_1_behavior(self):
        intent = parse_command(
            "Create a file named todo.txt in Documents containing buy milk then eggs"
        )
        self.assertEqual(intent.intent, CREATE_TEXT_FILE)

    def test_quoted_then_is_not_a_separator(self):
        intent = parse_command(
            'Create a report named s.txt in Documents titled "A" containing "x then y"'
        )
        self.assertNotEqual(intent.intent, TASK_SEQUENCE)

    def test_unsupported_part_abandons_the_split(self):
        intent = parse_command("List files in Documents then dance the macarena")
        self.assertNotEqual(intent.intent, TASK_SEQUENCE)


# --------------------------------------------------------------------------- #
# Planning and validation: every step is validated independently
# --------------------------------------------------------------------------- #


class TestSequencePlanning(unittest.TestCase):
    def test_plan_builds_validated_steps(self):
        intent = parse_command(FOLDER_THEN_FILE)
        action = plan_from_intent(intent, default_location="Documents")
        self.assertEqual(action["action"], "task_sequence")
        self.assertEqual(len(action["steps"]), 2)
        self.assertEqual(action["steps"][0]["action"], "create_directory")
        self.assertEqual(action["steps"][1]["action"], "create_text_file")

    def test_description_enumerates_every_step(self):
        intent = parse_command(FOLDER_THEN_FILE)
        action = plan_from_intent(intent, default_location="Documents")
        from pathlib import Path

        description = describe_action(action, home=Path("/home/u"))
        self.assertIn("2 step(s)", description)
        self.assertIn("->", description)

    def test_step_plan_error_names_the_step(self):
        intent = parse_command(
            "Activate the OK button in notepad then list files in Documents"
        )
        with self.assertRaises(PlanValidationError) as ctx:
            plan_from_intent(intent, default_location="Documents")
        self.assertIn("Step 1", str(ctx.exception))

    def test_nested_sequence_is_rejected_even_from_json(self):
        inner = {
            "action": "task_sequence",
            "steps": [{"action": "inspect_application", "application": "libreoffice-writer"}],
        }
        with self.assertRaises(PlanValidationError):
            validate_action({"action": "task_sequence", "steps": [inner]})

    def test_step_count_is_bounded(self):
        step = {"action": "inspect_application", "application": "libreoffice-writer"}
        with self.assertRaises(PlanValidationError) as ctx:
            validate_action({"action": "task_sequence", "steps": [step] * 9})
        self.assertIn("at most 8", str(ctx.exception))


# --------------------------------------------------------------------------- #
# Permissions: each step is checked individually; first denial denies all
# --------------------------------------------------------------------------- #


class TestSequencePermissions(unittest.TestCase):
    def test_every_step_must_be_permitted(self):
        s = AutomationSandbox()
        try:
            decision = s.checker.check_action(
                {
                    "action": "task_sequence",
                    "steps": [
                        {"action": "create_directory", "path": "Documents/A"},
                        {
                            "action": "read_control",
                            "application": "evince",
                            "control": {"role": "label", "name": "x"},
                        },
                        {"action": "create_directory", "path": "Documents/B"},
                    ],
                }
            )
            self.assertFalse(decision.allowed)
            self.assertIn("step 2", decision.reason)
        finally:
            s.cleanup()

    def test_traversal_step_denies_the_sequence(self):
        s = AutomationSandbox()
        try:
            decision = s.checker.check_action(
                {
                    "action": "task_sequence",
                    "steps": [{"action": "create_directory", "path": "../evil"}],
                }
            )
            self.assertFalse(decision.allowed)
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Execution: stop on failure, skip dependents, verify every step
# --------------------------------------------------------------------------- #


class TestSequenceExecution(unittest.TestCase):
    def test_all_steps_succeed_and_are_reverified(self):
        s = AutomationSandbox()
        try:
            report = s.auto_pipeline().run(FOLDER_THEN_FILE)
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            steps = report.execution.data["steps"]
            self.assertEqual([step["status"] for step in steps], ["ok", "ok"])
            self.assertTrue(s.doc_path("Batch").is_dir())
            self.assertEqual(
                s.doc_path("log.txt").read_text(encoding="utf-8"), "done"
            )
            # Every step was verified independently, with its own checks.
            self.assertEqual(report.verification["status"], STATUS_PASSED)
            names = {check["name"] for check in report.verification["checks"]}
            self.assertIn("step_1:path_exists", names)
            self.assertIn("step_2:path_exists", names)
        finally:
            s.cleanup()

    def test_first_failure_stops_dependent_steps(self):
        s = AutomationSandbox()
        try:
            report = s.auto_pipeline().run(
                "Create a folder named First in Documents then read the text "
                "of the Missing label in LibreOffice Writer then create a "
                "folder named Third in Documents"
            )
            self.assertEqual(report.status, STATUS_FAILED)
            steps = report.execution.data["steps"]
            self.assertEqual(
                [step["status"] for step in steps], ["ok", "failed", "skipped"]
            )
            self.assertEqual(report.execution.data["failed_step"], 2)
            # The dependent step really did not run.
            self.assertTrue(s.doc_path("First").is_dir())
            self.assertFalse(s.doc_path("Third").exists())
            self.assertIn("NOT reported as successful", report.error)
        finally:
            s.cleanup()

    def test_runtime_permission_denial_stops_the_sequence(self):
        s = AutomationSandbox()
        try:
            action = validate_action(
                {
                    "action": "task_sequence",
                    "steps": [
                        {"action": "create_directory", "path": "Documents/Step1"},
                        {
                            "action": "read_control",
                            "application": "evince",
                            "control": {"role": "label", "name": "x"},
                        },
                        {"action": "create_directory", "path": "Documents/Step3"},
                    ],
                }
            )
            runner = TaskSequenceRunner(s.checker, dispatcher=s.dispatcher)
            result = runner.run(action)
            self.assertFalse(result.ok)
            statuses = [step["status"] for step in result.data["steps"]]
            self.assertEqual(statuses, ["ok", STATUS_DENIED, STATUS_SKIPPED])
            self.assertEqual(result.data["failed_step"], 2)
            self.assertTrue(s.doc_path("Step1").is_dir())
            self.assertFalse(s.doc_path("Step3").exists())
        finally:
            s.cleanup()

    def test_step_data_is_kept_for_per_step_verification(self):
        s = AutomationSandbox()
        try:
            report = s.auto_pipeline().run(
                "Inspect the state of LibreOffice Writer then find the Save "
                "button in LibreOffice Writer"
            )
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            steps = report.execution.data["steps"]
            self.assertIn("snapshot", steps[0]["data"])
            self.assertIn("control", steps[1]["data"])
        finally:
            s.cleanup()

    def test_lying_step_data_fails_sequence_verification(self):
        s = AutomationSandbox(adapter_factory=LyingAdapter)
        try:
            report = s.auto_pipeline().run(
                "Inspect the state of LibreOffice Writer then find the Save "
                "button in LibreOffice Writer"
            )
            # Execution succeeded, but the second step's reported control
            # does not match its validated locator: the *whole* task
            # fails verification instead of being reported successful.
            self.assertEqual(report.status, STATUS_FAILED)
            self.assertIn("step 2", report.error)
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Approval and dry runs
# --------------------------------------------------------------------------- #


class TestSequenceApproval(unittest.TestCase):
    def test_sequence_is_rejected_without_approval(self):
        s = AutomationSandbox()
        try:
            report = s.auto_pipeline(approve=False).run(FOLDER_THEN_FILE)
            self.assertEqual(report.status, STATUS_REJECTED)
            self.assertFalse(s.doc_path("Batch").exists())
            self.assertFalse(s.doc_path("log.txt").exists())
        finally:
            s.cleanup()

    def test_even_read_only_steps_require_approval_for_a_sequence(self):
        # task_sequence is a mutating action (it *runs* other actions),
        # so the approver is invoked even when every step is read-only.
        s = AutomationSandbox()
        try:
            report = s.unapproved_auto_pipeline().run(
                "Inspect the state of LibreOffice Writer then find the Save "
                "button in LibreOffice Writer"
            )
            self.assertEqual(report.status, STATUS_REJECTED)
        finally:
            s.cleanup()

    def test_dry_run_executes_no_step_and_never_asks(self):
        s = AutomationSandbox()
        try:
            report = s.unapproved_auto_pipeline().run(
                FOLDER_THEN_FILE, dry_run=True
            )
            self.assertEqual(report.status, STATUS_DRY_RUN)
            self.assertEqual(report.approval, "dry_run")
            self.assertFalse(s.doc_path("Batch").exists())
            self.assertFalse(s.doc_path("log.txt").exists())
            steps = report.execution.data["steps"]
            self.assertTrue(all(step["status"] == "dry_run" for step in steps))
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Audit
# --------------------------------------------------------------------------- #


class TestSequenceAudit(unittest.TestCase):
    def test_sequence_run_is_audited(self):
        s = AutomationSandbox()
        try:
            report = s.auto_pipeline().run(FOLDER_THEN_FILE)
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            record = s.audit_records()[-1]
            self.assertEqual(record["action"]["action"], "task_sequence")
            self.assertEqual(record["status"], STATUS_SUCCESS)
            self.assertEqual(len(record["action"]["steps"]), 2)
            # Step content is redacted like any other content.
            step_two = record["action"]["steps"][1]
            self.assertEqual(
                step_two["content"], "<redacted:4 chars>"  # "done"
            )
        finally:
            s.cleanup()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
