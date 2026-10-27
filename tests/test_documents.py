"""Tests for v0.2 document creation: text reports and content sources.

Covers: new parser intents, report rendering, end-to-end creation,
approval rejection, dry-run, existing-file protection, path traversal,
symlink escapes, 'using file.json' sources (permission-checked, size-,
encoding- and JSON-validated), audit redaction of structured content,
lying handlers failing verification, and a source scan proving the
document tools cannot delete, move or shell out.
"""

from __future__ import annotations

import json
import pathlib
import unittest

from ai_core.pipeline import (
    STATUS_DENIED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
)
from ai_core.command_parser import (
    CREATE_PDF,
    CREATE_REPORT,
    CREATE_SPREADSHEET,
    UNSUPPORTED,
    parse_command,
)
from ai_core.planner import (
    ACTION_SCHEMA,
    ContentSourceError,
    PlanValidationError,
    plan_from_intent,
    validate_action,
)
from integrations import ActionDispatcher, build_default_dispatcher
from integrations.documents.report import render_text_report
from integrations.filesystem.tools import ToolResult

from helpers import Sandbox, UnapprovedPipeline

SAMPLE_CONTENT = {
    "title": "Quarterly Summary",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Overview"},
        {"type": "paragraph", "text": "Sales rose 12 percent this quarter."},
        {"type": "list", "style": "bullet", "items": ["North led growth", "South recovered"]},
        {"type": "list", "style": "numbered", "items": ["First step", "Second step"]},
    ],
}


class DocumentTestCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()

    def tearDown(self):
        self.sandbox.cleanup()


# --------------------------------------------------------------------------- #
# Parser
# --------------------------------------------------------------------------- #


class TestDocumentParsing(unittest.TestCase):
    def test_report_with_containing_text(self):
        intent = parse_command(
            'create a report named s.txt in Documents containing "Hello world."'
        )
        self.assertEqual(intent.intent, CREATE_REPORT)
        self.assertEqual(intent.slots["name"], "s.txt")
        self.assertEqual(intent.slots["location"], "Documents")
        self.assertEqual(intent.slots["text"], "Hello world.")

    def test_report_with_title_and_text(self):
        intent = parse_command(
            'create a pdf named s.pdf titled "Readme" containing "Welcome."'
        )
        self.assertEqual(intent.intent, CREATE_PDF)
        self.assertEqual(intent.slots["title"], "Readme")
        self.assertEqual(intent.slots["text"], "Welcome.")

    def test_report_with_json_source(self):
        intent = parse_command("create a report using report.json in Documents")
        self.assertEqual(intent.intent, CREATE_REPORT)
        self.assertEqual(intent.slots["source"], "report.json")

    def test_spreadsheet_with_json_source(self):
        intent = parse_command(
            "create a spreadsheet named sales.xlsx in Documents using sales.json"
        )
        self.assertEqual(intent.intent, CREATE_SPREADSHEET)
        self.assertEqual(intent.slots["source"], "sales.json")

    def test_spreadsheet_with_inline_json(self):
        intent = parse_command(
            'create a spreadsheet named s.xlsx from {"sheets": [{"name": "A", "rows": [[1]]}]}'
        )
        self.assertEqual(intent.intent, CREATE_SPREADSHEET)
        self.assertEqual(
            intent.slots["inline"],
            {"sheets": [{"name": "A", "rows": [[1]]}]},
        )

    def test_spreadsheet_without_spec_is_unsupported_with_format_help(self):
        intent = parse_command("create a spreadsheet named s.xlsx in Documents")
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertIn("JSON", intent.slots["reason"])

    def test_report_without_content_is_unsupported(self):
        intent = parse_command("create a report named s.txt in Documents")
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertIn("content", intent.slots["reason"])

    def test_using_and_from_together_rejected(self):
        intent = parse_command(
            'create a spreadsheet named s.xlsx using a.json from {"sheets": []}'
        )
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertIn("not both", intent.slots["reason"])

    def test_using_mixed_with_containing_rejected(self):
        intent = parse_command(
            'create a report named s.txt using a.json containing "hi"'
        )
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertIn("using", intent.slots["reason"])

    def test_source_and_simple_text_together_rejected(self):
        intent = parse_command(
            'create a report named s.txt in Documents titled "T" using a.json'
        )
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertIn("not both", intent.slots["reason"])

    def test_invalid_inline_json_rejected(self):
        intent = parse_command("create a spreadsheet named s.xlsx from {not json}")
        self.assertEqual(intent.intent, UNSUPPORTED)
        self.assertIn("not valid JSON", intent.slots["reason"])

    def test_workbook_synonym_maps_to_spreadsheet(self):
        intent = parse_command("create a workbook named w.xlsx using w.json")
        self.assertEqual(intent.intent, CREATE_SPREADSHEET)

    def test_v01_commands_still_parse(self):
        # Regression: a v0.1 text-file command whose name mentions a
        # document word must keep its original meaning.
        intent = parse_command(
            "create a file named report.txt in Documents containing hello"
        )
        self.assertEqual(intent.intent, "create_text_file")
        self.assertEqual(intent.slots["name"], "report.txt")


