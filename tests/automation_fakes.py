"""Fake AT-SPI world for the v0.4 automation tests.

Nothing in this module touches the real desktop: the fakes implement
the exact same node surface the real ``AtspiSession`` primitives use
(``get_role_name``/``get_child_at_index``/``do_action``/...), so the
real ``AtspiSession``, ``LibreOfficeAdapter``, ``AutomationTools`` and
dispatcher code runs unmodified against an in-memory tree.

Every dependency of the tools is injected here:

* ``which``      -> a fixed set of "installed" executables,
* ``proc`` root  -> a temporary directory with ``<pid>/comm`` files,
* the session    -> :class:`FakeAtspiModule` behind a real AtspiSession,
* clock/sleep    -> a fake monotonic counter (no real waiting).
"""

from __future__ import annotations

import json
from typing import Callable, Dict, Optional, Sequence, Tuple

from ai_core.permissions import PermissionChecker
from integrations.automation.atspi_backend import AtspiSession
from integrations.automation.base import (
    ACTION_PERFORMED,
    CAP_ACTIVATE,
    CAP_ENTER,
    CAP_FIND,
    CAP_INSPECT,
    CAP_READ,
    CAP_SAVE,
    CONTROL_IDENTIFIED,
    INTERFACE_DETECTED,
    RESULT_VERIFIED,
    ActionResult,
    AppSnapshot,
    AutomationAdapter,
    ControlInfo,
    ControlLocator,
)
from integrations import build_default_dispatcher
from integrations.automation.tools import AutomationTools
from tests.helpers import Sandbox


class FakeState:
    """One accessibility state (``checked``, ``sensitive``, ...)."""

    def __init__(self, name: str) -> None:
        self.name = name


class FakeNode:
    """An in-memory accessible with the GObject-style AT-SPI surface.

    Interfaces are *conditional*, exactly like the real world:

    * ``get_text``/``get_character_count`` exist only when the node has
      text to expose (buttons don't),
    * ``get_n_actions``/``do_action`` exist always but report zero
      actions when the node exposes none,
    * ``set_text_contents`` always exists (so a field that refuses text
      is simulated by raising, which the real session converts into an
      honest failure).
    """

    def __init__(
        self,
        role: str,
        name: str = "",
        *,
        text: Optional[str] = None,
        actions: Sequence[str] = (),
        states: Sequence[str] = (),
    ) -> None:
        self._role = role
        self._name = name
        self.children: list = []
        self._actions = list(actions)
        self._states = [FakeState(s) for s in states]
        self._text = text

        # Behaviour knobs used by tests ------------------------------- #
        self.activated = 0
        self.action_refused = False
        self.action_raises = False
        self.corrupt_set_text = False
        self.on_action: Optional[Callable[[], None]] = None

        if text is not None:
            # Only text-exposing nodes have the text interface.
            self.get_text = self._get_text  # type: ignore[attr-defined]
            self.get_character_count = self._character_count  # type: ignore[attr-defined]

    # -- tree --------------------------------------------------------- #

    def add(self, *nodes: "FakeNode") -> "FakeNode":
        self.children.extend(nodes)
        return self

    # -- AT-SPI surface ---------------------------------------------- #

    def get_role_name(self) -> str:
        return self._role

    def get_name(self) -> str:
        return self._name

    def get_child_count(self) -> int:
        return len(self.children)

    def get_child_at_index(self, index: int):
        if 0 <= index < len(self.children):
            return self.children[index]
        return None

    def get_n_actions(self) -> int:
        return len(self._actions)

    def get_action_name(self, index: int) -> Optional[str]:
        if 0 <= index < len(self._actions):
            return self._actions[index]
        return None

    def do_action(self, index: int = 0) -> bool:
        if self.action_raises:
            raise RuntimeError("the control went away")
        if self.action_refused or not self._actions:
            return False
        self.activated += 1
        if self.on_action is not None:
            self.on_action()
        return True

    def get_states(self):
        return list(self._states)

    def set_text_contents(self, text: str) -> None:
        self._text = text + (" CORRUPTED" if self.corrupt_set_text else "")

    # -- text interface (only present when text is not None) --------- #

    def _get_text(self, start: int, end: int) -> Optional[str]:
        return self._text

    def _character_count(self) -> int:
        return len(self._text or "")


class FakeDesktop:
    def __init__(self, apps) -> None:
        self._apps = list(apps)

    def get_child_count(self) -> int:
        return len(self._apps)

    def get_child_at_index(self, index: int):
        if 0 <= index < len(self._apps):
            return self._apps[index]
        return None


