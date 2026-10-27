"""Tests for v0.2 PDF generation with reportlab.

Covers: PDF creation end-to-end, structural validation of the produced
file (signature, catalog, objects, startxref, %%EOF), rejection of
non-PDF bytes, multi-page documents, unsupported inputs, approval
rejection, dry-run, existing-file protection, traversal, symlink
escapes and the missing-dependency message.

PDF generation is skipped when reportlab is absent; the structural
validator (pure standard library) is always tested.
"""

from __future__ import annotations

import importlib.util
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
from ai_core.planner import PlanValidationError, validate_action
from integrations.documents.pdf import validate_pdf_bytes

from helpers import Sandbox, UnapprovedPipeline

HAVE_REPORTLAB = importlib.util.find_spec("reportlab") is not None

SAMPLE_CONTENT = {
    "title": "Readme",
    "blocks": [
        {"type": "heading", "level": 1, "text": "Welcome"},
        {"type": "paragraph", "text": "This document was generated locally and offline."},
        {"type": "list", "style": "bullet", "items": ["No network access", "No cloud service"]},
    ],
}


# --------------------------------------------------------------------------- #
# Structural validator (pure stdlib, no reportlab needed)
# --------------------------------------------------------------------------- #


class TestPdfValidation(unittest.TestCase):
    def test_empty_rejected(self):
        ok, detail = validate_pdf_bytes(b"")
        self.assertFalse(ok)
        self.assertIn("too small", detail)

    def test_non_bytes_rejected(self):
        ok, detail = validate_pdf_bytes("not bytes")
        self.assertFalse(ok)

    def test_missing_signature_rejected(self):
        ok, detail = validate_pdf_bytes(b"X" * 200)
        self.assertFalse(ok)
        self.assertIn("signature", detail)

    def test_truncated_pdf_rejected(self):
        fake = b"%PDF-1.4\n" + b"0" * 200
        ok, detail = validate_pdf_bytes(fake)
        self.assertFalse(ok)
        self.assertIn("EOF", detail)

    def test_pdf_without_catalog_rejected(self):
        fake = (
            b"%PDF-1.4\n1 0 obj\n<< >>\nendobj\n2 0 obj\n<< >>\nendobj\n"
            b"3 0 obj\n<< >>\nendobj\nstartxref\n9\n%%EOF\n"
        )
        ok, detail = validate_pdf_bytes(fake)
        self.assertFalse(ok)
        self.assertIn("Catalog", detail)

    def test_startxref_out_of_range_rejected(self):
        fake = (
            b"%PDF-1.4\n1 0 obj\n<< /Catalog 2 0 R >>\nendobj\n"
            + b"2 0 obj\n<< >>\nendobj\n" + b"3 0 obj\n<< >>\nendobj\n"
            + b"startxref\n999999\n%%EOF\n"
        )
        ok, detail = validate_pdf_bytes(fake)
        self.assertFalse(ok)
        self.assertIn("outside", detail)


# --------------------------------------------------------------------------- #
# Validation of the action shape
# --------------------------------------------------------------------------- #


class TestPdfActionValidation(unittest.TestCase):
    def test_pdf_extension_appended(self):
        action = validate_action(
            {"action": "create_pdf", "path": "Documents/doc", "content": {"title": "T"}}
        )
        self.assertEqual(action["path"], "Documents/doc.pdf")

    def test_pdf_rejects_txt_extension(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_action(
                {"action": "create_pdf", "path": "Documents/doc.txt", "content": {"title": "T"}}
            )
        self.assertIn(".pdf", str(ctx.exception))

    def test_pdf_rejects_bad_content(self):
        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "create_pdf", "path": "Documents/doc.pdf", "content": {"blocks": "text"}}
            )


