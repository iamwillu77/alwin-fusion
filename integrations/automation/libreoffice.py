"""LibreOffice Writer/Calc automation adapter (v0.4).

The adapter implements :class:`integrations.automation.base.AutomationAdapter`
on top of an AT-SPI session.  It is the only code that knows how to
talk to LibreOffice's accessible interface, and it is deliberately
small:

* **inspect** — find the application on the accessibility bus, find the
  module window (``... - LibreOffice Writer`` / ``... - LibreOffice Calc``)
  and report which state is *proven*,
* **find/read** — locate a control by role + accessible name, read its
  accessible text (bounded),
* **activate** — invoke the control's accessibility *action* (no
  keystrokes, no coordinates, no shell),
* **enter text** — replace an input field's content (role ``entry``
  only; document bodies are never edited through GUI automation),
* **save** — activate a *reviewed* Save control name and honestly stop
  at ``action_performed``: the caller must confirm the write by file
  digest before anyone may say "saved".

Application-provided strings (window titles, accessible names, control
text) are untrusted display data: they are sanitized through
:func:`~integrations.automation.base.safe_app_text` before reaching any
message, and no permission or policy decision is ever based on them —
identity is re-resolved from the trusted registry hints on every call.
"""

from __future__ import annotations

import re
from typing import Optional

from integrations.automation.atspi_backend import (
    AtspiSession,
    ambiguity_error,
    node_is_defunct,
    normalize_name,
    normalize_role,
    resolve_candidates,
)
from integrations.automation.base import (
    ACTIVATABLE_ROLES,
    AMBIGUOUS_TARGET,
    CAP_ACTIVATE,
    CAP_ENTER,
    CAP_FIND,
    CAP_INSPECT,
    CAP_LIST,
    CAP_READ,
    CAP_SAVE,
    CONTROL_IDENTIFIED,
    DOCUMENT_OPEN,
    ENTERABLE_ROLES,
    FAILED_UNKNOWN,
    INTERFACE_DETECTED,
    MAX_CONTROL_NAME_CHARS,
    MAX_LIST_CONTROLS,
    MAX_LIST_DEPTH,
    MAX_READ_TEXT_CHARS,
    READABLE_ROLES,
    ACTION_PERFORMED,
    RESULT_VERIFIED,
    STALE_TARGET,
    STRUCTURAL_ROLES,
    AppSnapshot,
    AmbiguousTargetError,
    AutomationAdapter,
    AutomationError,
    BackendError,
    CapabilityUnavailable,
    ControlInfo,
    ControlLocator,
    ActionResult,
    StaleStateError,
    failure_data,
    safe_app_text,
)

#: Reviewed Save-control accessible names (lower-case, normalized).
#: Anything outside this set is *not* activated for saving — ALWIN does
#: not hunt for "Save\u2026" variants at runtime.
REVIEWED_SAVE_NAMES = frozenset(
    {"save", "save document", "save file", "save the document"}
)

#: Roles that may perform the reviewed save activation.
SAVE_CONTROL_ROLES = frozenset({"button", "toggle button"})

#: How each registered LibreOffice integration identifies its windows
#: and its application node on the accessibility bus.  Identity comes
#: from this reviewed table only — never from application-supplied text.
MODULE_WINDOW_HINTS = {
    "libreoffice-writer": ("libreoffice writer",),
    "libreoffice-calc": ("libreoffice calc",),
}
APP_NAME_HINTS = {
    "libreoffice-writer": (
        "libreoffice writer",
        "libreoffice-writer",
        "swriter",
        "soffice",
        "libreoffice",
    ),
    "libreoffice-calc": (
        "libreoffice calc",
        "libreoffice-calc",
        "scalc",
        "soffice",
        "libreoffice",
    ),
}

_WINDOW_ROLE = frozenset({"frame", "window"})
_UNSAFE_SEARCH_RE = re.compile(r"[\x00-\x1f\x7f]")


def _clean_hint(value: str) -> str:
    return " ".join(_UNSAFE_SEARCH_RE.sub(" ", (value or "")).lower().split())


