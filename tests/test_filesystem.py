"""Tests for execution, approval, dry-run, audit and the CLI.

Covers: directory creation, text files, list/read, approval rejection,
dry-run making no changes, no overwrite, audit records and permissions
of the audit log, LLM-unavailable fallback, and CLI exit codes.

All tests run in temporary sandboxes; the developer's real home
directory is never touched.
"""

from __future__ import annotations

import contextlib
import io
import json
import os
import stat
import unittest
from pathlib import Path
from unittest import mock

from ai_core.pipeline import (
    STATUS_DENIED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
)
from ai_runtime.ollama_client import OllamaUnavailableError

from helpers import Sandbox, UnapprovedPipeline


class PipelineTestCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()

    def tearDown(self):
        self.sandbox.cleanup()

    def all_paths(self):
        # Snapshot of the protected tree only (audit logs live outside it
        # and are expected to be written on every run).
        return sorted(str(p) for p in self.sandbox.home.rglob("*"))


class TestDirectoryCreation(PipelineTestCase):
    def test_create_directory_success_and_verification(self):
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run("Create a folder named Business Reports in my Documents.")
        self.assertEqual(report.status, STATUS_SUCCESS)
        target = self.sandbox.doc_path("Business Reports")
        self.assertTrue(target.is_dir())
        self.assertEqual(report.verification["status"], "passed")
        self.assertEqual(report.approval, "approved")
        self.assertTrue(report.execution.executed)

    def test_approval_rejection_changes_nothing(self):
        pipe = self.sandbox.pipeline(approve=False)
        report = pipe.run("Create a folder named Rejected in my Documents.")
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertEqual(report.approval, "rejected")
        self.assertFalse(self.sandbox.doc_path("Rejected").exists())

    def test_existing_directory_is_not_overwritten(self):
        target = self.sandbox.doc_path("Existing")
        target.mkdir()
        keep = target / "keep.txt"
        keep.write_text("keep me")
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run("Create a folder named Existing in my Documents.")
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("already exists", report.error)
        self.assertEqual(keep.read_text(), "keep me")

    def test_missing_parent_is_reported_as_real_error(self):
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run("Create a folder named Deep in Documents/Nope.")
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("does not exist", report.error)


