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

from ai_core.executor import ExecutionResult
from ai_core.permissions import PermissionChecker

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
