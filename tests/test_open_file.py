"""End-to-end open_file / detect_applications pipeline tests (v0.3.0).

Every test runs the full trust pipeline
(parse -> plan -> permission -> approval -> execute -> verify -> audit)
inside a temporary sandbox with an *injected* fake launcher: no real
desktop application is ever started and the developer's real Documents
directory is never touched.

Covered: opening permitted files with the approved argv, approval
rejection, dry runs (approver must not even be called), unknown
integration ids, unavailable applications, traversal and symlink
escapes, outside-root refusal, missing files, launch-failure
reporting, lying launch results failing verification, audit contents
(redaction + required fields), and preservation of v0.1/v0.2 commands.
"""

from __future__ import annotations

import json
import pathlib
import tempfile
import unittest

from ai_core.pipeline import (
    STATUS_DENIED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
)
from integrations import build_default_dispatcher
from integrations.apps.launch import LaunchAdapter, LaunchError, LaunchOutcome
from integrations.apps.tools import ApplicationTools
from tests.helpers import Sandbox, UnapprovedPipeline


class FakeProcess:
    def __init__(self, returncode=None, pid=4242):
        self.pid = pid
        self._rc = returncode

    def poll(self):
        return self._rc


class AppSandbox(Sandbox):
    """Sandbox with fake app environment (which/popen) attached."""

    def __init__(self, *, installed=("evince", "xdg-open"),
                 popen_returncode=None, popen_error=None):
        super().__init__()
        from ai_core.permissions import PermissionChecker

        self.checker = PermissionChecker(self.config)
        self.installed = set(installed)
        self.popen_calls = []
        self.popen_returncode = popen_returncode
        self.popen_error = popen_error

        def fake_which(name):
            return f"/usr/bin/{name}" if name in self.installed else None

        def fake_popen(argv, **kwargs):
            self.popen_calls.append((list(argv), kwargs))
            if self.popen_error is not None:
                raise self.popen_error
            return FakeProcess(returncode=self.popen_returncode)

        self.which = fake_which
        self.launcher = LaunchAdapter(popen=fake_popen, which=fake_which)
        self.app_tools = ApplicationTools(
            self.checker,
            launcher=self.launcher,
            which=fake_which,
            desktop_dirs=(),  # never scan the real desktop-entry dirs
        )
        self.dispatcher = build_default_dispatcher(
            self.checker, app_tools=self.app_tools
        )

    def app_pipeline(self, *, approve=True, **kwargs):
        return self.pipeline(
            approve=approve, dispatcher=self.dispatcher, **kwargs
        )

    def unapproved_app_pipeline(self, **kwargs):
        return UnapprovedPipeline(
            self.config, dispatcher=self.dispatcher, **kwargs
        )

    def audit_records(self):
        path = self.config.audit_path
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]


def make_pdf(sandbox, name="summary.pdf", content=b"%PDF-1.4 test"):
    target = sandbox.doc_path(name)
    target.write_bytes(content)
    return target


def make_txt(sandbox, name="note.txt", content="hello"):
    target = sandbox.doc_path(name)
    target.write_text(content, encoding="utf-8")
    return target


# --------------------------------------------------------------------------- #
# Opening permitted files
# --------------------------------------------------------------------------- #


class TestOpenPermittedFiles(unittest.TestCase):
    def test_opens_permitted_pdf_with_evince_argv_only(self):
        s = AppSandbox()
        try:
            pdf = make_pdf(s)
            report = s.app_pipeline().run(
                "Open summary.pdf in Documents with evince"
            )
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            self.assertEqual(len(s.popen_calls), 1)
            argv, kwargs = s.popen_calls[0]
            self.assertEqual(argv, ["/usr/bin/evince", str(pdf)])
            self.assertFalse(kwargs["shell"])
            # Verification confirms only the process start.
            names = [c["name"] for c in report.verification["checks"]]
            self.assertIn("process_started", names)
            self.assertIn("path_exists", names)
            self.assertEqual(report.verification["status"], "passed")
        finally:
            s.cleanup()

    def test_opens_with_desktop_default_association(self):
        s = AppSandbox()
        try:
            txt = make_txt(s)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            argv, kwargs = s.popen_calls[0]
            self.assertEqual(argv, ["/usr/bin/xdg-open", str(txt)])
            self.assertFalse(kwargs["shell"])
        finally:
            s.cleanup()

    def test_opens_xlsx_with_libreoffice_calc_when_installed(self):
        s = AppSandbox(installed=("soffice",))
        try:
            target = s.doc_path("sales.xlsx")
            target.write_bytes(b"fake-xlsx")
            report = s.app_pipeline().run(
                "Open sales.xlsx in Documents with LibreOffice Calc"
            )
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            argv, _ = s.popen_calls[0]
            self.assertEqual(argv, ["/usr/bin/soffice", "--calc", str(target)])
        finally:
            s.cleanup()

    def test_honest_message_does_not_claim_completion(self):
        s = AppSandbox()
        try:
            make_txt(s)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_SUCCESS)
            message = report.verification["message"]
            self.assertIn("process was started", message)
            self.assertIn("does not inspect or control", message)
            for forbidden in ("task completed", "document edited",
                              "visually inspected", "recalculated",
                              "success of the task"):
                self.assertNotIn(forbidden, message.lower())
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Approval and dry-run behaviour
# --------------------------------------------------------------------------- #


