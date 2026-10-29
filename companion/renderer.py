"""Character renderer interface (v0.11.0) — the seam to a future 3D renderer.

A renderer knows how to *display* the character and play its
animations; it never decides what the character should do.  The
controller (:class:`companion.states.CharacterController`) owns the
current state and calls into a renderer through this interface; the
trusted task core is never touched by any renderer.

Interface contract (what a future 3D implementation must provide):

* ``show_state(state, expression=...)``  — enter a character state
  (validate membership against :data:`companion.states.ALL_STATES`;
  unknown states raise, so a renderer cannot invent behaviour),
* ``set_expression(expression)``         — change the face without
  changing the state,
* ``attach(widget_or_parent)``           — bind to whatever surface
  the host provides (GTK widget today, a 3D viewport later),
* ``detach()``                           — release everything,
* ``tick(now)``                          — advance animation (the host
  drives the clock; a renderer never starts its own thread),
* ``is_available``                       — honest capability flag: a
  renderer that cannot draw (no display, missing bindings) reports
  False and the companion degrades to text-only presentation.

Guarantees every implementation must uphold (checked by tests):

1. ``show_state``/``set_expression``/``tick`` never raise for ordinary
   states — the controller also wraps every call, but a well-behaved
   renderer should not need it; genuinely broken renderers are
   contained by the controller either way.
2. A renderer executes no operating-system commands, imports no
   execution engine, and has no reference to the task manager.
3. Renderer failures never affect task execution — the renderer is a
   leaf of the dependency graph; nothing in ``ai_core``/``integrations``
   can see it.
4. Rendering is host-driven (GTK draw callback / frame clock).  No
   renderer may create its own threads or timers.

The 2D implementation (:mod:`companion.renderer2d`) draws a lightweight
procedural vector character with cairo; a future ``renderer3d`` would
implement this same interface over a 3D scene, reusing the controller,
the state vocabulary, the task-event adapter, greeting logic, speech
content and companion controls unchanged.
"""

from __future__ import annotations

import abc
from typing import Optional

from companion.states import ALL_STATES, EXPRESSIONS

#: The expression vocabulary renderers must accept.  A renderer may
#: approximate an expression visually, but it may not reject one.
ALL_EXPRESSIONS = frozenset(EXPRESSIONS.values()) | {"neutral"}


class RendererUnavailable(Exception):
    """The renderer cannot run here (no display, missing bindings)."""


class CharacterRenderer(abc.ABC):
    """Abstract renderer: display the character, play its animations."""

    #: Short human name for the status line ("2D vector", "3D", ...).
    name: str = "renderer"

    @property
    @abc.abstractmethod
    def is_available(self) -> bool:
        """Honest capability flag; False = degrade to text-only."""

    # ------------------------------------------------------------------ #

    @abc.abstractmethod
    def show_state(self, state: str, *, expression: Optional[str] = None) -> None:
        """Enter *state* (a member of companion.states.ALL_STATES)."""

    @abc.abstractmethod
    def set_expression(self, expression: str) -> None:
        """Change the face; ``neutral`` is always accepted."""

    @abc.abstractmethod
    def tick(self, now: float) -> None:
        """Advance animation to *now* (seconds, monotonic)."""

    # ------------------------------------------------------------------ #

    def attach(self, target) -> None:
        """Bind to a host surface (GTK widget today; 3D viewport later).

        The default implementation ignores the target so headless
        tests can drive any renderer without a display.
        """

    def detach(self) -> None:
        """Release host resources (default: nothing to release)."""

    def start(self) -> None:
        """Begin host-driven animation (default: nothing to start).

        A renderer that owns a frame timer (e.g. the 2D GTK renderer)
        overrides this to begin redrawing.  The window calls ``stop()``
        whenever the character is hidden so no timers run off-screen.
        """

    def stop(self) -> None:
        """Pause/stop animation and release per-frame resources.

        The default is a no-op so headless renderers need not implement
        it; the 2D renderer overrides it to drop its GLib frame timer.
        """

    # ------------------------------------------------------------------ #
    # Validation helpers for implementations
    # ------------------------------------------------------------------ #

    @staticmethod
    def validate_state(state: str) -> str:
        if state not in ALL_STATES:
            raise ValueError(
                f"Unknown character state {state!r}; renderers may not "
                f"invent states (allowed: {', '.join(ALL_STATES)})."
            )
        return state

    @staticmethod
    def validate_expression(expression: str) -> str:
        if expression not in ALL_EXPRESSIONS:
            # Expressions are presentation-only and forgiving: an
            # unknown one degrades to neutral rather than raising.
            return "neutral"
        return expression


class NullRenderer(CharacterRenderer):
    """Headless renderer: accepts everything, draws nothing.

    Used by automated tests, by ``--smoke``/``--check`` modes and
    whenever GTK is unavailable — the whole companion stack still runs
    (states, adapter, greeting, speech) with zero display dependency.
    """

    name = "null (headless)"

    def __init__(self) -> None:
        self._state = "idle"
        self._expression = "neutral"
        #: Every call is recorded so tests can assert the renderer was
        #: driven correctly (and that state order is truthful).
        self.calls: list = []

    @property
    def is_available(self) -> bool:
        return True  # a null renderer always "works" — it draws nothing

    def show_state(self, state: str, *, expression: Optional[str] = None) -> None:
        self.validate_state(state)
        self._state = state
        self._expression = self.validate_expression(expression or "neutral")
        self.calls.append(("state", state, self._expression))

    def set_expression(self, expression: str) -> None:
        self._expression = self.validate_expression(expression)
        self.calls.append(("expression", self._expression))

    def tick(self, now: float) -> None:
        self.calls.append(("tick", now))

    @property
    def state(self) -> str:
        return self._state


__all__ = [
    "ALL_EXPRESSIONS",
    "CharacterRenderer",
    "NullRenderer",
    "RendererUnavailable",
]