# --------------------------------------------------------------------------- #
# Planner / action validation
# --------------------------------------------------------------------------- #


class TestDocumentValidation(unittest.TestCase):
    def test_report_appends_txt_extension(self):
        action = validate_action(
            {"action": "create_report", "path": "Documents/summary", "content": {"title": "T"}}
        )
        self.assertEqual(action["path"], "Documents/summary.txt")

    def test_report_rejects_wrong_extension(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_action(
                {
                    "action": "create_report",
                    "path": "Documents/summary.docx",
                    "content": {"title": "T"},
                }
            )
        self.assertIn(".txt", str(ctx.exception))

    def test_hidden_filename_rejected(self):
        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "create_report", "path": "Documents/.secret", "content": {"title": "T"}}
            )

    def test_traversal_rejected_at_planner(self):
        with self.assertRaises(PlanValidationError):
            validate_action(
                {
                    "action": "create_report",
                    "path": "Documents/../../evil.txt",
                    "content": {"title": "T"},
                }
            )

    def test_unknown_content_field_rejected(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_action(
                {
                    "action": "create_report",
                    "path": "Documents/x.txt",
                    "content": {"title": "T", "macros": True},
                }
            )
        self.assertIn("unknown field", str(ctx.exception))

    def test_unknown_spec_field_rejected(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_action(
                {
                    "action": "create_spreadsheet",
                    "path": "Documents/x.xlsx",
                    "spec": {"sheets": [{"name": "A", "rows": [[1]]}], "vba": "code"},
                }
            )
        self.assertIn("unknown field", str(ctx.exception))

    def test_spreadsheet_requires_sheets(self):
        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "create_spreadsheet", "path": "Documents/x.xlsx", "spec": {}}
            )

    def test_model_proposal_for_report_is_validated(self):
        from ai_core.planner import parse_model_action

        action = parse_model_action(
            json.dumps(
                {
                    "action": "create_report",
                    "path": "Documents/note.txt",
                    "content": {"title": "Note", "blocks": [{"type": "paragraph", "text": "hi"}]},
                }
            )
        )
        self.assertEqual(action["path"], "Documents/note.txt")

    def test_model_proposal_cannot_reference_a_source_file(self):
        # The model never gets a 'source' key: no file reads from model
        # output, only inline content.
        with self.assertRaises(PlanValidationError):
            validate_action(
                {
                    "action": "create_report",
                    "path": "Documents/x.txt",
                    "content": {"title": "T"},
                    "source": "/etc/passwd",
                }
            )

    def test_plan_from_intent_without_loader_raises_content_source_error(self):
        intent = parse_command("create a report using report.json")
        with self.assertRaises(ContentSourceError):
            plan_from_intent(intent)

    def test_describe_actions(self):
        from ai_core.planner import describe_action

        self.assertIn(
            "Create text report",
            describe_action(
                {"action": "create_report", "path": "Documents/x.txt", "content": SAMPLE_CONTENT}
            ),
        )
        self.assertIn(
            "Create PDF document",
            describe_action(
                {"action": "create_pdf", "path": "Documents/x.pdf", "content": SAMPLE_CONTENT}
            ),
        )
        self.assertIn(
            "Create spreadsheet",
            describe_action(
                {
                    "action": "create_spreadsheet",
                    "path": "Documents/x.xlsx",
                    "spec": {"sheets": [{"name": "A", "rows": [[1]]}]},
                }
            ),
        )


