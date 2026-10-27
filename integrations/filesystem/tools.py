"""Filesystem tools: the only code in this milestone that touches disk.

Every tool:
* resolves its path through :class:`ai_core.permissions.PermissionChecker`
  first (defence in depth — the pipeline checks too),
* refuses to overwrite or delete anything,
* never runs a shell command,
* honours dry-run mode by reporting without changing anything.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ai_core.permissions import PermissionChecker, pretty_path

MAX_READ_BYTES = 1_048_576  # 1 MiB read limit for text files


@dataclass(frozen=True)
class ToolResult:
    """Outcome of a single tool call."""

    ok: bool
    message: str
    changed: bool = False
    error: Optional[str] = None
    data: Dict = field(default_factory=dict)


class FileSystemTools:
    """Read-only and create-only filesystem operations."""

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

    # ------------------------------------------------------------------ #
    # Operations
    # ------------------------------------------------------------------ #

    def list_directory(self, path: str, *, dry_run: bool = False) -> ToolResult:
        resolved = self.checker.resolve(path, operation="read")
        if not resolved.exists():
            return ToolResult(
                ok=False,
                message="",
                error=f"{self._pretty(resolved)} does not exist.",
            )
        if not resolved.is_dir():
            return ToolResult(
                ok=False,
                message="",
                error=f"{self._pretty(resolved)} is not a directory.",
            )
        try:
            entries = sorted(resolved.iterdir(), key=lambda p: p.name.lower())
        except OSError as exc:
            return ToolResult(ok=False, message="", error=f"Cannot list directory: {exc}")

        listing = []
        for entry in entries:
            if entry.is_symlink():
                kind = "symlink"
            elif entry.is_dir():
                kind = "directory"
            else:
                kind = "file"
            listing.append({"name": entry.name, "type": kind})

        suffix = " (dry run, no changes)" if dry_run else ""
        return ToolResult(
            ok=True,
            message=f"Found {len(listing)} entries in {self._pretty(resolved)}{suffix}",
            data={"path": str(resolved), "entries": listing},
        )

    def create_directory(self, path: str, *, dry_run: bool = False) -> ToolResult:
        resolved = self.checker.resolve(path, operation="create")
        if resolved.exists() or resolved.is_symlink():
            return ToolResult(ok=False, message="", error=self._already_exists(self._pretty(resolved)))
        if not resolved.parent.exists():
            return ToolResult(ok=False, message="", error=self._missing_parent(resolved))

        if dry_run:
            return ToolResult(
                ok=True,
                message=f"Would create directory {self._pretty(resolved)} (dry run, no changes made)",
                changed=False,
                data={"path": str(resolved), "dry_run": True},
            )

        try:
            os.mkdir(resolved, mode=0o755)
        except OSError as exc:
            return ToolResult(
                ok=False,
                message="",
                error=f"Could not create directory: {exc.strerror or exc}",
            )
        return ToolResult(
            ok=True,
            message=f"Created directory {self._pretty(resolved)}",
            changed=True,
            data={"path": str(resolved)},
        )

    def create_text_file(
        self, path: str, content: str, *, dry_run: bool = False
    ) -> ToolResult:
        if not isinstance(content, str):
            return ToolResult(ok=False, message="", error="Content must be text.")
        resolved = self.checker.resolve(path, operation="create")
        if resolved.exists() or resolved.is_symlink():
            return ToolResult(ok=False, message="", error=self._already_exists(self._pretty(resolved)))
        if not resolved.parent.exists():
            return ToolResult(ok=False, message="", error=self._missing_parent(resolved))

        size = len(content.encode("utf-8"))
        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would create file {self._pretty(resolved)} "
                    f"({size} bytes, dry run, no changes made)"
                ),
                changed=False,
                data={"path": str(resolved), "bytes": size, "dry_run": True},
            )

        try:
            # O_EXCL guarantees we never clobber an existing file, even in
            # a race between the permission check and this call.
            fd = os.open(resolved, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                os.write(fd, content.encode("utf-8"))
            finally:
                os.close(fd)
        except FileExistsError:
            return ToolResult(ok=False, message="", error=self._already_exists(self._pretty(resolved)))
        except OSError as exc:
            return ToolResult(
                ok=False,
                message="",
                error=f"Could not create file: {exc.strerror or exc}",
            )
        return ToolResult(
            ok=True,
            message=f"Created file {self._pretty(resolved)} ({size} bytes)",
            changed=True,
            data={"path": str(resolved), "bytes": size},
        )

    def read_file(self, path: str, *, dry_run: bool = False) -> ToolResult:
        resolved = self.checker.resolve(path, operation="read")
        if not resolved.exists():
            return ToolResult(
                ok=False, message="", error=f"{self._pretty(resolved)} does not exist."
            )
        if resolved.is_dir():
            return ToolResult(
                ok=False, message="", error=f"{self._pretty(resolved)} is a directory, not a file."
            )
        try:
            size = resolved.stat().st_size
        except OSError as exc:
            return ToolResult(ok=False, message="", error=f"Cannot stat file: {exc}")
        if size > MAX_READ_BYTES:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"File is {size} bytes; the read limit is "
                    f"{MAX_READ_BYTES} bytes in this version."
                ),
            )
        try:
            raw = resolved.read_bytes()
        except OSError as exc:
            return ToolResult(ok=False, message="", error=f"Cannot read file: {exc}")
        text = raw.decode("utf-8", errors="replace")
        return ToolResult(
            ok=True,
            message=f"Read {self._pretty(resolved)} ({len(raw)} bytes)",
            data={"path": str(resolved), "content": text, "bytes": len(raw)},
        )


def make_fs_handlers(tools: FileSystemTools) -> Dict[str, Callable[..., ToolResult]]:
    """Bind filesystem tools to the ``handler(action, dry_run=...)`` shape."""
    return {
        "list_directory": lambda action, *, dry_run=False: tools.list_directory(
            action["path"], dry_run=dry_run
        ),
        "create_directory": lambda action, *, dry_run=False: tools.create_directory(
            action["path"], dry_run=dry_run
        ),
        "create_text_file": lambda action, *, dry_run=False: tools.create_text_file(
            action["path"], action.get("content", ""), dry_run=dry_run
        ),
        "read_file": lambda action, *, dry_run=False: tools.read_file(
            action["path"], dry_run=dry_run
        ),
    }
