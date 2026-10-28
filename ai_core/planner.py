"""Structured planning: intents and model proposals become validated actions.

The planner is the trust boundary between "what was asked for" and
"what may be executed".  Both the deterministic parser and the local
LLM go through :func:`validate_action`, which enforces an explicit
action allowlist and an explicit key schema per action.

Model output is loaded with ``json.loads`` only.  It is never
``eval``/``exec``-ed and never turned into a shell command.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Callable, Optional

from ai_core import command_parser, specs
from ai_core.command_parser import ParsedIntent
from ai_core.permissions import pretty_path
from ai_core.specs import SpecError
from integrations.apps.registry import (
    BUILTIN_REGISTRY,
    RegistryError,
    resolve_integration_id,
)

# --------------------------------------------------------------------------- #
# Action allowlist and per-action schema
# --------------------------------------------------------------------------- #

#: action name -> allowed top-level keys (besides nothing else is accepted)
ACTION_SCHEMA: dict[str, frozenset] = {
    "create_directory": frozenset({"action", "path"}),
    "list_directory": frozenset({"action", "path"}),
    "read_file": frozenset({"action", "path"}),
    "create_text_file": frozenset({"action", "path", "content"}),
    "create_report": frozenset({"action", "path", "content"}),
    "create_pdf": frozenset({"action", "path", "content"}),
    "create_spreadsheet": frozenset({"action", "path", "spec"}),
    "detect_applications": frozenset({"action", "integration"}),
    "open_file": frozenset({"action", "path", "application"}),
    # ---- v0.4 desktop automation (closed set; see AUTOMATION_ACTIONS) ----
    "inspect_application": frozenset({"action", "application"}),
    "find_control": frozenset({"action", "application", "control"}),
    "read_control": frozenset({"action", "application", "control"}),
    "activate_control": frozenset({"action", "application", "control"}),
    "enter_text": frozenset({"action", "application", "control", "content"}),
    "save_document": frozenset({"action", "application", "path"}),
    "task_sequence": frozenset({"action", "steps"}),
    "explain": frozenset({"action", "target"}),
}

PATH_ACTIONS = (
    "create_directory",
    "list_directory",
    "read_file",
    "create_text_file",
    "create_report",
    "create_pdf",
    "create_spreadsheet",
    "open_file",
)

#: Actions that change the filesystem or the world outside the sandbox
#: and therefore need user approval.  ``open_file`` launches an
#: external application process — read-only detection
#: (``detect_applications``) is deliberately *not* in this set.
#:
#: v0.4: the three state-changing automation mutations (activate,
#: enter text, save) join the set because they act on a running
#: application; ``task_sequence`` joins because it *runs* other actions
#: (each sequence is approved as a whole, with every step enumerated in
#: the proposal).  The read-only automation actions (inspect, find,
#: read) stay out, exactly like detection.
MUTATING_ACTIONS = frozenset(
    {
        "create_directory",
        "create_text_file",
        "create_report",
        "create_pdf",
        "create_spreadsheet",
        "open_file",
        "activate_control",
        "enter_text",
        "save_document",
        "task_sequence",
    }
)

#: The v0.4 automation actions (three read-only, three mutating; the
#: seventh action, task_sequence, composes them).  Anything outside
#: this set is rejected by :func:`validate_action` before permissions
#: ever run.
AUTOMATION_ACTIONS = frozenset(
    {
        "inspect_application",
        "find_control",
        "read_control",
        "activate_control",
        "enter_text",
        "save_document",
    }
)

#: Canonical accessible roles accepted in ``control.role``.  The values
#: are AT-SPI role names ('toggle button', 'page tab', ...);
#: ``integrations.automation`` keeps the per-operation role policy sets
#: (readable / activatable / enterable) and tests pin both together.
KNOWN_ROLES = frozenset(
    {
        "application",
        "button",
        "check box",
        "combo box",
        "document frame",
        "entry",
        "frame",
        "heading",
        "label",
        "link",
        "list",
        "list item",
        "menu bar",
        "menu item",
        "page tab",
        "paragraph",
        "radio button",
        "status bar",
        "table",
        "table cell",
        "text",
        "toggle button",
        "tool bar",
        "window",
    }
)

#: Only reviewed file types may be the target of a GUI save (the
#: extension check happens at plan time, containment at permission time).
SAVEABLE_FILE_EXTENSIONS = frozenset({".txt", ".xlsx"})

#: Payload bounds for automation actions (kept in sync with
#: ``integrations.automation.base``; tests pin the pair).
MAX_CONTROL_NAME_CHARS = 120
MAX_ENTERED_TEXT_CHARS = 4096
MAX_SEQUENCE_STEPS = 8

#: Generated-document actions and the only file extension each produces.
DOCUMENT_EXTENSIONS = {
    "create_report": ".txt",
    "create_pdf": ".pdf",
    "create_spreadsheet": ".xlsx",
}

#: The only file types the v0.3 open_file action accepts (each
#: integration narrows this further with its own file_types).
OPENABLE_FILE_EXTENSIONS = frozenset({".txt", ".pdf", ".xlsx"})

MAX_PATH_LENGTH = 1024
MAX_CONTENT_CHARS = 64 * 1024


class PlanValidationError(Exception):
    """Raised when a proposed action is not valid or not allowed."""


class ContentSourceError(Exception):
    """The structured content source ('using file.json') could not be used.

    Raised by the pipeline's content loader for a file that exists
    inside the permitted roots but cannot be read, decoded or parsed.
    """


def _validate_path(value: object) -> str:
    if not isinstance(value, str):
        raise PlanValidationError(
            f"'path' must be a string, got {type(value).__name__}."
        )
    if not value.strip():
        raise PlanValidationError("'path' must not be empty.")
    if "\x00" in value:
        raise PlanValidationError("'path' contains a null byte.")
    if len(value) > MAX_PATH_LENGTH:
        raise PlanValidationError(f"'path' is too long (limit {MAX_PATH_LENGTH}).")
    if ".." in Path(value).parts:
        raise PlanValidationError("'path' must not contain '..'.")
    return value.strip()


def _resolve_application(value: object) -> str:
    """Resolve an automation target against the trusted registry.

    Automation names an application only by a reviewed registry id —
    never by executable, path or command-line flag.  Unknown tokens are
    rejected here, before permissions and before any adapter runs.
    """
    try:
        application = resolve_integration_id(value, BUILTIN_REGISTRY)
    except RegistryError as exc:
        raise PlanValidationError(str(exc)) from exc
    if not application:
        raise PlanValidationError(
            "Automation needs an explicit 'application' id, e.g. "
            "'LibreOffice Writer' or 'libreoffice-calc'."
        )
    return application


def _validate_control(payload: object, *, action: str) -> dict:
    """Validate a control locator: canonical role + optional name."""
    if not isinstance(payload, dict):
        raise PlanValidationError(
            f"'control' must be an object, got {type(payload).__name__}."
        )
    extra = set(payload) - {"role", "name"}
    if extra:
        raise PlanValidationError(
            f"'control' has unexpected fields: {', '.join(sorted(extra))}."
        )
    role = payload.get("role")
    if not isinstance(role, str) or not role.strip():
        raise PlanValidationError("'control.role' is required, e.g. 'button'.")
    role_key = " ".join(role.replace("_", " ").lower().split())
    if role_key not in KNOWN_ROLES:
        raise PlanValidationError(
            f"'{role_key}' is not a known accessible role. Known roles: "
            f"{', '.join(sorted(KNOWN_ROLES))}."
        )
    name = payload.get("name", "")
    if name is None:
        name = ""
    if not isinstance(name, str):
        raise PlanValidationError(
            f"'control.name' must be a string, got {type(name).__name__}."
        )
    name = name.strip()
    if "\x00" in name:
        raise PlanValidationError("'control.name' contains a null byte.")
    if len(name) > MAX_CONTROL_NAME_CHARS:
        raise PlanValidationError(
            f"'control.name' is too long (limit {MAX_CONTROL_NAME_CHARS})."
        )
    if action in ("read_control", "activate_control", "enter_text") and not name:
        raise PlanValidationError(
            f"'{action}' needs 'control.name' identifying the exact control, "
            f"e.g. {{'role': 'button', 'name': 'Save'}} (only find_control "
            f"may search by role alone)."
        )
    return {"role": role_key, "name": name}


def validate_action(payload: object) -> dict:
    """Validate a proposed action against the allowlist and schema.

    Accepts dicts (already-parsed JSON or planner output) and returns a
    normalized copy.  Raises :class:`PlanValidationError` otherwise.
    """
    if not isinstance(payload, dict):
        raise PlanValidationError(
            f"An action must be a JSON object, got {type(payload).__name__}."
        )

    action = payload.get("action")
    if not isinstance(action, str) or not action:
        raise PlanValidationError("An action needs a string 'action' field.")
    if action not in ACTION_SCHEMA:
        raise PlanValidationError(
            f"Action '{action}' is not allowed. Allowed actions: "
            f"{', '.join(sorted(ACTION_SCHEMA))}."
        )

    allowed_keys = ACTION_SCHEMA[action]
    extra = set(payload) - allowed_keys
    if extra:
        raise PlanValidationError(
            f"Action '{action}' has unexpected fields: {', '.join(sorted(extra))}."
        )

    if action == "explain":
        target = payload.get("target")
        if not isinstance(target, dict):
            raise PlanValidationError("'explain' needs a nested 'target' action object.")
        validated_target = validate_action(target)
        if validated_target["action"] == "explain":
            raise PlanValidationError("Nested explanations are not allowed.")
        return {"action": "explain", "target": validated_target}

    if action == "detect_applications":
        # No path: detection is read-only and never touches files.
        try:
            integration = resolve_integration_id(
                payload.get("integration", ""), BUILTIN_REGISTRY
            )
        except RegistryError as exc:
            raise PlanValidationError(str(exc)) from exc
        return {"action": action, "integration": integration}

    # ---- v0.4 desktop automation ------------------------------------- #

    if action == "task_sequence":
        steps = payload.get("steps")
        if not isinstance(steps, list):
            raise PlanValidationError(
                f"'task_sequence' needs a 'steps' list, got "
                f"{type(steps).__name__}."
            )
        if not steps:
            raise PlanValidationError("'task_sequence' needs at least one step.")
        if len(steps) > MAX_SEQUENCE_STEPS:
            raise PlanValidationError(
                f"'task_sequence' accepts at most {MAX_SEQUENCE_STEPS} steps "
                f"(got {len(steps)})."
            )
        validated_steps = []
        for index, step in enumerate(steps, start=1):
            try:
                checked = validate_action(step)
            except PlanValidationError as exc:
                raise PlanValidationError(
                    f"Step {index} of the sequence is invalid: {exc}"
                ) from exc
            if checked["action"] in ("task_sequence", "explain"):
                raise PlanValidationError(
                    f"Step {index} of the sequence cannot be a nested "
                    f"'{checked['action']}'."
                )
            validated_steps.append(checked)
        return {"action": "task_sequence", "steps": validated_steps}

    if action == "inspect_application":
        return {
            "action": action,
            "application": _resolve_application(payload.get("application", "")),
        }

    if action in (
        "find_control",
        "read_control",
        "activate_control",
        "enter_text",
    ):
        application = _resolve_application(payload.get("application", ""))
        control = _validate_control(payload.get("control"), action=action)
        if action == "enter_text":
            text = payload.get("content")
            if not isinstance(text, str):
                raise PlanValidationError(
                    f"'enter_text' needs string 'content', got "
                    f"{type(text).__name__}."
                )
            if not text:
                raise PlanValidationError("'enter_text' needs non-empty 'content'.")
            if "\x00" in text:
                raise PlanValidationError("'enter_text' content contains a null byte.")
            if len(text) > MAX_ENTERED_TEXT_CHARS:
                raise PlanValidationError(
                    f"Entered text is too long (limit "
                    f"{MAX_ENTERED_TEXT_CHARS} characters)."
                )
            return {
                "action": action,
                "application": application,
                "control": control,
                "content": text,
            }
        return {"action": action, "application": application, "control": control}

    if action == "save_document":
        application = _resolve_application(payload.get("application", ""))
        path = _validate_path(payload.get("path"))
        suffix = Path(path).suffix.lower()
        if suffix not in SAVEABLE_FILE_EXTENSIONS:
            supported = ", ".join(sorted(SAVEABLE_FILE_EXTENSIONS))
            raise PlanValidationError(
                f"'save_document' supports only {supported} files in this "
                f"milestone (got '{suffix or 'no extension'}')."
            )
        return {"action": action, "application": application, "path": path}

    path = _validate_path(payload.get("path"))

    if action == "create_text_file":
        content = payload.get("content")
        if not isinstance(content, str):
            raise PlanValidationError(
                f"'create_text_file' needs string 'content', got {type(content).__name__}."
            )
        if len(content) > MAX_CONTENT_CHARS:
            raise PlanValidationError(
                f"Content is too long (limit {MAX_CONTENT_CHARS} characters)."
            )
        return {"action": action, "path": path, "content": content}

    if action in DOCUMENT_EXTENSIONS:
        # Extension and file-name rules for generated documents.
        try:
            path = specs.ensure_extension(path, DOCUMENT_EXTENSIONS[action])
            specs.validate_output_filename(path)
        except SpecError as exc:
            raise PlanValidationError(str(exc)) from exc

        if action == "create_spreadsheet":
            try:
                spec = specs.validate_spreadsheet_spec(payload.get("spec"))
            except SpecError as exc:
                raise PlanValidationError(str(exc)) from exc
            return {"action": action, "path": path, "spec": spec}

        try:
            content = specs.validate_report_content(payload.get("content"))
        except SpecError as exc:
            raise PlanValidationError(str(exc)) from exc
        return {"action": action, "path": path, "content": content}

    if action == "open_file":
        # 'application' is an *integration identifier* resolved against
        # the trusted registry — never an executable, a path or a flag.
        # The registry is the closed allowlist of launch mechanisms; an
        # unknown token is rejected here, before anything is executed.
        try:
            application = resolve_integration_id(
                payload.get("application"), BUILTIN_REGISTRY
            )
        except RegistryError as exc:
            raise PlanValidationError(str(exc)) from exc
        if not application:
            raise PlanValidationError(
                "'open_file' needs an 'application' integration id, e.g. "
                "'libreoffice-calc' or 'default'."
            )
        suffix = Path(path).suffix.lower()
        if suffix not in OPENABLE_FILE_EXTENSIONS:
            supported = ", ".join(sorted(OPENABLE_FILE_EXTENSIONS))
            raise PlanValidationError(
                f"'open_file' supports only {supported} files in this "
                f"milestone (got '{suffix or 'no extension'}')."
            )
        return {"action": action, "path": path, "application": application}

    return {"action": action, "path": path}


# --------------------------------------------------------------------------- #
# Deterministic planning
# --------------------------------------------------------------------------- #


def _join_path(location: str, name: str) -> str:
    """Combine a location phrase and a name into a plan path.

    The result is relative to the user's home directory (or absolute if
    the user typed an absolute location or name).
    """
    location = (location or "").strip()
    name = (name or "").strip()
    if name.startswith(("/", "~")):
        # An absolute or home-relative name wins over the location phrase.
        return name
    if not location:
        return name or "."
    if location.startswith("/"):
        return f"{location.rstrip('/')}/{name}"
    location = location.strip("/")
    return f"{location.rstrip('/')}/{name}"


def _source_stem(source: str) -> str:
    """File-name stem of a content source path ('a/b/spec.json' -> 'spec')."""
    base = source.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1]
    if "." in base:
        base = base.rsplit(".", 1)[0]
    return base


def _resolve_document_content(
    slots: dict,
    *,
    action: str,
    source: str,
    content_loader: Optional[Callable[[str], dict]],
) -> dict:
    """Resolve structured content for a document action from its slots.

    Sources, in order of trust: a JSON file read through the
    permission-checked ``content_loader``, inline JSON already parsed by
    the deterministic parser, or simple ``titled``/``containing`` text
    (reports and PDFs only) turned into a minimal content structure.
    """
    inline = slots.get("inline")

    if source:
        if content_loader is None:
            raise ContentSourceError(
                "Reading a content file is not available in this context."
            )
        loaded = content_loader(source)
        if not isinstance(loaded, dict):
            raise ContentSourceError(
                "The content file must contain a single JSON object."
            )
        return loaded

    if inline is not None:
        if not isinstance(inline, dict):
            raise PlanValidationError(
                "Inline JSON must be a JSON object."
            )
        return inline

    if action == "create_spreadsheet":
        # The parser already rejects this; keep the guard as defence in depth.
        raise PlanValidationError(
            "A spreadsheet needs a JSON specification ('using file.json' or "
            "inline 'from {...}')."
        )

    title = (slots.get("title") or "").strip()
    text = (slots.get("text") or "").strip()
    if not title and not text:
        raise PlanValidationError(
            "Report content is empty: add 'titled ...', 'containing ...' or "
            "a 'using <file.json>' source."
        )
    blocks = []
    if text:
        blocks.append({"type": "paragraph", "text": text})
    return {"title": title or None, "blocks": blocks}


_INTENT_TO_ACTION = {
    command_parser.CREATE_REPORT: "create_report",
    command_parser.CREATE_PDF: "create_pdf",
    command_parser.CREATE_SPREADSHEET: "create_spreadsheet",
}


def plan_from_intent(
    intent: ParsedIntent,
    *,
    default_location: str = "",
    content_loader: Optional[Callable[[str], dict]] = None,
) -> dict:
    """Turn a parsed intent into a validated action dict.

    ``default_location`` is the home-relative location used when the
    command does not name one (the pipeline passes the primary
    permitted root).  ``content_loader`` reads a 'using file.json'
    source through the caller's permission checks.  Raises
    :class:`PlanValidationError` for unsupported intents and
    :class:`ContentSourceError` when a content source cannot be read.
    """
    slots = intent.slots or {}

    if intent.intent == command_parser.UNSUPPORTED:
        raise PlanValidationError(slots.get("reason") or "Unsupported command.")

    if intent.intent == command_parser.CREATE_DIRECTORY:
        location = slots.get("location") or default_location
        path = _join_path(location, slots.get("name", ""))
        return validate_action({"action": "create_directory", "path": path})

    if intent.intent == command_parser.LIST_DIRECTORY:
        path = (slots.get("location") or default_location or "").strip() or "."
        return validate_action({"action": "list_directory", "path": path})

    if intent.intent == command_parser.READ_FILE:
        location = slots.get("location") or default_location
        path = _join_path(location, slots.get("name", ""))
        return validate_action({"action": "read_file", "path": path})

    if intent.intent == command_parser.CREATE_TEXT_FILE:
        location = slots.get("location") or default_location
        path = _join_path(location, slots.get("name", ""))
        return validate_action(
            {"action": "create_text_file", "path": path, "content": slots.get("content", "")}
        )

    if intent.intent in _INTENT_TO_ACTION:
        action_name = _INTENT_TO_ACTION[intent.intent]
        source = (slots.get("source") or "").strip()
        name = (slots.get("name") or "").strip()
        if not name and source:
            # Derive the output name from the JSON source file stem.
            name = _source_stem(source) + DOCUMENT_EXTENSIONS[action_name]
        if not name:
            example = {
                "create_report": "summary.txt",
                "create_pdf": "summary.pdf",
                "create_spreadsheet": "sales.xlsx",
            }[action_name]
            raise PlanValidationError(
                f"Please include a file name, e.g. 'named {example}'."
            )
        location = (slots.get("location") or default_location or "").strip()
        if source and not source.startswith(("/", "~")) and "/" not in source and location:
            # A bare 'using spec.json' is read from the same folder the
            # document is written to.  The loader still permission-checks
            # the final path like every other filesystem access.
            source = f"{location.strip('/')}/{source}"
        path = _join_path(location, name)
        content = _resolve_document_content(
            slots, action=action_name, source=source, content_loader=content_loader
        )
        payload = {"action": action_name, "path": path}
        payload["spec" if action_name == "create_spreadsheet" else "content"] = content
        return validate_action(payload)

    if intent.intent == command_parser.OPEN_FILE:
        location = slots.get("location") or default_location
        path = _join_path(location, slots.get("name", ""))
        try:
            application = resolve_integration_id(
                slots.get("application"), BUILTIN_REGISTRY
            )
        except RegistryError as exc:
            raise PlanValidationError(str(exc)) from exc
        if not application:
            raise PlanValidationError(
                "Please name the application, e.g. 'Open sales.xlsx in "
                "Documents with LibreOffice Calc' (or 'with system default')."
            )
        return validate_action(
            {"action": "open_file", "path": path, "application": application}
        )

    if intent.intent == command_parser.DETECT_APPLICATIONS:
        try:
            integration = resolve_integration_id(
                slots.get("application"), BUILTIN_REGISTRY
            )
        except RegistryError as exc:
            raise PlanValidationError(str(exc)) from exc
        return validate_action(
            {"action": "detect_applications", "integration": integration}
        )

    # ---- v0.4 desktop automation ------------------------------------- #

    if intent.intent == command_parser.INSPECT_APPLICATION:
        return validate_action(
            {
                "action": "inspect_application",
                "application": slots.get("application", ""),
            }
        )

    if intent.intent in (
        command_parser.FIND_CONTROL,
        command_parser.READ_CONTROL,
        command_parser.ACTIVATE_CONTROL,
        command_parser.ENTER_TEXT,
    ):
        action_name = {
            command_parser.FIND_CONTROL: "find_control",
            command_parser.READ_CONTROL: "read_control",
            command_parser.ACTIVATE_CONTROL: "activate_control",
            command_parser.ENTER_TEXT: "enter_text",
        }[intent.intent]
        payload = {
            "action": action_name,
            "application": slots.get("application", ""),
            "control": {
                "role": slots.get("role", ""),
                "name": slots.get("name", ""),
            },
        }
        if action_name == "enter_text":
            payload["content"] = slots.get("content", "")
        return validate_action(payload)

    if intent.intent == command_parser.SAVE_DOCUMENT:
        path = (slots.get("path") or "").strip()
        if path and not path.startswith(("/", "~")) and "/" not in path:
            # A bare file name ('to notes.txt') is placed in the same
            # default location other commands write to ('Documents').
            path = _join_path(default_location, path)
        return validate_action(
            {
                "action": "save_document",
                "application": slots.get("application", ""),
                "path": path,
            }
        )

    if intent.intent == command_parser.TASK_SEQUENCE:
        commands = list(slots.get("commands") or [])
        if not commands:
            raise PlanValidationError("The sequence contains no steps.")
        steps = []
        for index, command in enumerate(commands, start=1):
            sub = command_parser.parse_command(command)
            if sub.intent in (
                command_parser.UNSUPPORTED,
                command_parser.EXIT,
                command_parser.TASK_SEQUENCE,
            ):
                reason = sub.slots.get("reason") or "this step is not supported"
                raise PlanValidationError(
                    f"Step {index} of the sequence could not be planned: {reason}"
                )
            try:
                step = plan_from_intent(
                    sub,
                    default_location=default_location,
                    content_loader=content_loader,
                )
            except PlanValidationError as exc:
                raise PlanValidationError(f"Step {index} of the sequence: {exc}") from exc
            if step.get("action") in ("task_sequence", "explain"):
                raise PlanValidationError(
                    f"Step {index} of the sequence cannot be a nested "
                    f"'{step.get('action')}'."
                )
            steps.append(step)
        return validate_action({"action": "task_sequence", "steps": steps})

    if intent.intent == command_parser.EXPLAIN:
        inner = command_parser.parse_command(slots.get("target", ""))
        if inner.intent == command_parser.UNSUPPORTED:
            raise PlanValidationError(
                "Nothing to explain: "
                + (inner.slots.get("reason") or "unsupported operation.")
            )
        target = plan_from_intent(
            inner, default_location=default_location, content_loader=content_loader
        )
        return validate_action({"action": "explain", "target": target})

    if intent.intent == command_parser.EXIT:
        raise PlanValidationError("No operation to plan for an exit command.")

    raise PlanValidationError(f"Cannot plan intent '{intent.intent}'.")


# --------------------------------------------------------------------------- #
# Human-readable descriptions (used for previews and explanations)
# --------------------------------------------------------------------------- #


def describe_action(
    action: dict, *, resolved: Optional[Path] = None, home: Optional[Path] = None
) -> str:
    """One-line description of a validated action for display."""
    name = action.get("action")
    if name == "explain":
        return "Explain: " + describe_action(
            action.get("target", {}), resolved=resolved, home=home
        )

    if resolved is not None:
        display = pretty_path(resolved, home)
    else:
        display = pretty_path(action.get("path", "?"), home)

    if name == "create_directory":
        return f"Create directory {display}"
    if name == "create_text_file":
        content = action.get("content", "")
        size = len(content.encode("utf-8"))
        return f"Create text file {display} ({size} bytes of content)"
    if name == "create_report":
        blocks = (action.get("content") or {}).get("blocks") or []
        return f"Create text report {display} ({len(blocks)} blocks)"
    if name == "create_pdf":
        blocks = (action.get("content") or {}).get("blocks") or []
        return f"Create PDF document {display} ({len(blocks)} blocks)"
    if name == "create_spreadsheet":
        sheets = (action.get("spec") or {}).get("sheets") or []
        return f"Create spreadsheet {display} ({len(sheets)} sheets)"
    if name == "open_file":
        return f"Open {display} with {_application_name(action.get('application'))}"
    if name == "detect_applications":
        integration = action.get("integration") or ""
        if integration:
            return f"Detect application {_application_name(integration)} (read-only)"
        return "Detect installed applications (read-only)"
    if name == "list_directory":
        return f"List contents of {display}"
    if name == "read_file":
        return f"Read file {display}"
    # ---- v0.4 desktop automation ------------------------------------- #
    if name == "inspect_application":
        return (
            f"Inspect application state of "
            f"{_application_name(action.get('application', ''))} (read-only)"
        )
    if name == "find_control":
        return (
            f"Find {_control_label(action.get('control') or {})} in "
            f"{_application_name(action.get('application', ''))} (read-only)"
        )
    if name == "read_control":
        return (
            f"Read text of {_control_label(action.get('control') or {})} in "
            f"{_application_name(action.get('application', ''))} (read-only)"
        )
    if name == "activate_control":
        return (
            f"Activate {_control_label(action.get('control') or {})} in "
            f"{_application_name(action.get('application', ''))}"
        )
    if name == "enter_text":
        return (
            f"Enter {len(action.get('content', ''))} characters of text into "
            f"{_control_label(action.get('control') or {})} in "
            f"{_application_name(action.get('application', ''))}"
        )
    if name == "save_document":
        return (
            f"Save the active {_application_name(action.get('application', ''))} "
            f"document to {display} (confirmed only if the file changes)"
        )
    if name == "task_sequence":
        steps = action.get("steps") or []
        joined = " -> ".join(
            describe_action(step, home=home) for step in steps
        )
        return f"Run task sequence of {len(steps)} step(s): {joined}"
    return f"Unknown action {name!r}"


def _control_label(control: dict) -> str:
    """One-line, newline-free label for a control locator (approval text)."""
    label = f"role '{str(control.get('role', '?'))}'"
    name = " ".join(str(control.get("name", "")).split())
    if name:
        label += f" name '{name[:60]}'"
    return label


def _application_name(integration_id: str) -> str:
    """Human-readable name for a registered integration id."""
    try:
        return BUILTIN_REGISTRY.get(integration_id).name
    except RegistryError:
        return integration_id or "?"


# --------------------------------------------------------------------------- #
# Optional local-LLM planning
# --------------------------------------------------------------------------- #

SYSTEM_PROMPT = (
    "You translate one user request into exactly one JSON action for a "
    "sandboxed file assistant. Reply with a single JSON object and nothing else.\n"
    "Allowed actions and fields:\n"
    '{"action": "create_directory", "path": "..."}\n'
    '{"action": "list_directory", "path": "..."}\n'
    '{"action": "read_file", "path": "..."}\n'
    '{"action": "create_text_file", "path": "...", "content": "..."}\n'
    '{"action": "create_report", "path": "...", '
    '"content": {"title": "...", "blocks": [{"type": "heading", "level": 1, '
    '"text": "..."}, {"type": "paragraph", "text": "..."}, {"type": "list", '
    '"style": "bullet", "items": ["..."]}]} }\n'
    '{"action": "create_pdf", "path": "...", "content": { ...same as '
    "create_report... }}\n"
    '{"action": "create_spreadsheet", "path": "...", "spec": {"title": "...", '
    '"sheets": [{"name": "Sheet1", "headers": ["A", "B"], "rows": [[1, 2]], '
    '"formulas": [{"cell": "C2", "formula": "=SUM(A1:B1)"}]}]}}\n'
    '{"action": "detect_applications", "integration": "libreoffice-writer"} '
    '  (omit "integration" or use "" to detect all)\n'
    '{"action": "open_file", "path": "Documents/report.txt", "application": '
    '"libreoffice-writer"}\n'
    # ---- v0.4 desktop automation (closed set) ----
    '{"action": "inspect_application", "application": "libreoffice-writer"} '
    "  (read-only state report)\n"
    '{"action": "find_control", "application": "libreoffice-writer", '
    '"control": {"role": "button", "name": "Save"}}  '
    '("name" may be omitted to find the first control of a role)\n'
    '{"action": "read_control", "application": "libreoffice-calc", '
    '"control": {"role": "entry", "name": "Search"}}  (read-only)\n'
    '{"action": "activate_control", "application": "libreoffice-writer", '
    '"control": {"role": "button", "name": "OK"}}\n'
    '{"action": "enter_text", "application": "libreoffice-writer", '
    '"control": {"role": "entry", "name": "Find"}, "content": "..."}\n'
    '{"action": "save_document", "application": "libreoffice-writer", '
    '"path": "Documents/notes.txt"}\n'
    '{"action": "task_sequence", "steps": [ ...1 to 8 validated actions, '
    "one object each, no nested sequences... ]}\n"
    '{"action": "explain", "target": { ...one of the above... }}\n'
    "Rules:\n"
    "- 'path' is relative to the user's home directory, e.g. "
    '"Documents/Business Reports".\n'
    "- Never use absolute paths or '..'.\n"
    "- File extensions are fixed: .txt for reports, .pdf for PDFs, "
    ".xlsx for spreadsheets.\n"
    "- Structured content is validated field by field; formulas are stored "
    "as text and are never calculated here.\n"
    "- You can never read files: never reference a file path as content "
    "source; inline the full content instead.\n"
    f"- 'application' and 'integration' must be one of these exact ids: "
    f"{', '.join(BUILTIN_REGISTRY.ids)}. Never invent an executable name, "
    "path or command-line flag — no such field exists.\n"
    "- 'open_file' only opens files that already exist inside the "
    "permitted roots; the user approves every launch.\n"
    "- Automation 'application' must be an application with the 'automate' "
    "capability: exactly "
    f"{', '.join(sorted(i.id for i in BUILTIN_REGISTRY.definitions if 'automate' in i.capabilities))} "
    "in this milestone.\n"
    "- Automation 'control.role' must be one of: "
    f"{', '.join(sorted(KNOWN_ROLES))}.\n"
    "- 'read_control', 'activate_control' and 'enter_text' need an exact "
    "'control.name'; only 'find_control' may omit it.\n"
    "- Read-only automation: inspect_application, find_control, read_control. "
    "Mutating (user approval required): activate_control, enter_text, "
    "save_document, task_sequence.\n"
    "- 'enter_text' targets input fields only (role 'entry'); document "
    "bodies are created with the document actions, never typed into.\n"
    "- 'save_document' never claims a save by itself: the file must change "
    "on disk for a save to be verified.\n"
    "- No action ever exists for shell commands, keystrokes, mouse "
    "coordinates, installing software or disabling security settings.\n"
    "- No other action ever exists: never propose delete, move, shell, "
    "network or code-execution actions.\n"
    "- If the request cannot be mapped, reply with "
    '{"action": "unsupported", "reason": "..."}\n'
)


def _strip_code_fences(text: str) -> str:
    fenced = re.match(r"^```[a-zA-Z]*\s*(.*?)\s*```$", text.strip(), re.DOTALL)
    if fenced:
        return fenced.group(1).strip()
    return text.strip()


def parse_model_action(text: str) -> dict:
    """Parse and validate JSON proposed by the local model.

    Only ``json.loads`` is used; the result is validated independently
    of the model by :func:`validate_action`.
    """
    cleaned = _strip_code_fences(text or "")
    if not cleaned:
        raise PlanValidationError("The local model returned an empty response.")
    try:
        payload = json.loads(cleaned)
    except json.JSONDecodeError as exc:
        raise PlanValidationError(
            f"The local model did not return valid JSON ({exc.msg})."
        ) from exc
    if isinstance(payload, dict) and payload.get("action") == "unsupported":
        raise PlanValidationError(
            payload.get("reason")
            or "The local model could not map this request to an action."
        )
    return validate_action(payload)


def plan_via_llm(command: str, client) -> dict:
    """Ask the local Ollama runtime for an action, then validate it.

    Raises :class:`~ai_runtime.ollama_client.OllamaError` when the local
    runtime is unavailable and :class:`PlanValidationError` when the
    model's proposal is not acceptable.  Callers fall back to the
    deterministic parser in both cases.
    """
    prompt = f"User request: {command.strip()}\nJSON action:"
    response = client.generate(prompt=prompt, system=SYSTEM_PROMPT, json_mode=True)
    return parse_model_action(response)