class TestApprovalAndDryRun(unittest.TestCase):
    def test_approval_rejection_prevents_launch(self):
        s = AppSandbox()
        try:
            make_txt(s)
            report = s.app_pipeline(approve=False).run(
                "Open note.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_REJECTED)
            self.assertEqual(s.popen_calls, [])
            self.assertEqual(report.approval, "rejected")
        finally:
            s.cleanup()

    def test_dry_run_never_calls_approver_or_popen(self):
        s = AppSandbox()
        try:
            make_txt(s)
            # UnapprovedPipeline fails if the approver is ever called.
            report = s.unapproved_app_pipeline().run(
                "Open note.txt in Documents with the default application",
                dry_run=True,
            )
            self.assertEqual(report.status, STATUS_DRY_RUN)
            self.assertEqual(report.approval, "dry_run")
            self.assertEqual(s.popen_calls, [])
        finally:
            s.cleanup()

    def test_detection_never_requires_approval(self):
        s = AppSandbox()
        try:
            report = s.unapproved_app_pipeline().run(
                "Which applications are installed?"
            )
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            self.assertEqual(s.popen_calls, [])
            self.assertNotIn(report.action["action"],
                             frozenset({"open_file"}))
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Missing / unknown / unavailable applications
# --------------------------------------------------------------------------- #


class TestMissingApplication(unittest.TestCase):
    def test_uninstalled_app_fails_without_launch_or_install(self):
        s = AppSandbox(installed=("xdg-open",))
        try:
            make_txt(s)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with LibreOffice Writer"
            )
            self.assertEqual(report.status, STATUS_FAILED)
            self.assertIn("never installs", report.error)
            self.assertEqual(s.popen_calls, [])
            # No install attempt either: detection never ran a command.
            self.assertEqual(report.execution.executed, False)
        finally:
            s.cleanup()

    def test_uninstalled_app_detection_reports_not_installed(self):
        s = AppSandbox(installed=())
        try:
            report = s.app_pipeline().run("Which applications are installed?")
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            statuses = {
                i["id"]: i["status"]
                for i in report.execution.data["integrations"]
            }
            self.assertEqual(statuses["libreoffice-writer"], "not_installed")
            self.assertEqual(s.popen_calls, [])
        finally:
            s.cleanup()

    def test_unknown_integration_from_user_text_is_unsupported(self):
        s = AppSandbox()
        try:
            make_txt(s)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with Notepad"
            )
            self.assertEqual(report.status, STATUS_UNSUPPORTED)
            self.assertIn("Unknown application", report.error)
            self.assertEqual(s.popen_calls, [])
        finally:
            s.cleanup()

    def test_unknown_integration_from_llm_is_rejected_before_execution(self):
        from ai_core.planner import PlanValidationError, plan_via_llm
        from tests.test_command_parser import FakeLLMClient

        client = FakeLLMClient(
            response='{"action": "open_file", "path": "Documents/note.txt",'
                     ' "application": "hacker-tool"}'
        )
        with self.assertRaises(PlanValidationError):
            plan_via_llm("open note.txt with hacker-tool", client)

    def test_installed_but_unsupported_reported(self):
        s = AppSandbox()
        try:
            report = s.app_pipeline().run("Which applications are installed?")
            self.assertEqual(report.status, STATUS_SUCCESS)
            # v0.3 knows about common unsupported viewers via the report.
            self.assertIn("unsupported", report.execution.data)
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Path security
# --------------------------------------------------------------------------- #


