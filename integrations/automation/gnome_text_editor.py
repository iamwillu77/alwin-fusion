"""GNOME Text Editor (org.gnome.TextEditor) automation adapter (v0.17).

Built from the REAL accessibility tree observed on the reference host
(see scripts/verify_app_interaction.py): the GTK4 application exposes

* ``application`` 'gnome-text-editor',
* one ``frame`` whose untrusted title looks like
  ``<file> (<dir>) - Text Editor``,
* buttons / toggle buttons / page tabs / labels beneath a chain of
  panels and groupings (depth ~6-10), with ``click`` actions on the
  interactive ones,

but it exposes **no** ``text``/``document frame``/``entry`` role and
**no** Text or EditableText interface for the document body — so
reading or typing the document through AT-SPI is honestly
UNSUPPORTED here.  This adapter therefore supports exactly:
inspect, find, bounded listing, and activation of an identified
control (with post-action state verification when observable).
Nothing is invented: what the interface does not expose is reported
as unavailable, never simulated.

Identity and security follow the reviewed LibreOffice adapter's rules:
application/window identity comes from reviewed name hints (never from
approval decisions), every mutation re-resolves and revalidates the
target fresh (ambiguity and defunct checks included), all
application-provided strings are sanitized display data, and no
keyboard/mouse/shell fallback exists.
"""

from __future__ import annotations

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
    ACTION_PERFORMED,
    CAP_ACTIVATE,
    CAP_FIND,
    CAP_INSPECT,
    CAP_LIST,
    CONTROL_IDENTIFIED,
    ENTERABLE_ROLES,
    FAILED_UNKNOWN,
    INTERFACE_DETECTED,
    MAX_LIST_CONTROLS,
    MAX_LIST_DEPTH,
    READABLE_ROLES,
    RESULT_VERIFIED,
    STALE_TARGET,
    STRUCTURAL_ROLES,
    AppSnapshot,
    AmbiguousTargetError,
    ActionResult,
    AutomationAdapter,
    AutomationError,
    BackendError,
    ControlInfo,
    ControlLocator,
    StaleStateError,
    failure_data,
    safe_app_text,
)

#: Reviewed accessibility-bus identity for the GNOME Text Editor
#: application node and its window.  Titles are untrusted display data
#: used only to LOCATE the window, never to authorize anything.
APP_NAME_HINTS = ("gnome-text-editor", "org.gnome.texteditor")
WINDOW_TITLE_HINTS = ("text editor",)

_WINDOW_ROLE = frozenset({"frame", "window"})


def _clean_hint(value: object) -> str:
    text = value if isinstance(value, str) else ""
    return " ".join(text.replace("\x00", " ").lower().split())


