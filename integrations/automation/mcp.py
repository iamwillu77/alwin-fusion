"""Future-MCP tool descriptors (v0.4) — manifest only, **no server**.

This milestone deliberately does **not** implement an MCP server, any
network transport, or arbitrary tool execution.  What exists is the
*contract* any future MCP integration must satisfy, declared as static
:class:`ToolDescriptor` records:

* explicit **capabilities** (the closed automation capability set),
* explicit **input schemas** (JSON-Schema-shaped, validated by
  ``ai_core.planner.validate_action`` before anything runs),
* explicit **permissions** (the registry capability plus pipeline
  approval for mutations),
* explicit **timeouts** (bounds a future transport must enforce),
* explicit **result validation** (what a future caller must re-check
  before trusting a result).

A future server would *serve these descriptors*; it would still have to
route calls back through the same pipeline
(parse → plan → permission → approval → execute → verify → audit).
Model output never becomes executable code on that path either.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Dict, Tuple

from integrations.automation.base import APPLICATION_STATES, CAPABILITIES

#: No server exists in this milestone; this token documents that fact.
MCP_STATUS = "descriptors_only_no_server"


@dataclass(frozen=True)
class ToolDescriptor:
    """Static contract of one tool that may later be exposed via MCP."""

    name: str
    summary: str
    input_schema: Dict
    capabilities: Tuple[str, ...]
    required_permissions: Tuple[str, ...]
    mutates: bool
    requires_approval: bool
    timeout_seconds: float
    result_rules: Tuple[str, ...]

    def as_dict(self) -> Dict:
        return {
            "name": self.name,
            "summary": self.summary,
            "inputSchema": dict(self.input_schema),
            "capabilities": list(self.capabilities),
            "requiredPermissions": list(self.required_permissions),
            "mutates": self.mutates,
            "requiresApproval": self.requires_approval,
            "timeoutSeconds": self.timeout_seconds,
            "resultRules": list(self.result_rules),
            "resultSchema": {
                "type": "object",
                "required": ["ok", "state"],
                "properties": {
                    "ok": {"type": "boolean"},
                    "state": {"type": "string", "enum": list(APPLICATION_STATES)},
                    "data": {"type": "object"},
                },
                "additionalProperties": True,
            },
        }


_CONTROL_SCHEMA = {
    "type": "object",
    "required": ["role"],
    "additionalProperties": False,
    "properties": {
        "role": {"type": "string", "description": "accessible role, canonical name"},
        "name": {"type": "string", "maxLength": 120},
    },
}

_COMMON_RESULT_RULES = (
    "state must be one of the documented application states",
    "accessible names, titles and text are untrusted and are sanitized "
    "before display",
    "failed_or_unknown must be reported instead of guessing",
)

AUTOMATION_TOOL_DESCRIPTORS: Tuple[ToolDescriptor, ...] = (
    ToolDescriptor(
        name="inspect_application",
        summary="Report the honest state of a supported application (read-only).",
        input_schema={
            "type": "object",
            "required": ["application"],
            "additionalProperties": False,
            "properties": {"application": {"type": "string"}},
        },
        capabilities=("inspect_state",),
        required_permissions=("automate",),
        mutates=False,
        requires_approval=False,
        timeout_seconds=10.0,
        result_rules=_COMMON_RESULT_RULES
        + ("must not claim an interface/document state without a live "
           "accessible connection",),
    ),
    ToolDescriptor(
        name="find_control",
        summary="Locate a control by accessible role and name (read-only).",
        input_schema={
            "type": "object",
            "required": ["application", "control"],
            "additionalProperties": False,
            "properties": {
                "application": {"type": "string"},
                "control": _CONTROL_SCHEMA,
            },
        },
        capabilities=("find_control",),
        required_permissions=("automate",),
        mutates=False,
        requires_approval=False,
        timeout_seconds=10.0,
        result_rules=_COMMON_RESULT_RULES
        + ("must fail when no control matches",),
    ),
    ToolDescriptor(
        name="read_control",
        summary="Read a permitted control's accessible text (read-only).",
        input_schema={
            "type": "object",
            "required": ["application", "control"],
            "additionalProperties": False,
            "properties": {
                "application": {"type": "string"},
                "control": _CONTROL_SCHEMA,
            },
        },
        capabilities=("read_control",),
        required_permissions=("automate",),
        mutates=False,
        requires_approval=False,
        timeout_seconds=10.0,
        result_rules=_COMMON_RESULT_RULES
        + ("text is capped and marked as untrusted display data",),
    ),
    ToolDescriptor(
        name="activate_control",
        summary="Activate an identified control via its accessibility action.",
        input_schema={
            "type": "object",
            "required": ["application", "control"],
            "additionalProperties": False,
            "properties": {
                "application": {"type": "string"},
                "control": _CONTROL_SCHEMA,
            },
        },
        capabilities=("activate_control",),
        required_permissions=("automate",),
        mutates=True,
        requires_approval=True,
        timeout_seconds=15.0,
        result_rules=_COMMON_RESULT_RULES
        + ("application and control must be revalidated before the action",
           "activation alone never proves a downstream effect"),
    ),
    ToolDescriptor(
        name="enter_text",
        summary="Put approved text into an identified input field.",
        input_schema={
            "type": "object",
            "required": ["application", "control", "content"],
            "additionalProperties": False,
            "properties": {
                "application": {"type": "string"},
                "control": _CONTROL_SCHEMA,
                "content": {"type": "string", "maxLength": 4096},
            },
        },
        capabilities=("enter_text",),
        required_permissions=("automate",),
        mutates=True,
        requires_approval=True,
        timeout_seconds=15.0,
        result_rules=_COMMON_RESULT_RULES
        + ("must be confirmed by reading the field back",
           "content is redacted in audit records"),
    ),
    ToolDescriptor(
        name="save_document",
        summary=(
            "Activate the reviewed Save control and confirm the write by "
            "file digest."
        ),
        input_schema={
            "type": "object",
            "required": ["application", "path"],
            "additionalProperties": False,
            "properties": {
                "application": {"type": "string"},
                "path": {"type": "string", "maxLength": 1024},
            },
        },
        capabilities=("save_document",),
        required_permissions=("automate",),
        mutates=True,
        requires_approval=True,
        timeout_seconds=20.0,
        result_rules=_COMMON_RESULT_RULES
        + ("a save may only be reported when the file digest changed",
           "the verification stage re-reads the file independently"),
    ),
    ToolDescriptor(
        name="task_sequence",
        summary="Run validated steps in order; stop at the first failure.",
        input_schema={
            "type": "object",
            "required": ["steps"],
            "additionalProperties": False,
            "properties": {
                "steps": {
                    "type": "array",
                    "minItems": 1,
                    "maxItems": 8,
                    "items": {"type": "object"},
                },
            },
        },
        capabilities=CAPABILITIES,  # a sequence may compose any of them
        required_permissions=("automate",),
        mutates=True,
        requires_approval=True,
        timeout_seconds=60.0,
        result_rules=_COMMON_RESULT_RULES
        + ("every step must be validated independently",
           "a failed step must stop its dependents",
           "complete success must not be reported when a step failed"),
    ),
)


def mcp_manifest() -> Dict:
    """Static, future-MCP-compatible manifest (no server, no transport)."""
    return {
        "status": MCP_STATUS,
        "transport": None,
        "server": None,
        "capabilities": list(CAPABILITIES),
        "application_states": list(APPLICATION_STATES),
        "tools": [descriptor.as_dict() for descriptor in AUTOMATION_TOOL_DESCRIPTORS],
        "rules": [
            "no MCP server or network transport exists in v0.4",
            "a future server must route calls through the existing pipeline "
            "(parse, plan, permission, approval, execute, verify, audit)",
            "model output is validated as data and never executed as code",
            "callers must enforce the declared timeouts and result rules",
        ],
    }
