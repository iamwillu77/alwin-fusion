"""Desktop-automation interfaces (v0.4).

This module defines the *seam* between the trusted core (planner,
permissions, dispatcher) and any concrete accessibility backend.  It is
deliberately separate from:

* natural-language parsing (``ai_core.command_parser``),
* model inference (``ai_core.planner`` / ``ai_runtime``),
* application discovery and launching (``integrations.apps``),
* permission decisions (``ai_core.permissions``),
* filesystem tools (``integrations.filesystem``),
* document generation (``integrations.documents``).

Everything here is data and abstract interfaces: no D-Bus call, no
process launch, no file access happens in this module.

Design rules encoded by these types:

* every operation reports one of a fixed set of *application states*;
  an unverifiable state is ``failed_or_unknown`` — never a guess,
* adapters expose an explicit *capability set*; an operation outside
  that set fails with an honest "unsupported" report instead of being
  faked,
* accessible names, window titles and control text are untrusted
  display data: they are sanitized before they reach a message and
  they never take part in a permission or policy decision.
"""

from __future__ import annotations

import re
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Dict, FrozenSet, Optional, Tuple

# --------------------------------------------------------------------------- #
# Application states (the honest state machine of v0.4)
# --------------------------------------------------------------------------- #

#: The application is not installed (never installed automatically).
NOT_INSTALLED = "not_installed"
#: Installed, but no process was observed.
INSTALLED_NOT_RUNNING = "installed_not_running"
#: A launch was requested (through the v0.3 open_file flow).
LAUNCH_REQUESTED = "launch_requested"
#: A process was observed — which proves *nothing* about its window.
PROCESS_STARTED = "process_started"
#: The application exposed an accessible (AT-SPI) interface.
INTERFACE_DETECTED = "interface_detected"
#: The target document was identified in the interface (reliably).
DOCUMENT_OPEN = "document_open"
#: A specific control was located by role and accessible name.
CONTROL_IDENTIFIED = "control_identified"
#: An allowlisted action was performed on a revalidated control.
ACTION_PERFORMED = "action_performed"
#: The action ran, but no independent state change could be observed —
#: reported as executed-but-UNVERIFIED, never as success (v0.17).
EXECUTED_UNVERIFIED = "executed_unverified"
#: The result was confirmed by an independent re-read (e.g. file digest,
#: or a re-inspected accessibility state change) (v0.17).
RESULT_VERIFIED = "result_verified"
#: More than one control matched the locator — the target is ambiguous
#: and NO action was taken (v0.17: duplicate names are never guessed).
AMBIGUOUS_TARGET = "ambiguous_target"
#: The identified control or its application went away (defunct
#: accessible) — the target is stale and NO action was taken (v0.17).
STALE_TARGET = "stale_target"
#: The operation failed or the state cannot be verified honestly.
FAILED_UNKNOWN = "failed_or_unknown"

APPLICATION_STATES = (
    NOT_INSTALLED,
    INSTALLED_NOT_RUNNING,
    LAUNCH_REQUESTED,
    PROCESS_STARTED,
    INTERFACE_DETECTED,
    DOCUMENT_OPEN,
    CONTROL_IDENTIFIED,
    ACTION_PERFORMED,
    EXECUTED_UNVERIFIED,
    RESULT_VERIFIED,
    AMBIGUOUS_TARGET,
    STALE_TARGET,
    FAILED_UNKNOWN,
)

#: States that may only be reported when a live accessible connection
#: to the application exists.  The verifier cross-checks this.
INTERFACE_STATES = frozenset(
    {INTERFACE_DETECTED, DOCUMENT_OPEN, CONTROL_IDENTIFIED}
)

# --------------------------------------------------------------------------- #
# Automation capabilities (the closed operation set)
# --------------------------------------------------------------------------- #

CAP_INSPECT = "inspect_state"
CAP_FIND = "find_control"
CAP_READ = "read_control"
CAP_ACTIVATE = "activate_control"
CAP_ENTER = "enter_text"
CAP_SAVE = "save_document"
#: v0.17 bounded, read-only listing of an application's controls
#: (roles/names/states/action names only — never document text).
CAP_LIST = "list_controls"

CAPABILITIES = (
    CAP_INSPECT,
    CAP_FIND,
    CAP_READ,
    CAP_ACTIVATE,
    CAP_ENTER,
    CAP_SAVE,
    CAP_LIST,
)

READ_ONLY_CAPABILITIES = frozenset(
    {CAP_INSPECT, CAP_FIND, CAP_READ, CAP_LIST}
)

# --------------------------------------------------------------------------- #
# Bounds (every tree walk and every text value is capped)
# --------------------------------------------------------------------------- #

