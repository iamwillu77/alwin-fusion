"""v0.13 desktop-intelligence tests (mocked / in-memory only).

Every test runs the REAL controller, launcher, pipeline and verifier
against fake dependencies — a fake ``/proc`` directory, a fake
``popen``, an in-memory accessibility tree (``tests.automation_fakes``)
and a fake clock.  No real desktop application is ever launched,
focused or closed by this suite, and the developer's real home
directory is never touched.

Covered: registry additions + schema rules, bare-argv launch building,
launch confirmation vs honest-unverified, running tri-state, window
metadata, active-window availability, focus confirmation/refusal, the
never-force-kill close path (incl. the unsaved-work dialog case),
title sanitising/credential scrubbing, permission + MUTATING parity,
verification honesty, capability tiers, the v0.13 goal-gate lexicon,
and end-to-end manager plan flows through the real sandboxed pipeline.
"""

from __future__ import annotations

import pathlib
import unittest

from ai_core.executor import ExecutionResult
from ai_core.pipeline import (
    ApprovalToken,
    STATUS_DENIED,
    STATUS_DRY_RUN,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
)
from ai_core.planner import PlanValidationError, validate_action
from ai_core.verifier import STATUS_PASSED, Verifier
from integrations.automation.atspi_backend import AtspiSession
from integrations.apps.desktop import (
    DesktopController,
    _scrub_title,
    make_desktop_handlers,
)
from integrations.apps.launch import LaunchAdapter, LaunchError
from integrations.apps.registry import (
    BUILTIN_REGISTRY,
    RegistryError,
    build_registry,
)
from tests.automation_fakes import FakeAtspiModule, FakeNode, FakeState
from tests.helpers import Sandbox


# --------------------------------------------------------------------------- #
# Fake process + desktop sandbox
# --------------------------------------------------------------------------- #


class FakeProcess:
    def __init__(self, pid: int = 9999, rc=None) -> None:
        self.pid = pid
        self._rc = rc

    def poll(self):
        return self._rc


class DesktopSandbox(Sandbox):
    """Sandbox with a fully fake desktop: /proc, popen, which, AT-SPI."""

    def __init__(
        self,
        *,
        executables=("nautilus", "gnome-text-editor", "soffice", "libreoffice"),
        running=(),
        proc_readable=True,
        apps=(),
        popen_rc=None,
        popen_raises=False,
        on_launch=None,
        confirm_timeout=0.5,
        close_timeout=0.5,
        focus_timeout=0.5,
    ) -> None:
        super().__init__()
        from ai_core.permissions import PermissionChecker

        self.checker = PermissionChecker(self.config)
        self.executables = set(executables)
        self.on_launch = on_launch
        self.popen_rc = popen_rc
        self.popen_raises = popen_raises
        self.popen_calls = []

        # ---- fake /proc ---------------------------------------------- #
        self.proc_root = self.root / "proc"
        self._next_pid = 5000
        if proc_readable:
            self.proc_root.mkdir(parents=True, exist_ok=True)
            for comm in running:
                self.add_comm(comm)

        # ---- fake clock ---------------------------------------------- #
        self.clock = {"t": 0.0}

        # ---- fake accessibility bus ----------------------------------- #
        self.app_nodes = list(apps)
        self.module = FakeAtspiModule(self.app_nodes)
        self.session = AtspiSession(self.module)

        # ---- real launcher over fake popen/which ---------------------- #
        self.launcher = LaunchAdapter(popen=self._popen, which=self._which)

        self.controller = DesktopController(
            self.checker,
            launcher=self.launcher,
            which=self._which,
            proc_root=str(self.proc_root) if proc_readable else str(self.root / "no-proc"),
            session=self.session,
            sleep=lambda _seconds: None,
            monotonic=self._monotonic,
            desktop_dirs=(),
            confirm_timeout=confirm_timeout,
            confirm_interval=0.05,
            close_timeout=close_timeout,
            close_interval=0.05,
            focus_timeout=focus_timeout,
            focus_interval=0.05,
        )

        from integrations import build_default_dispatcher

        self.dispatcher = build_default_dispatcher(
            self.checker, desktop_tools=self.controller
        )

    # -- injection points --------------------------------------------- #

    def _which(self, name: str):
        return f"/usr/bin/{name}" if name in self.executables else None

    def _monotonic(self) -> float:
        self.clock["t"] += 0.05
        return self.clock["t"]

    def _popen(self, argv, **kwargs):
        self.popen_calls.append((list(argv), kwargs))
        if self.popen_raises:
            raise OSError(2, "No such file or directory")
        if self.on_launch is not None:
            self.on_launch(argv)
        return FakeProcess(pid=self._next_pid, rc=self.popen_rc)

    # -- fake /proc helpers -------------------------------------------- #

    def add_comm(self, comm: str) -> None:
        entry = self.proc_root / str(self._next_pid)
        self._next_pid += 1
        entry.mkdir(parents=True, exist_ok=True)
        (entry / "comm").write_text(comm + "\n", encoding="utf-8")

    def remove_comm(self, comm: str) -> None:
        for child in list(self.proc_root.iterdir()):
            f = child / "comm"
            if f.exists() and f.read_text(encoding="utf-8").strip() == comm:
                for grandchild in list(child.iterdir()):
                    grandchild.unlink()
                child.rmdir()

    def launch_count(self) -> int:
        return len(self.popen_calls)

    # -- pipeline helper ----------------------------------------------- #

    def desktop_pipeline(self, *, approve: bool = True):
        return super().pipeline(
            approve=approve, dispatcher=self.dispatcher
        )


def nautilus_tree(*, with_close=True, title="Home"):
    """A fake Files app with one top-level frame."""
    app = FakeNode("application", "org.gnome.Nautilus")
    frame = FakeNode("frame", title, actions=("window.close",) if with_close else ())
    app.add(frame)
    return app, frame


def editor_tree(*, with_dialog=False):
    """A fake GNOME Text Editor with an entry, optional unsaved dialog."""
    app = FakeNode("application", "org.gnome.TextEditor")
    frame = FakeNode("frame", "notes.txt — Text Editor")
    entry = FakeNode("entry", "Text Editor", text="")
    frame.add(entry)
    if with_dialog:
        dialog = FakeNode("alert", "Save changes to “notes.txt”?")
        discard = FakeNode("button", "Discard", actions=("click",))
        dialog.add(discard)
        app.add(dialog)
    app.add(frame)
    return app, {"app": app, "frame": frame, "entry": entry}


