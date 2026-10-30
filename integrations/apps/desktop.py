"""Desktop state awareness and application lifecycle control (v0.13).

Answers the five questions the v0.13 milestone requires — each one
*honestly*, from evidence, for a **registered** application only:

1. **discover** — is it installed?  (PATH + desktop-entry evidence,
   reused from ``discovery.detect_report``),
2. **launch** — start it (argv-only, reviewed template, ``shell=False``)
   and then **re-observe** ``/proc`` to distinguish "the request was
   accepted" from "the application is actually running",
3. **inspect state** — running tri-state (``True``/``False``/``None`` =
   unknown, from ``/proc``) plus bounded window metadata (name, role,
   active flag) from the accessibility bus when it is available,
4. **focus** — deliver a focus request through the accessibility
   Component interface and then re-read the ``active`` state; a
   compositor that ignores the request is reported as such, never as
   success,
5. **close** — invoke the window's own ``window.close`` accessibility
   action and then verify the window actually disappeared.  **No
   signal is ever sent, nothing is ever force-killed**; a dialog
   (e.g. unsaved work) that keeps the window alive is detected and
   reported honestly.

Non-negotiables honoured here:

* nothing in this module executes model-generated text: only a
  validated registry id can select an application, and the argv comes
  from the reviewed registry template,
* no shell, ever; launches are argv lists (``LaunchAdapter``),
* no screenshots, no screen contents, no window *contents* — only
  bounded, credential-redacted window **metadata** (title, role,
  active flag) is ever read, and only when a desktop operation
  actually needs it,
* every wait is bounded (``confirm_timeout``/``close_timeout``/
  ``focus_timeout``) and the controller is synchronous on the task
  worker (the GTK thread is never involved),
* accessibility content is *untrusted data*: titles are sanitized
  (``safe_app_text``), bounded, and scrubbed with the same
  credential redactor the audit layer uses — a hostile window title
  can neither inject instructions nor leak secrets into records.
"""

from __future__ import annotations

import re
import shutil
import time
from pathlib import Path
from typing import Callable, Dict, List, Optional, Sequence, Tuple

from ai_core.audit import redact
from ai_core.permissions import PermissionChecker, pretty_path
from integrations.automation.atspi_backend import AtspiSession
from integrations.automation.base import safe_app_text
from integrations.automation.processes import DEFAULT_PROC_ROOT, any_running
from integrations.apps.discovery import default_desktop_dirs, detect_report
from integrations.apps.launch import LaunchAdapter, LaunchError
from integrations.apps.registry import (
    BUILTIN_REGISTRY,
    IntegrationDefinition,
    IntegrationRegistry,
    RegistryError,
    _normalize_app_token,
)
from integrations.filesystem.tools import ToolResult

# --------------------------------------------------------------------------- #
# Bounds and reviewed name sets
# --------------------------------------------------------------------------- #

#: Bound for a window title we echo into a record or the UI.
WINDOW_TITLE_CHARS = 80
#: Bound on how many windows are listed per application.
MAX_WINDOWS_LISTED = 8
#: Bound on how many applications are scanned on the a11y bus.
MAX_APPS_SCANNED = 40

#: Launch-confirmation poll (request accepted -> process observed).
CONFIRM_TIMEOUT = 4.0
CONFIRM_INTERVAL = 0.25
#: Close verification poll (window.close -> window gone).
CLOSE_TIMEOUT = 6.0
CLOSE_INTERVAL = 0.25
#: Focus confirmation poll (grab_focus -> active state).
FOCUS_TIMEOUT = 2.0
FOCUS_INTERVAL = 0.2

#: Window-level accessibility actions accepted as "close" (reviewed
#: names used by GNOME/mutter and KDE; matching is exact, lowercased).
CLOSE_ACTION_NAMES = ("window.close", "close", "quit")

#: Reviewed window-level accessibility actions accepted as a focus
#: *fallback* when the compositor refuses Component.grab_focus (real
#: observation on GNOME Wayland).  "activate" presents/raises the
#: window where the toolkit supports it; success is still verified by
#: re-reading the active state, never assumed.
ACTIVATE_ACTION_NAMES = ("default.activate", "activate", "present", "raise")

