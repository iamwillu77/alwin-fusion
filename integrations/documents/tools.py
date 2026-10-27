"""Document tools: create reports, PDFs and spreadsheets on disk.

Every tool follows the same contract as ``integrations.filesystem``:

* re-resolves its path through the permission checker (defence in
  depth — the pipeline checked already),
* re-validates its content against ``ai_core.specs`` before rendering,
* refuses to overwrite an existing file or follow a symlink, using
  ``O_EXCL`` so a race cannot clobber anything either,
* honours dry-run mode by reporting without changing anything,
* reports the real error, including a missing optional dependency.
"""

from __future__ import annotations

import os
from typing import Callable, Dict

from ai_core.permissions import PermissionChecker, PathSecurityError, pretty_path
from ai_core.specs import (
    SpecError,
    validate_report_content,
    validate_spreadsheet_spec,
)
from integrations.documents import deps
from integrations.documents.pdf import render_pdf, validate_pdf_bytes
from integrations.documents.report import render_text_report
from integrations.documents.spreadsheet import build_workbook
from integrations.filesystem.tools import ToolResult


from integrations.documents import deps as _doc_deps

#: Re-exported for callers that handle a missing optional dependency.
MissingDependencyError = _doc_deps.MissingDependencyError


class DocumentTools:
    """Create-only document operations (reports, PDFs, spreadsheets)."""

    def __init__(self, checker: PermissionChecker) -> None:
        self.checker = checker

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _pretty(self, path) -> str:
        return pretty_path(path, self.checker.home)

    @staticmethod
    def _already_exists(path) -> str:
        return (
            f"{path} already exists. Overwriting or merging is not "
            f"supported in this version."
        )

    @staticmethod
    def _missing_parent(path) -> str:
        return (
            f"Parent directory {path.parent} does not exist. "
            f"Create it first with an explicit, approved action."
        )

    def _prepare(self, path: str):
        """Permission-check *path* and refuse existing files / bad parents.

        Returns ``(resolved, None)`` on success or ``(None, ToolResult)``
        on failure.
        """
        try:
            resolved = self.checker.resolve(path, operation="create")
        except PathSecurityError as exc:
            return None, ToolResult(ok=False, message="", error=str(exc))
        if resolved.exists() or resolved.is_symlink():
            return None, ToolResult(
                ok=False, message="", error=self._already_exists(self._pretty(resolved))
            )
        if not resolved.parent.exists():
            return None, ToolResult(
                ok=False, message="", error=self._missing_parent(resolved)
            )
        return resolved, None

    @staticmethod
    def _write_new(resolved, data: bytes):
        """Write *data* to a new file with O_EXCL (never overwrites)."""
        try:
            fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, data)
            finally:
                os.close(fd)
        except FileExistsError:
            return DocumentTools._already_exists(pretty_path(resolved, None))
        except OSError as exc:
            return f"Could not create file: {exc.strerror or exc}"
        return None

    # ------------------------------------------------------------------ #
    # Operations
    # ------------------------------------------------------------------ #

    def create_report(self, path: str, content, *, dry_run: bool = False) -> ToolResult:
        try:
            content = validate_report_content(content)
        except SpecError as exc:
            return ToolResult(ok=False, message="", error=f"Invalid report content: {exc}")

        data = render_text_report(content).encode("utf-8")
        resolved, failure = self._prepare(path)
        if failure is not None:
            return failure

        blocks = len(content["blocks"])
        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would create text report {self._pretty(resolved)} "
                    f"({len(data)} bytes, {blocks} blocks, dry run, no changes made)"
                ),
                changed=False,
                data={"path": str(resolved), "bytes": len(data), "dry_run": True},
            )

        error = self._write_new(resolved, data)
        if error is not None:
            return ToolResult(
                ok=False,
                message="",
                error=error.replace(str(resolved), self._pretty(resolved)),
            )
        return ToolResult(
            ok=True,
            message=(
                f"Created text report {self._pretty(resolved)} "
                f"({len(data)} bytes, {blocks} blocks)"
            ),
            changed=True,
            data={"path": str(resolved), "bytes": len(data), "blocks": blocks},
        )

    def create_pdf(self, path: str, content, *, dry_run: bool = False) -> ToolResult:
        try:
            content = validate_report_content(content)
        except SpecError as exc:
            return ToolResult(ok=False, message="", error=f"Invalid PDF content: {exc}")

        resolved, failure = self._prepare(path)
        if failure is not None:
            return failure

        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would create PDF document {self._pretty(resolved)} "
                    f"(dry run, no changes made)"
                ),
                changed=False,
                data={"path": str(resolved), "dry_run": True},
            )

        try:
            data, pages = render_pdf(content)
        except deps.MissingDependencyError as exc:
            return ToolResult(ok=False, message="", error=str(exc))
        ok, detail = validate_pdf_bytes(data)
        if not ok:  # pragma: no cover - defensive: reportlab produced a bad file
            return ToolResult(
                ok=False,
                message="",
                error=f"Generated PDF failed validation: {detail}",
            )

        error = self._write_new(resolved, data)
        if error is not None:
            return ToolResult(
                ok=False,
                message="",
                error=error.replace(str(resolved), self._pretty(resolved)),
            )
        return ToolResult(
            ok=True,
            message=(
                f"Created PDF document {self._pretty(resolved)} "
                f"({len(data)} bytes, {pages} pages)"
            ),
            changed=True,
            data={"path": str(resolved), "bytes": len(data), "pages": pages},
        )

    def create_spreadsheet(self, path: str, spec, *, dry_run: bool = False) -> ToolResult:
        try:
            spec = validate_spreadsheet_spec(spec)
        except SpecError as exc:
            return ToolResult(
                ok=False, message="", error=f"Invalid spreadsheet specification: {exc}"
            )

        resolved, failure = self._prepare(path)
        if failure is not None:
            return failure

        sheet_names = [s["name"] for s in spec["sheets"]]
        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would create spreadsheet {self._pretty(resolved)} "
                    f"({len(sheet_names)} sheets, dry run, no changes made)"
                ),
                changed=False,
                data={"path": str(resolved), "dry_run": True},
            )

        try:
            data = build_workbook(spec)
        except deps.MissingDependencyError as exc:
            return ToolResult(ok=False, message="", error=str(exc))

        error = self._write_new(resolved, data)
        if error is not None:
            return ToolResult(
                ok=False,
                message="",
                error=error.replace(str(resolved), self._pretty(resolved)),
            )
        return ToolResult(
            ok=True,
            message=(
                f"Created spreadsheet {self._pretty(resolved)} "
                f"({len(data)} bytes, {len(sheet_names)} sheets: "
                f"{', '.join(sheet_names)})"
            ),
            changed=True,
            data={
                "path": str(resolved),
                "bytes": len(data),
                "sheets": sheet_names,
            },
        )


def make_doc_handlers(tools: DocumentTools) -> Dict[str, Callable[..., ToolResult]]:
    """Bind document tools to the ``handler(action, dry_run=...)`` shape."""
    return {
        "create_report": lambda action, *, dry_run=False: tools.create_report(
            action["path"], action.get("content"), dry_run=dry_run
        ),
        "create_pdf": lambda action, *, dry_run=False: tools.create_pdf(
            action["path"], action.get("content"), dry_run=dry_run
        ),
        "create_spreadsheet": lambda action, *, dry_run=False: tools.create_spreadsheet(
            action["path"], action.get("spec"), dry_run=dry_run
        ),
    }
