"""Filesystem tools: the only code in this milestone that touches disk.

Every tool:
* resolves its path through :class:`ai_core.permissions.PermissionChecker`
  first (defence in depth — the pipeline checks too),
* refuses to overwrite or delete anything unless the operation IS the
  reviewed, approval-gated replacement (v0.18 ``replace_file_content``),
* never runs a shell command,
* honours dry-run mode by reporting without changing anything.
"""

from __future__ import annotations

import hashlib
import os
import uuid
from dataclasses import dataclass, field
from typing import Callable, Dict, List, Optional

from ai_core.permissions import PermissionChecker, pretty_path

MAX_READ_BYTES = 1_048_576  # 1 MiB read limit for text files
#: v0.18 replacement cap (defence in depth; the planner caps content
#: much lower at 64 KiB).
MAX_REPLACE_BYTES = 1_048_576


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
        # v0.18: binary files are REFUSED, not laundered into mojibake
        # by error-tolerant decoding.  A NUL byte means this is not a
        # UTF-8 text document.
        if b"\x00" in raw:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{self._pretty(resolved)} looks like a binary file "
                    f"(contains NUL bytes); only UTF-8 text documents are "
                    f"readable."
                ),
            )
        text = raw.decode("utf-8", errors="replace")
        return ToolResult(
            ok=True,
            message=f"Read {self._pretty(resolved)} ({len(raw)} bytes)",
            data={"path": str(resolved), "content": text, "bytes": len(raw)},
        )

    def replace_file_content(
        self, path: str, content: str, *, dry_run: bool = False
    ) -> ToolResult:
        """v0.18: atomically replace an EXISTING text document.

        The only overwrite primitive in the closed set, and it is
        approval-gated upstream (planner.MUTATING_ACTIONS).  Safety
        contract:

        * the target must already exist and be a regular file — a
          replace is never a silent create, and a symlink target is
          refused (replacing *through* a link would write elsewhere),
        * the new content is written to an O_EXCL temp file in the
          SAME directory, fsync'd, then moved over the target with
          ``os.replace`` (atomic within the filesystem), so a crash or
          write failure can never leave a half-written document — the
          original stays byte-identical,
        * the evidence returned is size + sha256, never the content,
        * this is NOT race-free (documented): another process touching
          the file between resolution and replace is detected by the
          verifier's independent re-read, not prevented.
        """
        if not isinstance(content, str):
            return ToolResult(ok=False, message="", error="Content must be text.")
        if "\x00" in content:
            return ToolResult(
                ok=False, message="",
                error="Content contains a null byte; only UTF-8 text is supported.",
            )
        payload = content.encode("utf-8")
        if len(payload) > MAX_REPLACE_BYTES:
            return ToolResult(
                ok=False, message="",
                error=f"Content is too large (limit {MAX_REPLACE_BYTES} bytes).",
            )
        resolved = self.checker.resolve(path, operation="create")
        if resolved.is_symlink():
            return ToolResult(
                ok=False, message="",
                error=(
                    f"{self._pretty(resolved)} is a symbolic link; "
                    f"replacing through a link is refused (create a real "
                    f"file instead)."
                ),
            )
        if not resolved.exists():
            return ToolResult(
                ok=False, message="",
                error=(
                    f"{self._pretty(resolved)} does not exist — "
                    f"'replace_file_content' never creates files; use "
                    f"'create_text_file' for a new document."
                ),
            )
        if resolved.is_dir():
            return ToolResult(
                ok=False, message="",
                error=f"{self._pretty(resolved)} is a directory, not a file.",
            )
        try:
            before_size = resolved.stat().st_size
        except OSError as exc:
            return ToolResult(ok=False, message="", error=f"Cannot stat file: {exc}")

        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would replace {self._pretty(resolved)} "
                    f"({before_size} -> {len(payload)} bytes, dry run, no "
                    f"changes made)"
                ),
                changed=False,
                data={"path": str(resolved), "bytes": len(payload),
                      "previous_bytes": before_size, "dry_run": True},
            )

        temp_path = None
        try:
            temp_path = resolved.parent / (
                f".{resolved.name}.alwin-replace-{uuid.uuid4().hex[:12]}"
            )
            fd = os.open(temp_path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o644)
            try:
                written = 0
                view = memoryview(payload)
                while written < len(view):
                    written += os.write(fd, view[written:])
                os.fsync(fd)
            finally:
                os.close(fd)
            # Atomic within the filesystem: the target is either the
            # complete old file or the complete new file.
            os.replace(temp_path, resolved)
            temp_path = None
        except OSError as exc:
            # Nothing partial can exist at the target; clean the temp.
            if temp_path is not None:
                try:
                    os.unlink(temp_path)
                except OSError:
                    pass
            return ToolResult(
                ok=False, message="",
                error=(
                    f"Could not replace file: {exc.strerror or exc}. The "
                    f"original content was not modified."
                ),
            )
        digest = hashlib.sha256(payload).hexdigest()
        return ToolResult(
            ok=True,
            message=(
                f"Replaced {self._pretty(resolved)} "
                f"({before_size} -> {len(payload)} bytes)"
            ),
            changed=True,
            data={
                "path": str(resolved),
                "bytes": len(payload),
                "previous_bytes": before_size,
                "sha256": digest,
            },
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
        "replace_file_content": lambda action, *, dry_run=False: tools.replace_file_content(
            action["path"], action.get("content", ""), dry_run=dry_run
        ),
        "read_file": lambda action, *, dry_run=False: tools.read_file(
            action["path"], dry_run=dry_run
        ),
    }
