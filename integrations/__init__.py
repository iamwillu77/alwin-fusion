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

from integrations.apps.tools import ApplicationTools, make_app_handlers
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
) -> ActionDispatcher:
    """Dispatcher with the filesystem, document and application integrations.

    ``app_tools`` may be supplied to inject a test double (fake
    launcher, fake ``which``, fake desktop directories); the default
    builds the real read-only/argv-only application tools.
    """
    tools = FileSystemTools(checker)
    handlers = make_fs_handlers(tools)
    doc_tools = DocumentTools(checker)
    doc_handlers = make_doc_handlers(doc_tools)
    apps = app_tools or ApplicationTools(checker)
    app_handlers = make_app_handlers(apps)

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
    return dispatcher


__all__ = [
    "ActionDispatcher",
    "DispatchError",
    "HandlerSpec",
    "build_default_dispatcher",
]
