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
)

#: Actions that change the filesystem and therefore need user approval.
MUTATING_ACTIONS = frozenset(
    {
        "create_directory",
        "create_text_file",
        "create_report",
        "create_pdf",
        "create_spreadsheet",
    }
)

#: Generated-document actions and the only file extension each produces.
DOCUMENT_EXTENSIONS = {
    "create_report": ".txt",
    "create_pdf": ".pdf",
    "create_spreadsheet": ".xlsx",
}

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
    if name == "list_directory":
        return f"List contents of {display}"
    if name == "read_file":
        return f"Read file {display}"
    return f"Unknown action {name!r}"


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