#: Roles that may be *read* (their accessible text is exposed).
READABLE_ROLES = frozenset(
    {
        "entry",
        "text",
        "paragraph",
        "label",
        "heading",
        "document frame",
        "table cell",
        "status bar",
        "list item",
        "page tab",
        "link",
        "combo box",
    }
)

#: Roles that may be *activated* through the accessibility action
#: interface (no keystroke, no mouse coordinates — ever).
ACTIVATABLE_ROLES = frozenset(
    {
        "button",
        "toggle button",
        "check box",
        "radio button",
        "menu item",
        "page tab",
        "link",
        "list item",
    }
)

#: Roles that may receive entered text.  v0.4 allows single-line input
#: fields only: document bodies are *not* editable through automation
#: (content creation stays with the trusted document generators).
ENTERABLE_ROLES = frozenset({"entry"})

MAX_CONTROL_NAME_CHARS = 120
MAX_ENTERED_TEXT_CHARS = 4096
MAX_READ_TEXT_CHARS = 4096
MAX_TREE_NODES = 4000
MAX_TREE_DEPTH = 40

#: v0.17 bounded control listing: at most this many summaries, within
#: this depth below the identified window, each name capped again by
#: MAX_CONTROL_NAME_CHARS on serialization.
#: Depth note: GTK4 applications (e.g. GNOME Text Editor) bury their
#: real controls under a deep chain of layout panels — the reference
#: host exposes buttons/toggles at depth 10-18, so the bound is 20
#: (measured on the real tree, still strictly bounded).
MAX_LIST_CONTROLS = 40
MAX_LIST_DEPTH = 20
#: Structural layout roles with no accessible name carry no
#: user-meaningful information (measured: a GTK4 editor window has
#: dozens of unnamed 'panel'/'grouping' nodes).  Listings keep the
#: bounds but summarize the CONTROLS, not the layout scaffolding —
#: named structural nodes still appear.
STRUCTURAL_ROLES = frozenset(
    {"panel", "grouping", "separator", "scroll pane", "layered pane",
     "split pane", "filler", "section"}
)
#: How many ambiguous candidates the backend collects before it stops
#: (enough to say "more than one match" — never a full desktop dump).
MAX_AMBIGUOUS_CANDIDATES = 8

_CONTROL_UNSAFE_RE = re.compile(r"[\x00-\x1f\x7f]")


def safe_app_text(value: object, limit: int = 80) -> str:
    """Sanitize application-provided text for use in a message.

    Accessible names, window titles and control text are *untrusted*:
    they may contain newlines, control characters or enormous strings.
    This collapses them into a single printable line with a hard cap so
    they can never forge log lines or overwrite terminal output.
    """
    text = value if isinstance(value, str) else ""
    text = _CONTROL_UNSAFE_RE.sub(" ", text)
    text = " ".join(text.split())
    if len(text) > limit:
        text = text[: max(0, limit - 1)] + "\u2026"
    return text


# --------------------------------------------------------------------------- #
# Errors
# --------------------------------------------------------------------------- #


class AutomationError(Exception):
    """Base class for desktop-automation failures."""


class CapabilityUnavailable(AutomationError):
    """The adapter does not support the requested operation."""


class ControlNotFound(AutomationError):
    """No control matched the (revalidated) locator."""


class StaleStateError(AutomationError):
    """The application or control disappeared between check and action."""


class AmbiguousTargetError(AutomationError):
    """Several controls matched the locator — nothing was attempted.

    v0.17: duplicate accessible names are never resolved by guessing;
    the caller must narrow the locator.
    """


class BackendError(AutomationError):
    """The accessibility backend failed (connection lost, app exited)."""


# --------------------------------------------------------------------------- #
# Value types
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class ControlLocator:
    """What the caller looks for: a role plus (optionally) a name."""

    role: str
    name: str = ""


@dataclass(frozen=True)
class ControlInfo:
    """A control as the backend actually reported it (revalidated)."""

    role: str
    name: str
    states: Tuple[str, ...] = ()
    action_names: Tuple[str, ...] = ()
    readable: bool = False
    enterable: bool = False
    index_path: Tuple[int, ...] = ()

    def as_dict(self) -> Dict:
        return {
            "role": self.role,
            "name": safe_app_text(self.name, MAX_CONTROL_NAME_CHARS),
            "states": list(self.states),
            "action_names": [safe_app_text(a, 40) for a in self.action_names],
            "readable": self.readable,
            "enterable": self.enterable,
            "index_path": list(self.index_path),
        }


