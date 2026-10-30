"""AT-SPI backend session (v0.4).

A thin, defensive wrapper around the ``Atspi-2.0`` GObject introspection
binding.  The module is imported *lazily* and only when a session is
actually connected, so:

* the core works (and tests run) on machines without ``gi``/AT-SPI,
* a missing backend surfaces as :class:`AtspiUnavailable` — an honest
  "automation unavailable" report, never a fake success.

The session exposes only the primitives the adapters need (walk, read,
act, set text, read states).  Every primitive converts backend
exceptions into :class:`BackendError`, because an application can exit
at any moment and a dead call must become ``failed_or_unknown``.

Security notes:

* bounded walks (``MAX_TREE_NODES``/``MAX_TREE_DEPTH``) keep a hostile
  or cyclic tree from hanging the engine,
* no keyboard events, no mouse events, no shell — the only mutation
  primitives are the accessibility *action* interface (activate an
  already-identified control) and ``EditableText.set_text_contents``
  (an already-identified input field).
"""

from __future__ import annotations

from typing import List, Optional, Tuple

from integrations.automation.base import (
    MAX_TREE_DEPTH,
    MAX_TREE_NODES,
    AutomationError,
    BackendError,
    ControlLocator,
)


class AtspiUnavailable(AutomationError):
    """The AT-SPI binding or bus is not available in this environment."""


def normalize_role(role: str) -> str:
    """Canonical role string: lower-case, single spaces, underscores gone.

    ``toggle_button``, ``Toggle Button`` and ``toggle button`` all
    normalize to ``toggle button`` — the same canonical form the
    planner validates against.
    """
    return " ".join((role or "").replace("_", " ").lower().split())


def normalize_name(name: str) -> str:
    """Canonical accessible name for comparison (case/whitespace blind)."""
    return " ".join((name or "").lower().split())


