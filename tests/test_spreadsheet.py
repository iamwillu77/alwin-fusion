"""Tests for v0.2 spreadsheet creation with openpyxl.

Covers: workbook creation end-to-end, reopening and inspecting the
file, formula preservation (strings written, never calculated),
formula and cell-reference validation, number formats, column widths,
styles, unsupported inputs, approval rejection, dry-run, existing-file
protection, traversal and symlink escapes.

The suite is skipped as a whole when openpyxl is not installed, so the
core test suite still passes on machines without the optional
dependency.
"""

from __future__ import annotations

import importlib.util
import json
import pathlib
import unittest

from ai_core.command_parser import UNSUPPORTED, parse_command
from ai_core.pipeline import (
    STATUS_DENIED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
)
from ai_core.planner import PlanValidationError, validate_action

from helpers import Sandbox, UnapprovedPipeline

HAVE_OPENPYXL = importlib.util.find_spec("openpyxl") is not None

SAMPLE_SPEC = {
    "title": "Sales 2026",
    "sheets": [
        {
            "name": "Q1",
            "headers": ["Region", "Amount"],
            "rows": [["North", 100], ["South", 250.5], ["Total", None]],
            "formulas": [{"cell": "B4", "formula": "=SUM(B2:B3)"}],
            "number_formats": {"B2:B3": "#,##0.00"},
            "column_widths": {"A": 16},
            "styles": {"header_bold": True, "freeze_header": True, "header_fill": "#DDEBF7"},
        }
    ],
}


@unittest.skipUnless(HAVE_OPENPYXL, "openpyxl is not installed")
class SpreadsheetTestCase(unittest.TestCase):
    def setUp(self):
        self.sandbox = Sandbox()

    def tearDown(self):
        self.sandbox.cleanup()

    def _spec_file(self, spec=SAMPLE_SPEC, name="sales.json") -> str:
        path = self.sandbox.doc_path(name)
        path.write_text(json.dumps(spec))
        return name


# --------------------------------------------------------------------------- #
# Validation of specs and formulas (no filesystem needed)
# --------------------------------------------------------------------------- #


class TestSpecValidation(unittest.TestCase):
    def test_valid_spec_round_trips(self):
        action = validate_action(
            {"action": "create_spreadsheet", "path": "Documents/s.xlsx", "spec": SAMPLE_SPEC}
        )
        self.assertEqual(action["path"], "Documents/s.xlsx")
        self.assertEqual(action["spec"]["sheets"][0]["name"], "Q1")

    def test_normalized_spec_revalidates_cleanly(self):
        # The tools layer re-validates the planner's normalized output
        # (defence in depth), so validation must be idempotent — including
        # for headerless sheets that receive default style entries.
        from ai_core.specs import validate_spreadsheet_spec

        headerless = {"sheets": [{"name": "A", "rows": [[1]]}]}
        normalized = validate_spreadsheet_spec(headerless)
        again = validate_spreadsheet_spec(normalized)
        self.assertEqual(normalized, again)
        normalized_sample = validate_spreadsheet_spec(SAMPLE_SPEC)
        self.assertEqual(
            validate_spreadsheet_spec(normalized_sample), normalized_sample
        )

    def test_xlsx_extension_appended(self):
        action = validate_action(
            {"action": "create_spreadsheet", "path": "Documents/book", "spec": {"sheets": [{"name": "A", "rows": [[1]]}]}}
        )
        self.assertEqual(action["path"], "Documents/book.xlsx")

    def test_wrong_extension_rejected(self):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_action(
                {
                    "action": "create_spreadsheet",
                    "path": "Documents/book.csv",
                    "spec": {"sheets": [{"name": "A", "rows": [[1]]}]},
                }
            )
        self.assertIn(".xlsx", str(ctx.exception))

    def _assert_spec_rejected(self, spec, fragment):
        with self.assertRaises(PlanValidationError) as ctx:
            validate_action(
                {"action": "create_spreadsheet", "path": "Documents/x.xlsx", "spec": spec}
            )
        self.assertIn(fragment, str(ctx.exception))

    # ---- formula security ------------------------------------------------ #

    def test_network_formula_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["V"], "rows": [[1]], "formulas": [{"cell": "A2", "formula": '=WEBSERVICE("http://evil")'}]}]},
            "not allowed",
        )

    def test_hyperlink_formula_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["V"], "rows": [[1]], "formulas": [{"cell": "A2", "formula": '=HYPERLINK("http://evil")'}]}]},
            "not allowed",
        )

    def test_external_workbook_link_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["V"], "rows": [[1]], "formulas": [{"cell": "A2", "formula": "=SUM([Book1]Sheet1!A1)"}]}]},
            "external workbook",
        )

    def test_cross_sheet_reference_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["V"], "rows": [[1]], "formulas": [{"cell": "A2", "formula": "=Sheet2!A1"}]}]},
            "cross-sheet",
        )

    def test_reference_outside_declared_grid_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["X"], "rows": [[1]], "formulas": [{"cell": "B5", "formula": "=B4"}]}]},
            "outside the declared data grid",
        )

    def test_unknown_function_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["V"], "rows": [[1]], "formulas": [{"cell": "A2", "formula": "=SOMETHING(A1)"}]}]},
            "not allowed",
        )

    def test_unbalanced_parentheses_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["V"], "rows": [[1]], "formulas": [{"cell": "A2", "formula": "=SUM(A1"}]}]},
            "unbalanced",
        )

    def test_cell_string_starting_with_equals_rejected(self):
        # openpyxl would silently turn this into a formula, bypassing
        # formula validation — so row strings may not start with '='.
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "rows": [["=1+1"]]}]},
            "must not start with '='",
        )

    def test_duplicate_formula_cell_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["V"], "rows": [[1]], "formulas": [
                {"cell": "A2", "formula": "=A1"},
                {"cell": "A2", "formula": "=A1*2"},
            ]}]},
            "more than one formula",
        )

    # ---- structure limits ------------------------------------------------ #

    def test_unknown_number_format_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "rows": [[1]], "number_formats": {"A1": "0.0.0.0"}}]},
            "format must be one of",
        )

    def test_column_width_out_of_range_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "rows": [[1]], "column_widths": {"A": 999}}]},
            "between 0 and 255",
        )

    def test_invalid_sheet_name_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "bad/name", "rows": [[1]]}]},
            "invalid character",
        )

    def test_duplicate_sheet_names_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "rows": [[1]]}, {"name": "a", "rows": [[2]]}]},
            "duplicate sheet name",
        )

    def test_too_many_rows_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "rows": [[1]] * 5001}]},
            "too many rows",
        )

    def test_non_scalar_cell_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "rows": [[{"nested": "object"}]]}]},
            "must be a number, text",
        )

    def test_row_wider_than_headers_rejected(self):
        self._assert_spec_rejected(
            {"sheets": [{"name": "A", "headers": ["X"], "rows": [[1, 2, 3]]}]},
            "headers",
        )