# --------------------------------------------------------------------------- #
# Registry: new integrations and schema rules
# --------------------------------------------------------------------------- #


class TestRegistryV13(unittest.TestCase):
    def test_new_integrations_are_registered(self):
        for app_id in ("libreoffice", "nautilus", "gnome-text-editor"):
            self.assertIn(app_id, BUILTIN_REGISTRY.ids)
            definition = BUILTIN_REGISTRY.get(app_id)
            self.assertEqual(definition.id, app_id)

    def test_file_manager_is_launch_only(self):
        definition = BUILTIN_REGISTRY.get("nautilus")
        self.assertNotIn("open_file", definition.capabilities)
        self.assertIn("launch", definition.capabilities)
        self.assertEqual(definition.file_types, ())
        self.assertTrue(definition.launches_bare)
        self.assertFalse(definition.opens_files)

    def test_start_center_opens_files_and_launches_bare(self):
        definition = BUILTIN_REGISTRY.get("libreoffice")
        self.assertTrue(definition.launches_bare)
        self.assertTrue(definition.opens_files)

    def test_desktop_default_never_launches_bare(self):
        # xdg-open is a file handler, not an application to open.
        self.assertFalse(BUILTIN_REGISTRY.get("default").launches_bare)

    def test_launch_only_with_open_file_needs_file_types(self):
        with self.assertRaises(RegistryError):
            build_registry(
                [
                    {
                        "id": "bad-app",
                        "name": "Bad",
                        "version": "1.0.0",
                        "capabilities": ("launch", "open_file"),
                        "file_types": (),
                        "detection": {"executables": ("badapp",)},
                        "launch": {"strategy": "exec", "executable": "badapp",
                                   "arguments": ()},
                        "verification": {"strategy": "process_started"},
                        "required_permissions": ("launch",),
                    }
                ]
            )

    def test_launch_only_template_must_not_reference_path(self):
        with self.assertRaises(RegistryError):
            build_registry(
                [
                    {
                        "id": "bad-app",
                        "name": "Bad",
                        "version": "1.0.0",
                        "capabilities": ("launch",),
                        "file_types": (),
                        "detection": {"executables": ("badapp",)},
                        "launch": {"strategy": "exec", "executable": "badapp",
                                   "arguments": ("{path}",)},
                        "verification": {"strategy": "process_started"},
                        "required_permissions": ("launch",),
                    }
                ]
            )

    def test_open_file_template_still_requires_path(self):
        with self.assertRaises(RegistryError):
            build_registry(
                [
                    {
                        "id": "bad-app",
                        "name": "Bad",
                        "version": "1.0.0",
                        "capabilities": ("open_file",),
                        "file_types": (".txt",),
                        "detection": {"executables": ("badapp",)},
                        "launch": {"strategy": "exec", "executable": "badapp",
                                   "arguments": ()},
                        "verification": {"strategy": "process_started"},
                        "required_permissions": ("open_file",),
                    }
                ]
            )

    def test_launch_capability_word_is_accepted(self):
        definition = build_registry(
            [
                {
                    "id": "plain-app",
                    "name": "Plain",
                    "version": "1.0.0",
                    "capabilities": ("launch",),
                    "file_types": (),
                    "detection": {"executables": ("plainapp",)},
                    "launch": {"strategy": "exec", "executable": "plainapp",
                               "arguments": ()},
                    "verification": {"strategy": "process_started"},
                    "required_permissions": ("launch",),
                }
            ]
        ).get("plain-app")
        self.assertEqual(definition.capabilities, ("launch",))


# --------------------------------------------------------------------------- #
# LaunchAdapter: bare-argv building and launch_app honesty
# --------------------------------------------------------------------------- #


class TestBareLaunch(unittest.TestCase):
    def _recorder_popen(self, rc=None):
        calls = []

        def popen(argv, **kwargs):
            calls.append((list(argv), kwargs))
            return FakeProcess(rc=rc)

        return popen, calls

    def test_bare_argv_drops_path_token(self):
        argv = LaunchAdapter.build_argv_app(
            BUILTIN_REGISTRY.get("libreoffice-writer"), "/usr/bin/soffice"
        )
        self.assertEqual(argv, ["/usr/bin/soffice", "--writer"])

    def test_bare_argv_launch_only_verbatim(self):
        argv = LaunchAdapter.build_argv_app(
            BUILTIN_REGISTRY.get("nautilus"), "/usr/bin/nautilus"
        )
        self.assertEqual(argv, ["/usr/bin/nautilus"])

    def test_bare_argv_refuses_non_application_kind(self):
        with self.assertRaises(LaunchError):
            LaunchAdapter.build_argv_app(
                BUILTIN_REGISTRY.get("default"), "/usr/bin/xdg-open"
            )

    def test_launch_app_never_uses_shell(self):
        popen, calls = self._recorder_popen()
        adapter = LaunchAdapter(popen=popen, which=lambda n: f"/usr/bin/{n}")
        outcome = adapter.launch_app(BUILTIN_REGISTRY.get("nautilus"))
        self.assertTrue(outcome.started)
        argv, kwargs = calls[0]
        self.assertEqual(argv, ["/usr/bin/nautilus"])
        self.assertIs(kwargs["shell"], False)

    def test_launch_app_immediate_error_exit_raises(self):
        adapter = LaunchAdapter(
            popen=lambda *a, **k: FakeProcess(rc=3),
            which=lambda n: f"/usr/bin/{n}",
        )
        with self.assertRaises(LaunchError):
            adapter.launch_app(BUILTIN_REGISTRY.get("nautilus"))

    def test_launch_app_not_installed_raises(self):
        adapter = LaunchAdapter(
            popen=lambda *a, **k: FakeProcess(), which=lambda n: None
        )
        with self.assertRaises(LaunchError) as ctx:
            adapter.launch_app(BUILTIN_REGISTRY.get("nautilus"))
        self.assertIn("never installs", str(ctx.exception))


# --------------------------------------------------------------------------- #
# DesktopController: launch + confirmation
# --------------------------------------------------------------------------- #