# --------------------------------------------------------------------------- #
# End-to-end pipeline
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAVE_REPORTLAB, "reportlab is not installed")
class TestPdfPipeline(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()

    def tearDown(self):
        self.sandbox.cleanup()

    def _content_file(self, content=SAMPLE_CONTENT, name="doc.json") -> str:
        self.sandbox.doc_path(name).write_text(json.dumps(content))
        return name

    def test_create_pdf_from_title_and_text(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a pdf named readme.pdf in Documents titled "Readme" containing "Welcome."'
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        path = self.sandbox.doc_path("readme.pdf")
        self.assertTrue(path.is_file())
        data = path.read_bytes()
        self.assertGreater(len(data), 1000)
        self.assertTrue(data.startswith(b"%PDF-"))
        self.assertEqual(report.verification["status"], "passed")

    def test_created_pdf_structurally_valid(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a pdf named readme.pdf in Documents containing "Hello PDF world."'
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        ok, detail = validate_pdf_bytes(self.sandbox.doc_path("readme.pdf").read_bytes())
        self.assertTrue(ok, detail)
        names = [c["name"] for c in report.verification["checks"]]
        self.assertIn("pdf_structure_valid", names)

    def test_pdf_from_json_source(self):
        source = self._content_file()
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(f"create a pdf named out.pdf in Documents using {source}")
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        ok, detail = validate_pdf_bytes(self.sandbox.doc_path("out.pdf").read_bytes())
        self.assertTrue(ok, detail)

    def test_long_content_spans_multiple_pages(self):
        long_content = {
            "title": "Long",
            "blocks": [
                {"type": "paragraph", "text": " ".join(["paragraph text"] * 120)}
                for _ in range(10)
            ],
        }
        self._content_file(long_content, "long.json")
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            "create a pdf named long.pdf in Documents using long.json"
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        self.assertGreater(report.execution.data.get("pages", 0), 1)

    def test_non_latin1_text_does_not_crash(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a pdf named uni.pdf in Documents containing "Grüße — 你好"'
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        ok, detail = validate_pdf_bytes(self.sandbox.doc_path("uni.pdf").read_bytes())
        self.assertTrue(ok, detail)

    def test_approval_rejection_creates_nothing(self):
        pipeline = self.sandbox.pipeline(approve=False)
        report = pipeline.run(
            'create a pdf named nope.pdf in Documents containing "text"'
        )
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("nope.pdf").exists())

    def test_dry_run_creates_nothing(self):
        pipeline = UnapprovedPipeline(self.sandbox.config)
        report = pipeline.run(
            'create a pdf named dry.pdf in Documents containing "text"', dry_run=True
        )
        self.assertEqual(report.status, STATUS_DRY_RUN)
        self.assertFalse(self.sandbox.doc_path("dry.pdf").exists())

    def test_existing_file_is_never_overwritten(self):
        path = self.sandbox.doc_path("keep.pdf")
        path.write_bytes(b"ORIGINAL")
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a pdf named keep.pdf in Documents containing "new"'
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("already exists", report.error)
        self.assertEqual(path.read_bytes(), b"ORIGINAL")

    def test_traversal_is_rejected(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a pdf named ../../evil.pdf in Documents containing "x"'
        )
        self.assertIn(report.status, (STATUS_DENIED, STATUS_UNSUPPORTED))
        self.assertFalse((self.sandbox.root / "evil.pdf").exists())

    def test_symlink_escape_is_denied(self):
        outside = self.sandbox.root / "outside"
        outside.mkdir()
        link = self.sandbox.documents / "escape"
        link.symlink_to(outside, target_is_directory=True)
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a pdf named escape/out.pdf in Documents containing "x"'
        )
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse((outside / "out.pdf").exists())

    def test_garbage_pdf_fails_verification(self):
        # A handler that writes invalid bytes must fail verification.
        from ai_core.permissions import PermissionChecker
        from integrations import ActionDispatcher
        from integrations.filesystem.tools import ToolResult

        checker = PermissionChecker(self.sandbox.config)
        dispatcher = ActionDispatcher()

        def bad_handler(action, *, dry_run=False):
            resolved = checker.resolve(action["path"], operation="create")
            with open(resolved, "wb") as handle:
                handle.write(b"%PDF-1.4 but not really a pdf " * 10)
            return ToolResult(ok=True, message="claimed success", changed=True)

        dispatcher.register(
            "create_pdf", bad_handler, description="test", mutates=True, requires_approval=True
        )
        pipeline = self.sandbox.pipeline(approve=True, dispatcher=dispatcher)
        report = pipeline.run(
            'create a pdf named bad.pdf in Documents containing "x"'
        )
        self.assertEqual(report.status, STATUS_FAILED)
        names = [c["name"] for c in report.verification["checks"]]
        self.assertIn("pdf_structure_valid", names)

    def test_missing_reportlab_produces_install_hint(self):
        from unittest import mock

        from ai_core.permissions import PermissionChecker
        from integrations.documents import deps
        from integrations.documents.tools import DocumentTools

        tools = DocumentTools(PermissionChecker(self.sandbox.config))
        real_require = deps.require

        def fake_require(name):
            if name == "reportlab":
                raise deps.MissingDependencyError("reportlab")
            return real_require(name)

        with mock.patch.object(deps, "require", fake_require):
            result = tools.create_pdf("Documents/x.pdf", SAMPLE_CONTENT)
        self.assertFalse(result.ok)
        self.assertIn("pip install", result.error)
        self.assertIn("reportlab", result.error)
        self.assertFalse(self.sandbox.doc_path("x.pdf").exists())


if __name__ == "__main__":
    unittest.main()