class TestPathSecurity(unittest.TestCase):
    def test_traversal_path_rejected_at_planning(self):
        s = AppSandbox()
        try:
            report = s.app_pipeline().run(
                "Open ../secret.txt in Documents with evince"
            )
            self.assertIn(report.status, (STATUS_UNSUPPORTED, STATUS_DENIED))
            self.assertEqual(s.popen_calls, [])
        finally:
            s.cleanup()

    def test_symlink_escape_denied(self):
        s = AppSandbox()
        try:
            outside = s.root / "outside.txt"
            outside.write_text("secret outside roots")
            link = s.doc_path("link.txt")
            link.symlink_to(outside)
            report = s.app_pipeline().run(
                "Open link.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_DENIED)
            self.assertFalse(report.permission["allowed"])
            self.assertEqual(s.popen_calls, [])
        finally:
            s.cleanup()

    def test_file_outside_roots_denied(self):
        s = AppSandbox()
        try:
            downloads = s.home / "Downloads"
            downloads.mkdir()
            (downloads / "x.txt").write_text("not permitted")
            report = s.app_pipeline().run(
                "Open x.txt from Downloads with the default application"
            )
            self.assertEqual(report.status, STATUS_DENIED)
            self.assertEqual(s.popen_calls, [])
        finally:
            s.cleanup()

    def test_missing_file_reports_failure(self):
        s = AppSandbox()
        try:
            report = s.app_pipeline().run(
                "Open ghost.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_FAILED)
            self.assertIn("does not exist", report.error)
            self.assertEqual(s.popen_calls, [])
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Launch-failure reporting and verification
# --------------------------------------------------------------------------- #


class TestLaunchFailures(unittest.TestCase):
    def test_popen_oserror_reported_as_failure(self):
        s = AppSandbox(popen_error=OSError(13, "Permission denied"))
        try:
            make_txt(s)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_FAILED)
            self.assertIn("Permission denied", report.error)
            self.assertEqual(report.verification["status"], "failed")
        finally:
            s.cleanup()

    def test_immediate_nonzero_exit_reported_as_failure(self):
        s = AppSandbox(popen_returncode=7)
        try:
            make_pdf(s)
            report = s.app_pipeline().run(
                "Open summary.pdf in Documents with evince"
            )
            self.assertEqual(report.status, STATUS_FAILED)
            self.assertIn("status 7", report.error)
            self.assertIn("not opened", report.error)
        finally:
            s.cleanup()

    def test_process_still_running_verifies_as_passed(self):
        s = AppSandbox()  # FakeProcess.poll() -> None (running)
        try:
            make_pdf(s)
            report = s.app_pipeline().run(
                "Open summary.pdf in Documents with evince"
            )
            self.assertEqual(report.status, STATUS_SUCCESS)
            detail = report.execution.data["detail"]
            self.assertIn("still running", detail)
            self.assertIn("does not wait for or control", detail)
        finally:
            s.cleanup()

    def test_clean_handoff_exit_zero_verifies(self):
        s = AppSandbox(popen_returncode=0)
        try:
            make_txt(s)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_SUCCESS)
            self.assertIn("exited cleanly", report.execution.data["detail"])
        finally:
            s.cleanup()

    def test_lying_launch_result_fails_verification(self):
        """A handler claiming success without a real start must fail."""
        s = AppSandbox()
        try:
            make_txt(s)

            class LyingAdapter(LaunchAdapter):
                def launch(self, definition, file_path):
                    return LaunchOutcome(
                        started=False,
                        integration_id=definition.id,
                        application_name=definition.name,
                        executable="/usr/bin/xdg-open",
                        pid=None,
                        returncode=None,
                        detail="process started",  # the lie
                    )

            tools = ApplicationTools(
                s.checker,
                launcher=LyingAdapter(which=s.which),
                which=s.which,
                desktop_dirs=(),
            )
            dispatcher = build_default_dispatcher(s.checker, app_tools=tools)
            pipeline = s.pipeline(approve=True, dispatcher=dispatcher)
            report = pipeline.run(
                "Open note.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_FAILED)
            self.assertEqual(report.verification["status"], "failed")
            names = [c["name"] for c in report.verification["checks"]]
            self.assertIn("process_started", names)
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Audit requirements and redaction
# --------------------------------------------------------------------------- #


class TestAudit(unittest.TestCase):
    def test_audit_records_required_open_file_fields(self):
        s = AppSandbox()
        try:
            make_pdf(s)
            report = s.app_pipeline().run(
                "Open summary.pdf in Documents with evince"
            )
            self.assertEqual(report.status, STATUS_SUCCESS)
            records = s.audit_records()
            rec = next(
                r for r in records if r["action"].get("action") == "open_file"
            )
            # Required by the milestone.
            self.assertTrue(rec["task_id"])
            self.assertEqual(rec["action"]["application"], "evince")
            self.assertEqual(rec["action"]["path"], "Documents/summary.pdf")
            self.assertEqual(rec["permission"]["allowed"], True)
            self.assertEqual(rec["permission"]["operation"], "open")
            self.assertEqual(rec["approval"], "approved")
            self.assertTrue(rec["execution"]["ok"])
            self.assertEqual(rec["verification"]["status"], "passed")
            self.assertIsNone(rec["error"])
        finally:
            s.cleanup()

    def test_audit_records_failure_reason(self):
        s = AppSandbox(installed=())
        try:
            make_txt(s)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with LibreOffice Writer"
            )
            self.assertEqual(report.status, STATUS_FAILED)
            rec = s.audit_records()[-1]
            self.assertFalse(rec["execution"]["ok"])
            self.assertIn("never installs", rec["error"])
            self.assertEqual(rec["verification"]["status"], "failed")
        finally:
            s.cleanup()

    def test_audit_never_contains_file_contents(self):
        s = AppSandbox()
        try:
            secret = "SECRET-MARKER-8f3a do not log me"
            make_txt(s, "note.txt", secret)
            report = s.app_pipeline().run(
                "Open note.txt in Documents with the default application"
            )
            self.assertEqual(report.status, STATUS_SUCCESS)
            raw = s.config.audit_path.read_text(encoding="utf-8")
            self.assertNotIn(secret, raw)
        finally:
            s.cleanup()

    def test_audit_records_detection_as_read_only(self):
        s = AppSandbox()
        try:
            report = s.app_pipeline().run("Which applications are installed?")
            self.assertEqual(report.status, STATUS_SUCCESS)
            rec = s.audit_records()[-1]
            self.assertEqual(rec["action"]["action"], "detect_applications")
            self.assertEqual(rec["permission"]["operation"], "detect")
            self.assertNotIn(rec["action"]["action"], {"open_file"})
            self.assertTrue(rec["execution"]["ok"])
            self.assertEqual(rec["verification"]["status"], "passed")
            self.assertEqual(rec["approval"], "not_required")
        finally:
            s.cleanup()