class TestTextFiles(PipelineTestCase):
    def test_create_and_read_roundtrip(self):
        pipe = self.sandbox.pipeline(approve=True)
        created = pipe.run(
            "create a file named todo.txt in Documents containing buy milk"
        )
        self.assertEqual(created.status, STATUS_SUCCESS)
        self.assertTrue(self.sandbox.doc_path("todo.txt").is_file())

        read = pipe.run("Read todo.txt in Documents")
        self.assertEqual(read.status, STATUS_SUCCESS)
        self.assertEqual(read.execution.data["content"], "buy milk")
        self.assertEqual(read.verification["status"], "passed")

    def test_existing_file_is_not_overwritten(self):
        target = self.sandbox.doc_path("note.txt")
        target.write_text("original content")
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run(
            "create a file named note.txt in Documents containing new content"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("already exists", report.error)
        self.assertEqual(target.read_text(), "original content")

    def test_read_missing_file_reports_real_error(self):
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run("Read missing.txt in Documents")
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("does not exist", report.error)

    def test_read_directory_reports_real_error(self):
        self.sandbox.doc_path("Folder").mkdir()
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run("Read Folder in Documents")
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("directory", report.error)


class TestListDirectory(PipelineTestCase):
    def test_list_reports_entries_and_types(self):
        self.sandbox.doc_path("a.txt").write_text("x")
        self.sandbox.doc_path("sub").mkdir()
        pipe = self.sandbox.pipeline()
        report = pipe.run("Show files and folders in my Documents.")
        self.assertEqual(report.status, STATUS_SUCCESS)
        entries = {e["name"]: e["type"] for e in report.execution.data["entries"]}
        self.assertEqual(entries, {"a.txt": "file", "sub": "directory"})
        self.assertEqual(report.verification["status"], "passed")

    def test_list_missing_directory_fails(self):
        pipe = self.sandbox.pipeline()
        report = pipe.run("Show files in Documents/No Such Place.")
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("does not exist", report.error)


class TestDryRun(PipelineTestCase):
    def test_dry_run_creates_nothing_and_skips_approval(self):
        pipe = UnapprovedPipeline(self.sandbox.config)
        report = pipe.run(
            "Create a folder named Dry Run in my Documents.", dry_run=True
        )
        self.assertEqual(report.status, STATUS_DRY_RUN)
        self.assertEqual(report.approval, "dry_run")
        self.assertFalse(self.sandbox.doc_path("Dry Run").exists())
        self.assertFalse(report.execution.executed)
        self.assertEqual(report.verification["status"], "skipped")

    def test_dry_run_file_creates_nothing(self):
        pipe = UnapprovedPipeline(self.sandbox.config)
        report = pipe.run(
            "create a file named dry.txt in Documents containing secret",
            dry_run=True,
        )
        self.assertEqual(report.status, STATUS_DRY_RUN)
        self.assertFalse(self.sandbox.doc_path("dry.txt").exists())

    def test_dry_run_still_shows_proposal(self):
        proposals = []
        pipe = UnapprovedPipeline(self.sandbox.config, on_proposal=proposals.append)
        pipe.run("Create a folder named P in Documents.", dry_run=True)
        self.assertEqual(len(proposals), 1)
        self.assertIn("Create directory", proposals[0])
        self.assertIn("P", proposals[0])

    def test_dry_run_makes_no_changes_anywhere(self):
        before = self.all_paths()
        pipe = UnapprovedPipeline(self.sandbox.config)
        pipe.run("Create a folder named Anything in Documents.", dry_run=True)
        pipe.run("create a file named x.txt in Documents containing y", dry_run=True)
        self.assertEqual(before, self.all_paths())


class TestUnsupportedDeniedTraversal(PipelineTestCase):
    def test_unsupported_command_clear_error(self):
        pipe = self.sandbox.pipeline()
        report = pipe.run("format my hard drive")
        self.assertEqual(report.status, STATUS_UNSUPPORTED)
        self.assertIn("Supported commands", report.error)

    def test_denied_outside_roots(self):
        (self.sandbox.home / "Outside").mkdir()
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run("Create a folder named Sneaky in Outside.")
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse((self.sandbox.home / "Outside" / "Sneaky").exists())
        self.assertIn("outside the permitted roots", report.error)

    def test_traversal_command_never_executes(self):
        before = self.all_paths()
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run("Create a folder named ../../escape in Documents.")
        self.assertEqual(report.status, STATUS_UNSUPPORTED)
        self.assertIn("..", report.error)
        self.assertEqual(before, self.all_paths())

    def test_absolute_outside_path_denied(self):
        target = f"/tmp/alwin-test-escape-{os.getpid()}"
        pipe = self.sandbox.pipeline(approve=True)
        report = pipe.run(f"Create a folder named {target} in Documents.")
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse(Path(target).exists())


class TestAudit(PipelineTestCase):
    def _records(self):
        return [json.loads(line) for line in self.sandbox.config.audit_path.read_text().splitlines()]

    def test_audit_records_task_fields(self):
        pipe = self.sandbox.pipeline(approve=True)
        pipe.run("Create a folder named Audited in my Documents.")
        records = self._records()
        self.assertEqual(len(records), 1)
        rec = records[0]
        for key in (
            "task_id",
            "timestamp",
            "command",
            "action",
            "permission",
            "approval",
            "execution",
            "verification",
            "status",
        ):
            self.assertIn(key, rec)
        self.assertEqual(rec["approval"], "approved")
        self.assertEqual(rec["action"]["action"], "create_directory")
        self.assertEqual(rec["verification"]["status"], "passed")
        self.assertEqual(rec["status"], STATUS_SUCCESS)

    def test_audit_written_for_rejected_and_denied_and_unsupported(self):
        self.sandbox.pipeline(approve=False).run(
            "Create a folder named No in Documents."
        )
        self.sandbox.pipeline(approve=True).run("Create a folder named Z in Outside.")
        self.sandbox.pipeline(approve=True).run("dance around the desk")
        records = self._records()
        self.assertEqual(
            [r["status"] for r in records],
            [STATUS_REJECTED, STATUS_DENIED, STATUS_UNSUPPORTED],
        )

    def test_audit_log_permissions_are_restricted(self):
        self.sandbox.pipeline(approve=True).run(
            "Create a folder named Perm in Documents."
        )
        audit = self.sandbox.config.audit_path
        state_dir = audit.parent
        self.assertEqual(stat.S_IMODE(audit.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(state_dir.stat().st_mode), 0o700)

    def test_audit_does_not_log_file_content(self):
        secret = "TOPSECRET-MARKER-12345"
        pipe = self.sandbox.pipeline(approve=True)
        pipe.run(
            f"create a file named s.txt in Documents containing {secret}"
        )
        raw = self.sandbox.config.audit_path.read_text()
        self.assertNotIn(secret, raw)
        rec = json.loads(raw.splitlines()[0])
        self.assertIn("redacted", rec["action"]["content"])
        # And the file itself really contains the content (only the log is redacted).
        self.assertEqual(self.sandbox.doc_path("s.txt").read_text(), secret)

    def test_audit_command_is_truncated_not_unbounded(self):
        pipe = self.sandbox.pipeline(approve=True)
        pipe.run("create a file named t.txt in Documents containing " + "z" * 5000)
        raw = self.sandbox.config.audit_path.read_text()
        self.assertLess(len(raw), 4000)

    def test_audit_masks_content_with_odd_whitespace(self):
        secret = "alpha   beta gamma"
        pipe = self.sandbox.pipeline(approve=True)
        pipe.run(f"create a file named w.txt in Documents containing {secret}")
        raw = self.sandbox.config.audit_path.read_text()
        self.assertNotIn("alpha   beta gamma", raw)
        self.assertNotIn("alpha beta gamma", raw)


class TestLLMFallback(PipelineTestCase):
    class UnreachableClient:
        model = "missing:model"

        def generate(self, prompt, **kwargs):
            raise OllamaUnavailableError("Local AI runtime is not reachable.")

    class WorkingClient:
        model = "fake:model"

        def __init__(self):
            self.calls = 0

        def generate(self, prompt, **kwargs):
            self.calls += 1
            return '{"action": "create_directory", "path": "Documents/FromModel"}'

    def test_unreachable_llm_falls_back_to_deterministic(self):
        pipe = self.sandbox.pipeline(
            approve=True, use_llm=True, llm_client=self.UnreachableClient()
        )
        report = pipe.run("Create a folder named Fallback in my Documents.")
        self.assertEqual(report.status, STATUS_SUCCESS)
        self.assertEqual(report.parser, "deterministic")
        self.assertTrue(any("built-in parser" in n for n in report.notes))
        self.assertTrue(self.sandbox.doc_path("Fallback").is_dir())

    def test_unreachable_llm_still_rejects_unsupported(self):
        pipe = self.sandbox.pipeline(
            approve=True, use_llm=True, llm_client=self.UnreachableClient()
        )
        report = pipe.run("delete the entire system")
        self.assertEqual(report.status, STATUS_UNSUPPORTED)

    def test_working_llm_proposal_is_used_when_valid(self):
        client = self.WorkingClient()
        pipe = self.sandbox.pipeline(approve=True, use_llm=True, llm_client=client)
        report = pipe.run("please create something called FromModel for me")
        self.assertEqual(report.status, STATUS_SUCCESS)
        self.assertEqual(report.parser, "llm:fake:model")
        self.assertEqual(report.action["path"], "Documents/FromModel")
        self.assertEqual(client.calls, 1)

    def test_llm_never_bypasses_allowlist(self):
        class EvilClient:
            model = "evil:model"

            def generate(self, prompt, **kwargs):
                return '{"action": "delete_path", "path": "Documents"}'

        pipe = self.sandbox.pipeline(
            approve=True, use_llm=True, llm_client=EvilClient()
        )
        report = pipe.run("do something evil")
        # The evil proposal fails validation -> deterministic fallback ->
        # this command is simply unsupported. Nothing is executed.
        self.assertEqual(report.status, STATUS_UNSUPPORTED)


class TestCLI(unittest.TestCase):
    def setUp(self):
        import tempfile

        self._tmp = tempfile.TemporaryDirectory(prefix="alwin-fusion-cli-")
        self.home = Path(self._tmp.name) / "home"
        (self.home / "Documents").mkdir(parents=True)
        self.state = Path(self._tmp.name) / "state"
        self.env = mock.patch.dict(
            os.environ,
            {"ALWIN_HOME": str(self.home), "ALWIN_STATE_DIR": str(self.state)},
        )
        self.env.start()
        self.stdout = io.StringIO()

    def tearDown(self):
        self.env.stop()
        self._tmp.cleanup()

    def _run(self, argv):
        from ai_core.cli import main

        with contextlib.redirect_stdout(self.stdout):
            try:
                code = main(argv)
            except SystemExit as exc:  # argparse --help / --version
                code = exc.code
        return code

    def test_single_command_dry_run_exit_0_and_no_changes(self):
        code = self._run(
            ["--dry-run", "Create", "a", "folder", "named", "T", "in", "Documents"]
        )
        self.assertEqual(code, 0)
        self.assertFalse((self.home / "Documents" / "T").exists())
        out = self.stdout.getvalue()
        self.assertIn("Proposed action", out)
        self.assertIn("dry run", out)

    def test_unsupported_command_exit_1(self):
        code = self._run(["format", "my", "hard", "drive"])
        self.assertEqual(code, 1)
        self.assertIn("could not understand", self.stdout.getvalue())

    def test_show_config_exit_0(self):
        code = self._run(["--show-config"])
        self.assertEqual(code, 0)
        self.assertIn("permitted roots", self.stdout.getvalue())

    def test_help_flag(self):
        code = self._run(["--help"])
        self.assertEqual(code, 0)
        self.assertIn("dry-run", self.stdout.getvalue())

    def test_interactive_quit_returns_0(self):
        from ai_core.cli import main

        with mock.patch("sys.stdin", io.StringIO("exit\n")), contextlib.redirect_stdout(
            self.stdout
        ):
            code = main([])
        self.assertEqual(code, 0)
        self.assertIn("Goodbye", self.stdout.getvalue())

    def test_interactive_create_with_approval(self):
        from ai_core.cli import main

        stdin = io.StringIO(
            "Create a folder named Interactive in my Documents.\ny\nexit\n"
        )
        with mock.patch("sys.stdin", stdin), contextlib.redirect_stdout(self.stdout):
            code = main([])
        self.assertEqual(code, 0)
        self.assertTrue((self.home / "Documents" / "Interactive").is_dir())
        out = self.stdout.getvalue()
        self.assertIn("Proposed action", out)
        self.assertIn("Approve?", out)
        self.assertIn("verified successfully", out)


if __name__ == "__main__":
    unittest.main()