@dataclass(frozen=True)
class AppSnapshot:
    """The result of a read-only state inspection."""

    application: str
    integration: str
    state: str
    installed: Optional[bool] = None
    process_running: Optional[bool] = None
    interface_detected: bool = False
    document: str = ""
    document_reliable: bool = False
    a11y_available: bool = False
    capabilities: Tuple[str, ...] = ()
    detail: str = ""

    def as_dict(self) -> Dict:
        return {
            "application": self.application,
            "integration": self.integration,
            "state": self.state,
            "installed": self.installed,
            "process_running": self.process_running,
            "interface_detected": self.interface_detected,
            "document": self.document,
            "document_reliable": self.document_reliable,
            "a11y_available": self.a11y_available,
            "capabilities": list(self.capabilities),
            "detail": safe_app_text(self.detail, 200),
        }


@dataclass(frozen=True)
class ActionResult:
    """Outcome of one automation operation."""

    ok: bool
    state: str
    capability: str
    message: str
    control: Optional[ControlInfo] = None
    data: Dict = field(default_factory=dict)
    error: str = ""

    def as_dict(self) -> Dict:
        payload: Dict = {
            "ok": self.ok,
            "state": self.state,
            "capability": self.capability,
            "message": self.message,
            "error": self.error,
            "data": dict(self.data),
        }
        if self.control is not None:
            payload["control"] = self.control.as_dict()
        return payload


# --------------------------------------------------------------------------- #
# The adapter interface
# --------------------------------------------------------------------------- #


class AutomationAdapter(ABC):
    """One application's automation surface.

    Implementations must:

    * re-resolve the application and the target control *fresh* for
      every mutating operation (no cached handles, no stale state),
    * refuse any capability outside :meth:`capabilities`,
    * report ``failed_or_unknown`` instead of guessing,
    * never expose keystroke injection or coordinate clicking.
    """

    #: Integration id this adapter drives (e.g. ``libreoffice-writer``).
    application: str = ""

    @abstractmethod
    def capabilities(self) -> FrozenSet[str]:
        """The automation operations this application actually exposes."""

    def supports(self, capability: str) -> bool:
        return capability in self.capabilities()

    @abstractmethod
    def inspect(self) -> AppSnapshot:
        """Read-only state inspection (never mutates, never launches)."""

    @abstractmethod
    def find_control(self, locator: ControlLocator) -> ActionResult:
        """Locate a control by role (+ name); read-only."""

    @abstractmethod
    def read_control(self, locator: ControlLocator) -> ActionResult:
        """Read a permitted control's accessible text; read-only."""

    @abstractmethod
    def activate_control(self, locator: ControlLocator) -> ActionResult:
        """Activate an identified control via its accessibility action."""

    @abstractmethod
    def enter_text(self, locator: ControlLocator, text: str) -> ActionResult:
        """Put approved text into an identified input field."""

    @abstractmethod
    def save_document(self) -> ActionResult:
        """Activate the reviewed Save control (never claims "saved")."""


class UnavailableAdapter(AutomationAdapter):
    """Honest stand-in when a backend is missing or the app has no adapter.

    Every operation fails with the same clear reason; nothing is faked.
    ``inspect`` still reports a *state* (e.g. ``not_installed``) because
    that information is known without a backend.
    """

    def __init__(
        self,
        application: str,
        reason: str,
        *,
        state: str = FAILED_UNKNOWN,
        installed: Optional[bool] = None,
        process_running: Optional[bool] = None,
    ) -> None:
        self.application = application
        self.reason = reason
        self.state = state
        self.installed = installed
        self.process_running = process_running

    def capabilities(self) -> FrozenSet[str]:
        return frozenset()

    def _fail(self, capability: str) -> ActionResult:
        return ActionResult(
            ok=False,
            state=self.state,
            capability=capability,
            message="",
            error=(
                f"Automation capability '{capability}' is unavailable for "
                f"{self.application or 'this application'}: {self.reason}"
            ),
            data={"unsupported": capability, "reason": self.reason},
        )

    def inspect(self) -> AppSnapshot:
        return AppSnapshot(
            application=self.application,
            integration=self.application,
            state=self.state,
            installed=self.installed,
            process_running=self.process_running,
            interface_detected=False,
            a11y_available=False,
            capabilities=(),
            detail=self.reason,
        )

    def find_control(self, locator: ControlLocator) -> ActionResult:
        return self._fail(CAP_FIND)

    def read_control(self, locator: ControlLocator) -> ActionResult:
        return self._fail(CAP_READ)

    def activate_control(self, locator: ControlLocator) -> ActionResult:
        return self._fail(CAP_ACTIVATE)

    def enter_text(self, locator: ControlLocator, text: str) -> ActionResult:
        return self._fail(CAP_ENTER)

    def save_document(self) -> ActionResult:
        return self._fail(CAP_SAVE)