class FakeAtspiModule:
    """Stand-in for ``gi.repository.Atspi`` (desktop enumeration only)."""

    def __init__(self, apps=(), *, desktop_error: Optional[Exception] = None) -> None:
        self._apps = list(apps)
        self.desktop_error = desktop_error
        self.queried = False

    def init(self) -> None:
        pass

    def get_desktop_count(self) -> int:
        self.queried = True
        if self.desktop_error is not None:
            raise self.desktop_error
        return 1

    def get_desktop(self, index: int) -> FakeDesktop:
        if self.desktop_error is not None:
            raise self.desktop_error
        return FakeDesktop(self._apps)


# --------------------------------------------------------------------------- #
# Application trees
# --------------------------------------------------------------------------- #


def build_writer_tree() -> Tuple[FakeNode, Dict[str, FakeNode]]:
    app = FakeNode("application", "LibreOffice Writer")
    window = FakeNode("frame", "notes.txt - LibreOffice Writer")
    save = FakeNode("button", "Save", actions=("click",))
    ok = FakeNode("button", "OK", actions=("click",))
    bold = FakeNode("toggle button", "Bold", actions=("toggle",), states=("checked",))
    label = FakeNode("label", "Title", text="Hello Title")
    entry = FakeNode("entry", "Find", text="")
    status = FakeNode("status bar", "Status", text="Page 1 of 1")
    body = FakeNode("document frame", "", text="Document body text.")
    hostile = FakeNode("button", "Evil\n\x1b[31mName", actions=("click",))
    window.add(save, ok, bold, label, entry, status, body, hostile)
    app.add(window)
    return app, {
        "app": app,
        "window": window,
        "save": save,
        "ok": ok,
        "bold": bold,
        "label": label,
        "entry": entry,
        "status": status,
        "body": body,
        "hostile": hostile,
    }


def build_calc_tree() -> Tuple[FakeNode, Dict[str, FakeNode]]:
    app = FakeNode("application", "LibreOffice Calc")
    window = FakeNode("frame", "sales.xlsx - LibreOffice Calc")
    search = FakeNode("entry", "Search", text="")
    ok = FakeNode("button", "OK", actions=("click",))
    window.add(search, ok)
    app.add(window)
    return app, {"app": app, "window": window, "search": search, "ok": ok}


def build_untitled_writer_tree() -> Tuple[FakeNode, Dict[str, FakeNode]]:
    """A running Writer whose window title does not match the module."""
    app = FakeNode("application", "LibreOffice Writer")
    window = FakeNode("frame", "Untitled 1")
    window.add(FakeNode("button", "Save", actions=("click",)))
    app.add(window)
    return app, {"app": app, "window": window}


# --------------------------------------------------------------------------- #
# Adapters with canned (possibly dishonest) results
# --------------------------------------------------------------------------- #


class LyingAdapter(AutomationAdapter):
    """Reports a *different* control than the validated locator asked for.

    The verification stage must catch this: an adapter that claims
    success while pointing at the wrong control fails verification.
    """

    def __init__(self, application: str) -> None:
        self.application = application

    def capabilities(self):
        return frozenset(
            {CAP_INSPECT, CAP_FIND, CAP_READ, CAP_ACTIVATE, CAP_ENTER, CAP_SAVE}
        )

    def inspect(self) -> AppSnapshot:
        return AppSnapshot(
            application=self.application,
            integration=self.application,
            state=INTERFACE_DETECTED,
            installed=True,
            process_running=True,
            interface_detected=True,
            a11y_available=True,
            capabilities=tuple(sorted(self.capabilities())),
            detail="fake interface",
        )

    def _wrong_control(self, capability: str) -> ActionResult:
        info = ControlInfo(role="label", name="Wrong Control")
        return ActionResult(
            ok=True,
            state=CONTROL_IDENTIFIED,
            capability=capability,
            message="found it",
            control=info,
            data={"control": info.as_dict(), "state": CONTROL_IDENTIFIED},
        )

    def find_control(self, locator: ControlLocator) -> ActionResult:
        return self._wrong_control(CAP_FIND)

    def read_control(self, locator: ControlLocator) -> ActionResult:
        result = self._wrong_control(CAP_READ)
        data = dict(result.data)
        data.update({"text": "lying text", "text_chars": 11, "text_truncated": False})
        return ActionResult(
            ok=True,
            state=CONTROL_IDENTIFIED,
            capability=CAP_READ,
            message="read it",
            control=result.control,
            data=data,
        )

    def activate_control(self, locator: ControlLocator) -> ActionResult:
        return ActionResult(
            ok=True,
            state=RESULT_VERIFIED,
            capability=CAP_ACTIVATE,
            message="pressed",
            control=ControlInfo(role="button", name="Whatever"),
            data={
                "action_performed": True,
                "revalidated": True,
                "state": RESULT_VERIFIED,
            },
        )

    def enter_text(self, locator: ControlLocator, text: str) -> ActionResult:
        return ActionResult(
            ok=True,
            state=RESULT_VERIFIED,
            capability=CAP_ENTER,
            message="typed",
            control=ControlInfo(role="entry", name="Whatever"),
            data={
                "action_performed": True,
                "revalidated": True,
                "readback_match": True,
                "entered_chars": len(text),
                "state": RESULT_VERIFIED,
            },
        )

    def save_document(self) -> ActionResult:
        return ActionResult(
            ok=True,
            state=ACTION_PERFORMED,
            capability=CAP_SAVE,
            message="save pressed",
            data={"action_performed": True, "revalidated": True},
        )