class GnomeTextEditorAdapter(AutomationAdapter):
    """AT-SPI-backed adapter for ``gnome-text-editor``."""

    def __init__(self, application: str, session: Optional[AtspiSession] = None):
        if application != "gnome-text-editor":
            raise ValueError(
                f"{application!r} is not the GNOME Text Editor integration."
            )
        self.application = application
        self.session = session if session is not None else AtspiSession()
        self._app_hints = tuple(_clean_hint(h) for h in APP_NAME_HINTS)
        self._window_hints = tuple(
            _clean_hint(h) for h in WINDOW_TITLE_HINTS
        )

    def capabilities(self):
        # Deliberately NOT CAP_READ / CAP_ENTER / CAP_SAVE: the real
        # tree exposes no document text interface (see module docstring).
        return frozenset(
            {CAP_INSPECT, CAP_FIND, CAP_ACTIVATE, CAP_LIST}
        )

    # ------------------------------------------------------------------ #
    # Identity: fresh lookup on every call
    # ------------------------------------------------------------------ #

    def _find_app(self):
        apps = self.session.applications()
        for app in apps:
            app_name = _clean_hint(self.session.name(app))
            if any(hint in app_name for hint in self._app_hints):
                return app
        return None

    def _editor_window(self, app):
        """The editor frame (untrusted title used only to locate it)."""
        try:
            for child in self.session.children(app):
                if self.session.role(child) not in _WINDOW_ROLE:
                    continue
                title = _clean_hint(self.session.name(child))
                if any(hint in title for hint in self._window_hints):
                    return child
        except BackendError:
            raise
        return None

    def _root(self):
        app = self._find_app()
        if app is None:
            return None, None
        window = self._editor_window(app)
        return app, (window if window is not None else app)

    def _revalidate(self, locator: ControlLocator):
        """Fresh app + control lookup immediately before a mutation."""
        app, root = self._root()
        if app is None:
            raise AutomationError(
                "the application no longer exposes an accessible "
                "interface (it may have exited); operation aborted, "
                "state unknown"
            )
        matches, ambiguous = resolve_candidates(self.session, root, locator)
        if ambiguous:
            raise AmbiguousTargetError(
                ambiguity_error(locator, len(matches))
            )
        node = matches[0] if matches else None
        if node is None:
            raise AutomationError(
                "the target control could not be re-located; operation "
                "aborted, state unknown"
            )
        if node_is_defunct(self.session, node):
            raise StaleStateError(
                "the target control is defunct (the editor closed it); "
                "operation aborted, state unknown"
            )
        if normalize_role(self.session.role(node)) != normalize_role(
            locator.role
        ):
            raise AutomationError(
                "the target control changed identity before the action; "
                "operation aborted, state unknown"
            )
        if locator.name and normalize_name(
            self.session.name(node)
        ) != normalize_name(locator.name):
            raise AutomationError(
                "the target control's accessible name changed before "
                "the action; operation aborted, state unknown"
            )
        return app, root, node

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #

    def _control_info(self, node) -> ControlInfo:
        role = normalize_role(self.session.role(node))
        return ControlInfo(
            role=role,
            name=self.session.name(node),
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
                f"Automation capability '{capability}' could not run: "
                f"{exc} (state unknown)"
            ),
            data=failure_data(capability, category,
                              reason=str(exc)),
        )

    # ------------------------------------------------------------------ #
    # Read-only operations
    # ------------------------------------------------------------------ #

    def inspect(self) -> AppSnapshot:
        try:
            app, root = self._root()
        except BackendError as exc:
            return self._unavailable(CAP_INSPECT, exc)
        if app is None:
            return AppSnapshot(
                application=self.application,
                integration=self.application,
                state="installed_not_running",
                installed=True,
                process_running=False,
                interface_detected=False,
                a11y_available=self.session.connected,
                capabilities=tuple(sorted(self.capabilities())),
                detail=(
                    "The editor is not exposing an accessible interface "
                    "(not running, or accessibility is unavailable)."
                ),
            )
        window_title = ""
        if root is not None and root is not app:
            window_title = safe_app_text(self.session.name(root), 120)
        return AppSnapshot(
            application=self.application,
            integration=self.application,
            state=INTERFACE_DETECTED,
            installed=True,
            process_running=True,
            interface_detected=True,
            document=window_title,
            document_reliable=False,  # title is untrusted display data
            a11y_available=True,
            capabilities=tuple(sorted(self.capabilities())),
            detail=(
                "Editor window detected. The document TEXT is not "
                "exposed through accessibility on this build (no text "
                "interface) — reading or typing the document is "
                "unsupported, not simulated."
            ),
        )

    def list_controls(self, locator: Optional[ControlLocator] = None
                      ) -> ActionResult:
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
                    "found (state unknown)."
                ),
                data={"category": "app_missing"},
            )
        start = root
        if locator is not None:
            matches, ambiguous = resolve_candidates(
                self.session, root, locator
            )
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
                    error="No control matched the locator (state unknown).",
                )
            start = matches[0]
        summaries = []
        truncated = False
        try:
            for node, depth in self.session.walk(start):
                if len(summaries) >= MAX_LIST_CONTROLS:
                    truncated = True
                    break
                if depth == 0 or depth > MAX_LIST_DEPTH:
                    continue
                role = self.session.role(node)
                if not role or role == "application":
                    continue
                # Unnamed layout scaffolding is traversal noise, not a
                # control: it does not count toward the summary cap.
                if role in STRUCTURAL_ROLES and not self.session.name(node):
                    continue
                summaries.append(self._control_info(node).as_dict())
        except BackendError as exc:
            return self._unavailable(CAP_LIST, exc)
        return ActionResult(
            ok=True,
            state=INTERFACE_DETECTED,
            capability=CAP_LIST,
            message=(
                f"Listed {len(summaries)} control(s)"
                + (" (bounded listing is truncated)" if truncated else "")
                + "."
            ),
            data={
                "controls": summaries,
                "count": len(summaries),
                "truncated": truncated,
                "state": INTERFACE_DETECTED,
            },
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
                    "No accessible interface for this application was "
                    "found (state unknown: the application may not be "
                    "running or may not expose accessibility "
                    "information)."
                ),
                data={"category": "app_missing"},
            )
        matches, ambiguous = resolve_candidates(self.session, root, locator)
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
                    "The matching control is defunct; nothing was "
                    "reported as identified."
                ),
                data=failure_data(CAP_FIND, "stale_target",
                                  state=STALE_TARGET),
            )
        if node is None:
            wanted = f"role '{safe_app_text(locator.role, 40)}'"
            if locator.name:
                wanted += (
                    f" name '{safe_app_text(locator.name, 120)}'"
                )
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_FIND,
                message="",
                error=(
                    f"No control matched {wanted} in the editor window "
                    f"(not found or not exposed through accessibility)."
                ),
            )
        info = self._control_info(node)
        return ActionResult(
            ok=True,
            state=CONTROL_IDENTIFIED,
            capability=CAP_FIND,
            message=(
                f"Control identified: role '{info.role}'"
                + (
                    f", accessible name '{safe_app_text(info.name, 60)}'"
                    if info.name
                    else ""
                )
                + "."
            ),
            control=info,
            data={"control": info.as_dict(), "state": CONTROL_IDENTIFIED},
        )

    # ------------------------------------------------------------------ #
    # Mutations (approval-gated upstream; revalidated here)
    # ------------------------------------------------------------------ #

    def activate_control(self, locator: ControlLocator) -> ActionResult:
        if normalize_role(locator.role) not in ACTIVATABLE_ROLES:
            return ActionResult(
                ok=False,
                state=FAILED_UNKNOWN,
                capability=CAP_ACTIVATE,
                message="",
                error=(
                    f"Role '{safe_app_text(locator.role, 40)}' is not "
                    f"activatable in the editor (nothing was performed)."
                ),
                data=failure_data(CAP_ACTIVATE, "unsupported"),
            )
        try:
            app, root, node = self._revalidate(locator)
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
                    "The control was identified but exposes no "
                    "accessibility action (state unknown — nothing was "
                    "performed)."
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
        verified = state_changed
        return ActionResult(
            ok=True,
            state=RESULT_VERIFIED if verified else ACTION_PERFORMED,
            capability=CAP_ACTIVATE,
            message=(
                "Control state re-read after activation (result "
                "verified)."
                if verified
                else (
                    "Action performed on the revalidated control; the "
                    "editor exposes no independent state change to "
                    "verify the outcome (executed, UNVERIFIED)."
                )
            ),
            control=info,
            data={
                "action_performed": True,
                "revalidated": True,
                "performed_action": safe_app_text(action_names[0], 40),
                "state_changed": state_changed,
                "verified": verified,
                "states_before": list(before),
                "states_after": list(after),
                "state": RESULT_VERIFIED if verified else ACTION_PERFORMED,
            },
        )

    # The document body has no accessibility text interface here —
    # these are honest, permanent UNSUPPORTED answers (never faked).
    def read_control(self, locator: ControlLocator) -> ActionResult:
        return ActionResult(
            ok=False,
            state=FAILED_UNKNOWN,
            capability="read_control",
            message="",
            error=(
                "GNOME Text Editor does not expose document text "
                "through accessibility on this build (no text "
                "interface) — reading is unsupported, not simulated."
            ),
            data=failure_data("read_control", "unsupported"),
        )

    def enter_text(self, locator: ControlLocator, text: str) -> ActionResult:
        return ActionResult(
            ok=False,
            state=FAILED_UNKNOWN,
            capability="enter_text",
            message="",
            error=(
                "GNOME Text Editor does not expose an editable text "
                "interface through accessibility on this build — "
                "typing is unsupported, not simulated."
            ),
            data=failure_data("enter_text", "unsupported"),
        )

    def save_document(self) -> ActionResult:
        return ActionResult(
            ok=False,
            state=FAILED_UNKNOWN,
            capability="save_document",
            message="",
            error=(
                "No reviewed save control is exposed through "
                "accessibility for GNOME Text Editor — unsupported."
            ),
            data=failure_data("save_document", "unsupported"),
        )


__all__ = ["GnomeTextEditorAdapter"]