class AtspiSession:
    """Lazily-connected AT-SPI session.

    ``module`` may inject a stand-in exposing the same symbols
    (``init``, ``get_desktop_count``, ``get_desktop``) for tests.
    """

    def __init__(self, module=None, *, max_nodes: int = MAX_TREE_NODES,
                 max_depth: int = MAX_TREE_DEPTH) -> None:
        self._module = module
        self._connected = module is not None
        self.max_nodes = max_nodes
        self.max_depth = max_depth

    @property
    def connected(self) -> bool:
        return self._connected

    # ------------------------------------------------------------------ #

    def connect(self) -> None:
        """Import and initialize AT-SPI (once).  Raises on failure."""
        if self._connected:
            return
        try:
            import gi  # type: ignore

            gi.require_version("Atspi", "2.0")
            from gi.repository import Atspi as module  # type: ignore
        except Exception as exc:  # noqa: BLE001 - any import issue = unavailable
            raise AtspiUnavailable(
                "the AT-SPI binding (python3-gi + gir1.2-atspi) is not "
                f"available ({type(exc).__name__}: {exc})"
            ) from exc
        try:
            module.init()
        except Exception as exc:  # noqa: BLE001 - bus not reachable
            raise AtspiUnavailable(
                f"the accessibility bus could not be initialized "
                f"({type(exc).__name__}: {exc})"
            ) from exc
        self._module = module
        self._connected = True

    @property
    def module(self):
        self.connect()
        return self._module

    # ------------------------------------------------------------------ #
    # Primitives (the seam adapters and tests rely on)
    # ------------------------------------------------------------------ #

    def applications(self) -> List:
        """Top-level application accessibles currently on the bus."""
        module = self.module
        try:
            apps = []
            for index in range(module.get_desktop_count()):
                desktop = module.get_desktop(index)
                for child_index in range(desktop.get_child_count()):
                    child = desktop.get_child_at_index(child_index)
                    if child is not None:
                        apps.append(child)
            return apps
        except Exception as exc:  # noqa: BLE001 - dead app/bus
            raise BackendError(
                f"the accessibility registry did not respond ({exc})"
            ) from exc

    def children(self, node) -> List:
        try:
            count = node.get_child_count()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"accessible node went away ({exc})") from exc
        out = []
        for index in range(max(0, count)):
            try:
                child = node.get_child_at_index(index)
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"accessible node went away ({exc})") from exc
            if child is not None:
                out.append(child)
        return out

    def role(self, node) -> str:
        try:
            return normalize_role(node.get_role_name() or "")
        except Exception:  # noqa: BLE001 - role is best-effort metadata
            return ""

    def name(self, node) -> str:
        try:
            return node.get_name() or ""
        except Exception:  # noqa: BLE001
            return ""

    def find_in(self, root, locator: ControlLocator):
        """Depth-first search bounded by ``max_nodes``/``max_depth``.

        Returns the first node whose role matches (and whose accessible
        name matches when the locator names one), or ``None``.
        """
        wanted_role = normalize_role(locator.role)
        wanted_name = normalize_name(locator.name)
        visited = 0
        stack: List[Tuple[object, int]] = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > self.max_depth:
                continue
            visited += 1
            if visited > self.max_nodes:
                return None  # bounded: an oversized tree is not searched
            if self.role(node) == wanted_role:
                if not wanted_name or normalize_name(self.name(node)) == wanted_name:
                    return node
            if depth < self.max_depth:
                for child in reversed(self.children(node)):
                    stack.append((child, depth + 1))
        return None

    def find_all(self, root, locator: ControlLocator,
                 *, max_matches: int = 5) -> List:
        """Every node matching *locator*, bounded (v0.13).

        Same bounds as :meth:`find_in`; returns at most *max_matches*
        nodes so a caller can detect an ambiguous locator (several
        controls share the role/name) instead of silently acting on
        the first one.
        """
        wanted_role = normalize_role(locator.role)
        wanted_name = normalize_name(locator.name)
        matches: List = []
        visited = 0
        stack: List[Tuple[object, int]] = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > self.max_depth:
                continue
            visited += 1
            if visited > self.max_nodes:
                break  # bounded: an oversized tree is not searched
            if self.role(node) == wanted_role and (
                not wanted_name
                or normalize_name(self.name(node)) == wanted_name
            ):
                matches.append(node)
                if len(matches) >= max_matches:
                    break
            if depth < self.max_depth:
                for child in reversed(self.children(node)):
                    stack.append((child, depth + 1))
        return matches

    def walk(self, root, *, max_nodes: Optional[int] = None):
        """Yield ``(node, depth)`` pairs with hard bounds (for inspection)."""
        limit = max_nodes or self.max_nodes
        visited = 0
        stack: List[Tuple[object, int]] = [(root, 0)]
        while stack:
            node, depth = stack.pop()
            if depth > self.max_depth:
                continue
            visited += 1
            if visited > limit:
                return
            yield node, depth
            if depth < self.max_depth:
                for child in reversed(self.children(node)):
                    stack.append((child, depth + 1))

    def read_text(self, node) -> Optional[str]:
        """Accessible text of *node*, or ``None`` when not text-exposed."""
        get_text = getattr(node, "get_text", None)
        if get_text is None:
            return None
        try:
            if hasattr(node, "get_character_count"):
                end = node.get_character_count()
                return get_text(0, end)
            return get_text(0, -1)
        except Exception:  # noqa: BLE001 - not a text interface
            return None

    def actions(self, node) -> Tuple[str, ...]:
        """Names of the accessibility actions exposed by *node*."""
        get_n = getattr(node, "get_n_actions", None)
        get_name = getattr(node, "get_action_name", None)
        if get_n is None or get_name is None:
            return ()
        try:
            count = int(get_n())
        except Exception:  # noqa: BLE001
            return ()
        names = []
        for index in range(max(0, count)):
            try:
                names.append(str(get_name(index) or f"action-{index}"))
            except Exception:  # noqa: BLE001
                names.append(f"action-{index}")
        return tuple(names)

    def do_action(self, node, index: int = 0) -> bool:
        """Invoke an accessibility action by index (no keys, no mouse)."""
        do_it = getattr(node, "do_action", None)
        if do_it is None:
            return False
        try:
            return bool(do_it(index))
        except Exception as exc:  # noqa: BLE001 - app exited mid-action
            raise BackendError(f"the control action failed ({exc})") from exc

    def do_named_action(self, node, names) -> Optional[str]:
        """Invoke the first accessibility action named in *names* (v0.13).

        Returns the action name that was invoked, or ``None`` when the
        node exposes none of them.  Used for the window-level lifecycle
        actions (``window.close``) that GNOME/KDE expose on frames —
        still no keys, no mouse, no signals.
        """
        wanted = {str(n).lower() for n in names}
        get_n = getattr(node, "get_n_actions", None)
        get_name = getattr(node, "get_action_name", None)
        if get_n is None or get_name is None:
            return None
        try:
            count = int(get_n())
        except Exception:  # noqa: BLE001
            return None
        for index in range(max(0, count)):
            try:
                name = str(get_name(index) or "")
            except Exception:  # noqa: BLE001
                continue
            if name.lower() in wanted:
                self.do_action(node, index)
                return name
        return None

    def grab_focus(self, node) -> bool:
        """Focus *node* via the accessibility Component interface (v0.13).

        Returns False when the node exposes no component interface;
        raises :class:`BackendError` when the request itself fails (a
        dead application/bus).  No keyboard or mouse events are ever
        synthesized.
        """
        get_component = getattr(node, "get_component_iface", None)
        if get_component is None:
            return False
        try:
            component = get_component()
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"the focus request failed ({exc})") from exc
        if component is None:
            return False
        grab = getattr(component, "grab_focus", None)
        if grab is None:
            return False
        try:
            return bool(grab())
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"the focus request failed ({exc})") from exc

    def set_text(self, node, text: str) -> bool:
        """Replace an input field's contents (``EditableText``)."""
        setter = getattr(node, "set_text_contents", None)
        if setter is None:
            insert = getattr(node, "insert_text", None)
            if insert is None:
                return False
            try:
                insert(0, text, len(text))
                return True
            except Exception as exc:  # noqa: BLE001
                raise BackendError(f"text entry failed ({exc})") from exc
        try:
            setter(text)
            return True
        except Exception as exc:  # noqa: BLE001
            raise BackendError(f"text entry failed ({exc})") from exc

    def states(self, node) -> Tuple[str, ...]:
        """A11y state names (e.g. ``active``, ``defunct``) — used for
        focus/active/liveness checks.

        Handles both binding styles: pyatspi-style objects with
        ``.name``/``get_name()``, and the ``gi`` GObject-introspection
        binding where ``get_state_set()`` yields ``StateType`` enums
        (``value_nick`` like ``'active'``).  The gi path is the real
        one on this machine — the pre-v0.13 implementation returned
        ``()`` there because the enums expose no ``get_name`` (real
        bug found during the v0.13 environment audit).
        """
        getter = getattr(node, "get_state", None)
        states = getattr(node, "get_states", None)
        state_set = getattr(node, "get_state_set", None)
        raw = None
        try:
            if state_set is not None:
                ss = state_set()
                ss_get = getattr(ss, "get_states", None)
                raw = ss_get() if ss_get is not None else ss
            elif states is not None:
                raw = states()
            elif getter is not None:
                raw = getter()
        except Exception:  # noqa: BLE001
            return ()
        if not raw:
            return ()
        out = []
        for item in raw:
            name = None
            nick = getattr(item, "value_nick", None)  # gi StateType enum
            if isinstance(nick, str) and nick:
                name = nick
            else:
                name = getattr(item, "name", None)
                if name is None and hasattr(item, "get_name"):
                    try:
                        name = item.get_name()
                    except Exception:  # noqa: BLE001
                        name = None
            if isinstance(name, str) and name:
                lowered = name.lower()
                if lowered.startswith("atspi_state_"):
                    lowered = lowered[len("atspi_state_"):]
                out.append(lowered)
        return tuple(sorted(set(out)))
