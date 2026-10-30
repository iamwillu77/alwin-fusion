"""Desktop-automation tool handlers (v0.4).

``AutomationTools`` binds validated automation actions to a concrete
adapter and enforces the rules that must hold *regardless* of the
backend:

* the application must be a reviewed registry id (re-checked here —
  defence in depth, the pipeline checked too),
* read-only operations (inspect / find / read) never mutate anything,
* mutations (activate / enter / save) honour dry-run by reporting
  without acting,
* **save** is confirmed by file digest: the Save control's activation
  alone is never reported as a save, and an unconfirmed write is an
  honest failure ("state unknown"),
* application-provided text never appears in a message unsanitized.

Nothing here launches processes, sends keystrokes, clicks coordinates
or installs software.  All probes default to read-only system state
(``PATH`` lookup + ``/proc``) and every external dependency (``which``,
``/proc`` root, clock, sleep, adapter) is injectable so tests run
entirely on mocks and temporary directories.
"""

from __future__ import annotations

import hashlib
import shutil
import time
from dataclasses import replace
from pathlib import Path
from typing import Callable, Dict, Optional

from ai_core.permissions import PermissionChecker, PathSecurityError, pretty_path
from integrations.automation.atspi_backend import AtspiSession
from integrations.automation.base import (
    ACTION_PERFORMED,
    CAP_ACTIVATE,
    CAP_ENTER,
    CAP_FIND,
    CAP_INSPECT,
    CAP_LIST,
    CAP_READ,
    CAP_SAVE,
    FAILED_UNKNOWN,
    INSTALLED_NOT_RUNNING,
    NOT_INSTALLED,
    PROCESS_STARTED,
    RESULT_VERIFIED,
    ActionResult,
    AppSnapshot,
    AutomationAdapter,
    AutomationError,
    ControlLocator,
    UnavailableAdapter,
    safe_app_text,
)
from integrations.automation.gnome_text_editor import GnomeTextEditorAdapter
from integrations.automation.libreoffice import LibreOfficeAdapter
from integrations.automation import processes
from integrations.apps.registry import (
    BUILTIN_REGISTRY,
    IntegrationDefinition,
    IntegrationRegistry,
    RegistryError,
)
from integrations.filesystem.tools import ToolResult

#: Integration id -> adapter builder.  v0.4 automates LibreOffice
#: Writer and Calc; v0.17 adds GNOME Text Editor (bounded inspection +
#: activation only — its real tree exposes no document text interface).
#: Every other id gets an honest :class:`UnavailableAdapter` (and the
#: permission stage denies it before any of this runs).
ADAPTER_BUILDERS: Dict[str, Callable[..., AutomationAdapter]] = {
    "libreoffice-writer": LibreOfficeAdapter,
    "libreoffice-calc": LibreOfficeAdapter,
    "gnome-text-editor": GnomeTextEditorAdapter,
}