#: Roles that indicate a modal (e.g. unsaved-work) dialog is blocking.
MODAL_ROLES = frozenset({"alert", "dialog"})
#: Top-level window roles.
WINDOW_ROLES = frozenset({"frame", "window", "dialog"})

#: Credential-shaped text that must never be echoed from an untrusted
#: window title into a record, the audit log or the UI.  Deterministic
#: regex only (no model, no network): a title like
#: "Login — password: hunter2" becomes "Login — password: [redacted]".
_CREDENTIAL_TEXT_RE = re.compile(
    r"(?i)\b((?:password|passwd|pwd|secret|token|api[_-]?key|access[_-]?key|"
    r"credential|authorization|auth[_-]?token)s?\s*[:=]\s*)(\S+)"
)
_BEARER_RE = re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9._\-]{8,})")


def _scrub_title(text: str) -> str:
    """Scrub credential-shaped substrings from untrusted free text."""
    text = _CREDENTIAL_TEXT_RE.sub(r"\1[redacted]", text)
    return _BEARER_RE.sub(r"\1[redacted]", text)


def _safe_title(value: object) -> str:
    """Bounded, sanitized, credential-scrubbed window title (untrusted)."""
    return _scrub_title(redact(safe_app_text(value, WINDOW_TITLE_CHARS)))


