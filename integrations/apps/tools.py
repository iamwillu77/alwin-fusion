"""Application tools: detect installed apps, open approved files.

Every tool follows the contract of ``integrations.filesystem`` and
``integrations.documents``:

* the file to open is re-resolved through the permission checker
  (defence in depth — the pipeline checked already), and must be an
  existing regular file inside the permitted roots,
* only a *registered integration id* selects an application; the
  executable and its flags come from the reviewed registry, never from
  the caller,
* ``open_file`` honours dry-run by reporting without launching, and
  reports launch failures exactly as they happened,
* ``detect`` is strictly read-only: nothing is launched, installed,
  removed or updated.
"""

from __future__ import annotations

import shutil
from pathlib import Path
from typing import Callable, Dict, Optional, Sequence

from ai_core.permissions import PermissionChecker, PathSecurityError, pretty_path
from integrations.apps.discovery import (
    default_desktop_dirs,
    detect_report,
)
from integrations.apps.launch import LaunchAdapter, LaunchError
from integrations.apps.registry import (
    BUILTIN_REGISTRY,
    IntegrationDefinition,
    IntegrationRegistry,
    RegistryError,
)
from integrations.filesystem.tools import ToolResult


class ApplicationTools:
    """Discovery and controlled launching for registered applications."""

    def __init__(
        self,
        checker: PermissionChecker,
        *,
        registry: IntegrationRegistry = BUILTIN_REGISTRY,
        launcher: Optional[LaunchAdapter] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        desktop_dirs: Optional[Sequence[Path]] = None,
    ) -> None:
        self.checker = checker
        self.registry = registry
        self.launcher = launcher or LaunchAdapter()
        self._which = which or shutil.which
        self._desktop_dirs = (
            tuple(desktop_dirs)
            if desktop_dirs is not None
            else default_desktop_dirs(checker.home)
        )

    def _pretty(self, path) -> str:
        return pretty_path(path, self.checker.home)

    # ------------------------------------------------------------------ #
    # Detection (read-only)
    # ------------------------------------------------------------------ #

    def detect(self, integration_id: str = "", *, dry_run: bool = False) -> ToolResult:
        """Report the availability of registered applications."""
        if integration_id and integration_id not in self.registry:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"Unknown application '{integration_id}'. Known: "
                    f"{', '.join(self.registry.ids)}."
                ),
            )

        report = detect_report(
            self.registry, which=self._which, desktop_dirs=self._desktop_dirs
        )
        integrations = report["integrations"]
        if integration_id:
            integrations = [i for i in integrations if i["id"] == integration_id]

        counts: Dict[str, int] = {}
        for item in integrations:
            counts[item["status"]] = counts.get(item["status"], 0) + 1
        summary = ", ".join(f"{n} {status}" for status, n in sorted(counts.items()))
        message = f"Detected {len(integrations)} application(s): {summary or 'none'}."
        if report["unsupported"]:
            names = ", ".join(u["name"] for u in report["unsupported"])
            message += f" Installed but unsupported: {names}."
        if report["errors"]:
            message += (
                f" {len(report['errors'])} detection warning(s); see the report "
                f"details. Nothing was launched, installed or changed."
            )
        else:
            message += " Nothing was launched, installed or changed."

        return ToolResult(
            ok=True,
            message=message,
            changed=False,
            data={
                "integrations": integrations,
                "unsupported": report["unsupported"],
                "errors": report["errors"],
            },
        )

    # ------------------------------------------------------------------ #
    # Opening files with a registered application
    # ------------------------------------------------------------------ #

    def open_file(
        self, path: str, application: str, *, dry_run: bool = False
    ) -> ToolResult:
        """Open an existing, permitted file with a registered application."""
        try:
            definition: IntegrationDefinition = self.registry.get(application)
        except RegistryError as exc:
            return ToolResult(ok=False, message="", error=str(exc))

        try:
            resolved = self.checker.resolve(path, operation="open")
        except PathSecurityError as exc:
            return ToolResult(ok=False, message="", error=str(exc))

        if not resolved.exists() or not resolved.is_file():
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{self._pretty(resolved)} does not exist or is not a file. "
                    f"Only existing files can be opened."
                ),
            )

        suffix = resolved.suffix.lower()
        if not definition.supports_file_type(suffix):
            supported = ", ".join(definition.file_types)
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{definition.name} does not support '{suffix or 'no'}' files "
                    f"(supported: {supported})."
                ),
            )

        # Availability pre-check (read-only): a clear failure without
        # ever attempting to install anything.
        executable = self.launcher.resolve_executable(definition)
        if executable is None:
            wanted = " or ".join(
                n
                for n in (
                    definition.launch.executable,
                    definition.launch.fallback_executable,
                )
                if n
            )
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{definition.name} does not appear to be installed "
                    f"(executable '{wanted}' not found on PATH). Alwin Fusion "
                    f"never installs applications automatically."
                ),
            )

        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would open {self._pretty(resolved)} with "
                    f"{definition.name} (dry run, no launch performed)"
                ),
                changed=False,
                data={
                    "path": str(resolved),
                    "integration": definition.id,
                    "application": definition.name,
                    "executable": executable,
                    "dry_run": True,
                },
            )

        try:
            outcome = self.launcher.launch(definition, resolved)
        except LaunchError as exc:
            return ToolResult(ok=False, message="", error=str(exc))

        return ToolResult(
            ok=True,
            message=(
                f"Opened {self._pretty(resolved)} with {definition.name} "
                f"({outcome.detail})"
            ),
            changed=True,
            data={
                "path": str(resolved),
                "integration": outcome.integration_id,
                "application": outcome.application_name,
                "executable": outcome.executable,
                "pid": outcome.pid,
                "returncode": outcome.returncode,
                "started": outcome.started,
                "detail": outcome.detail,
            },
        )


def make_app_handlers(tools: ApplicationTools) -> Dict[str, Callable[..., ToolResult]]:
    """Bind application tools to the ``handler(action, dry_run=...)`` shape."""
    return {
        "detect_applications": lambda action, *, dry_run=False: tools.detect(
            action.get("integration", ""), dry_run=dry_run
        ),
        "open_file": lambda action, *, dry_run=False: tools.open_file(
            action["path"], action.get("application", ""), dry_run=dry_run
        ),
    }
