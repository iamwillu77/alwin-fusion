"""Path resolution, containment checks and permission decisions.

Every filesystem access in Alwin Fusion must pass through
:class:`PermissionChecker` before it happens.  The checker:

* rejects null bytes, over-long paths and ``..`` traversal,
* resolves the real path (following symbolic links) and requires the
  result to live inside one of the configured permitted roots,
* rejects protected system locations even if a root were misconfigured,
* prefers the home-directory interpretation of a relative path when a
  matching entry already exists there, so an existing file outside the
  roots can never be silently re-targeted into a root.

This is a reference-monitor style gate: the executor and the
filesystem tools both call it, but the tools call it again themselves
(defence in depth).
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Optional, Union

from ai_core.config import Config

PathLike = Union[str, Path]

MAX_PATH_LENGTH = 1024

#: Locations that must never be touched, whatever a root configuration says.
PROTECTED_PREFIXES = (
    "/etc",
    "/usr",
    "/bin",
    "/sbin",
    "/boot",
    "/var",
    "/proc",
    "/sys",
    "/dev",
    "/root",
    "/lib",
    "/lib64",
    "/opt",
    "/snap",
    "/srv",
)


class PathSecurityError(Exception):
    """Base class for path validation failures."""


class InvalidPathError(PathSecurityError):
    """The path is malformed (null byte, traversal, empty, ...)."""


class PermissionDenied(PathSecurityError):
    """The path is well-formed but not permitted by the configuration."""


@dataclass(frozen=True)
class PermissionDecision:
    """Result of a permission check; never raises."""

    allowed: bool
    operation: str
    requested: str
    resolved: Optional[Path]
    reason: str

    def as_dict(self) -> dict:
        return {
            "allowed": self.allowed,
            "operation": self.operation,
            "requested": self.requested,
            "resolved": str(self.resolved) if self.resolved is not None else None,
            "reason": self.reason,
        }


def pretty_path(path: PathLike, home: Optional[PathLike]) -> str:
    """Render *path* as ``~/...`` when it lives inside *home*."""
    p = Path(path)
    if home is None:
        return str(p)
    home_p = Path(home)
    try:
        if p == home_p:
            return "~"
        if p.is_relative_to(home_p):
            return "~/" + p.relative_to(home_p).as_posix()
    except (ValueError, OSError):
        pass
    return str(p)


class PermissionChecker:
    """Validates requested paths against configured permitted roots."""

    def __init__(self, config: Config) -> None:
        self.config = config
        self.home = Path(os.path.realpath(config.home))
        self.roots = tuple(Path(os.path.realpath(r)) for r in config.permitted_roots)

    # ------------------------------------------------------------------ #
    # Internals
    # ------------------------------------------------------------------ #

    def _candidates(self, raw: str) -> list[Path]:
        """Possible interpretations of a user-supplied path, in order.

        Plan paths are relative to the home directory (the schema used by
        the planner, e.g. ``Documents/Business Reports``); ``.`` means the
        primary permitted root.  Absolute and ``~`` paths are taken as
        typed.  There is deliberately no fallback interpretation: an
        existing home-level entry outside the roots is denied, never
        silently re-targeted into a root.
        """
        if raw in ("", "."):
            return [self.roots[0]]
        if raw == "~":
            return [self.home]
        if raw.startswith("~/"):
            return [self.home / raw[2:]]
        p = Path(raw)
        if p.is_absolute():
            return [p]
        return [self.home / p]

    def _inside_roots(self, resolved: Path) -> bool:
        return any(resolved == root or resolved.is_relative_to(root) for root in self.roots)

    @staticmethod
    def _is_protected(resolved: Path) -> bool:
        for prefix in PROTECTED_PREFIXES:
            p = Path(prefix)
            if resolved == p or resolved.is_relative_to(p):
                return True
        return False

    def _roots_display(self) -> str:
        return ", ".join(pretty_path(r, self.home) for r in self.roots)

    # ------------------------------------------------------------------ #
    # Public API
    # ------------------------------------------------------------------ #

    def resolve(self, path: PathLike, *, operation: str = "read") -> Path:
        """Resolve *path* and return it, or raise :class:`PathSecurityError`."""
        if not isinstance(path, str):
            raise InvalidPathError(
                f"Path must be a string, got {type(path).__name__}."
            )
        raw = path.strip()
        if not raw:
            raise InvalidPathError("Path must not be empty.")
        if "\x00" in raw:
            raise InvalidPathError("Path contains a null byte.")
        if len(raw) > MAX_PATH_LENGTH:
            raise InvalidPathError(
                f"Path is too long (limit {MAX_PATH_LENGTH} characters)."
            )
        if ".." in Path(raw).parts:
            raise InvalidPathError(
                "Path traversal with '..' is not allowed."
            )

        candidates = self._candidates(raw)
        last_reason = ""
        for candidate in candidates:
            resolved = Path(os.path.realpath(candidate))
            if self._is_protected(resolved):
                last_reason = f"{resolved} is a protected system location"
                continue
            if not self._inside_roots(resolved):
                last_reason = (
                    f"'{raw}' resolves to {resolved}, which is outside the "
                    f"permitted roots ({self._roots_display()})"
                )
                continue
            return resolved

        raise PermissionDenied(
            f"Operation '{operation}' on '{raw}' is not permitted: "
            f"{last_reason or 'no permitted path matches'}."
        )

    def check(self, path: PathLike, *, operation: str = "read") -> PermissionDecision:
        """Non-raising variant of :meth:`resolve`."""
        requested = path if isinstance(path, str) else str(path)
        try:
            resolved = self.resolve(requested, operation=operation)
        except PathSecurityError as exc:
            return PermissionDecision(
                allowed=False,
                operation=operation,
                requested=requested,
                resolved=None,
                reason=str(exc),
            )
        return PermissionDecision(
            allowed=True,
            operation=operation,
            requested=requested,
            resolved=resolved,
            reason="inside permitted roots",
        )

    def check_action(self, action: dict) -> PermissionDecision:
        """Run the permission stage for a validated plan."""
        name = action.get("action")
        if name == "explain":
            target = action.get("target") or {}
            target_path = target.get("path")
            if not isinstance(target_path, str) or not target_path:
                return PermissionDecision(
                    allowed=True,
                    operation="describe",
                    requested=str(target),
                    resolved=None,
                    reason="explanation does not touch the filesystem",
                )
            decision = self.check(target_path, operation="create")
            # Explanations are informational: even a denied target path may
            # be explained; the decision is reported, not enforced.
            return PermissionDecision(
                allowed=decision.allowed,
                operation="describe",
                requested=target_path,
                resolved=decision.resolved,
                reason=decision.reason,
            )
        # Defence in depth: this mapping duplicates planner.MUTATING_ACTIONS
        # on purpose (the planner imports this module, so it cannot be
        # imported back without a cycle).  tests/ pins both sets.
        if name in (
            "create_directory",
            "create_text_file",
            "create_report",
            "create_pdf",
            "create_spreadsheet",
        ):
            operation = "create"
        elif name in ("list_directory", "read_file"):
            operation = "read"
        elif name == "open_file":
            # Opening a file with an application requires the same
            # containment checks as any other access; the path must
            # resolve inside the permitted roots before a launch can
            # even be considered.  The launch itself additionally
            # requires user approval (planner.MUTATING_ACTIONS).
            operation = "open"
        elif name == "detect_applications":
            # Read-only detection never touches the filesystem through
            # the pipeline (the tools use fixed system locations), so
            # there is no path to check here.
            return PermissionDecision(
                allowed=True,
                operation="detect",
                requested=str(action.get("integration", "")),
                resolved=None,
                reason="application detection is read-only",
            )
        else:
            return PermissionDecision(
                allowed=False,
                operation="unknown",
                requested=str(action.get("path", "")),
                resolved=None,
                reason=f"action '{name}' is not in the allowlist",
            )
        return self.check(action.get("path", ""), operation=operation)