# --------------------------------------------------------------------------- #
# Rendering
# --------------------------------------------------------------------------- #


class TestReportRendering(unittest.TestCase):
    def test_render_is_deterministic_and_complete(self):
        text = render_text_report(SAMPLE_CONTENT)
        self.assertEqual(text, render_text_report(SAMPLE_CONTENT))
        self.assertIn("Quarterly Summary", text)
        self.assertIn("# Overview", text)
        self.assertIn("Sales rose 12 percent this quarter.", text)
        self.assertIn("- North led growth", text)
        self.assertIn("1. First step", text)
        self.assertTrue(text.endswith("\n"))

    def test_long_paragraph_wraps_within_width(self):
        content = {"title": None, "blocks": [{"type": "paragraph", "text": "word " * 200}]}
        text = render_text_report(content)
        for line in text.splitlines():
            self.assertLessEqual(len(line), 78)


# --------------------------------------------------------------------------- #
# End-to-end pipeline
# --------------------------------------------------------------------------- #


class TestReportPipeline(DocumentTestCase):
    def test_create_report_from_containing_text(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a report named summary.txt in Documents containing "Hello world."'
        )
        self.assertEqual(report.status, STATUS_SUCCESS)
        path = self.sandbox.doc_path("summary.txt")
        self.assertTrue(path.is_file())
        self.assertIn("Hello world.", path.read_text())
        self.assertEqual(report.approval, "approved")
        self.assertEqual(report.verification["status"], "passed")

    def test_create_report_from_json_source(self):
        source = self.sandbox.doc_path("report.json")
        source.write_text(json.dumps(SAMPLE_CONTENT))
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run("create a report named out.txt in Documents using report.json")
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        text = self.sandbox.doc_path("out.txt").read_text()
        self.assertIn("Quarterly Summary", text)
        self.assertIn("# Overview", text)

    def test_source_name_derives_output_name(self):
        source = self.sandbox.doc_path("report.json")
        source.write_text(json.dumps(SAMPLE_CONTENT))
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run("create a report in Documents using report.json")
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        self.assertTrue(self.sandbox.doc_path("report.txt").is_file())

    def test_approval_rejection_creates_nothing(self):
        pipeline = self.sandbox.pipeline(approve=False)
        report = pipeline.run(
            'create a report named x.txt in Documents containing "secret text"'
        )
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("x.txt").exists())
        self.assertEqual(report.approval, "rejected")

    def test_dry_run_creates_nothing(self):
        pipeline = UnapprovedPipeline(self.sandbox.config)
        report = pipeline.run(
            'create a report named dry.txt in Documents containing "text"',
            dry_run=True,
        )
        self.assertEqual(report.status, STATUS_DRY_RUN)
        self.assertFalse(self.sandbox.doc_path("dry.txt").exists())
        self.assertEqual(report.approval, "dry_run")

    def test_existing_file_is_never_overwritten(self):
        path = self.sandbox.doc_path("exists.txt")
        path.write_text("ORIGINAL")
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a report named exists.txt in Documents containing "new content"'
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("already exists", report.error)
        self.assertEqual(path.read_text(), "ORIGINAL")

    def test_traversal_name_is_rejected(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a report named ../../evil.txt in Documents containing "x"'
        )
        self.assertIn(report.status, (STATUS_DENIED, STATUS_UNSUPPORTED))
        self.assertFalse((self.sandbox.root / "evil.txt").exists())
        self.assertFalse((self.sandbox.home / "evil.txt").exists())

    def test_absolute_path_is_denied(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a report named /tmp/alwin-evil.txt in Documents containing "x"'
        )
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse(pathlib.Path("/tmp/alwin-evil.txt").exists())

    def test_symlink_escape_is_denied(self):
        outside = self.sandbox.root / "outside"
        outside.mkdir()
        link = self.sandbox.documents / "escape"
        link.symlink_to(outside, target_is_directory=True)
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a report named escape/out.txt in Documents containing "x"'
        )
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse((outside / "out.txt").exists())

    def test_symlink_file_is_not_followed(self):
        outside_file = self.sandbox.root / "victim.txt"
        outside_file.write_text("SAFE")
        link = self.sandbox.documents / "innocent.txt"
        link.symlink_to(outside_file)
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a report named innocent.txt in Documents containing "x"'
        )
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertEqual(outside_file.read_text(), "SAFE")

    def test_wrong_extension_is_rejected(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a report named summary.docx in Documents containing "x"'
        )
        self.assertEqual(report.status, STATUS_UNSUPPORTED)
        self.assertFalse(self.sandbox.doc_path("summary.docx").exists())

    def test_lying_handler_fails_verification(self):
        # A handler that claims success without writing must not pass.
        dispatcher = ActionDispatcher()

        def liar(action, *, dry_run=False):
            return ToolResult(ok=True, message="Created it, trust me", changed=True)

        dispatcher.register(
            "create_report",
            liar,
            description="test",
            mutates=True,
            requires_approval=True,
        )
        from ai_core.config import Config  # noqa: F401 - documentation

        pipeline = self.sandbox.pipeline(approve=True, dispatcher=dispatcher)
        report = pipeline.run(
            'create a report named lie.txt in Documents containing "x"'
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertFalse(self.sandbox.doc_path("lie.txt").exists())


# --------------------------------------------------------------------------- #
# Content sources: permission, size, encoding, JSON shape
# --------------------------------------------------------------------------- #


class TestContentSources(DocumentTestCase):
    def _pipeline(self):
        return self.sandbox.pipeline(approve=True)

    def test_missing_source_file_fails(self):
        report = self._pipeline().run(
            "create a report named out.txt in Documents using missing.json"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("does not exist", report.error)

    def test_source_outside_roots_is_denied(self):
        (self.sandbox.root / "secret.json").write_text(json.dumps({"title": "Leak"}))
        report = self._pipeline().run(
            "create a report named out.txt in Documents using ../secret.json"
        )
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse(self.sandbox.doc_path("out.txt").exists())

    def test_source_symlink_escape_is_denied(self):
        outside = self.sandbox.root / "outside.json"
        outside.write_text(json.dumps({"title": "Leak"}))
        link = self.sandbox.documents / "innocent.json"
        link.symlink_to(outside)
        report = self._pipeline().run(
            "create a report named out.txt in Documents using innocent.json"
        )
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse(self.sandbox.doc_path("out.txt").exists())

    def test_non_json_source_fails(self):
        self.sandbox.doc_path("bad.json").write_text("not json at all")
        report = self._pipeline().run(
            "create a report named out.txt in Documents using bad.json"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("not valid JSON", report.error)

    def test_json_array_source_fails(self):
        self.sandbox.doc_path("arr.json").write_text("[1, 2, 3]")
        report = self._pipeline().run(
            "create a report named out.txt in Documents using arr.json"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("single JSON object", report.error)

    def test_non_utf8_source_fails(self):
        self.sandbox.doc_path("bin.json").write_bytes(b"\xff\xfe\x00binary")
        report = self._pipeline().run(
            "create a report named out.txt in Documents using bin.json"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("UTF-8", report.error)

    def test_oversized_source_fails(self):
        big = json.dumps({"title": "T", "blocks": [{"type": "paragraph", "text": "x" * 600_000}]})
        self.sandbox.doc_path("big.json").write_text(big)
        report = self._pipeline().run(
            "create a report named out.txt in Documents using big.json"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("limit", report.error)

    def test_invalid_spec_in_source_fails_planning(self):
        self.sandbox.doc_path("spec.json").write_text(
            json.dumps({"sheets": [{"name": "A", "rows": [["bad"]], "unknown": 1}]})
        )
        report = self._pipeline().run(
            "create a spreadsheet named out.xlsx in Documents using spec.json"
        )
        self.assertEqual(report.status, STATUS_UNSUPPORTED)
        self.assertIn("unknown field", report.error)


# --------------------------------------------------------------------------- #
# Audit redaction of structured content
# --------------------------------------------------------------------------- #


class TestDocumentAudit(DocumentTestCase):
    def test_inline_report_text_is_redacted(self):
        secret = "TOPSECRET-MARKER-777"
        pipeline = self.sandbox.pipeline(approve=True)
        pipeline.run(f'create a report named a.txt in Documents containing "{secret}"')
        raw = self.sandbox.config.audit_path.read_text()
        self.assertNotIn(secret, raw)

    def test_spreadsheet_spec_is_redacted(self):
        pipeline = self.sandbox.pipeline(approve=True)
        pipeline.run(
            'create a spreadsheet named s.xlsx from '
            '{"sheets": [{"name": "A", "headers": ["Secret"], "rows": [["HIDDEN-VALUE"]]}]}'
        )
        raw = self.sandbox.config.audit_path.read_text()
        self.assertNotIn("HIDDEN-VALUE", raw)
        records = [json.loads(line) for line in raw.splitlines() if line.strip()]
        action = records[-1]["action"]
        self.assertEqual(action.get("spec"), "<redacted>")

    def test_json_source_content_is_not_logged(self):
        self.sandbox.doc_path("report.json").write_text(json.dumps(SAMPLE_CONTENT))
        pipeline = self.sandbox.pipeline(approve=True)
        pipeline.run("create a report named b.txt in Documents using report.json")
        raw = self.sandbox.config.audit_path.read_text()
        self.assertNotIn("Sales rose 12 percent", raw)


# --------------------------------------------------------------------------- #
# Meta: the document tools have no destructive capability
# --------------------------------------------------------------------------- #


class TestDocumentToolsSafety(unittest.TestCase):
    def test_document_tools_have_no_destructive_calls(self):
        import integrations.documents.tools as doc_tools_mod
        import integrations.documents.spreadsheet as sheet_mod
        import integrations.documents.pdf as pdf_mod
        import integrations.documents.report as report_mod

        for module in (doc_tools_mod, sheet_mod, pdf_mod, report_mod):
            source = pathlib.Path(module.__file__).read_text(encoding="utf-8")
            for banned in (
                "os.remove(",
                "os.unlink(",
                ".unlink(",
                "rmtree",
                "shutil.",
                "os.rename(",
                "os.replace(",
                "os.system(",
                "subprocess",
                "shell=True",
            ):
                with self.subTest(module=module.__name__, banned=banned):
                    self.assertNotIn(banned, source)

    def test_default_dispatcher_registers_document_handlers(self):
        from ai_core.config import load_config
        import tempfile

        with tempfile.TemporaryDirectory() as td:
            home = pathlib.Path(td) / "home"
            (home / "Documents").mkdir(parents=True)
            config = load_config(home=home, roots=["Documents"], state_dir=home / "s")
            from ai_core.permissions import PermissionChecker

            dispatcher = build_default_dispatcher(PermissionChecker(config))
            for name in ("create_report", "create_pdf", "create_spreadsheet"):
                spec = dispatcher.spec(name)
                self.assertTrue(spec.mutates)
                self.assertTrue(spec.requires_approval)


if __name__ == "__main__":
    unittest.main()
