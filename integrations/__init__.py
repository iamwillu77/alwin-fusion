"""Integration registry for Alwin Fusion.

This package owns the :class:`ActionDispatcher`, the single place
where validated actions are bound to executable handlers.

Adding a future integration (LibreOffice, PDF, application discovery,
desktop automation, MCP servers, ...) means:

1. create ``integrations/<name>/`` with its own tools module,
2. write a ``build_dispatcher``-style factory that registers handlers
   for new action names, and
3. extend ``ai_core.planner.ACTION_SCHEMA`` with the new action's
   schema and, if it mutates state, its approval requirement.

Handlers receive a *validated* action dict plus ``dry_run`` and must
return a ``ToolResult``.  Nothing here runs shell commands; the
dispatcher only routes to trusted Python callables.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Dict

from integrations.apps.desktop import DesktopController, make_desktop_handlers
from integrations.apps.tools import ApplicationTools, make_app_handlers
from integrations.automation.sequence import TaskSequenceRunner
from integrations.automation.tools import AutomationTools, make_automation_handlers
from integrations.documents.tools import DocumentTools, make_doc_handlers
from integrations.filesystem.tools import FileSystemTools, ToolResult, make_fs_handlers


class DispatchError(Exception):
    """Raised when an action has no registered handler."""


@dataclass(frozen=True)
class HandlerSpec:
    """Metadata for one registered action handler."""

    name: str
    description: str
    handler: Callable[..., ToolResult]
    mutates: bool
    requires_approval: bool


class ActionDispatcher:
    """Routes validated actions to trusted Python handlers."""

    def __init__(self) -> None:
        self._specs: Dict[str, HandlerSpec] = {}

    def register(
        self,
        action_name: str,
        handler: Callable[..., ToolResult],
        *,
        description: str,
        mutates: bool,
        requires_approval: bool,
    ) -> None:
        if not action_name.isidentifier():
            raise ValueError(f"Invalid action name: {action_name!r}")
        if action_name in self._specs:
            raise ValueError(f"Action '{action_name}' is already registered.")
        if requires_approval and not mutates:
            raise ValueError("Only mutating actions may require approval.")
        self._specs[action_name] = HandlerSpec(
            name=action_name,
            description=description,
            handler=handler,
            mutates=mutates,
            requires_approval=requires_approval,
        )

    def spec(self, action_name: str) -> HandlerSpec:
        try:
            return self._specs[action_name]
        except KeyError:
            raise DispatchError(
                f"No handler registered for action '{action_name}'."
            ) from None

    @property
    def names(self) -> tuple:
        return tuple(sorted(self._specs))

    def dispatch(self, action: dict, *, dry_run: bool = False) -> ToolResult:
        spec = self.spec(action.get("action", ""))
        return spec.handler(action, dry_run=dry_run)


def build_default_dispatcher(
    checker,
    *,
    app_tools: "ApplicationTools | None" = None,
    automation_tools: "AutomationTools | None" = None,
    desktop_tools: "DesktopController | None" = None,
    step_observer: "Callable[..., None] | None" = None,
) -> ActionDispatcher:
    """Dispatcher with the filesystem, document, application and
    desktop-automation integrations.

    ``app_tools`` and ``automation_tools`` may be supplied to inject
    test doubles (fake launcher, fake accessibility session, fake
    adapters); the defaults build the real read-only probes and
    argv-only launchers.

    ``step_observer`` (optional) receives
    ``(index, total, action_name, status, error=None)`` at each
    ``task_sequence`` step boundary — observation only, used by the
    task experience (v0.5) for live progress.  It can neither
    influence, skip nor approve any step.
    """
    tools = FileSystemTools(checker)
    handlers = make_fs_handlers(tools)
    doc_tools = DocumentTools(checker)
    doc_handlers = make_doc_handlers(doc_tools)
    apps = app_tools or ApplicationTools(checker)
    app_handlers = make_app_handlers(apps)
    automation = automation_tools or AutomationTools(checker)
    automation_handlers = make_automation_handlers(automation)

    dispatcher = ActionDispatcher()
    dispatcher.register(
        "list_directory",
        handlers["list_directory"],
        description="List entries of a directory inside the permitted roots.",
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "read_file",
        handlers["read_file"],
        description="Read a UTF-8 text file inside the permitted roots.",
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "create_directory",
        handlers["create_directory"],
        description="Create a new directory (never overwrites).",
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "create_text_file",
        handlers["create_text_file"],
        description="Create a new UTF-8 text file (never overwrites).",
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "create_report",
        doc_handlers["create_report"],
        description="Create a formatted plain-text report (never overwrites).",
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "create_pdf",
        doc_handlers["create_pdf"],
        description="Create a PDF document with reportlab (never overwrites).",
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "create_spreadsheet",
        doc_handlers["create_spreadsheet"],
        description="Create an .xlsx workbook with openpyxl (never overwrites).",
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "detect_applications",
        app_handlers["detect_applications"],
        description=(
            "Detect registered desktop applications (read-only: no launch, "
            "no install, no change)."
        ),
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "open_file",
        app_handlers["open_file"],
        description=(
            "Open an existing, permission-checked file with a registered "
            "application (argv-only launch; approval required)."
        ),
        mutates=True,
        requires_approval=True,
    )

    # ---- v0.13 desktop intelligence & application control ------------ #
    desktop = desktop_tools or DesktopController(checker)
    desktop_handlers = make_desktop_handlers(desktop)
    dispatcher.register(
        "launch_application",
        desktop_handlers["launch_application"],
        description=(
            "Launch a registered application without a file (argv-only, "
            "verified by re-observing /proc; approval required)."
        ),
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "application_status",
        desktop_handlers["application_status"],
        description=(
            "Report a registered application's installed/running/window "
            "state (read-only; /proc + accessibility metadata)."
        ),
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "active_window",
        desktop_handlers["active_window"],
        description=(
            "Report the currently active window via the accessibility bus "
            "(read-only metadata; honest 'unavailable' otherwise)."
        ),
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "focus_application",
        desktop_handlers["focus_application"],
        description=(
            "Request focus for a registered application's window through "
            "the accessibility Component interface, then re-read the "
            "active state (approval required)."
        ),
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "close_application",
        desktop_handlers["close_application"],
        description=(
            "Close a registered application's window via its own "
            "window.close accessibility action and verify it disappeared "
            "— never a signal, never a force-kill (approval required)."
        ),
        mutates=True,
        requires_approval=True,
    )

    # ---- v0.4 desktop automation ------------------------------------ #
    dispatcher.register(
        "inspect_application",
        automation_handlers["inspect_application"],
        description=(
            "Report the honest state of a supported application "
            "(read-only: no launch, no install, no change)."
        ),
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "find_control",
        automation_handlers["find_control"],
        description=(
            "Find a control by accessible role and name in a supported "
            "application (read-only)."
        ),
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "read_control",
        automation_handlers["read_control"],
        description=(
            "Read a permitted control's accessible text (read-only, "
            "bounded, untrusted display data)."
        ),
        mutates=False,
        requires_approval=False,
    )
    dispatcher.register(
        "activate_control",
        automation_handlers["activate_control"],
        description=(
            "Activate an identified control through its accessibility "
            "action — revalidated first; no keystrokes, no coordinates "
            "(approval required)."
        ),
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "enter_text",
        automation_handlers["enter_text"],
        description=(
            "Enter approved text into an identified input field "
            "(role 'entry' only; read back to confirm; approval required)."
        ),
        mutates=True,
        requires_approval=True,
    )
    dispatcher.register(
        "save_document",
        automation_handlers["save_document"],
        description=(
            "Activate the reviewed Save control and confirm the write by "
            "file digest — an unchanged file is an honest failure "
            "(approval required)."
        ),
        mutates=True,
        requires_approval=True,
    )
    # The sequence runner closes over the dispatcher it dispatches
    # through (built above), re-checking permissions per step.
    sequence_runner = TaskSequenceRunner(
        checker, dispatcher=dispatcher, step_observer=step_observer
    )
    dispatcher.register(
        "task_sequence",
        lambda action, *, dry_run=False: sequence_runner.run(
            action, dry_run=dry_run
        ),
        description=(
            "Run validated steps in order with stop-on-failure semantics "
            "(every step re-checked; approval required for the sequence)."
        ),
        mutates=True,
        requires_approval=True,
    )
    return dispatcher


__all__ = [
    "ActionDispatcher",
    "DesktopController",
    "DispatchError",
    "HandlerSpec",
    "build_default_dispatcher",
]