class DesktopController:
    """Registry-scoped desktop operations: launch, status, focus, close.

    All state comes from injected, boring sources — ``/proc`` for
    processes, the accessibility session for windows — so tests drive
    the whole class without touching a real desktop.
    """

    def __init__(
        self,
        checker: PermissionChecker,
        *,
        registry: IntegrationRegistry = BUILTIN_REGISTRY,
        launcher: Optional[LaunchAdapter] = None,
        which: Optional[Callable[[str], Optional[str]]] = None,
        proc_root: Optional[str] = None,
        comm_names=None,
        session=None,
        sleep: Optional[Callable[[float], None]] = None,
        monotonic: Optional[Callable[[], float]] = None,
        desktop_dirs: Optional[Sequence[Path]] = None,
        confirm_timeout: float = CONFIRM_TIMEOUT,
        confirm_interval: float = CONFIRM_INTERVAL,
        close_timeout: float = CLOSE_TIMEOUT,
        close_interval: float = CLOSE_INTERVAL,
        focus_timeout: float = FOCUS_TIMEOUT,
        focus_interval: float = FOCUS_INTERVAL,
    ) -> None:
        self.checker = checker
        self.registry = registry
        self.launcher = launcher or LaunchAdapter()
        self._which = which if which is not None else shutil.which
        # v0.13 real-bug fix (found by the live verification script):
        # an unset proc_root must resolve to the REAL /proc — passing
        # None through would make the probe listdir(None) (the CWD) and
        # silently report "not running" for every application.
        self._proc_root = (
            proc_root if proc_root is not None else DEFAULT_PROC_ROOT
        )
        self._comm_names = comm_names
        self.session = session if session is not None else AtspiSession()
        self._sleep = sleep if sleep is not None else time.sleep
        self._monotonic = monotonic if monotonic is not None else time.monotonic
        self._desktop_dirs = (
            tuple(desktop_dirs)
            if desktop_dirs is not None
            else default_desktop_dirs(checker.home)
        )
        self.confirm_timeout = float(confirm_timeout)
        self.confirm_interval = float(confirm_interval)
        self.close_timeout = float(close_timeout)
        self.close_interval = float(close_interval)
        self.focus_timeout = float(focus_timeout)
        self.focus_interval = float(focus_interval)

    # ------------------------------------------------------------------ #
    # Trusted resolution and probes
    # ------------------------------------------------------------------ #

    def _pretty(self, path) -> str:
        return pretty_path(path, self.checker.home)

    def _definition(self, application: str) -> Tuple[Optional[IntegrationDefinition],
                                                     Optional[str]]:
        """Resolve the reviewed registry id, or an honest error."""
        try:
            return self.registry.get(application), None
        except RegistryError as exc:
            return None, str(exc)

    def _running(self, definition: IntegrationDefinition) -> Optional[bool]:
        """Tri-state process observation: True/False, None = unknown."""
        return any_running(
            definition.detection.executables,
            proc_root=self._proc_root,
            comm_names=self._comm_names,
        )

    def _a11y_candidates(self, definition: IntegrationDefinition) -> frozenset:
        """Separator/case-blind accessibility-bus names for the app.

        Real AT-SPI names vary by toolkit ('libreoffice',
        'org.gnome.Nautilus', 'nautilus'); the comparison normalizes
        both sides the same way the registry alias resolver does.
        """
        names = set(definition.a11y_names)
        names.add(definition.id)
        names.add(definition.name)
        names.update(definition.detection.executables)
        names.update(definition.detection.desktop_entries)
        return frozenset(
            _normalize_app_token(n) for n in names if n and _normalize_app_token(n)
        )

    # -- accessibility windows ------------------------------------------ #

    def _app_nodes(self) -> Optional[List]:
        """Application nodes on the a11y bus, or None when unavailable."""
        try:
            apps = self.session.applications()
        except Exception:  # noqa: BLE001 - no bindings/bus = unavailable
            return None
        return list(apps)[:MAX_APPS_SCANNED]

    def _windows_for(
        self, definition: IntegrationDefinition
    ) -> Optional[List[Tuple[object, List[object]]]]:
        """``[(app_node, [top-level window nodes])]`` for the app.

        ``None`` = the accessibility bus is unavailable (honest
        unknown); ``[]`` = the bus works but the app exposes no
        window right now.
        """
        apps = self._app_nodes()
        if apps is None:
            return None
        wanted = self._a11y_candidates(definition)
        found: List[Tuple[object, List[object]]] = []
        for app in apps:
            app_name = _normalize_app_token(self.session.name(app) or "")
            if app_name not in wanted:
                continue
            windows = [
                child
                for child in self.session.children(app)
                if self.session.role(child) in WINDOW_ROLES
            ]
            found.append((app, windows[:MAX_WINDOWS_LISTED]))
        return found

    def _window_summary(self, window) -> Dict:
        """Bounded, untrusted-safe metadata for one window."""
        states = self.session.states(window)
        return {
            "title": _safe_title(self.session.name(window)),
            "role": self.session.role(window),
            "active": "active" in states,
        }

    def _has_modal(self, app_node) -> bool:
        """Does the app currently show a dialog/alert (depth-bounded)?"""
        try:
            for child in self.session.children(app_node):
                if self.session.role(child) in MODAL_ROLES:
                    return True
        except Exception:  # noqa: BLE001 - the app may have exited
            return False
        return False

    # ------------------------------------------------------------------ #
    # 2/3. Launch and status
    # ------------------------------------------------------------------ #

    def launch(self, application: str, *, dry_run: bool = False) -> ToolResult:
        """Start a registered application (no file) and re-observe /proc."""
        definition, error = self._definition(application)
        if definition is None:
            return ToolResult(ok=False, message="", error=error)

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

        was_running = self._running(definition)

        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would launch {definition.name} (dry run, no launch "
                    f"performed)"
                ),
                changed=False,
                data={
                    "application": definition.id,
                    "application_name": definition.name,
                    "executable": executable,
                    "was_running": was_running,
                    "dry_run": True,
                },
            )

        try:
            outcome = self.launcher.launch_app(definition)
        except LaunchError as exc:
            return ToolResult(ok=False, message="", error=str(exc))

        # Distinguish "request accepted" from "process observed": poll
        # /proc (bounded) for the application's executables.
        observed = was_running is True
        deadline = self._monotonic() + self.confirm_timeout
        while not observed and self._monotonic() < deadline:
            if self._running(definition) is True:
                observed = True
                break
            self._sleep(self.confirm_interval)

        data = {
            "application": definition.id,
            "application_name": definition.name,
            "executable": outcome.executable,
            "pid": outcome.pid,
            "returncode": outcome.returncode,
            "launch_requested": True,
            "process_observed": observed,
            "was_running": was_running,
            "detail": outcome.detail,
        }
        if observed and was_running is True:
            message = (
                f"{definition.name} was already running; the launch request "
                f"was handed to the running instance and its process remains "
                f"observed."
            )
        elif observed:
            message = (
                f"Launched {definition.name}; the application process is now "
                f"observed running. This confirms the process only — Alwin "
                f"Fusion does not claim to see or control its window."
            )
        else:
            message = (
                f"Launch request accepted for {definition.name} "
                f"({outcome.detail}) but the process was not observed within "
                f"{self.confirm_timeout:.0f}s; startup is UNVERIFIED."
            )
        return ToolResult(ok=True, message=message, changed=True, data=data)

    def status(self, application: str, *, dry_run: bool = False) -> ToolResult:
        """Installed/running/window state for one registered application."""
        definition, error = self._definition(application)
        if definition is None:
            return ToolResult(ok=False, message="", error=error)

        report = detect_report(
            self.registry, which=self._which, desktop_dirs=self._desktop_dirs
        )
        entry = next(
            (i for i in report["integrations"] if i["id"] == definition.id), None
        )
        installed = bool(entry and entry["status"] == "available")
        running = self._running(definition)

        windows: Optional[List[Dict]] = None
        windows_found = 0
        app_windows = self._windows_for(definition)
        if app_windows is not None:
            windows = []
            for _app_node, frames in app_windows:
                for frame in frames:
                    if len(windows) >= MAX_WINDOWS_LISTED:
                        break
                    windows.append(self._window_summary(frame))
            windows_found = len(windows)

        a11y_state = "unavailable" if app_windows is None else "available"
        tiers = [
            "discover",
            *(["launch"] if definition.launches_bare else []),
            "inspect",  # /proc process observation works for every app
        ]
        if a11y_state == "available" and windows_found:
            tiers.append("window_state")
        if "automate" in definition.capabilities:
            tiers.append("interact")
        if "open_file" in definition.capabilities:
            tiers.append("open_file")
        if not ("automate" in definition.capabilities):
            tiers.append("open_only_for_control")

        data = {
            "application": definition.id,
            "application_name": definition.name,
            "installed": installed,
            "install_status": entry["status"] if entry else "detection_failed",
            "running": running,  # True / False / None (unknown)
            "running_source": "/proc",
            "windows": windows,
            "windows_found": windows_found,
            "a11y": a11y_state,
            "tiers": tuple(tiers),
            "evidence": {
                "executables_found": list(entry["executables_found"]) if entry else [],
                "desktop_entries_found": list(entry["desktop_entries_found"]) if entry else [],
            },
        }
        running_text = (
            "running" if running is True
            else "not running" if running is False
            else "running state UNKNOWN (/proc unreadable)"
        )
        message = (
            f"{definition.name}: "
            f"{'installed' if installed else 'NOT installed'}, {running_text}, "
            f"{windows_found} window(s) via accessibility "
            f"({a11y_state})."
        )
        return ToolResult(ok=True, message=message, changed=False, data=data)

    # ------------------------------------------------------------------ #
    # 4. Active window
    # ------------------------------------------------------------------ #

    def active_window(self, *, dry_run: bool = False) -> ToolResult:
        """The currently active window, or an honest 'cannot observe'."""
        apps = self._app_nodes()
        if apps is None:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    "The active window cannot be observed: the accessibility "
                    "bus is not available in this environment. Alwin Fusion "
                    "reports this limitation instead of guessing."
                ),
            )

        for app in apps[:MAX_APPS_SCANNED]:
            try:
                windows = [
                    child
                    for child in self.session.children(app)
                    if self.session.role(child) in WINDOW_ROLES
                ]
            except Exception:  # noqa: BLE001 - app exited mid-scan
                continue
            for window in windows[:MAX_WINDOWS_LISTED]:
                states = self.session.states(window)
                if "active" in states:
                    data = {
                        "source": "atspi",
                        "application": _safe_title(self.session.name(app)),
                        "title": _safe_title(self.session.name(window)),
                        "role": self.session.role(window),
                        "active": True,
                    }
                    return ToolResult(
                        ok=True,
                        message=(
                            f"Active window: '{data['title']}' "
                            f"({data['role']}, application "
                            f"'{data['application']}')."
                        ),
                        changed=False,
                        data=data,
                    )

        return ToolResult(
            ok=True,
            message=(
                "The accessibility bus is available but no window currently "
                "reports the active state."
            ),
            changed=False,
            data={
                "source": "atspi",
                "active": None,
                "detail": "no window reports the active state right now",
            },
        )

    # ------------------------------------------------------------------ #
    # 5. Focus and close
    # ------------------------------------------------------------------ #

    def focus(self, application: str, *, dry_run: bool = False) -> ToolResult:
        """Deliver a focus request to the app's window; verify honestly."""
        definition, error = self._definition(application)
        if definition is None:
            return ToolResult(ok=False, message="", error=error)

        app_windows = self._windows_for(definition)
        if app_windows is None:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"Cannot focus {definition.name}: the accessibility bus "
                    f"is not available in this environment."
                ),
            )
        frames = [f for _app, ws in app_windows for f in ws]
        if not frames:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"No accessibility window found for {definition.name} — "
                    f"it may not be running, or it exposes no accessibility "
                    f"information."
                ),
            )

        frame = frames[0]
        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would request focus for {definition.name} "
                    f"(dry run, nothing performed)"
                ),
                changed=False,
                data={
                    "application": definition.id,
                    "windows_found": len(frames),
                    "dry_run": True,
                },
            )

        # Already active?  Then focus is already delivered — say so
        # honestly instead of issuing a needless request.
        if "active" in self.session.states(frame):
            return ToolResult(
                ok=True,
                message=(
                    f"{definition.name} is already the active window "
                    f"(confirmed by the accessibility active state); no "
                    f"focus request was needed."
                ),
                changed=False,
                data={
                    "application": definition.id,
                    "windows_found": len(frames),
                    "focus_delivered": False,
                    "already_active": True,
                    "became_active": True,
                    "mechanism": "already-active",
                },
            )

        mechanism = "atspi-component-grab_focus"
        try:
            delivered = self.session.grab_focus(frame)
        except Exception:  # noqa: BLE001 - BackendError: this compositor
            # refused the Component request (real observation on GNOME
            # Wayland: 'atspi_error: 1' — cross-application focus is
            # restricted).  Fall back to the window's own 'activate'
            # accessibility action (present/raise), the same
            # reviewed-mechanism pattern close() uses.  Whether the
            # compositor honours it is still verified below — never
            # assumed.
            delivered = False
            try:
                used = self.session.do_named_action(frame, ACTIVATE_ACTION_NAMES)
            except Exception:  # noqa: BLE001 - dead app/bus
                used = None
            if used is not None:
                delivered = True
                mechanism = f"atspi-action-{used}"
        if not delivered:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"The window of {definition.name} does not accept a "
                    f"focus request through the accessibility Component "
                    f"interface or its own activate action; Alwin Fusion "
                    f"never synthesizes keys or pointer events."
                ),
            )

        # Confirm: the window should report the active state shortly.
        became_active = False
        deadline = self._monotonic() + self.focus_timeout
        while self._monotonic() < deadline:
            if "active" in self.session.states(frame):
                became_active = True
                break
            self._sleep(self.focus_interval)

        data = {
            "application": definition.id,
            "windows_found": len(frames),
            "focus_delivered": True,
            "already_active": False,
            "became_active": became_active,
            "mechanism": mechanism,
        }
        if became_active:
            message = (
                f"{definition.name} is now the active window "
                f"(confirmed by the accessibility active state)."
            )
            return ToolResult(ok=True, message=message, changed=True, data=data)
        return ToolResult(
            ok=True,
            message=(
                f"A focus request was delivered to {definition.name} but its "
                f"window did not report the active state within "
                f"{self.focus_timeout:.0f}s (the compositor may restrict "
                f"focus); focus is UNVERIFIED."
            ),
            changed=True,
            data=data,
        )

    def close(self, application: str, *, dry_run: bool = False) -> ToolResult:
        """Close the app's window via its own accessibility action.

        Never a signal, never a force-kill: if the window does not go
        away (e.g. an unsaved-work dialog), that is reported honestly.
        """
        definition, error = self._definition(application)
        if definition is None:
            return ToolResult(ok=False, message="", error=error)

        app_windows = self._windows_for(definition)
        if app_windows is None:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"Cannot close {definition.name}: the accessibility bus "
                    f"is not available in this environment."
                ),
            )
        entries = [(app, f) for app, ws in app_windows for f in ws]
        if not entries:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"No window found for {definition.name} — nothing to "
                    f"close (it may already be closed)."
                ),
            )

        app_node, frame = entries[0]
        title = _safe_title(self.session.name(frame))
        frame_role = self.session.role(frame)
        if dry_run:
            return ToolResult(
                ok=True,
                message=(
                    f"Would close the window '{title}' of {definition.name} "
                    f"(dry run, nothing performed)"
                ),
                changed=False,
                data={
                    "application": definition.id,
                    "window_title": title,
                    "windows_found": len(entries),
                    "dry_run": True,
                },
            )

        used = self.session.do_named_action(frame, CLOSE_ACTION_NAMES)
        if used is None:
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"The window of {definition.name} exposes no close "
                    f"action through accessibility. Alwin Fusion never "
                    f"terminates processes — close it manually."
                ),
            )

        # Verify the targeted window actually disappeared (bounded
        # poll).  The app may legitimately keep OTHER windows open, so
        # the check is against the targeted window's identity
        # (role + title), not "any window remains".
        closed = False
        deadline = self._monotonic() + self.close_timeout
        while self._monotonic() < deadline:
            remaining = self._windows_for(definition)
            if remaining is None:
                # Bus went away mid-close: cannot confirm either way.
                break
            still_open = any(
                self.session.role(w) == frame_role
                and _safe_title(self.session.name(w)) == title
                for _app, ws in remaining
                for w in ws
            )
            if not still_open:
                closed = True
                break
            self._sleep(self.close_interval)

        running_after = self._running(definition)
        data = {
            "application": definition.id,
            "window_title": title,
            "windows_found": len(entries),
            "action_used": used,
            "closed_confirmed": closed,
            "running_after": running_after,
        }
        if closed:
            lingering = (
                " the process is still running in the background"
                if running_after is True
                else ""
            )
            return ToolResult(
                ok=True,
                message=(
                    f"Closed the window '{title}' of {definition.name} via "
                    f"its '{used}' accessibility action and verified the "
                    f"window is gone;{lingering or ' the process also exited'}."
                ),
                changed=True,
                data=data,
            )

        if self._has_modal(app_node):
            return ToolResult(
                ok=False,
                message="",
                error=(
                    f"{definition.name} did not close: a dialog is showing "
                    f"(likely asking about unsaved work). Nothing was "
                    f"force-closed and no signal was sent."
                ),
                data=data,
            )
        return ToolResult(
            ok=False,
            message="",
            error=(
                f"The '{used}' request was delivered to {definition.name} "
                f"but its window is still present after "
                f"{self.close_timeout:.0f}s; the close is UNVERIFIED and "
                f"nothing was forced."
            ),
            data=data,
        )


def make_desktop_handlers(
    controller: DesktopController,
) -> Dict[str, Callable[..., ToolResult]]:
    """Bind desktop operations to the ``handler(action, dry_run=...)`` shape."""
    return {
        "launch_application": lambda action, *, dry_run=False: controller.launch(
            action["application"], dry_run=dry_run
        ),
        "application_status": lambda action, *, dry_run=False: controller.status(
            action["application"], dry_run=dry_run
        ),
        "active_window": lambda action, *, dry_run=False: controller.active_window(
            dry_run=dry_run
        ),
        "focus_application": lambda action, *, dry_run=False: controller.focus(
            action["application"], dry_run=dry_run
        ),
        "close_application": lambda action, *, dry_run=False: controller.close(
            action["application"], dry_run=dry_run
        ),
    }
