"""Verification stage.

Success is reported only after the filesystem really contains what the
action claimed to produce.  The verifier re-checks the world
independently of the execution result: a handler that *claims* success
without doing the work fails verification.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ai_core import specs
from ai_core.executor import ExecutionResult
from ai_core.permissions import PermissionChecker
from integrations.documents.pdf import validate_pdf_bytes
from integrations.documents.report import render_text_report
from integrations.documents.spreadsheet import inspect_workbook

STATUS_PASSED = "passed"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"


@dataclass
class VerificationResult:
    """Outcome of the verification stage."""

    status: str
    message: str
    checks: List[Dict] = field(default_factory=list)

    @property
    def verified(self) -> bool:
        return self.status == STATUS_PASSED

    def as_dict(self) -> Dict:
        return {"status": self.status, "message": self.message, "checks": self.checks}


class Verifier:
    """Re-checks the results of an execution against the filesystem."""

    def __init__(self, checker: PermissionChecker) -> None:
        self.checker = checker

    # ------------------------------------------------------------------ #

    @staticmethod
    def _add(checks: List[Dict], name: str, passed: bool, detail: str = "") -> bool:
        checks.append({"name": name, "passed": bool(passed), "detail": detail})
        return bool(passed)

    def _resolve(self, action: dict, operation: str) -> Path:
        # Re-run the permission check during verification: if the world
        # changed underneath us, verification fails instead of trusting
        # the earlier decision.
        return self.checker.resolve(action.get("path", ""), operation=operation)

    def verify(self, action: dict, execution: ExecutionResult) -> VerificationResult:
        name = action.get("action")
        checks: List[Dict] = []

        if not execution.ok:
            return VerificationResult(
                status=STATUS_FAILED,
                message=(
                    "Execution failed, so the result cannot be verified. "
                    f"{execution.error or execution.message or ''}".strip()
                ),
                checks=checks,
            )

        if execution.dry_run:
            self._add(checks, "action_valid", True, "validated before execution")
            self._add(
                checks,
                "dry_run_no_change",
                True,
                "dry-run mode made no filesystem changes",
            )
            return VerificationResult(
                status=STATUS_SKIPPED,
                message="Dry run: nothing was executed, so nothing was verified on disk.",
                checks=checks,
            )

        try:
            return self._verify_executed(name, action, execution, checks)
        except Exception as exc:  # noqa: BLE001 - a verification error is a failure
            return VerificationResult(
                status=STATUS_FAILED,
                message=f"Verification failed with error: {type(exc).__name__}: {exc}",
                checks=checks,
            )

    # ------------------------------------------------------------------ #

    def _verify_executed(
        self,
        name: Optional[str],
        action: dict,
        execution: ExecutionResult,
        checks: List[Dict],
    ) -> VerificationResult:
        if name == "create_directory":
            resolved = self._resolve(action, "create")
            exists = self._add(
                checks, "path_exists", resolved.exists(), str(resolved)
            )
            is_dir = False
            if exists:
                is_dir = self._add(
                    checks, "is_directory", resolved.is_dir(), str(resolved)
                )
                self._add(
                    checks, "not_a_symlink", not resolved.is_symlink(), str(resolved)
                )
            ok = exists and is_dir
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Folder created and verified successfully."
                    if ok
                    else f"Verification failed: {resolved} is not an existing directory."
                ),
                checks=checks,
            )

        if name == "create_text_file":
            resolved = self._resolve(action, "create")
            exists = self._add(checks, "path_exists", resolved.exists(), str(resolved))
            is_file = False
            if exists:
                is_file = self._add(checks, "is_file", resolved.is_file(), str(resolved))
                if is_file:
                    actual = resolved.read_bytes()
                    expected = action.get("content", "").encode("utf-8")
                    self._add(
                        checks,
                        "content_matches",
                        actual == expected,
                        f"{len(actual)} bytes on disk",
                    )
            ok = exists and is_file and all(c["passed"] for c in checks)
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "File created and verified successfully."
                    if ok
                    else f"Verification failed: {resolved} does not contain the expected content."
                ),
                checks=checks,
            )

        if name == "list_directory":
            resolved = self._resolve(action, "read")
            is_dir = self._add(checks, "is_directory", resolved.is_dir(), str(resolved))
            reported = execution.data.get("entries")
            actual_names = []
            if is_dir and isinstance(reported, list):
                actual_names = sorted(p.name for p in resolved.iterdir())
                reported_names = sorted(e.get("name", "") for e in reported)
                self._add(
                    checks,
                    "entries_match",
                    actual_names == reported_names,
                    f"{len(actual_names)} entries on disk",
                )
            ok = is_dir and all(c["passed"] for c in checks)
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    f"Listing verified: {len(actual_names)} entries."
                    if ok
                    else "Verification failed: directory listing does not match the filesystem."
                ),
                checks=checks,
            )

        if name == "read_file":
            resolved = self._resolve(action, "read")
            is_file = self._add(checks, "is_file", resolved.is_file(), str(resolved))
            if is_file and isinstance(execution.data.get("content"), str):
                actual = resolved.read_text(encoding="utf-8", errors="replace")
                self._add(
                    checks,
                    "content_matches",
                    actual == execution.data.get("content"),
                    f"{len(actual)} chars re-read",
                )
            ok = is_file and all(c["passed"] for c in checks)
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Read verified against the filesystem."
                    if ok
                    else "Verification failed: file changed while reading."
                ),
                checks=checks,
            )

        if name in ("create_report", "create_pdf", "create_spreadsheet"):
            return self._verify_document(name, action, checks)

        if name == "explain":
            explanation = execution.data.get("explanation")
            self._add(
                checks,
                "explanation_present",
                isinstance(explanation, str) and bool(explanation.strip()),
                "explanation text",
            )
            ok = all(c["passed"] for c in checks)
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message="Explanation verified (no filesystem change expected)."
                if ok
                else "Verification failed: no explanation was produced.",
                checks=checks,
            )

        return VerificationResult(
            status=STATUS_FAILED,
            message=f"No verification rule for action '{name}'.",
            checks=checks,
        )

    # ------------------------------------------------------------------ #
    # Generated documents (v0.2)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _grid_cell(grid: List[List], a1: str):
        """Value at an A1-style coordinate in an inspected grid."""
        row, col = specs.parse_a1(a1)
        if row - 1 < len(grid):
            row_values = grid[row - 1]
            if col - 1 < len(row_values):
                return row_values[col - 1]
        return None

    @staticmethod
    def _expected_grid(sheet: dict) -> List[List]:
        """Grid the sheet must contain: headers row + data rows, padded."""
        rows: List[List] = []
        if sheet["headers"]:
            rows.append(list(sheet["headers"]))
        rows.extend(list(r) for r in sheet["rows"])
        width = max((len(r) for r in rows), default=0)
        return [list(r) + [None] * (width - len(r)) for r in rows]

    def _verify_spreadsheet(
        self, action: dict, actual: bytes, checks: List[Dict]
    ) -> bool:
        """Reopen the workbook and compare it with the approved spec."""
        try:
            inspected = inspect_workbook(actual)
        except Exception as exc:  # noqa: BLE001 - a bad file fails verification
            return self._add(
                checks, "workbook_reopens", False, f"{type(exc).__name__}: {exc}"
            )
        if not self._add(
            checks,
            "workbook_reopens",
            True,
            f"{len(inspected['sheets'])} sheet(s) re-read from disk",
        ):
            return False

        spec = action.get("spec") or {}
        expected_names = [s["name"] for s in spec.get("sheets", [])]
        names_ok = self._add(
            checks,
            "sheet_names_match",
            inspected["sheetnames"] == expected_names,
            f"expected {expected_names}, found {inspected['sheetnames']}",
        )

        cells_ok = True
        formulas_ok = True
        for sheet in spec.get("sheets", []):
            match = next(
                (s for s in inspected["sheets"] if s["name"] == sheet["name"]), None
            )
            if match is None:
                cells_ok = formulas_ok = False
                continue
            expected_grid = self._expected_grid(sheet)
            # Formula cells overlay whatever the data rows declared there.
            for entry in sheet["formulas"]:
                row, col = specs.parse_a1(entry["cell"])
                if row - 1 < len(expected_grid) and col - 1 < len(expected_grid[row - 1]):
                    expected_grid[row - 1][col - 1] = entry["formula"]
            actual_grid = match["grid"]
            # Compare the expected rectangle and require nothing outside it.
            for r_index, expected_row in enumerate(expected_grid):
                actual_row = actual_grid[r_index] if r_index < len(actual_grid) else []
                for c_index, expected_value in enumerate(expected_row):
                    actual_value = (
                        actual_row[c_index] if c_index < len(actual_row) else None
                    )
                    if actual_value != expected_value:
                        cells_ok = False
            for extra_row in actual_grid[len(expected_grid) :]:
                if any(value is not None for value in extra_row):
                    cells_ok = False
            for entry in sheet["formulas"]:
                stored = self._grid_cell(actual_grid, entry["cell"])
                if stored != entry["formula"]:
                    formulas_ok = False

        self._add(
            checks,
            "cells_match",
            cells_ok,
            "headers and rows re-read from the workbook",
        )
        self._add(
            checks,
            "formulas_preserved",
            formulas_ok,
            "formula strings are stored as text; they are not calculated here",
        )
        return names_ok and cells_ok and formulas_ok

    def _verify_document(
        self, name: str, action: dict, checks: List[Dict]
    ) -> VerificationResult:
        resolved = self._resolve(action, "create")
        exists = self._add(checks, "path_exists", resolved.exists(), str(resolved))
        is_file = False
        if exists:
            is_file = self._add(
                checks,
                "is_file",
                resolved.is_file() and not resolved.is_symlink(),
                str(resolved),
            )
        if not (exists and is_file):
            return VerificationResult(
                status=STATUS_FAILED,
                message=f"Verification failed: {resolved} is not a new file.",
                checks=checks,
            )
        try:
            actual = resolved.read_bytes()
        except OSError as exc:
            return VerificationResult(
                status=STATUS_FAILED,
                message=f"Verification failed: cannot re-read {resolved}: {exc}",
                checks=checks,
            )
        nonempty = self._add(
            checks, "nonempty", len(actual) > 0, f"{len(actual)} bytes on disk"
        )

        if name == "create_report":
            expected = render_text_report(
                specs.validate_report_content(action.get("content"))
            ).encode("utf-8")
            matches = self._add(
                checks,
                "content_matches",
                actual == expected,
                "re-rendered from the approved content and compared byte-for-byte",
            )
            ok = nonempty and matches
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Text report created and verified byte-for-byte."
                    if ok
                    else "Verification failed: the file does not match the rendered report."
                ),
                checks=checks,
            )

        if name == "create_pdf":
            valid, detail = validate_pdf_bytes(actual)
            structure_ok = self._add(checks, "pdf_structure_valid", valid, detail)
            ok = nonempty and structure_ok
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "PDF created and structurally verified (the bytes are not "
                    "re-rendered because PDFs embed creation metadata)."
                    if ok
                    else f"Verification failed: {detail}."
                ),
                checks=checks,
            )

        # create_spreadsheet
        ok = nonempty and self._verify_spreadsheet(action, actual, checks)
        return VerificationResult(
            status=STATUS_PASSED if ok else STATUS_FAILED,
            message=(
                "Workbook created, reopened and verified (formula cells are "
                "stored as text; open it in a spreadsheet application to "
                "calculate them)."
                if ok
                else "Verification failed: the workbook does not match the specification."
            ),
            checks=checks,
        )