class LibreOfficeAdapter(AutomationAdapter):
    """AT-SPI-backed adapter for one LibreOffice integration id."""

    def __init__(self, application: str, session: Optional[AtspiSession] = None):
        if application not in MODULE_WINDOW_HINTS:
            raise ValueError(
                f"{application!r} is not a reviewed LibreOffice integration."
            )
        self.application = application
        self.session = session if session is not None else AtspiSession()
        self._app_hints = tuple(_clean_hint(h) for h in APP_NAME_HINTS[application])
        self._window_hints = tuple(
            _clean_hint(h) for h in MODULE_WINDOW_HINTS[application]
        )

    def capabilities(self):
        return frozenset(
            {CAP_INSPECT, CAP_FIND, CAP_READ, CAP_ACTIVATE, CAP_ENTER,
             CAP_SAVE, CAP_LIST}
        )

    # ------------------------------------------------------------------ #
    # Identity: fresh lookup on every call (defence against stale state)
    # ------------------------------------------------------------------ #

    def _find_app(self):
        """Locate the LibreOffice application node (fresh each call)."""
        apps = self.session.applications()
        for app in apps:
            app_name = _clean_hint(self.session.name(app))
            if any(hint in app_name for hint in self._app_hints):
                return app
        return None

    def _module_window(self, app):
        """The top-level window of *this* module, if one is exposed.

        LibreOffice runs Writer and Calc in one process, so the module
        window (title like ``notes.txt - LibreOffice Writer``) is what
        distinguishes them.  Titles are untrusted display data: they are
        only used to *locate* the window, never to approve anything.
        """
        best = None
        try:
            for child in self.session.children(app):
                if self.session.role(child) not in _WINDOW_ROLE:
                    continue
                title = _clean_hint(self.session.name(child))
                if any(hint in title for hint in self._window_hints):
                    return child
                if best is None:
                    best = child
        except BackendError:
            raise
        return best

    def _root(self):
        """Search root: the module window when identified, else the app."""
        app = self._find_app()
        if app is None:
            return None, None
        window = self._module_window(app)
        return app, (window if window is not None else app)

    def _revalidate(self, locator: ControlLocator):
        """Fresh app + control lookup immediately before a mutation.

        v0.17: the same ambiguity and staleness rules as find — more
        than one match aborts (never acts on "probably the first one"),
        and a defunct accessible aborts as stale.
        """
        app, root = self._root()
        if app is None:
            raise AutomationError(
                "the application no longer exposes an accessible interface "
                "(it may have exited); operation aborted, state unknown"
            )
        matches, ambiguous = resolve_candidates(self.session, root, locator)
        if ambiguous:
            raise AmbiguousTargetError(
                ambiguity_error(locator, len(matches))
            )
        node = matches[0] if matches else None
        if node is None:
            wanted = f"role '{locator.role}'"
            if locator.name:
                wanted += f", name '{safe_app_text(locator.name, 60)}'"
            raise AutomationError(
                f"the control ({wanted}) could not be re-located; "
                "operation aborted, state unknown"
            )
        if node_is_defunct(self.session, node):
            raise StaleStateError(
                "the target control is defunct (the application closed "
                "it); operation aborted, state unknown"
            )
        # Identity re-check: the node we are about to touch must still
        # have the requested role and name.
        if normalize_role(self.session.role(node)) != normalize_role(locator.role):
            raise AutomationError(
                "the target control changed identity before the action; "
                "operation aborted, state unknown"
            )
        if locator.name and normalize_name(self.session.name(node)) != normalize_name(
            locator.name
        ):
            raise AutomationError(
                "the target control's accessible name changed before the "
                "action; operation aborted, state unknown"
            )
        return app, root, node

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _control_info(self, node) -> ControlInfo:
        role = normalize_role(self.session.role(node))
        name = self.session.name(node)
        return ControlInfo(
            role=role,
            name=name,
            states=self.session.states(node),
            action_names=self.session.actions(node),
            readable=role in READABLE_ROLES,
            enterable=role in ENTERABLE_ROLES,
        )

    @staticmethod
    def _unavailable(capability: str, exc: Exception) -> ActionResult:
        # v0.19: distinguish the failure kind — a dead bus/app call is
        # 'bus_error'; the application simply not being there (the
        # revalidation refusal) is 'app_missing'.
        category = ("bus_error" if isinstance(exc, BackendError)
                    else "app_missing")
        return ActionResult(
            ok=False,
            state=FAILED_UNKNOWN,
            capability=capability,
            message="",
            error=(
                f"Automation capability '{capability}' could not run: {exc} "
                f"(state unknown)"
            ),
            data=failure_data(capability, category,
                              reason=str(exc)),
        )

    def _guard_role(self, capability: str, locator: ControlLocator,
                    allowed: frozenset, what: str) -> None:
        if normalize_role(locator.role) not in allowed:
            roles = ", ".join(sorted(allowed))
            raise CapabilityUnavailable(
                f"capability '{capability}' does not accept role "
                f"'{safe_app_text(locator.role, 40)}': allowed {what} roles "
                f"are {roles}"
            )

    # ------------------------------------------------------------------ #
    # Read-only operations
    # ------------------------------------------------------------------ #

    def inspect(self) -> AppSnapshot:
        """Read-only: which state can actually be *proven* right now?"""
        app, root = self._root()
        if app is None:
            # Connection works but this application is not on the bus.
            # The caller (tools) already knows whether a process runs;
            # here we can only prove the interface is absent.
            raise BackendError(
                "no accessible interface for this application was found on "
                "the accessibility bus"
            )
        window = None if root is app else root
        document = ""
        reliable = False
        if window is not None:
            title = safe_app_text(self.session.name(window), 120)
            title_clean = _clean_hint(title)
            if any(hint in title_clean for hint in self._window_hints):
                # A module window with a module-matching title: the
                # document *label* is reported, flagged as app-supplied.
                document = title
                reliable = True
        state = DOCUMENT_OPEN if reliable else INTERFACE_DETECTED
        if window is not None:
            detail = (
                "module window identified; document title matched this module"
                if reliable
                else "module window identified; no document title matched "
                "this module (document state unknown)"
            )
        else:
            detail = "application interface detected (no window exposed)"
        return AppSnapshot(
            application=self.application,
            integration=self.application,
            state=state,
            installed=True,
            process_running=True,
            interface_detected=True,
            document=document,
            document_reliable=reliable,
            a11y_available=True,
            capabilities=tuple(sorted(self.capabilities())),
            detail=detail,
        )

    def list_controls(self, locator: Optional[ControlLocator] = None
                      ) -> ActionResult:
        """Read-only: a bounded summary of controls under the window.

        v0.17: at most ``MAX_LIST_CONTROLS`` entries within
        ``MAX_LIST_DEPTH``; roles, sanitized names, states and action
        names only — never document text, never raw accessible
        objects, never an unbounded dump.  When *locator* is given,
        the listing is rooted at the single matching control (still
        ambiguity-checked); otherwise it covers the application
        window.
        """
        try:
            app, root = self._root()
        except (BackendError, AutomationError) as exc:
            return self._unavailable(CAP_LIST, exc)
        if app is None:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_LIST,
                message="",
                error=(
                    "No accessible interface for this application was "
                    "found (state unknown: the application may not be "
                    "running or may not expose accessibility "
                    "information)."
                ),
                data={"category": "app_missing"},
            )
        start = root
        if locator is not None:
            try:
                matches, ambiguous = resolve_candidates(
                    self.session, root, locator
                )
            except BackendError as exc:
                return self._unavailable(CAP_LIST, exc)
            if ambiguous:
                return ActionResult(
                    ok=False,
                    state=AMBIGUOUS_TARGET,
                    capability=CAP_LIST,
                    message="",
                    error=ambiguity_error(locator, len(matches)),
                                        data=failure_data(CAP_LIST, "ambiguous",
                                      state=AMBIGUOUS_TARGET),
                )
            if not matches:
                return ActionResult(
                    ok=False,
                    state=FAILED_UNKNOWN,
                    capability=CAP_LIST,
                    message="",
                    error=(
                        "No control matched the locator; nothing was "
                        "listed (state unknown)."
                    ),
                )
            start = matches[0]
        summaries = []
        truncated = False
        try:
            for node, depth in self.session.walk(start):
                if len(summaries) >= MAX_LIST_CONTROLS:
                    truncated = True
                    break
                if depth == 0:
                    continue  # never list the root itself as a control
                if depth > MAX_LIST_DEPTH:
                    continue
                role = self.session.role(node)
                if not role or role == "application":
                    continue
                # Unnamed layout scaffolding is traversal noise, not a
                # control: it does not count toward the summary cap.
                if role in STRUCTURAL_ROLES and not self.session.name(node):
                    continue
                info = self._control_info(node)
                summaries.append(info.as_dict())
        except BackendError as exc:
            return self._unavailable(CAP_LIST, exc)
        data = {
            "controls": summaries,
            "count": len(summaries),
            "truncated": truncated,
            "state": INTERFACE_DETECTED,
        }
        return ActionResult(
            ok=True,
            state=INTERFACE_DETECTED,
            capability=CAP_LIST,
            message=(
                f"Listed {len(summaries)} control(s)"
                + (" (bounded listing is truncated)" if truncated else "")
                + "."
            ),
            data=data,
        )

    def find_control(self, locator: ControlLocator) -> ActionResult:
        try:
            app, root = self._root()
        except (BackendError, AutomationError) as exc:
            return self._unavailable(CAP_FIND, exc)
        if app is None:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_FIND,
                message="",
                error=(
                    "No accessible interface for this application was found "
                    "(state unknown: the application may not be running or may "
                    "not expose accessibility information)."
                ),
                data={"category": "app_missing"},
            )
        try:
            matches, ambiguous = resolve_candidates(
                self.session, root, locator
            )
        except BackendError as exc:
            return self._unavailable(CAP_FIND, exc)
        if ambiguous:
            return ActionResult(
                ok=False,
                state=AMBIGUOUS_TARGET,
                capability=CAP_FIND,
                message="",
                error=ambiguity_error(locator, len(matches)),
                            data=failure_data(CAP_FIND, "ambiguous",
                              matches=len(matches),
                              state=AMBIGUOUS_TARGET),
            )
        node = matches[0] if matches else None
        if node is not None and node_is_defunct(self.session, node):
            return ActionResult(
                ok=False,
                state=STALE_TARGET,
                capability=CAP_FIND,
                message="",
                error=(
                    "The matching control is defunct (the application "
                    "closed it); nothing was reported as identified."
                ),
                                data=failure_data(CAP_FIND, "stale_target",
                                  state=STALE_TARGET),
            )
        if node is None:
            wanted = f"role '{safe_app_text(locator.role, 40)}'"
            if locator.name:
                wanted += f" name '{safe_app_text(locator.name, MAX_CONTROL_NAME_CHARS)}'"
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_FIND,
                message="",
                error=(
                    f"No control matched {wanted} in "
                    f"{safe_app_text(self.application, 40)} (not found or not "
                    f"exposed through accessibility)."
                ),
            )
        info = self._control_info(node)
        return ActionResult(
            ok=True,
            state=CONTROL_IDENTIFIED,
            capability=CAP_FIND,
            message=(
                f"Control identified: role '{info.role}'"
                + (f", accessible name '{safe_app_text(info.name, 60)}'" if info.name else "")
                + "."
            ),
            control=info,
            data={"control": info.as_dict(), "state": CONTROL_IDENTIFIED},
        )

    def read_control(self, locator: ControlLocator) -> ActionResult:
        try:
            self._guard_role(CAP_READ, locator, READABLE_ROLES, "readable")
            app, root, node = self._revalidate(locator)
        except CapabilityUnavailable as exc:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_READ,
                message="",
                error=str(exc),
                                data=failure_data(CAP_READ, "unsupported"),
            )
        except (BackendError, AutomationError) as exc:
            return self._unavailable(CAP_READ, exc)
        text = self.session.read_text(node)
        if text is None:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_READ,
                message="",
                error=(
                    "The control was identified but exposes no readable text "
                    "through accessibility (state unknown — nothing was read)."
                ),
                                data=failure_data(CAP_READ, "unsupported",
                                  reason="the control exposes no "
                                         "accessible text"),
            )
        truncated = len(text) > MAX_READ_TEXT_CHARS
        stored = text[:MAX_READ_TEXT_CHARS]
        info = self._control_info(node)
        return ActionResult(
            ok=True,
            state=CONTROL_IDENTIFIED,
            capability=CAP_READ,
            message=(
                f"Read {len(text)} characters of accessible text from "
                f"role '{info.role}' (application-provided, untrusted display "
                f"data)."
            ),
            control=info,
            data={
                "text": stored,
                "text_chars": len(text),
                "text_truncated": truncated,
                "state": CONTROL_IDENTIFIED,
            },
        )

    # ------------------------------------------------------------------ #
    # Mutations (all of them revalidate the app and the control first)
    # ------------------------------------------------------------------ #

    def activate_control(self, locator: ControlLocator) -> ActionResult:
        try:
            self._guard_role(
                CAP_ACTIVATE, locator, ACTIVATABLE_ROLES, "activatable"
            )
            app, root, node = self._revalidate(locator)
        except CapabilityUnavailable as exc:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ACTIVATE,
                message="",
                error=str(exc),
                                data=failure_data(CAP_ACTIVATE, "unsupported"),
            )
        except AmbiguousTargetError as exc:
            return ActionResult(
                ok=False,
                state=AMBIGUOUS_TARGET,
                capability=CAP_ACTIVATE,
                message="",
                error=str(exc),
                                data=failure_data(CAP_ACTIVATE, "ambiguous",
                                  state=AMBIGUOUS_TARGET),
            )
        except StaleStateError as exc:
            return ActionResult(
                ok=False,
                state=STALE_TARGET,
                capability=CAP_ACTIVATE,
                message="",
                error=str(exc),
                                data=failure_data(CAP_ACTIVATE, "stale_target",
                                  state=STALE_TARGET),
            )
        except (BackendError, AutomationError) as exc:
            return self._unavailable(CAP_ACTIVATE, exc)

        action_names = self.session.actions(node)
        if not action_names:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ACTIVATE,
                message="",
                error=(
                    "The control was identified but exposes no accessibility "
                    "action (state unknown — nothing was performed)."
                ),
                                data=failure_data(CAP_ACTIVATE, "unsupported"),
            )
        before = self.session.states(node)
        try:
            performed = self.session.do_action(node, 0)
        except BackendError as exc:
            return self._unavailable(CAP_ACTIVATE, exc)
        if not performed:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ACTIVATE,
                message="",
                error="The application refused the action (state unknown).",
            )
        after = self.session.states(node)
        state_changed = bool(after) and bool(before) and after != before
        info = self._control_info(node)
        data = {
            "action_performed": True,
            "revalidated": True,
            "performed_action": safe_app_text(action_names[0], 40),
            "state_changed": state_changed,
            "verified": state_changed,
            "states_before": list(before),
            "states_after": list(after),
            "state": RESULT_VERIFIED if state_changed else ACTION_PERFORMED,
        }
        return ActionResult(
            ok=True,
            state=RESULT_VERIFIED if state_changed else ACTION_PERFORMED,
            capability=CAP_ACTIVATE,
            message=(
                "Control state re-read after activation (result verified)."
                if state_changed
                else (
                    "Action performed on the revalidated control; downstream "
                    "effects are not independently verifiable through "
                    "accessibility."
                )
            ),
            control=info,
            data=data,
        )

    def enter_text(self, locator: ControlLocator, text: str) -> ActionResult:
        try:
            self._guard_role(
                CAP_ENTER,
                locator,
                ENTERABLE_ROLES,
                "text-entry (v0.4 edits input fields only — document bodies "
                "are created with the trusted document tools)",
            )
            app, root, node = self._revalidate(locator)
        except CapabilityUnavailable as exc:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ENTER,
                message="",
                error=str(exc),
                                data=failure_data(CAP_ENTER, "unsupported"),
            )
        except AmbiguousTargetError as exc:
            return ActionResult(
                ok=False,
                state=AMBIGUOUS_TARGET,
                capability=CAP_ENTER,
                message="",
                error=str(exc),
                                data=failure_data(CAP_ENTER, "ambiguous",
                                  state=AMBIGUOUS_TARGET),
            )
        except StaleStateError as exc:
            return ActionResult(
                ok=False,
                state=STALE_TARGET,
                capability=CAP_ENTER,
                message="",
                error=str(exc),
                                data=failure_data(CAP_ENTER, "stale_target",
                                  state=STALE_TARGET),
            )
        except (BackendError, AutomationError) as exc:
            return self._unavailable(CAP_ENTER, exc)

        try:
            accepted = self.session.set_text(node, text)
        except BackendError as exc:
            return self._unavailable(CAP_ENTER, exc)
        if not accepted:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ENTER,
                message="",
                error=(
                    "The input field did not accept the text (state unknown — "
                    "nothing may be claimed)."
                ),
            )
        readback = self.session.read_text(node)
        info = self._control_info(node)
        if readback is None:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ENTER,
                message="",
                error=(
                    "Text was submitted but the field could not be read back; "
                    "the result is unknown and is not reported as verified."
                ),
                control=info,
                data={"action_performed": True, "readback_match": False},
            )
        if readback != text:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ENTER,
                message="",
                error=(
                    "The field reports different text than was entered; the "
                    "result is unknown and is not reported as verified."
                ),
                control=info,
                data={"action_performed": True, "readback_match": False},
            )
        return ActionResult(
            ok=True,
            state=RESULT_VERIFIED,
            capability=CAP_ENTER,
            message=(
                f"Text entered into role '{info.role}' and confirmed by "
                f"reading the field back ({len(readback)} characters)."
            ),
            control=info,
            data={
                "action_performed": True,
                "revalidated": True,
                "readback_match": True,
                "entered_chars": len(readback),
                "state": RESULT_VERIFIED,
            },
        )

    def save_document(self) -> ActionResult:
        """Activate the reviewed Save control — and never claim "saved"."""
        try:
            app, root = self._root()
        except (BackendError, AutomationError) as exc:
            return self._unavailable(CAP_SAVE, exc)
        if app is None or root is None:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_SAVE,
                message="",
                error=(
                    "No accessible LibreOffice window was found (state "
                    "unknown); nothing was saved."
                ),
            )
        if root is app:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_SAVE,
                message="",
                error=(
                    f"No {safe_app_text(self.application, 40)} window is "
                    "identified, so no Save control can be revalidated; "
                    "nothing was saved."
                ),
            )

        save_node = None
        save_name = ""
        try:
            for node, _depth in self.session.walk(root):
                if normalize_role(self.session.role(node)) not in SAVE_CONTROL_ROLES:
                    continue
                name = normalize_name(self.session.name(node))
                if name in REVIEWED_SAVE_NAMES:
                    save_node, save_name = node, self.session.name(node)
                    break
        except BackendError as exc:
            return self._unavailable(CAP_SAVE, exc)
        if save_node is None:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_SAVE,
                message="",
                error=(
                    "No reviewed Save control ("
                    + ", ".join(sorted(REVIEWED_SAVE_NAMES))
                    + ") was found in the application window; ALWIN never "
                    "falls back to keystrokes, so nothing was saved."
                ),
                data={"save_control_found": False},
            )
        # Identity re-check right before activating (fresh walk above,
        # name/role re-read here).
        if normalize_role(self.session.role(save_node)) not in SAVE_CONTROL_ROLES:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_SAVE,
                message="",
                error="The Save control changed before activation; state unknown.",
            )
        try:
            performed = self.session.do_action(save_node, 0)
        except BackendError as exc:
            return self._unavailable(CAP_SAVE, exc)
        if not performed:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_SAVE,
                message="",
                error="The application refused the Save action (state unknown).",
            )
        return ActionResult(
            ok=True,
            state=ACTION_PERFORMED,
            capability=CAP_SAVE,
            message=(
                "Save control activated. This does NOT prove a save: the "
                "target file's digest must change before a save can be "
                "reported."
            ),
            data={
                "action_performed": True,
                "revalidated": True,
                "save_control_found": True,
                "save_control": safe_app_text(save_name, 60),
                "saved_confirmed": False,
                "state": ACTION_PERFORMED,
            },
        )