# --------------------------------------------------------------------------- #
# End-to-end pipeline
# --------------------------------------------------------------------------- #


@unittest.skipUnless(HAVE_OPENPYXL, "openpyxl is not installed")
class TestSpreadsheetPipeline(SpreadsheetTestCase):
    def test_create_workbook_from_source_file(self):
        source = self._spec_file()
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            f"create a spreadsheet named sales.xlsx in Documents using {source}"
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        path = self.sandbox.doc_path("sales.xlsx")
        self.assertTrue(path.is_file())
        self.assertGreater(path.stat().st_size, 0)
        self.assertEqual(report.approval, "approved")
        self.assertEqual(report.verification["status"], "passed")

    def test_workbook_reopens_with_expected_content(self):
        from openpyxl import load_workbook

        self._spec_file()
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            "create a spreadsheet named sales.xlsx in Documents using sales.json"
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)

        workbook = load_workbook(self.sandbox.doc_path("sales.xlsx"), data_only=False)
        try:
            self.assertEqual(workbook.sheetnames, ["Q1"])
            sheet = workbook["Q1"]
            self.assertEqual(sheet["A1"].value, "Region")
            self.assertEqual(sheet["A2"].value, "North")
            self.assertEqual(sheet["B3"].value, 250.5)
            # Formula is stored as text, NOT as a calculated value.
            self.assertEqual(sheet["B4"].value, "=SUM(B2:B3)")
            self.assertEqual(sheet["B4"].data_type, "f")
            # Formatting applied.
            self.assertEqual(sheet["B2"].number_format, "#,##0.00")
            self.assertEqual(sheet.column_dimensions["A"].width, 16)
            self.assertEqual(sheet["A1"].font.bold, True)
            self.assertEqual(sheet.freeze_panes, "A2")
        finally:
            workbook.close()

    def test_formula_cells_are_reported_as_not_calculated(self):
        self._spec_file()
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            "create a spreadsheet named sales.xlsx in Documents using sales.json"
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        names = [c["name"] for c in report.verification["checks"]]
        self.assertIn("formulas_preserved", names)
        preserved = next(
            c for c in report.verification["checks"] if c["name"] == "formulas_preserved"
        )
        self.assertIn("not calculated", preserved["detail"])
        self.assertIn("calculate", report.verification["message"])

    def test_workbook_created_from_inline_json(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            'create a spreadsheet named inline.xlsx from '
            '{"sheets": [{"name": "Data", "headers": ["K"], "rows": [["v"]]}]}'
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        from openpyxl import load_workbook

        workbook = load_workbook(self.sandbox.doc_path("inline.xlsx"))
        try:
            self.assertEqual(workbook.sheetnames, ["Data"])
            self.assertEqual(workbook["Data"]["A2"].value, "v")
        finally:
            workbook.close()

    def test_multi_sheet_workbook(self):
        spec = {
            "sheets": [
                {"name": "One", "rows": [[1]]},
                {"name": "Two", "rows": [[2]]},
                {"name": "Three", "rows": [[3]]},
            ]
        }
        self._spec_file(spec, "multi.json")
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            "create a spreadsheet named multi.xlsx in Documents using multi.json"
        )
        self.assertEqual(report.status, STATUS_SUCCESS, report.error)
        from openpyxl import load_workbook

        workbook = load_workbook(self.sandbox.doc_path("multi.xlsx"))
        try:
            self.assertEqual(workbook.sheetnames, ["One", "Two", "Three"])
        finally:
            workbook.close()

    def test_no_spec_gives_clear_error(self):
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run("create a spreadsheet named x.xlsx in Documents")
        self.assertEqual(report.status, STATUS_UNSUPPORTED)
        self.assertIn("JSON", report.error)
        self.assertFalse(self.sandbox.doc_path("x.xlsx").exists())

    def test_approval_rejection_creates_nothing(self):
        self._spec_file()
        pipeline = self.sandbox.pipeline(approve=False)
        report = pipeline.run(
            "create a spreadsheet named nope.xlsx in Documents using sales.json"
        )
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertFalse(self.sandbox.doc_path("nope.xlsx").exists())

    def test_dry_run_creates_nothing(self):
        self._spec_file()
        pipeline = UnapprovedPipeline(self.sandbox.config)
        report = pipeline.run(
            "create a spreadsheet named dry.xlsx in Documents using sales.json",
            dry_run=True,
        )
        self.assertEqual(report.status, STATUS_DRY_RUN)
        self.assertFalse(self.sandbox.doc_path("dry.xlsx").exists())

    def test_existing_file_is_never_overwritten(self):
        self._spec_file()
        path = self.sandbox.doc_path("keep.xlsx")
        path.write_bytes(b"ORIGINAL-BYTES")
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            "create a spreadsheet named keep.xlsx in Documents using sales.json"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        self.assertIn("already exists", report.error)
        self.assertEqual(path.read_bytes(), b"ORIGINAL-BYTES")

    def test_traversal_is_rejected(self):
        self._spec_file()
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            "create a spreadsheet named ../../evil.xlsx in Documents using sales.json"
        )
        self.assertIn(report.status, (STATUS_DENIED, STATUS_UNSUPPORTED))
        self.assertFalse((self.sandbox.root / "evil.xlsx").exists())

    def test_symlink_escape_is_denied(self):
        outside = self.sandbox.root / "outside"
        outside.mkdir()
        link = self.sandbox.documents / "escape"
        link.symlink_to(outside, target_is_directory=True)
        self._spec_file()
        pipeline = self.sandbox.pipeline(approve=True)
        report = pipeline.run(
            "create a spreadsheet named escape/out.xlsx in Documents using sales.json"
        )
        self.assertEqual(report.status, STATUS_DENIED)
        self.assertFalse((outside / "out.xlsx").exists())

    def test_tampered_workbook_fails_verification(self):
        # Verification re-reads the real file: a handler that writes
        # something other than the approved workbook must fail, even
        # when it claims success.
        from ai_core.permissions import PermissionChecker
        from integrations import ActionDispatcher
        from integrations.filesystem.tools import ToolResult

        self._spec_file()
        checker = PermissionChecker(self.sandbox.config)
        dispatcher = ActionDispatcher()

        def bad_handler(action, *, dry_run=False):
            resolved = checker.resolve(action["path"], operation="create")
            with open(resolved, "wb") as handle:  # not a real workbook
                handle.write(b"this is not a workbook")
            return ToolResult(ok=True, message="claimed success", changed=True)

        dispatcher.register(
            "create_spreadsheet",
            bad_handler,
            description="test",
            mutates=True,
            requires_approval=True,
        )
        pipeline = self.sandbox.pipeline(approve=True, dispatcher=dispatcher)
        report = pipeline.run(
            "create a spreadsheet named tamper.xlsx in Documents using sales.json"
        )
        self.assertEqual(report.status, STATUS_FAILED)
        names = [c["name"] for c in report.verification["checks"]]
        self.assertIn("workbook_reopens", names)


class TestMissingDependencyMessage(SpreadsheetTestCase):
    def test_missing_openpyxl_produces_install_hint(self):
        from unittest import mock

        from ai_core.permissions import PermissionChecker
        from integrations.documents import deps
        from integrations.documents.tools import DocumentTools

        tools = DocumentTools(PermissionChecker(self.sandbox.config))
        real_require = deps.require

        def fake_require(name):
            if name == "openpyxl":
                raise deps.MissingDependencyError("openpyxl")
            return real_require(name)

        with mock.patch.object(deps, "require", fake_require):
            result = tools.create_spreadsheet("Documents/x.xlsx", SAMPLE_SPEC)
        self.assertFalse(result.ok)
        self.assertIn("pip install", result.error)
        self.assertIn("openpyxl", result.error)
        self.assertFalse(self.sandbox.doc_path("x.xlsx").exists())


if __name__ == "__main__":
    unittest.main()