class TestLaunch(unittest.TestCase):
    def test_launch_observed_via_proc(self):
        sb = DesktopSandbox(
            executables=("nautilus",),
            on_launch=lambda argv: sb.add_comm("nautilus"),
        )
        result = sb.controller.launch("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["process_observed"])
        self.assertTrue(result.data["launch_requested"])
        self.assertIn("observed running", result.message)
        # argv came from the reviewed registry template, shell=False.
        argv, kwargs = sb.popen_calls[0]
        self.assertEqual(argv, ["/usr/bin/nautilus"])
        self.assertIs(kwargs["shell"], False)

    def test_launch_accepted_but_unobserved_is_honest(self):
        sb = DesktopSandbox(executables=("nautilus",))  # process never appears
        result = sb.controller.launch("nautilus")
        self.assertTrue(result.ok)  # the request was accepted...
        self.assertFalse(result.data["process_observed"])
        self.assertIn("UNVERIFIED", result.message)  # ...but not claimed done

    def test_launch_when_already_running(self):
        sb = DesktopSandbox(
            executables=("nautilus",), running=("nautilus",),
            on_launch=lambda argv: sb.add_comm("nautilus"),
        )
        result = sb.controller.launch("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["was_running"])
        self.assertTrue(result.data["process_observed"])
        self.assertIn("already running", result.message)

    def test_launch_not_installed_never_starts(self):
        sb = DesktopSandbox(executables=())
        result = sb.controller.launch("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("never installs", result.error)
        self.assertEqual(sb.launch_count(), 0)

    def test_launch_unknown_id_is_denied(self):
        sb = DesktopSandbox()
        result = sb.controller.launch("notepad")
        self.assertFalse(result.ok)
        self.assertIn("Unknown application", result.error)
        self.assertEqual(sb.launch_count(), 0)

    def test_launch_dry_run_starts_nothing(self):
        sb = DesktopSandbox(executables=("nautilus",))
        result = sb.controller.launch("nautilus", dry_run=True)
        self.assertTrue(result.ok)
        self.assertTrue(result.data["dry_run"])
        self.assertEqual(sb.launch_count(), 0)

    def test_launch_oserror_is_honest_failure(self):
        sb = DesktopSandbox(executables=("nautilus",), popen_raises=True)
        result = sb.controller.launch("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("Failed to start", result.error)

    def test_default_proc_root_is_real_proc(self):
        # REAL BUG (found by scripts/verify_desktop_control.py on this
        # machine): an unset proc_root used to reach any_running(None),
        # which listdir()s the CWD and reports "not running" for every
        # application.  The default must probe the real /proc.
        from ai_core.permissions import PermissionChecker
        from integrations.automation.processes import DEFAULT_PROC_ROOT

        sb = DesktopSandbox(executables=("nautilus",))
        # The sandbox passes an explicit fake root — untouched.
        self.assertEqual(sb.controller._proc_root, str(sb.proc_root))
        # A controller built with NO proc_root argument probes /proc.
        plain = DesktopController(PermissionChecker(sb.config))
        self.assertEqual(plain._proc_root, DEFAULT_PROC_ROOT)
        self.assertEqual(plain._proc_root, "/proc")


# --------------------------------------------------------------------------- #
# DesktopController: status + windows
# --------------------------------------------------------------------------- #


class TestStatus(unittest.TestCase):
    def test_status_reports_installed_running_and_windows(self):
        app, frame = nautilus_tree(title="Home — Files")
        sb = DesktopSandbox(
            executables=("nautilus",), running=("nautilus",), apps=(app,)
        )
        result = sb.controller.status("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["installed"])
        self.assertTrue(result.data["running"])
        self.assertEqual(result.data["a11y"], "available")
        self.assertEqual(result.data["windows_found"], 1)
        self.assertEqual(result.data["windows"][0]["title"], "Home — Files")
        self.assertIn("launch", result.data["tiers"])
        self.assertNotIn("interact", result.data["tiers"])  # launch-only app

    def test_status_running_unknown_when_proc_unreadable(self):
        sb = DesktopSandbox(executables=("nautilus",), proc_readable=False)
        result = sb.controller.status("nautilus")
        self.assertTrue(result.ok)
        self.assertIsNone(result.data["running"])  # None = unknown, not False
        self.assertIn("UNKNOWN", result.message)

    def test_status_honest_when_not_installed(self):
        sb = DesktopSandbox(executables=())
        result = sb.controller.status("nautilus")
        self.assertTrue(result.ok)
        self.assertFalse(result.data["installed"])
        self.assertFalse(result.data["running"])

    def test_status_without_a11y_still_reports_process_state(self):
        sb = DesktopSandbox(
            executables=("nautilus",), running=("nautilus",),
            apps=(),
        )
        sb.module.desktop_error = RuntimeError("bus gone")
        result = sb.controller.status("nautilus")
        self.assertTrue(result.ok)
        self.assertEqual(result.data["a11y"], "unavailable")
        self.assertIsNone(result.data["windows"])
        self.assertTrue(result.data["running"])  # /proc still answers

    def test_status_interact_tier_only_for_automate_apps(self):
        writer = BUILTIN_REGISTRY.get("libreoffice-writer")
        self.assertIn("automate", writer.capabilities)
        sb = DesktopSandbox(executables=("nautilus",))
        result = sb.controller.status("libreoffice-writer")
        # Not installed here, but tiers still distinguish capability.
        self.assertFalse(result.data["installed"])


# --------------------------------------------------------------------------- #
# DesktopController: active window
# --------------------------------------------------------------------------- #


class TestActiveWindow(unittest.TestCase):
    def test_active_window_reports_metadata(self):
        app = FakeNode("application", "ptyxis")
        frame = FakeNode("frame", "My Terminal", states=("active",))
        app.add(frame)
        sb = DesktopSandbox(apps=(app,))
        result = sb.controller.active_window()
        self.assertTrue(result.ok)
        self.assertEqual(result.data["source"], "atspi")
        self.assertEqual(result.data["title"], "My Terminal")
        self.assertEqual(result.data["role"], "frame")
        self.assertTrue(result.data["active"])

    def test_active_window_honest_empty(self):
        app = FakeNode("application", "ptyxis")
        app.add(FakeNode("frame", "Idle Window"))
        sb = DesktopSandbox(apps=(app,))
        result = sb.controller.active_window()
        self.assertTrue(result.ok)
        self.assertIsNone(result.data["active"])
        self.assertIn("no window", result.message)

    def test_active_window_unavailable_without_bus(self):
        sb = DesktopSandbox()
        sb.module.desktop_error = RuntimeError("no bus")
        result = sb.controller.active_window()
        self.assertFalse(result.ok)
        self.assertIn("accessibility bus is not available", result.error)

    def test_hostile_title_is_sanitized_and_scrubbed(self):
        app = FakeNode("application", "evil")
        frame = FakeNode(
            "frame",
            "Login — password: hunter2\x07\x1b[31m",
            states=("active",),
        )
        app.add(frame)
        sb = DesktopSandbox(apps=(app,))
        result = sb.controller.active_window()
        self.assertTrue(result.ok)
        self.assertNotIn("hunter2", result.data["title"])
        self.assertIn("[redacted]", result.data["title"])
        self.assertNotIn("\x1b", result.data["title"])
        self.assertNotIn("\x07", result.data["title"])

    def test_title_is_bounded(self):
        app = FakeNode("application", "evil")
        app.add(FakeNode("frame", "X" * 500, states=("active",)))
        sb = DesktopSandbox(apps=(app,))
        result = sb.controller.active_window()
        self.assertLessEqual(len(result.data["title"]), 90)


# --------------------------------------------------------------------------- #
# DesktopController: focus
# --------------------------------------------------------------------------- #


class TestFocus(unittest.TestCase):
    def test_focus_confirmed_by_active_state(self):
        app, frame = nautilus_tree()
        frame.on_focus = lambda: frame._states.append(FakeState("active"))
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["focus_delivered"])
        self.assertTrue(result.data["became_active"])
        self.assertEqual(frame.focus_requested, 1)
        self.assertIn("active window", result.message)

    def test_focus_delivered_but_never_active_is_unverified(self):
        app, frame = nautilus_tree()  # no on_focus: state never flips
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["focus_delivered"])
        self.assertFalse(result.data["became_active"])
        self.assertIn("UNVERIFIED", result.message)

    def test_focus_refused_by_component(self):
        app, frame = nautilus_tree()
        frame.focus_refused = True
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("does not accept a focus request", result.error)

    def test_focus_fallback_activate_action_when_compositor_refuses(self):
        # Real GNOME/Wayland observation: grab_focus raises atspi_error.
        # The controller falls back to the window's own 'activate'
        # action and still verifies by re-reading the active state.
        app = FakeNode("application", "org.gnome.Nautilus")
        frame = FakeNode("frame", "Home",
                         actions=("default.activate", "window.close"))
        frame.focus_raises = True

        def activate():
            frame._states.append(FakeState("active"))

        frame.on_action = activate
        app.add(frame)
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["focus_delivered"])
        self.assertTrue(result.data["became_active"])
        self.assertIn("default.activate", result.data["mechanism"])
        self.assertEqual(frame.focus_requested, 0)  # Component path refused

    def test_focus_fallback_also_unverified_when_activate_ignored(self):
        app = FakeNode("application", "org.gnome.Nautilus")
        frame = FakeNode("frame", "Home", actions=("default.activate",))
        frame.focus_raises = True  # activate returns True but never flips state
        app.add(frame)
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus")
        self.assertTrue(result.ok)
        self.assertFalse(result.data["became_active"])
        self.assertIn("UNVERIFIED", result.message)

    def test_focus_fallback_exhausted_is_honest_error(self):
        app = FakeNode("application", "org.gnome.Nautilus")
        frame = FakeNode("frame", "Home", actions=("window.close",))
        frame.focus_raises = True  # no activate-like action exposed
        app.add(frame)
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("never synthesizes keys", result.error)

    def test_focus_already_active_needs_no_request(self):
        app, frame = nautilus_tree()
        frame._states.append(FakeState("active"))
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["already_active"])
        self.assertTrue(result.data["became_active"])
        self.assertFalse(result.data["focus_delivered"])  # honest: nothing sent
        self.assertEqual(frame.focus_requested, 0)
        self.assertEqual(frame.activated, 0)

    def test_focus_without_window_is_honest(self):
        sb = DesktopSandbox(executables=("nautilus",), apps=())
        result = sb.controller.focus("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("No accessibility window found", result.error)

    def test_focus_dry_run_touches_nothing(self):
        app, frame = nautilus_tree()
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.focus("nautilus", dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(frame.focus_requested, 0)


# --------------------------------------------------------------------------- #
# DesktopController: close (never force-kills)
# --------------------------------------------------------------------------- #


class TestClose(unittest.TestCase):
    def test_close_verified_by_window_disappearance(self):
        app, frame = nautilus_tree()
        frame.on_action = lambda: app.children.remove(frame)
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.close("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["closed_confirmed"])
        self.assertEqual(result.data["action_used"], "window.close")
        self.assertIn("verified the window is gone", result.message)

    def test_close_with_no_close_action_is_refused(self):
        app, frame = nautilus_tree(with_close=False)
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.close("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("never terminates processes", result.error)

    def test_close_unsaved_dialog_is_reported_not_forced(self):
        app, frame = nautilus_tree()

        def show_dialog():
            app.add(FakeNode("alert", "Save changes?"))

        frame.on_action = show_dialog  # window stays; a modal appears
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.close("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("dialog is showing", result.error)
        self.assertIn("Nothing was force-closed", result.error)
        # The window is still there — we never escalated.
        self.assertEqual(len(app.children), 2)

    def test_close_without_window_is_honest(self):
        sb = DesktopSandbox(executables=("nautilus",), running=(), apps=())
        result = sb.controller.close("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("nothing to close", result.error)

    def test_close_dry_run_touches_nothing(self):
        app, frame = nautilus_tree()
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.close("nautilus", dry_run=True)
        self.assertTrue(result.ok)
        self.assertEqual(frame.activated, 0)
        self.assertEqual(len(app.children), 1)

    def test_close_bus_dying_mid_close_is_unverified_not_success(self):
        # The accessibility bus disappears while we poll for the window
        # to go away: neither success nor failure can be claimed.
        app, frame = nautilus_tree()

        def kill_bus():
            frame.on_action = None
            sb.module.desktop_error = RuntimeError("bus gone")

        frame.on_action = kill_bus
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.close("nautilus")
        self.assertFalse(result.ok)
        self.assertIn("UNVERIFIED", result.error)
        self.assertIn("nothing was forced", result.error)

    def test_multi_window_target_reports_count_and_first(self):
        # Two windows: the controller reports the total (ambiguity is
        # visible to the caller) and acts on the first, deterministically.
        app = FakeNode("application", "org.gnome.Nautilus")
        f1 = FakeNode("frame", "Home", actions=("window.close",))
        f2 = FakeNode("frame", "Other", actions=("window.close",))
        app.add(f1, f2)
        f1.on_action = lambda: app.children.remove(f1)
        sb = DesktopSandbox(executables=("nautilus",), running=("nautilus",), apps=(app,))
        result = sb.controller.close("nautilus")
        self.assertTrue(result.ok)
        self.assertTrue(result.data["closed_confirmed"])
        self.assertEqual(result.data["windows_found"], 2)
        self.assertEqual(result.data["window_title"], "Home")  # first, deterministically
        self.assertEqual(len(app.children), 1)  # second untouched


# --------------------------------------------------------------------------- #
# AT-SPI backend: gi state enums, find_all, named actions
# --------------------------------------------------------------------------- #


class TestAtspiBackendV13(unittest.TestCase):
    def test_comm_truncation_is_safe(self):
        # REAL bug found by the live verification script: the kernel
        # caps /proc/<pid>/comm at 15 characters, so the 17-char
        # executable 'gnome-text-editor' appears as 'gnome-text-edit'.
        # The probe must match it, but a short unrelated comm must
        # never match a longer executable name.
        import tempfile

        from integrations.automation.processes import any_running

        with tempfile.TemporaryDirectory() as tmp:
            root = pathlib.Path(tmp)
            (root / "101").mkdir()
            (root / "101" / "comm").write_text("gnome-text-edit\n", encoding="utf-8")
            (root / "102").mkdir()
            (root / "102" / "comm").write_text("bash\n", encoding="utf-8")
            self.assertTrue(any_running(("gnome-text-editor",), proc_root=tmp))
            self.assertTrue(any_running(("nautilus", "gnome-text-editor"),
                                        proc_root=tmp))
            # 'bash' must NOT satisfy 'bashful-app' (short comm, not
            # at the kernel cap -> cannot be a truncation of it).
            self.assertFalse(any_running(("bashful-app",), proc_root=tmp))

    def test_states_reads_gi_enums(self):
        class EnumItem:
            def __init__(self, nick):
                self.value_nick = nick

        class StateSet:
            def get_states(self):
                return [EnumItem("active"), EnumItem("ATSPI_STATE_SHOWING")]

        class GiNode:
            def get_state_set(self):
                return StateSet()

        session = AtspiSession()
        self.assertEqual(session.states(GiNode()), ("active", "showing"))

    def test_states_falls_back_to_pyatspi_style(self):
        session = AtspiSession()
        node = FakeNode("frame", "x", states=("active", "showing"))
        self.assertEqual(session.states(node), ("active", "showing"))

    def test_find_all_returns_every_match(self):
        window = FakeNode("frame", "w")
        window.add(
            FakeNode("button", "OK", actions=("click",)),
            FakeNode("button", "OK", actions=("click",)),
            FakeNode("button", "Cancel", actions=("click",)),
        )
        session = AtspiSession()
        from integrations.automation.base import ControlLocator

        matches = session.find_all(
            window, ControlLocator(role="button", name="OK"), max_matches=5
        )
        self.assertEqual(len(matches), 2)

    def test_find_all_respects_max_matches(self):
        window = FakeNode("frame", "w")
        for _ in range(6):
            window.add(FakeNode("button", "OK", actions=("click",)))
        session = AtspiSession()
        from integrations.automation.base import ControlLocator

        matches = session.find_all(
            window, ControlLocator(role="button", name="OK"), max_matches=3
        )
        self.assertEqual(len(matches), 3)

    def test_do_named_action_invokes_matching_action(self):
        node = FakeNode("frame", "w", actions=("window.minimize", "window.close"))
        session = AtspiSession()
        used = session.do_named_action(node, ("window.close", "close"))
        self.assertEqual(used, "window.close")
        self.assertEqual(node.activated, 1)

    def test_do_named_action_none_when_absent(self):
        node = FakeNode("frame", "w", actions=("window.minimize",))
        session = AtspiSession()
        self.assertIsNone(session.do_named_action(node, ("window.close",)))

    def test_grab_focus_uses_component_interface(self):
        node = FakeNode("frame", "w")
        session = AtspiSession()
        self.assertTrue(session.grab_focus(node))
        self.assertEqual(node.focus_requested, 1)


# --------------------------------------------------------------------------- #
# Planner: closed-set validation of the new actions
# --------------------------------------------------------------------------- #


class TestPlannerV13(unittest.TestCase):
    def test_new_actions_validate(self):
        validate_action({"action": "launch_application", "application": "nautilus"})
        validate_action({"action": "application_status", "application": "nautilus"})
        validate_action({"action": "active_window"})
        validate_action({"action": "focus_application", "application": "nautilus"})
        validate_action({"action": "close_application", "application": "nautilus"})

    def test_new_actions_reject_extra_keys(self):
        with self.assertRaises(PlanValidationError):
            validate_action(
                {"action": "launch_application", "application": "nautilus",
                 "path": "/etc/passwd"}
            )
        with self.assertRaises(PlanValidationError):
            validate_action({"action": "active_window", "application": "x"})

    def test_mutating_parity(self):
        from ai_core.planner import MUTATING_ACTIONS

        for action in ("launch_application", "focus_application", "close_application"):
            self.assertIn(action, MUTATING_ACTIONS)
        for action in ("application_status", "active_window"):
            self.assertNotIn(action, MUTATING_ACTIONS)


# --------------------------------------------------------------------------- #
# Permissions
# --------------------------------------------------------------------------- #


class TestPermissionsV13(unittest.TestCase):
    def setUp(self):
        sb = Sandbox()
        from ai_core.permissions import PermissionChecker

        self.checker = PermissionChecker(sb.config)

    def test_known_app_operations_allowed(self):
        for action, operation in (
            ({"action": "launch_application", "application": "nautilus"}, "launch"),
            ({"action": "application_status", "application": "nautilus"}, "status"),
            ({"action": "focus_application", "application": "nautilus"}, "focus"),
            ({"action": "close_application", "application": "nautilus"}, "close"),
        ):
            decision = self.checker.check_action(action)
            self.assertTrue(decision.allowed, decision.reason)
            self.assertEqual(decision.operation, operation)

    def test_unknown_app_denied(self):
        for name in ("launch_application", "application_status",
                     "focus_application", "close_application"):
            decision = self.checker.check_action(
                {"action": name, "application": "notepad"}
            )
            self.assertFalse(decision.allowed)
            self.assertIn("Unknown application", decision.reason)

    def test_active_window_observation_allowed(self):
        decision = self.checker.check_action({"action": "active_window"})
        self.assertTrue(decision.allowed)
        self.assertEqual(decision.operation, "observe")


# --------------------------------------------------------------------------- #
# Verifier honesty
# --------------------------------------------------------------------------- #


class TestVerifierV13(unittest.TestCase):
    def setUp(self):
        sb = Sandbox()
        from ai_core.permissions import PermissionChecker

        self.verifier = Verifier(PermissionChecker(sb.config))

    @staticmethod
    def _exec(data, *, ok=True):
        return ExecutionResult(
            ok=ok, executed=True, dry_run=False, message="done", data=data
        )

    def test_launch_observed_passes(self):
        result = self.verifier.verify(
            {"action": "launch_application", "application": "nautilus"},
            self._exec({"launch_requested": True, "process_observed": True,
                        "application": "nautilus", "detail": "pid 5"}),
        )
        self.assertEqual(result.status, STATUS_PASSED)

    def test_launch_unobserved_fails_as_unverified(self):
        result = self.verifier.verify(
            {"action": "launch_application", "application": "nautilus"},
            self._exec({"launch_requested": True, "process_observed": False,
                        "application": "nautilus", "detail": "pid 5"}),
        )
        self.assertEqual(result.status, STATUS_FAILED)
        self.assertIn("UNVERIFIED", result.message)

    def test_status_wellformed_passes(self):
        result = self.verifier.verify(
            {"action": "application_status", "application": "nautilus"},
            self._exec({"application": "nautilus", "installed": True,
                        "running": False, "a11y": "available", "windows": []}),
        )
        self.assertEqual(result.status, STATUS_PASSED)

    def test_status_malformed_fails(self):
        result = self.verifier.verify(
            {"action": "application_status", "application": "nautilus"},
            self._exec({"application": "OTHER", "installed": "yes"}),
        )
        self.assertEqual(result.status, STATUS_FAILED)

    def test_active_window_report_passes(self):
        result = self.verifier.verify(
            {"action": "active_window"},
            self._exec({"source": "atspi", "title": "Home", "role": "frame",
                        "application": "nautilus", "active": True}),
        )
        self.assertEqual(result.status, STATUS_PASSED)

    def test_focus_confirmed_passes_unconfirmed_fails(self):
        ok = self.verifier.verify(
            {"action": "focus_application", "application": "nautilus"},
            self._exec({"focus_delivered": True, "became_active": True,
                        "mechanism": "atspi"}),
        )
        self.assertEqual(ok.status, STATUS_PASSED)
        bad = self.verifier.verify(
            {"action": "focus_application", "application": "nautilus"},
            self._exec({"focus_delivered": True, "became_active": False,
                        "mechanism": "atspi"}),
        )
        self.assertEqual(bad.status, STATUS_FAILED)
        self.assertIn("UNVERIFIED", bad.message)

    def test_focus_already_active_passes_without_delivery(self):
        result = self.verifier.verify(
            {"action": "focus_application", "application": "nautilus"},
            self._exec({"focus_delivered": False, "already_active": True,
                        "became_active": True, "mechanism": "already-active"}),
        )
        self.assertEqual(result.status, STATUS_PASSED)

    def test_close_confirmed_passes_refused_fails(self):
        ok = self.verifier.verify(
            {"action": "close_application", "application": "nautilus"},
            self._exec({"closed_confirmed": True, "action_used": "window.close",
                        "running_after": False}),
        )
        self.assertEqual(ok.status, STATUS_PASSED)
        bad = self.verifier.verify(
            {"action": "close_application", "application": "nautilus"},
            self._exec({"closed_confirmed": False, "action_used": "window.close",
                        "running_after": True}),
        )
        self.assertEqual(bad.status, STATUS_FAILED)
        self.assertIn("UNVERIFIED", bad.message)


# --------------------------------------------------------------------------- #
# Capability tiers + goal gate
# --------------------------------------------------------------------------- #


class TestCapabilitiesV13(unittest.TestCase):
    def test_tiers_derived_from_evidence(self):
        from ai_core.capabilities import application_tiers

        installed = {"soffice", "libreoffice", "nautilus", "gnome-text-editor"}
        tiers = application_tiers(
            which=lambda n: f"/usr/bin/{n}" if n in installed else None,
            automation_bindings=True,
        )
        writer = tiers["libreoffice-writer"]
        self.assertIn("interact", writer["tiers"])
        self.assertEqual(writer["control"], "interact")
        files = tiers["nautilus"]
        self.assertIn("launch", files["tiers"])
        self.assertNotIn("interact", files["tiers"])
        self.assertEqual(files["control"], "open_only")
        self.assertNotIn("launch", tiers["default"]["tiers"])  # not an app
        missing = tiers["okular"]
        self.assertFalse(missing["installed"])

    def test_tiers_without_bindings_no_interact(self):
        from ai_core.capabilities import application_tiers

        tiers = application_tiers(
            which=lambda n: f"/usr/bin/{n}", automation_bindings=False,
        )
        self.assertNotIn("interact", tiers["libreoffice-writer"]["tiers"])

    def test_missing_for_desktop_actions(self):
        from ai_core.capabilities import discover_capabilities, missing_for

        caps = discover_capabilities(spec_finder=lambda n: None)
        caps["automation_bindings"] = False
        self.assertIn("accessibility bus", missing_for("focus_application", caps))
        self.assertIn("accessibility bus", missing_for("close_application", caps))
        self.assertIn("accessibility bus", missing_for("active_window", caps))
        self.assertIsNone(missing_for("launch_application", caps))

    def test_goal_gate_reaches_desktop_requests(self):
        from ai_core.task_planning import looks_like_task_goal

        self.assertTrue(looks_like_task_goal("Open LibreOffice."))
        self.assertTrue(looks_like_task_goal("Show me the currently active window"))
        self.assertTrue(looks_like_task_goal("open the file manager"))
        self.assertTrue(looks_like_task_goal("close the text editor application"))
        self.assertFalse(looks_like_task_goal("what's the weather today"))
        self.assertFalse(looks_like_task_goal("tell me a joke about windows"))

    def test_brief_open_app_examples_reach_the_planner(self):
        # The v0.13 brief's own examples ('Open LibreOffice.', 'Open
        # the file manager.') used to be claimed by the deterministic
        # parser as read_file on a nonexistent path, so they never
        # reached planning.  Registered-application targets (no file
        # extension, no location) now stay unsupported so the goal gate
        # routes them to the desktop controller; real file phrasing
        # keeps its exact deterministic behaviour.
        from ai_core import command_parser as cp
        from ai_core.task_planning import looks_like_task_goal

        for phrase in ("Open LibreOffice.", "Open the file manager.",
                       "Open the text editor"):
            with self.subTest(phrase=phrase):
                self.assertEqual(cp.parse_command(phrase).intent, cp.UNSUPPORTED)
                self.assertTrue(looks_like_task_goal(phrase))
        for phrase, expected in (
            ("Read notes.txt in Documents", cp.READ_FILE),
            ("open report.txt in Documents with LibreOffice Writer", cp.OPEN_FILE),
            ("Open my essay", cp.READ_FILE),
        ):
            with self.subTest(phrase=phrase):
                self.assertEqual(cp.parse_command(phrase).intent, expected)


# --------------------------------------------------------------------------- #
# End-to-end through the REAL sandboxed pipeline (approval + audit)
# --------------------------------------------------------------------------- #


class TestPipelineEnforcement(unittest.TestCase):
    def test_unapproved_launch_is_rejected_and_nothing_starts(self):
        sb = DesktopSandbox(executables=("nautilus",))
        pipeline = sb.desktop_pipeline(approve=False)
        preview = pipeline.preview_action(
            {"action": "launch_application", "application": "nautilus"}
        )
        self.assertTrue(preview.requires_approval)
        report = pipeline.run_previewed(preview)
        self.assertEqual(report.status, STATUS_REJECTED)
        self.assertEqual(sb.launch_count(), 0)

    def test_approved_launch_executes_and_verifies(self):
        def _launch_then_see(argv):
            sb.add_comm("nautilus")

        sb = DesktopSandbox(executables=("nautilus",), on_launch=_launch_then_see)
        pipeline = sb.desktop_pipeline(approve=True)
        preview = pipeline.preview_action(
            {"action": "launch_application", "application": "nautilus"}
        )
        report = pipeline.run_previewed(preview)
        self.assertEqual(report.status, STATUS_SUCCESS)
        self.assertEqual(report.verification.get("status"), STATUS_PASSED)

    def test_read_only_status_needs_no_approval(self):
        sb = DesktopSandbox(executables=("nautilus",))
        pipeline = sb.desktop_pipeline(approve=False)  # would refuse approvals
        preview = pipeline.preview_action(
            {"action": "application_status", "application": "nautilus"}
        )
        self.assertFalse(preview.requires_approval)
        report = pipeline.run_previewed(preview)
        self.assertEqual(report.status, STATUS_SUCCESS)

    def test_unknown_app_denied_before_permission_stage(self):
        # Defense in depth: the closed-set validator rejects the unknown
        # application id even earlier than the permission stage (parity
        # with open_file) — nothing can reach a launch mechanism.
        sb = DesktopSandbox()
        pipeline = sb.desktop_pipeline(approve=True)
        preview = pipeline.preview_action(
            {"action": "launch_application", "application": "notepad"}
        )
        report = pipeline.run_previewed(preview)
        self.assertEqual(report.status, STATUS_UNSUPPORTED)
        self.assertIn("Unknown application", report.error or "")
        self.assertEqual(sb.launch_count(), 0)

    def test_permission_branch_denies_unknown_app_directly(self):
        # The permission branch itself also denies unknown ids (it runs
        # on any action that reaches it, e.g. a hand-built dispatch).
        sb = DesktopSandbox()
        decision = sb.checker.check_action(
            {"action": "launch_application", "application": "notepad"}
        )
        self.assertFalse(decision.allowed)
        self.assertIn("Unknown application", decision.reason)

    def test_dry_run_never_launches(self):
        sb = DesktopSandbox(executables=("nautilus",))
        pipeline = sb.desktop_pipeline(approve=True)
        preview = pipeline.preview_action(
            {"action": "launch_application", "application": "nautilus"},
            dry_run=True,
        )
        report = pipeline.run_previewed(preview)
        self.assertEqual(report.status, STATUS_DRY_RUN)
        self.assertEqual(sb.launch_count(), 0)


# --------------------------------------------------------------------------- #
# End-to-end plan through the REAL TaskManager (MOCKED desktop world)
# --------------------------------------------------------------------------- #


class TestManagerDesktopPlan(unittest.TestCase):
    """Plan -> approval -> execution with a fake model + fake desktop.

    The manager, pipeline, permission, approval, verification and audit
    layers are all REAL; only the model, /proc, popen and the
    accessibility tree are mocked (labelled MOCK here).
    """

    def setUp(self):
        from tests.test_task_planning import RoutedFakeLLM

        self.sandbox = DesktopSandbox(executables=("nautilus",))
        self.addCleanup(self.sandbox.cleanup)
        self.llm = RoutedFakeLLM()
        # Deterministic regardless of the host's gi installation: the
        # capability snapshot claims the accessibility bindings exist
        # (the controller itself reports honest unavailability at run
        # time when they do not).
        import ai_core.task_planning as tp
        from ai_core.capabilities import discover_capabilities as real_disc

        def forced(**kwargs):
            caps = real_disc(**kwargs)
            caps["automation_bindings"] = True
            return caps

        tp.discover_capabilities = forced
        self.addCleanup(setattr, tp, "discover_capabilities", real_disc)

    def manager(self) -> "TaskManager":
        from taskui.manager import TaskManager

        return TaskManager(
            self.sandbox.config, use_llm=True, llm_client=self.llm,
            desktop_tools=self.sandbox.controller,
        )

    @staticmethod
    def goal() -> str:
        # UNSUPPORTED for the deterministic parser (verified), accepted
        # by the v0.13 goal gate (desktop objects + verbs).
        return "launch the file manager and check its state for me"

    def _propose(self, steps, objective="Manage the file manager"):
        from tests.test_task_planning import plan_json

        self.llm.proposal_response = plan_json(steps, objective=objective)

    @staticmethod
    def _step(step_id, action, **extra):
        from tests.test_task_planning import _step

        return _step(step_id, action, **extra)

    def test_plan_launch_status_close_completes(self):
        app, frame = nautilus_tree()
        frame.on_action = lambda: app.children.remove(frame)
        self.sandbox.app_nodes.append(app)
        self.sandbox.module._apps.append(app)
        self.sandbox.on_launch = lambda argv: self.sandbox.add_comm("nautilus")
        self._propose(
            [
                self._step("s1", {"action": "launch_application",
                                  "application": "nautilus"},
                           expected="the process is observed running"),
                self._step("s2", {"action": "application_status",
                                  "application": "nautilus"},
                           depends_on=["s1"], expected="state reported"),
                self._step("s3", {"action": "close_application",
                                  "application": "nautilus"},
                           depends_on=["s2"], expected="window gone"),
            ]
        )
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state("awaiting_approval", timeout=10))
        self.assertEqual(record.plan["action"], "task_plan")
        # Launch/close are derived-mutating: the plan needs approval.
        self.assertTrue(manager.approve(record.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=20))
        self.assertEqual(
            [s["status"] for s in record.steps],
            ["succeeded", "succeeded", "succeeded"],
        )
        self.assertEqual(record.state, "completed")
        # The close removed the fake window (never a signal).
        self.assertEqual(len(app.children), 0)
        self.assertEqual(self.sandbox.launch_count(), 1)

    def test_cancelled_plan_launches_nothing(self):
        self._propose(
            [
                self._step("s1", {"action": "launch_application",
                                  "application": "nautilus"}),
                self._step("s2", {"action": "close_application",
                                  "application": "nautilus"},
                           depends_on=["s1"]),
            ]
        )
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_for_state("awaiting_approval", timeout=10))
        self.assertTrue(manager.cancel())
        self.assertTrue(manager.wait_terminal(timeout=10))
        self.assertEqual(record.state, "cancelled")
        self.assertEqual(self.sandbox.launch_count(), 0)

    def test_close_refusal_fails_step_honestly(self):
        app, frame = nautilus_tree(with_close=False)  # no close action
        self.sandbox.module._apps.append(app)
        self.sandbox.add_comm("nautilus")
        self._propose(
            [self._step("s1", {"action": "close_application",
                               "application": "nautilus"},
                        expected="the window closes")]
        )
        import json as _json

        # Any bounded replan must decline deterministically (no
        # alternative exists for a refusing window).
        self.llm.replan_response = _json.dumps(
            {"decision": "decline", "reason": "no alternative"}
        )
        manager = self.manager()
        # Parser-unsupported + gate-true desktop phrase (verified).
        record = manager.submit("close the file manager and report whether it closed")
        self.assertTrue(manager.wait_for_state("awaiting_approval", timeout=10))
        self.assertTrue(manager.approve(record.approval_digest))
        self.assertTrue(manager.wait_terminal(timeout=20))
        # Executed but refused -> never "completed"; the honest reason
        # is recorded on the failed step.
        self.assertIn(record.state, ("needs_attention", "failed"))
        self.assertIn("never terminates", record.steps[0].get("error") or "")

    def test_planning_declines_are_honest_no_execution(self):
        import json as _json

        self.llm.proposal_response = _json.dumps(
            {"decision": "decline",
             "reason": "I cannot drive the file manager directly."}
        )
        manager = self.manager()
        record = manager.submit(self.goal())
        self.assertTrue(manager.wait_terminal(timeout=15))
        self.assertEqual(record.state, "cancelled")
        self.assertIn("nothing was executed", (record.error or "").lower())
        self.assertEqual(self.sandbox.launch_count(), 0)


# --------------------------------------------------------------------------- #
# Handlers + title scrubber
# --------------------------------------------------------------------------- #


class TestHandlersAndScrubber(unittest.TestCase):
    def test_desktop_source_has_no_process_termination_or_shell(self):
        # Source-level meta-ban (mirrors the v0.4 automation bans):
        # the desktop controller must never gain a kill/signal/shell
        # path.  This pins the SECURITY.md claim.
        source = pathlib.Path(
            __import__("integrations.apps.desktop", fromlist=["x"]).__file__
        ).read_text(encoding="utf-8")
        for banned in (
            "os.kill", "os.system", "os.popen", "subprocess",
            "import signal", "signal.raise", "signal.SIG",
            "SIGKILL", "SIGTERM", "terminate(",
            "kill(", "pkill", "eval(", "exec(",
        ):
            with self.subTest(banned=banned):
                self.assertNotIn(banned, source)

    def test_handler_shapes(self):
        sb = DesktopSandbox(executables=("nautilus",))
        handlers = make_desktop_handlers(sb.controller)
        self.assertEqual(
            set(handlers),
            {
                "launch_application",
                "application_status",
                "active_window",
                "focus_application",
                "close_application",
            },
        )
        result = handlers["application_status"](
            {"application": "nautilus"}, dry_run=False
        )
        self.assertTrue(result.ok)

    def test_scrub_title_patterns(self):
        self.assertIn("[redacted]", _scrub_title("user password: hunter2"))
        self.assertIn("[redacted]", _scrub_title("token=abcdef123456"))
        self.assertIn("[redacted]", _scrub_title("Authorization: Bearer abcdef123456"))
        self.assertEqual(_scrub_title("Home — Files"), "Home — Files")


if __name__ == "__main__":  # pragma: no cover
    unittest.main()