# --------------------------------------------------------------------------- #
# Preservation of v0.1 / v0.2 functionality with the new integration attached
# --------------------------------------------------------------------------- #


class TestExistingFunctionalityPreserved(unittest.TestCase):
    """The v0.3 dispatcher must not change v0.1/v0.2 behaviour."""

    def test_filesystem_and_document_commands_still_work(self):
        s = AppSandbox()
        try:
            make_txt(s)  # v0.3 suite starts from an existing file
            pipeline = s.app_pipeline()

            report = pipeline.run("Create a folder named Archive in Documents")
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            self.assertTrue(s.doc_path("Archive").is_dir())

            report = pipeline.run(
                'Create a report named summary.txt in Documents containing '
                '"Sales rose 12 percent."'
            )
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            self.assertTrue(s.doc_path("summary.txt").is_file())

            report = pipeline.run("Show files and folders in my Documents.")
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            names = [e["name"] for e in report.execution.data["entries"]]
            self.assertIn("Archive", names)
            self.assertIn("summary.txt", names)

            report = pipeline.run("Read note.txt in Documents")
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)

            report = pipeline.run("explain create a folder named Z in Documents")
            self.assertEqual(report.status, STATUS_SUCCESS, report.error)
            self.assertIn("explanation", report.execution.data)
        finally:
            s.cleanup()

    def test_create_actions_still_require_approval(self):
        s = AppSandbox()
        try:
            report = s.app_pipeline(approve=False).run(
                "Create a folder named Nope in Documents"
            )
            self.assertEqual(report.status, STATUS_REJECTED)
            self.assertFalse(s.doc_path("Nope").exists())
        finally:
            s.cleanup()


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