class AutomationTools:
    """State inspection and allowlisted control operations."""

    def __init__(
        self,
        checker: PermissionChecker,
        *,
        registry: IntegrationRegistry = BUILTIN_REGISTRY,
        session=None,
        adapter_factory: Optional[Callable[[str], AutomationAdapter]] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        proc_root: Optional[str] = None,
        comm_names=None,
        sleep: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        save_timeout: float = 2.0,
        save_interval: float = 0.05,
    ) -> None:
        self.checker = checker
        self.registry = registry
        self.session = session if session is not None else AtspiSession()
        self._adapter_factory = adapter_factory
        self._which = which if which is not None else shutil.which
        self._proc_root = proc_root
        self._comm_names = comm_names
        self._sleep = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self.save_timeout = save_timeout
        self.save_interval = save_interval

    def _pretty(self, path) -> str:
        return pretty_path(path, self.checker.home)

    # ------------------------------------------------------------------ #
    # Adapters and read-only probes
    # ------------------------------------------------------------------ #

    def _definition(self, application: str) -> IntegrationDefinition:
        """Re-resolve the reviewed registry id (defence in depth)."""
        return self.registry.get(application)  # raises RegistryError

    def adapter_for(self, application: str) -> AutomationAdapter:
        if self._adapter_factory is not None:
            return self._adapter_factory(application)
        builder = ADAPTER_BUILDERS.get(application)
        if builder is None:
            return UnavailableAdapter(
                application,
                "no desktop-automation adapter is registered for this "
                "application (v0.4 automates LibreOffice Writer and Calc "
                "only)",
                state=FAILED_UNKNOWN,
            )
        return builder(application, session=self.session)

    @staticmethod
    def _probe_executables(definition: IntegrationDefinition) -> tuple:
        names = list(definition.detection.executables)
        for exe in (definition.launch.executable, definition.launch.fallback_executable):
            if exe and exe not in names:
                names.append(exe)
        return tuple(names)

    def _installed(self, definition: IntegrationDefinition) -> bool:
        return any(self._which(exe) for exe in self._probe_executables(definition))

    def _running(self, definition: IntegrationDefinition) -> Optional[bool]:
        kwargs = {}
        if self._proc_root is not None:
            kwargs["proc_root"] = self._proc_root
        if self._comm_names is not None:
            kwargs["comm_names"] = self._comm_names
        return processes.any_running(self._probe_executables(definition), **kwargs)

    def _result_tool(self, result: ActionResult, *, changed: bool) -> ToolResult:
        data = dict(result.data)
        data.setdefault("state", result.state)
        data["capability"] = result.capability
        if result.control is not None:
            data["control"] = result.control.as_dict()
        return ToolResult(
            ok=result.ok,
            message=result.message if result.ok else "",
            changed=changed and result.ok,
            error=result.error or None,
            data=data,
        )

    @staticmethod
    def _adapter_failure(capability: str, exc: Exception) -> ToolResult:
        return ToolResult(
            ok=False,
            message="",
            changed=False,
            error=f"Automation capability '{capability}' failed: {exc}",
            data={"state": FAILED_UNKNOWN, "capability": capability,
                  "unsupported": capability},
        )

    # ------------------------------------------------------------------ #
    # Read-only operations
    # ------------------------------------------------------------------ #

    def inspect(self, application: str, *, dry_run: bool = False) -> ToolResult:
        """Report the honest application state; never launches anything."""
        try:
            definition = self._definition(application)
        except RegistryError as exc:
            return ToolResult(ok=False, message="", error=str(exc))

        def snapshot(state: str, **kwargs) -> AppSnapshot:
            base = dict(
                application=application,
                integration=application,
                state=state,
                capabilities=(),
            )
            base.update(kwargs)
            return AppSnapshot(**base)

        installed = self._definition_installed(definition)
        if not installed:
            snap = snapshot(
                NOT_INSTALLED,
                installed=False,
                process_running=False,
                a11y_available=False,
                detail=(
                    "executable not found on PATH; Alwin Fusion never "
                    "installs applications automatically"
                ),
            )
            return self._inspect_tool(snap)

        running = self._running(definition)
        if running is None:
            snap = snapshot(
                FAILED_UNKNOWN,
                installed=True,
                process_running=None,
                a11y_available=False,
                detail=(
                    "the process state could not be verified (the process "
                    "directory is unreadable), so no running/not-running "
                    "claim is made"
                ),
            )
            return self._inspect_tool(snap)
        if not running:
            snap = snapshot(
                INSTALLED_NOT_RUNNING,
                installed=True,
                process_running=False,
                a11y_available=False,
                detail="installed; no running process observed",
            )
            return self._inspect_tool(snap)

        # A process is running: only the accessible interface can say
        # more — and its absence is reported as such, never upgraded.
        adapter = self.adapter_for(application)
        if not adapter.supports(CAP_INSPECT):
            reason = "this application has no automation adapter"
            if isinstance(adapter, UnavailableAdapter):
                reason = adapter.reason
            snap = snapshot(
                PROCESS_STARTED,
                installed=True,
                process_running=True,
                interface_detected=False,
                a11y_available=False,
                detail=(
                    f"process started, but the accessible interface was not "
                    f"detected: {reason}"
                ),
            )
            return self._inspect_tool(snap)
        try:
            snap = adapter.inspect()
        except AutomationError as exc:
            snap = snapshot(
                PROCESS_STARTED,
                installed=True,
                process_running=True,
                interface_detected=False,
                a11y_available=False,
                detail=(
                    "process started, but the accessible interface was not "
                    f"detected ({exc})"
                ),
            )
            return self._inspect_tool(snap)
        # The probe above proved installation and a running process;
        # fill anything the adapter left unknown.
        updates = {}
        if snap.installed is None:
            updates["installed"] = True
        if snap.process_running is None:
            updates["process_running"] = True
        if updates:
            snap = replace(snap, **updates)
        return self._inspect_tool(snap)

    def _definition_installed(self, definition: IntegrationDefinition) -> bool:
        return self._installed(definition)

    def _inspect_tool(self, snap: AppSnapshot) -> ToolResult:
        data = snap.as_dict()
        lines = [f"{snap.integration}: state '{snap.state}'."]
        if snap.detail:
            lines.append(f"  {data['detail']}")
        if snap.document:
            lines.append(
                f"  document (application-supplied title): {snap.document}"
            )
        if snap.capabilities:
            lines.append(f"  capabilities: {', '.join(snap.capabilities)}")
        return ToolResult(
            ok=True,
            message=" ".join(lines),
            changed=False,
            data={"snapshot": data, "state": snap.state},
        )

    def find_control(
        self, application: str, control: dict, *, dry_run: bool = False
    ) -> ToolResult:
        return self._operate(
            CAP_FIND, application, control, "find_control", dry_run=dry_run
        )

    def list_controls(
        self, application: str, control: Optional[dict] = None,
        *, dry_run: bool = False
    ) -> ToolResult:
        """v0.17 read-only bounded listing of an app's controls.

        *control* is optional: when given, the listing is rooted at
        that (ambiguity-checked) control; otherwise it covers the
        application window.  Never mutates, never reads document text.
        """
        try:
            self._definition(application)
        except RegistryError as exc:
            return ToolResult(ok=False, message="", error=str(exc))
        adapter = self.adapter_for(application)
        if not adapter.supports(CAP_LIST):
            reason = (
                adapter.reason
                if isinstance(adapter, UnavailableAdapter)
                else "the application does not expose this capability"
            )
            return ToolResult(
                ok=False,
                message="",
                error=f"Capability '{CAP_LIST}' is unavailable: {reason}.",
                data={"state": FAILED_UNKNOWN, "capability": CAP_LIST,
                      "unsupported": CAP_LIST},
            )
        locator = None
        if control:
            locator = ControlLocator(
                role=str(control.get("role", "")),
                name=str(control.get("name", "")),
            )
        try:
            result = adapter.list_controls(locator)
        except AutomationError as exc:
            return self._adapter_failure(CAP_LIST, exc)
        return self._result_tool(result, changed=False)

    def read_control(
        self, application: str, control: dict, *, dry_run: bool = False
    ) -> ToolResult:
        return self._operate(
            CAP_READ, application, control, "read_control", dry_run=dry_run
        )

    def _operate(
        self,
        capability: str,
        application: str,
        control: dict,
        operation: str,
        *,
        dry_run: bool,
    ) -> ToolResult:
        try:
            self._definition(application)
        except RegistryError as exc:
            return ToolResult(ok=False, message="", error=str(exc))
        adapter = self.adapter_for(application)
        if not adapter.supports(capability):
            reason = (
                adapter.reason
                if isinstance(adapter, UnavailableAdapter)
                else "the application does not expose this capability"
            )
            return ToolResult(
                ok=False,
                message="",
                error=f"Capability '{capability}' is unavailable: {reason}.",
                data={"state": FAILED_UNKNOWN, "capability": capability,
                      "unsupported": capability},
            )
        locator = ControlLocator(
            role=str(control.get("role", "")), name=str(control.get("name", ""))
        )
        method = getattr(adapter, operation)
        try:
            result = method(locator)
        except AutomationError as exc:
            return self._adapter_failure(capability, exc)
        return self._result_tool(result, changed=False)

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #

    @staticmethod
    def _control_label(control: dict) -> str:
        label = f"role '{control.get('role', '?')}'"
        name = control.get("name") or ""
        if name:
            label += f" named '{safe_app_text(name, 60)}'"
        return label

    def activate_control(
        self, application: str, control: dict, *, dry_run: bool = False
    ) -> ToolResult:
        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would activate {self._control_label(control)} in "
                    f"{application} (dry run, no action performed)."
                ),
                changed=False,
                data={"dry_run": True, "state": ACTION_PERFORMED},
            )
        return self._operate(
            CAP_ACTIVATE, application, control, "activate_control", dry_run=False
        )

    def enter_text(
        self, application: str, control: dict, content: str, *, dry_run: bool = False
    ) -> ToolResult:
        if dry_run:
            # The text itself is never echoed into a message or log.
            return ToolResult(
                ok=True,
                message=(
                    f"Would enter {len(content)} characters of text into "
                    f"{self._control_label(control)} in {application} "
                    f"(dry run, nothing was entered)."
                ),
                changed=False,
                data={"dry_run": True, "chars": len(content)},
            )
        try:
            self._definition(application)
        except RegistryError as exc:
            return ToolResult(ok=False, message="", error=str(exc))
        adapter = self.adapter_for(application)
        if not adapter.supports(CAP_ENTER):
            reason = (
                adapter.reason
                if isinstance(adapter, UnavailableAdapter)
                else "the application does not expose this capability"
            )
            return ToolResult(
                ok=False,
                message="",
                error=f"Capability '{CAP_ENTER}' is unavailable: {reason}.",
                data={"state": FAILED_UNKNOWN, "capability": CAP_ENTER,
                      "unsupported": CAP_ENTER},
            )
        locator = ControlLocator(
            role=str(control.get("role", "")), name=str(control.get("name", ""))
        )
        try:
            result = adapter.enter_text(locator, content)
        except AutomationError as exc:
            return self._adapter_failure(CAP_ENTER, exc)
        return self._result_tool(result, changed=True)

    # ------------------------------------------------------------------ #
    # Controlled save (digest-confirmed)
    # ------------------------------------------------------------------ #

    @staticmethod
    def _digest(path: Path) -> Optional[str]:
        try:
            return hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            return None

    def save_document(
        self, application: str, path: str, *, dry_run: bool = False
    ) -> ToolResult:
        try:
            self._definition(application)
        except RegistryError as exc:
            return ToolResult(ok=False, message="", error=str(exc))
        try:
            resolved = self.checker.resolve(path, operation="save")
        except PathSecurityError as exc:
            return ToolResult(ok=False, message="", error=str(exc))

        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would activate the reviewed Save control in "
                    f"{application} and confirm a digest change of "
                    f"{self._pretty(resolved)} (dry run, nothing performed)."
                ),
                changed=False,
                data={"dry_run": True, "path": str(resolved)},
            )

        if not resolved.exists() or not resolved.is_file():
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{self._pretty(resolved)} does not exist. ALWIN only "
                    f"saves over an existing, permission-checked file that "
                    f"the application already opened."
                ),
            )

        before = self._digest(resolved)
        if before is None:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{self._pretty(resolved)} could not be read before the "
                    f"save; state unknown."
                ),
            )

        adapter = self.adapter_for(application)
        if not adapter.supports(CAP_SAVE):
            reason = (
                adapter.reason
                if isinstance(adapter, UnavailableAdapter)
                else "the application does not expose this capability"
            )
            return ToolResult(
                ok=False,
                message="",
                error=f"Capability '{CAP_SAVE}' is unavailable: {reason}.",
                data={"state": FAILED_UNKNOWN, "capability": CAP_SAVE,
                      "unsupported": CAP_SAVE, "before_digest": before},
            )
        try:
            result = adapter.save_document()
        except AutomationError as exc:
            return self._adapter_failure(CAP_SAVE, exc)
        if not result.ok:
            tool = self._result_tool(result, changed=False)
            data = dict(tool.data)
            data["before_digest"] = before
            return ToolResult(
                ok=False, message="", error=tool.error, changed=False, data=data
            )

        # The control was activated — now, and only now, look at the
        # file.  A digest change is the *only* evidence of a save.
        deadline = self._monotonic() + self.save_timeout
        after = before
        file_changed = False
        file_missing = False
        while True:
            after = self._digest(resolved)
            if after is None:
                file_missing = True
                break
            if after != before:
                file_changed = True
                break
            if self._monotonic() >= deadline:
                break
            self._sleep(self.save_interval)

        data = dict(result.data)
        data.update(
            {
                "path": str(resolved),
                "before_digest": before,
                "after_digest": after,
                "file_changed": file_changed,
            }
        )
        if file_missing:
            data["state"] = FAILED_UNKNOWN
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{self._pretty(resolved)} disappeared while the Save "
                    f"control was active; state unknown."
                ),
                changed=False,
                data=data,
            )
        if not file_changed:
            data["state"] = FAILED_UNKNOWN
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"The Save control was activated, but {self._pretty(resolved)} "
                    f"did not change on disk within {self.save_timeout:g}s. "
                    f"A save cannot be confirmed — state unknown (the active "
                    f"document may be a different file or unmodified)."
                ),
                changed=False,
                data=data,
            )
        data["state"] = RESULT_VERIFIED
        return ToolResult(
            ok=True,
            message=(
                f"Save control activated and {self._pretty(resolved)} changed "
                f"on disk (sha256 differs from before); the verification stage "
                f"re-reads the file independently."
            ),
            changed=True,
            data=data,
        )


def make_automation_handlers(
    tools: AutomationTools,
) -> Dict[str, Callable[..., ToolResult]]:
    """Bind automation tools to the ``handler(action, dry_run=...)`` shape."""
    return {
        "inspect_application": lambda action, *, dry_run=False: tools.inspect(
            action["application"], dry_run=dry_run
        ),
        "find_control": lambda action, *, dry_run=False: tools.find_control(
            action["application"], action["control"], dry_run=dry_run
        ),
        "list_controls": lambda action, *, dry_run=False: tools.list_controls(
            action["application"], action.get("control"),
            dry_run=dry_run,
        ),
        "read_control": lambda action, *, dry_run=False: tools.read_control(
            action["application"], action["control"], dry_run=dry_run
        ),
        "activate_control": lambda action, *, dry_run=False: tools.activate_control(
            action["application"], action["control"], dry_run=dry_run
        ),
        "enter_text": lambda action, *, dry_run=False: tools.enter_text(
            action["application"], action["control"], action["content"],
            dry_run=dry_run,
        ),
        "save_document": lambda action, *, dry_run=False: tools.save_document(
            action["application"], action["path"], dry_run=dry_run
        ),
    }