class SwapSession(AtspiSession):
    """Session whose control search always returns a different node."""

    def __init__(self, module, swap_node: FakeNode) -> None:
        super().__init__(module)
        self.swap = swap_node

    def find_in(self, root, locator: ControlLocator):
        return self.swap


# --------------------------------------------------------------------------- #
# The sandbox
# --------------------------------------------------------------------------- #


class AutomationSandbox(Sandbox):
    """Sandbox with a fake desktop: fake /proc, fake which, fake AT-SPI.

    ``apps`` selects the fake application tree(s) on the bus:
    ``"writer"``, ``"calc"``, ``"both"``, ``"untitled"`` or ``None``.
    """

    def __init__(
        self,
        *,
        installed: bool = True,
        running: bool = True,
        proc_readable: bool = True,
        apps: Optional[str] = "writer",
        adapter_factory=None,
        session=None,
        save_timeout: float = 2.0,
    ) -> None:
        super().__init__()
        self.checker = PermissionChecker(self.config)

        # ---- fake /proc ---------------------------------------------- #
        self.proc_root = self.root / "proc"
        if proc_readable:
            self.proc_root.mkdir(parents=True, exist_ok=True)
            if running:
                entry = self.proc_root / "4242"
                entry.mkdir()
                (entry / "comm").write_text("soffice.bin\n", encoding="utf-8")
        # When proc_readable is False the directory is never created:
        # any_running() must then report *unknown*, never "not running".

        # ---- fake accessibility bus ----------------------------------- #
        self.nodes: Dict[str, FakeNode] = {}
        self.module: Optional[FakeAtspiModule] = None
        if apps is not None:
            if apps == "writer":
                tree_app, self.nodes = build_writer_tree()
                app_list = [tree_app]
            elif apps == "calc":
                tree_app, self.nodes = build_calc_tree()
                app_list = [tree_app]
            elif apps == "both":
                writer_app, writer_nodes = build_writer_tree()
                calc_app, calc_nodes = build_calc_tree()
                app_list = [writer_app, calc_app]
                self.nodes = {
                    **{f"writer_{k}": v for k, v in writer_nodes.items()},
                    **{f"calc_{k}": v for k, v in calc_nodes.items()},
                }
            elif apps == "untitled":
                tree_app, self.nodes = build_untitled_writer_tree()
                app_list = [tree_app]
            else:  # pragma: no cover - guard against typos
                raise ValueError(f"unknown fake app set {apps!r}")
            self.module = FakeAtspiModule(app_list)
        else:
            self.module = FakeAtspiModule([])

        self.session = session if session is not None else AtspiSession(self.module)

        # ---- fake probes ---------------------------------------------- #
        self.installed = bool(installed)
        self.clock = {"t": 0.0}

        def fake_which(name: str) -> Optional[str]:
            return f"/usr/bin/{name}" if self.installed else None

        def fake_monotonic() -> float:
            self.clock["t"] += 0.1
            return self.clock["t"]

        self.tools = AutomationTools(
            self.checker,
            session=self.session,
            adapter_factory=adapter_factory,
            which=fake_which,
            proc_root=str(self.proc_root) if proc_readable else str(self.root / "no-proc"),
            sleep=lambda _seconds: None,
            monotonic=fake_monotonic,
            save_timeout=save_timeout,
        )
        self.dispatcher = build_default_dispatcher(
            self.checker, automation_tools=self.tools
        )

    # ------------------------------------------------------------------ #

    def auto_pipeline(self, *, approve: bool = True, **kwargs):
        return super().pipeline(
            approve=approve, dispatcher=self.dispatcher, **kwargs
        )

    def unapproved_auto_pipeline(self, **kwargs):
        from tests.helpers import UnapprovedPipeline

        return UnapprovedPipeline(self.config, dispatcher=self.dispatcher, **kwargs)

    def audit_records(self):
        path = self.config.audit_path
        if not path.exists():
            return []
        return [
            json.loads(line)
            for line in path.read_text(encoding="utf-8").splitlines()
            if line.strip()
        ]
