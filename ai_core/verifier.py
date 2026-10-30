"""Verification stage.

Success is reported only after the filesystem really contains what the
action claimed to produce.  The verifier re-checks the world
independently of the execution result: a handler that *claims* success
without doing the work fails verification.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, List, Optional

from ai_core import specs
from ai_core.executor import ExecutionResult
from ai_core.permissions import PermissionChecker, pretty_path
from integrations.automation.base import (
    ACTION_PERFORMED,
    APPLICATION_STATES,
    CONTROL_IDENTIFIED,
    DOCUMENT_OPEN,
    INTERFACE_STATES,
    MAX_READ_TEXT_CHARS,
    RESULT_VERIFIED,
    safe_app_text,
)
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
                # Read EXACTLY like the executor does (raw bytes +
                # replace): Path.read_text() would normalize CRLF to
                # LF and make every Windows-line-ending file (any
                # csv.writer output) falsely fail with "file changed
                # while reading" (real bug found by the v0.12 planner
                # verification against a real CRLF fixture).
                try:
                    actual = resolved.read_bytes().decode(
                        "utf-8", errors="replace")
                except OSError:
                    actual = None
                self._add(
                    checks,
                    "content_matches",
                    actual == execution.data.get("content"),
                    f"{len(actual) if actual is not None else 0} chars "
                    "re-read",
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

        if name == "open_file":
            # Re-resolve the path: the file must still exist inside the
            # permitted roots, and the handler must have reported a
            # real process start (not a promise).
            resolved = self._resolve(action, "open")
            exists = self._add(checks, "path_exists", resolved.exists(), str(resolved))
            is_file = False
            if exists:
                is_file = self._add(checks, "is_file", resolved.is_file(), str(resolved))
            started = self._add(
                checks,
                "process_started",
                execution.data.get("started") is True,
                str(execution.data.get("detail") or "no launch detail reported"),
            )
            integration = self._add(
                checks,
                "integration_reported",
                bool(execution.data.get("integration")),
                str(execution.data.get("integration") or "missing"),
            )
            ok = exists and is_file and started and integration
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Verified: the file still exists inside the permitted roots "
                    "and the application process was started. This confirms the "
                    "launch only — Alwin Fusion does not inspect or control the "
                    "application window."
                    if ok
                    else "Verification failed: the launch was not confirmed "
                    "(file missing, not a file, or no process start reported)."
                ),
                checks=checks,
            )

        if name == "detect_applications":
            # Detection is read-only; verification only confirms the
            # report is well-formed (one status per requested
            # integration) — nothing on disk is checked because nothing
            # was changed.
            integrations = execution.data.get("integrations")
            well_formed = self._add(
                checks,
                "report_well_formed",
                isinstance(integrations, list) and bool(integrations)
                if not action.get("integration")
                else isinstance(integrations, list) and len(integrations) == 1,
                f"{len(integrations) if isinstance(integrations, list) else 0} "
                f"integration(s) reported",
            )
            statuses_known = self._add(
                checks,
                "statuses_known",
                isinstance(integrations, list)
                and all(
                    item.get("status")
                    in ("available", "not_installed", "detection_failed")
                    for item in integrations
                ),
                "every integration reports a known status",
            )
            ok = well_formed and statuses_known
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Detection report verified as read-only and well-formed; "
                    "nothing was launched, installed or changed."
                    if ok
                    else "Verification failed: the detection report is malformed."
                ),
                checks=checks,
            )

        if name in ("create_report", "create_pdf", "create_spreadsheet"):
            return self._verify_document(name, action, checks)

        # ---- v0.4 desktop automation --------------------------------- #
        if name in (
            "inspect_application",
            "find_control",
            "read_control",
            "activate_control",
            "enter_text",
            "save_document",
        ):
            return self._verify_automation(name, action, execution, checks)

        if name == "task_sequence":
            return self._verify_sequence(action, execution, checks)

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

    # ------------------------------------------------------------------ #
    # Desktop automation (v0.4)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _control_matches(
        checks: List[Dict], action: dict, execution: ExecutionResult
    ) -> bool:
        """The reported control must match the validated locator."""
        locator = action.get("control") or {}
        reported = execution.data.get("control")

        def _norm(value: object) -> str:
            # Names reach the parser lower-cased and whitespace-collapsed;
            # compare the same way the backend does — case and spacing
            # blind — but never ignore anything else.
            return " ".join(str(value or "").lower().split())

        found = Verifier._add(
            checks,
            "control_found",
            isinstance(reported, dict) and bool(reported.get("role")),
            f"role '{reported.get('role')}' reported"
            if isinstance(reported, dict)
            else "no control reported",
        )
        role_ok = Verifier._add(
            checks,
            "role_matches",
            isinstance(reported, dict)
            and _norm(reported.get("role")) == _norm(locator.get("role")),
            f"expected '{locator.get('role')}', got "
            f"'{reported.get('role') if isinstance(reported, dict) else None}'",
        )
        wanted_name = safe_app_text(locator.get("name", ""), 120)
        name_ok = Verifier._add(
            checks,
            "name_matches",
            not wanted_name
            or (
                isinstance(reported, dict)
                and _norm(reported.get("name")) == _norm(wanted_name)
            ),
            f"expected name '{wanted_name}'" if wanted_name else "role-only search",
        )
        return found and role_ok and name_ok

    def _verify_automation(
        self,
        name: str,
        action: dict,
        execution: ExecutionResult,
        checks: List[Dict],
    ) -> VerificationResult:
        data = execution.data

        if name == "inspect_application":
            snapshot = data.get("snapshot")
            well_formed = self._add(
                checks,
                "report_well_formed",
                isinstance(snapshot, dict)
                and snapshot.get("application") == action.get("application")
                and snapshot.get("state") in APPLICATION_STATES,
                f"state '{snapshot.get('state') if isinstance(snapshot, dict) else None}'",
            )
            interface_ok = True
            if isinstance(snapshot, dict) and snapshot.get("state") in INTERFACE_STATES:
                interface_ok = self._add(
                    checks,
                    "interface_state_backed",
                    snapshot.get("interface_detected") is True
                    and snapshot.get("a11y_available") is True,
                    "an interface state claims a live accessible connection",
                )
            document_ok = True
            if isinstance(snapshot, dict) and snapshot.get("state") == DOCUMENT_OPEN:
                document_ok = self._add(
                    checks,
                    "document_state_backed",
                    snapshot.get("document_reliable") is True
                    and bool(snapshot.get("document")),
                    "a document-open claim needs a reliably identified document",
                )
            ok = well_formed and interface_ok and document_ok
            state = snapshot.get("state") if isinstance(snapshot, dict) else "?"
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    f"State report verified as internally honest (reported "
                    f"state: '{state}'); inspection changed nothing."
                    if ok
                    else "Verification failed: the state report claims more "
                    "than its evidence supports."
                ),
                checks=checks,
            )

        if name == "find_control":
            control_ok = self._control_matches(checks, action, execution)
            state_ok = self._add(
                checks,
                "state_control_identified",
                data.get("state") == CONTROL_IDENTIFIED,
                str(data.get("state")),
            )
            ok = control_ok and state_ok
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Control identity verified against the validated locator "
                    "(read-only operation; nothing was changed)."
                    if ok
                    else "Verification failed: the reported control does not "
                    "match the validated locator."
                ),
                checks=checks,
            )

        if name == "read_control":
            control_ok = self._control_matches(checks, action, execution)
            text_reported = self._add(
                checks,
                "text_reported",
                isinstance(data.get("text"), str),
                f"{data.get('text_chars')} chars reported",
            )
            bounded = self._add(
                checks,
                "text_within_limit",
                isinstance(data.get("text"), str)
                and len(data["text"]) <= MAX_READ_TEXT_CHARS,
                f"stored {len(data.get('text', ''))} chars "
                f"(limit {MAX_READ_TEXT_CHARS})",
            )
            ok = control_ok and text_reported and bounded
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Read verified against the execution record (the text "
                    "itself is application-provided, untrusted display data "
                    "and is not written to the audit log)."
                    if ok
                    else "Verification failed: the read result is missing, "
                    "bounded incorrectly, or points at the wrong control."
                ),
                checks=checks,
            )

        if name == "activate_control":
            control_ok = self._control_matches(checks, action, execution)
            performed = self._add(
                checks,
                "action_performed",
                data.get("action_performed") is True,
                str(data.get("action_performed")),
            )
            revalidated = self._add(
                checks,
                "control_revalidated",
                data.get("revalidated") is True,
                "application and control were re-checked before the action",
            )
            state_ok = self._add(
                checks,
                "state_action_level",
                data.get("state") in (ACTION_PERFORMED, RESULT_VERIFIED),
                str(data.get("state")),
            )
            ok = control_ok and performed and revalidated and state_ok
            verified = data.get("state") == RESULT_VERIFIED
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Activation verified by re-reading the control state "
                    "(result verified)."
                    if ok and verified
                    else (
                        "Action performed on a revalidated control; "
                        "downstream effects are not independently verifiable "
                        "through accessibility (state: action_performed)."
                        if ok
                        else "Verification failed: the activation was not "
                        "reported with evidence (missing action/revalidation)."
                    )
                ),
                checks=checks,
            )

        if name == "enter_text":
            control_ok = self._control_matches(checks, action, execution)
            performed = self._add(
                checks,
                "action_performed",
                data.get("action_performed") is True,
                str(data.get("action_performed")),
            )
            readback = self._add(
                checks,
                "readback_confirmed",
                data.get("readback_match") is True
                and data.get("state") == RESULT_VERIFIED,
                "the field was read back after entry",
            )
            length_ok = self._add(
                checks,
                "entered_length_matches",
                data.get("entered_chars") == len(action.get("content", "")),
                f"{data.get('entered_chars')} chars entered, "
                f"{len(action.get('content', ''))} approved",
            )
            ok = control_ok and performed and readback and length_ok
            return VerificationResult(
                status=STATUS_PASSED if ok else STATUS_FAILED,
                message=(
                    "Entered text confirmed by reading the field back "
                    "(result verified through the application's accessible "
                    "text; the audit log stores a length marker only)."
                    if ok
                    else "Verification failed: the entered text could not be "
                    "confirmed (state unknown — no success is claimed)."
                ),
                checks=checks,
            )

        # name == "save_document"
        try:
            resolved = self.checker.resolve(action.get("path", ""), operation="save")
        except Exception as exc:  # noqa: BLE001 - re-resolution failure
            return VerificationResult(
                status=STATUS_FAILED,
                message=f"Verification failed: the save target is no longer "
                f"permitted ({exc}).",
                checks=checks,
            )
        exists = self._add(checks, "path_exists", resolved.exists(), str(resolved))
        is_file = exists and self._add(
            checks, "is_file", resolved.is_file(), str(resolved)
        )
        activation = self._add(
            checks,
            "save_activation_recorded",
            data.get("action_performed") is True,
            "the reviewed Save control was activated",
        )
        before = data.get("before_digest")
        after = data.get("after_digest")
        digests_ok = self._add(
            checks,
            "digests_recorded_and_changed",
            isinstance(before, str)
            and isinstance(after, str)
            and bool(before)
            and bool(after)
            and before != after,
            "sha256 before the activation differs from after",
        )
        changed_claim = self._add(
            checks,
            "file_change_confirmed",
            data.get("file_changed") is True,
            str(data.get("file_changed")),
        )
        disk_ok = False
        if is_file and isinstance(after, str) and after:
            try:
                current = hashlib.sha256(resolved.read_bytes()).hexdigest()
            except OSError as exc:
                current = ""
                self._add(checks, "disk_re_readable", False, str(exc))
            else:
                disk_ok = self._add(
                    checks,
                    "disk_matches_recorded_digest",
                    current == after,
                    "re-read independently at verification time",
                )
        else:
            self._add(checks, "disk_matches_recorded_digest", False, "no digest")
        ok = exists and is_file and activation and digests_ok and changed_claim and disk_ok
        return VerificationResult(
            status=STATUS_PASSED if ok else STATUS_FAILED,
            message=(
                f"Save verified: {pretty_path(resolved, self.checker.home)} on "
                f"disk matches the digest recorded after the Save control was "
                f"activated — the activation alone was never treated as a save."
                if ok
                else "Verification failed: the file change could not be "
                "confirmed against the recorded digest, so NO save is "
                "reported (state unknown)."
            ),
            checks=checks,
        )

    def _verify_sequence(
        self, action: dict, execution: ExecutionResult, checks: List[Dict]
    ) -> VerificationResult:
        """Re-verify every step independently; one failure fails the task."""
        steps = action.get("steps") or []
        records = execution.data.get("steps")
        recorded = isinstance(records, list) and len(records) == len(steps)
        self._add(
            checks,
            "all_steps_recorded",
            recorded,
            f"{len(records) if isinstance(records, list) else 0} of "
            f"{len(steps)} step results recorded",
        )
        statuses_ok = recorded and all(
            isinstance(record, dict) and record.get("status") == "ok"
            and record.get("ok") is True
            for record in records
        )
        self._add(
            checks,
            "no_step_failed_or_skipped",
            bool(statuses_ok),
            "every step reports status 'ok'"
            if statuses_ok
            else "a step failed, was denied or was skipped",
        )
        if not (recorded and statuses_ok):
            return VerificationResult(
                status=STATUS_FAILED,
                message=(
                    "Verification failed: the sequence has an incomplete or "
                    "unsuccessful step, so complete task success is NOT "
                    "reported."
                ),
                checks=checks,
            )

        for index, (step, record) in enumerate(zip(steps, records), start=1):
            step_execution = ExecutionResult(
                ok=True,
                executed=bool(record.get("executed")),
                dry_run=False,
                message=str(record.get("message") or ""),
                error=record.get("error"),
                data=dict(record.get("data") or {}),
            )
            sub_checks: List[Dict] = []
            result = self._verify_executed(
                step.get("action"), step, step_execution, sub_checks
            )
            for check in sub_checks:
                renamed = dict(check)
                renamed["name"] = f"step_{index}:{check['name']}"
                checks.append(renamed)
            if result.status != STATUS_PASSED:
                return VerificationResult(
                    status=STATUS_FAILED,
                    message=(
                        f"Verification failed at step {index} of {len(steps)}: "
                        f"{result.message} The task is not reported as "
                        f"successful."
                    ),
                    checks=checks,
                )
        return VerificationResult(
            status=STATUS_PASSED,
            message=(
                f"All {len(steps)} step(s) verified independently; partial "
                f"success is never reported as full success."
            ),
            checks=checks,
        )
