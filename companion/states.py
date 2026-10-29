"""Character state controller for the Alwin companion (v0.8.0).

This module owns the companion character's *current animation state*
and the validated transition graph between states.  It is deliberately
**pure Python** — no GTK, no threads, no I/O, no operating-system
commands — so the whole behaviour is unit-testable headless, and a
future 3D renderer reuses it unchanged.

Design rules:

* every state change goes through :meth:`CharacterController.enter`,
  which validates the transition against :data:`TRANSITIONS`; an
  invalid transition is never silently accepted — it falls back to the
  state's documented safe fallback (normally ``idle``),
* the controller is the *only* writer of the current state; renderers
  and views observe it,
* the controller talks to a renderer through the small
  :class:`companion.renderer.CharacterRenderer` interface only, and
  every renderer call is error-isolated: **an animation exception can
  never propagate** into the task engine or the UI loop,
* presentation states are derived from *trusted task results* by the
  adapter — nothing here can grant a permission, approve a plan, or
  change a task outcome.

The state vocabulary (16 states) covers the companion's life: sleep
and wake-up, the startup greeting, calm idle, and one honest state per
meaningful task stage — listening, thinking, walking (moving to work),
writing, coding, reading, waiting for approval, working, verifying —
plus the two outcome states: celebrating (only ever driven by a
verified success) and error, with resting for a long-quiet user.
"""

from __future__ import annotations

import time
from typing import Callable, Dict, List, Optional

# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #

STATE_SLEEPING = "sleeping"
STATE_WAKING_UP = "waking_up"
STATE_GREETING = "greeting"
STATE_IDLE = "idle"
STATE_LISTENING = "listening"
STATE_THINKING = "thinking"
STATE_WALKING = "walking"
STATE_WRITING = "writing"
STATE_CODING = "coding"
STATE_READING = "reading"
STATE_WAITING_FOR_APPROVAL = "waiting_for_approval"
STATE_WORKING = "working"
STATE_VERIFYING = "verifying"
STATE_CELEBRATING = "celebrating"
STATE_ERROR = "error"
STATE_RESTING = "resting"

ALL_STATES = (
    STATE_SLEEPING,
    STATE_WAKING_UP,
    STATE_GREETING,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_THINKING,
    STATE_WALKING,
    STATE_WRITING,
    STATE_CODING,
    STATE_READING,
    STATE_WAITING_FOR_APPROVAL,
    STATE_WORKING,
    STATE_VERIFYING,
    STATE_CELEBRATING,
    STATE_ERROR,
    STATE_RESTING,
)

#: The state the companion returns to when it has nothing to say.
FALLBACK_STATE = STATE_IDLE

#: The only legal character-state graph.  Anything not listed is an
#: invalid transition and is handled by the safe fallback.
TRANSITIONS: Dict[str, frozenset] = {
    STATE_SLEEPING: frozenset({STATE_WAKING_UP, STATE_IDLE}),
    STATE_WAKING_UP: frozenset(
        # v0.8: waking_up may flow into walking (the morning walk from
        # the bed to the greeting position) before settling on greeting.
        {STATE_GREETING, STATE_IDLE, STATE_ERROR, STATE_WALKING}
    ),
    STATE_GREETING: frozenset(
        {STATE_IDLE, STATE_LISTENING, STATE_THINKING, STATE_SLEEPING}
    ),
    STATE_IDLE: frozenset(
        {
            STATE_LISTENING,
            STATE_THINKING,
            STATE_SLEEPING,
            STATE_RESTING,
            STATE_WALKING,
            STATE_WORKING,
            STATE_WRITING,
            STATE_CODING,
            STATE_READING,
            STATE_WAITING_FOR_APPROVAL,
            STATE_WAKING_UP,
            # Outcome edges exist so a trusted result is never lost to
            # a fallback (e.g. the companion attached mid-task, or an
            # intermediate event was missed).  They are only ever
            # driven by the adapter, which reaches them solely from
            # the trusted lifecycle's completed/failed states.
            STATE_CELEBRATING,
            STATE_ERROR,
        }
    ),
    STATE_LISTENING: frozenset(
        {STATE_THINKING, STATE_IDLE, STATE_WAITING_FOR_APPROVAL, STATE_ERROR}
    ),
    STATE_THINKING: frozenset(
        {
            STATE_WAITING_FOR_APPROVAL,
            STATE_WALKING,
            STATE_WORKING,
            STATE_WRITING,
            STATE_CODING,
            STATE_READING,
            STATE_VERIFYING,
            STATE_IDLE,
            STATE_ERROR,
            # Robustness (see idle's note): a trusted success is never
            # lost to a fallback if an intermediate event was missed.
            STATE_CELEBRATING,
        }
    ),
    # "walking" is a short 2D transition (moving to the work), never a
    # claim of a literal 3D walk through a room.  v0.8: the morning walk
    # from the bed also resolves into greeting on arrival.
    STATE_WALKING: frozenset(
        {STATE_WORKING, STATE_WRITING, STATE_CODING, STATE_READING,
         STATE_VERIFYING, STATE_IDLE, STATE_ERROR, STATE_GREETING,
         STATE_CELEBRATING,  # robustness: fast trusted success
         STATE_WAITING_FOR_APPROVAL}
    ),
    STATE_WORKING: frozenset(
        {
            STATE_VERIFYING,
            STATE_WRITING,
            STATE_CODING,
            STATE_READING,
            STATE_WAITING_FOR_APPROVAL,
            STATE_CELEBRATING,
            STATE_IDLE,
            STATE_ERROR,
        }
    ),
    STATE_WRITING: frozenset(
        {STATE_VERIFYING, STATE_WORKING, STATE_CODING, STATE_CELEBRATING,
         STATE_IDLE, STATE_ERROR}
    ),
    STATE_CODING: frozenset(
        {STATE_VERIFYING, STATE_WORKING, STATE_WRITING, STATE_CELEBRATING,
         STATE_IDLE, STATE_ERROR}
    ),
    STATE_READING: frozenset(
        {STATE_VERIFYING, STATE_WORKING, STATE_THINKING, STATE_IDLE,
         STATE_ERROR}
    ),
    # A rejected plan is a calm idle, never an error and never a success.
    STATE_WAITING_FOR_APPROVAL: frozenset(
        {STATE_WALKING, STATE_WORKING, STATE_WRITING, STATE_CODING,
         STATE_READING, STATE_VERIFYING, STATE_IDLE, STATE_THINKING,
         STATE_ERROR}
    ),
    STATE_VERIFYING: frozenset(
        {STATE_CELEBRATING, STATE_IDLE, STATE_ERROR, STATE_RESTING}
    ),
    # Both outcome states always resolve calmly afterwards.
    STATE_CELEBRATING: frozenset(
        {STATE_IDLE, STATE_LISTENING, STATE_THINKING, STATE_SLEEPING}
    ),
    STATE_ERROR: frozenset(
        {STATE_IDLE, STATE_LISTENING, STATE_THINKING, STATE_RESTING,
         STATE_SLEEPING}
    ),
    STATE_RESTING: frozenset(
        {STATE_IDLE, STATE_WAKING_UP, STATE_LISTENING, STATE_THINKING,
         STATE_SLEEPING}
    ),
}

#: Human labels for the accessible text alternative (subtitles).
STATE_LABELS: Dict[str, str] = {
    STATE_SLEEPING: "sleeping",
    STATE_WAKING_UP: "waking up",
    STATE_GREETING: "greeting you",
    STATE_IDLE: "idle",
    STATE_LISTENING: "listening",
    STATE_THINKING: "thinking",
    STATE_WALKING: "heading over",
    STATE_WRITING: "writing",
    STATE_CODING: "coding",
    STATE_READING: "reading",
    STATE_WAITING_FOR_APPROVAL: "waiting for your approval",
    STATE_WORKING: "working",
    STATE_VERIFYING: "verifying the result",
    STATE_CELEBRATING: "celebrating a verified success",
    STATE_ERROR: "encountered a problem",
    STATE_RESTING: "resting",
}

#: Facial expression per state (renderers map these to artwork).
EXPRESSIONS: Dict[str, str] = {
    STATE_SLEEPING: "sleepy",
    STATE_WAKING_UP: "surprised",
    STATE_GREETING: "happy",
    STATE_IDLE: "neutral",
    STATE_LISTENING: "attentive",
    STATE_THINKING: "thoughtful",
    STATE_WALKING: "determined",
    STATE_WRITING: "focused",
    STATE_CODING: "focused",
    STATE_READING: "focused",
    STATE_WAITING_FOR_APPROVAL: "hopeful",
    STATE_WORKING: "focused",
    STATE_VERIFYING: "attentive",
    STATE_CELEBRATING: "proud",
    STATE_ERROR: "worried",
    STATE_RESTING: "sleepy",
}

#: How long the two outcome states hold before the presentation layer
#: returns the companion to a calm state (seconds).  ``None`` = holds
#: until something else happens.
AUTO_RETURN_DELAYS: Dict[str, Optional[float]] = {
    STATE_CELEBRATING: 4.0,
    STATE_ERROR: 6.0,
    STATE_GREETING: 6.0,
    STATE_WAKING_UP: 2.0,
}

#: States where the character animates continuously (the renderer may
#: idle the frame timer in every other state to save CPU).  ``idle``
#: counts as animated in v0.7 — its subtle breathing and blinking are
#: the character's resting life and run at a slower frame cadence.
ANIMATED_STATES = frozenset(
    {
        STATE_IDLE, STATE_WAKING_UP, STATE_GREETING, STATE_LISTENING,
        STATE_THINKING, STATE_WALKING, STATE_WRITING, STATE_CODING,
        STATE_READING, STATE_WAITING_FOR_APPROVAL, STATE_WORKING,
        STATE_VERIFYING, STATE_CELEBRATING, STATE_ERROR,
    }
)


class InvalidCharacterState(Exception):
    """A state or transition the character graph does not allow."""


def is_valid_transition(source: str, target: str) -> bool:
    """True when *source* -> *target* is an edge of the state graph."""
    if source not in TRANSITIONS or target not in TRANSITIONS:
        return False
    return target in TRANSITIONS[source]


# --------------------------------------------------------------------------- #
# Controller
# --------------------------------------------------------------------------- #

class CharacterController:
    """Validated owner of the character's current state.

    The controller:

    * validates every transition and applies the documented safe
      fallback when a transition is invalid (never raises into the
      caller for ordinary flow — presentation must not break tasks),
    * dispatches state/expression changes to a renderer through the
      :class:`~companion.renderer.CharacterRenderer` interface, with
      every renderer call wrapped so **an animation bug can never
      escape** (it is recorded in ``renderer_errors`` instead),
    * reports an optional ``on_change`` callback (used by the window
      to schedule auto-return timers and refresh the view),
    * exposes ``snapshot()`` — everything a view needs, with no GTK.
    """

    def __init__(
        self,
        renderer=None,
        *,
        on_change: Optional[Callable[[str, str, str], None]] = None,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        #: The renderer (any CharacterRenderer); None = headless use.
        self.renderer = renderer
        #: Optional (state, expression, reason) callback after each change.
        self.on_change = on_change
        #: Injectable clock for deterministic tests.
        self._clock = clock

        self._state: str = STATE_IDLE
        self._expression: str = EXPRESSIONS[STATE_IDLE]
        self._reason: str = "starting"
        self._entered_at: float = clock()
        #: Recent renderer failures (bounded); surfaced, never hidden.
        self.renderer_errors: List[str] = []

    # ------------------------------------------------------------------ #
    # State access
    # ------------------------------------------------------------------ #

    @property
    def state(self) -> str:
        return self._state

    @property
    def expression(self) -> str:
        return self._expression

    @property
    def reason(self) -> str:
        """Why the character is in its current state (for subtitles)."""
        return self._reason

    @property
    def entered_at(self) -> float:
        return self._entered_at

    def seconds_in_state(self) -> float:
        return max(0.0, self._clock() - self._entered_at)

    def auto_return_delay(self) -> Optional[float]:
        """Seconds until the presentation should return to idle (or hold)."""
        return AUTO_RETURN_DELAYS.get(self._state)

    # ------------------------------------------------------------------ #
    # Transitions
    # ------------------------------------------------------------------ #

    def enter(self, state: str, *, reason: str = "") -> bool:
        """Move to *state*; invalid transitions use the safe fallback.

        Returns True when the requested state was reached (possibly
        after a fallback hop), False when even the fallback was not
        reachable and the character stayed put.  Never raises for a
        graph violation — the presentation layer must not be able to
        break anything.
        """
        if state not in TRANSITIONS:
            # Unknown target (e.g. a renderer/asset gap): degrade to the
            # calm state — but never yank an already-calm character
            # anywhere, and never claim the unknown state was reached.
            if self._state == FALLBACK_STATE:
                return False
            if is_valid_transition(self._state, FALLBACK_STATE):
                self._apply(FALLBACK_STATE,
                            reason or f"unknown state {state!r}")
            return False
        if state == self._state:
            # Refresh the reason (e.g. a new task in the same state).
            self._reason = reason or self._reason
            self._emit()
            return True
        if is_valid_transition(self._state, state):
            return self._apply(state, reason)
        # An invalid request against an already-calm character is
        # answered honestly: stay put, change nothing.
        if self._state in (FALLBACK_STATE, STATE_RESTING):
            return False
        # Invalid edge: recover through one calm intermediate hop so a
        # *truthful* target (e.g. waiting_for_approval, celebrating —
        # driven only by the adapter from trusted records) is never
        # lost to a fallback.  Two hops max, always through the calm
        # state, never inventing a state.
        for intermediate in (FALLBACK_STATE, STATE_RESTING):
            if (is_valid_transition(self._state, intermediate)
                    and is_valid_transition(intermediate, state)):
                self._apply(intermediate,
                            reason or f"via {intermediate} to {state!r}")
                return self._apply(state, reason)
        # No honest path: the documented safe fallback (calm), never a
        # fabricated state.
        for fallback in (FALLBACK_STATE, STATE_RESTING):
            if is_valid_transition(self._state, fallback):
                ok = self._apply(fallback, reason or f"fallback from {state!r}")
                # One hop only: do not chain into a second fallback.
                return ok and fallback == state
        return False

    def force(self, state: str, *, reason: str = "") -> bool:
        """Unconditional set (startup/sleep only); validates membership.

        Used by the wake-up and sleep paths, which begin outside the
        ordinary graph (before the first greeting).  Still refuses
        unknown states, so a bad value can never strand the character.
        """
        if state not in TRANSITIONS:
            return False
        return self._apply(state, reason or "forced")

    def _apply(self, state: str, reason: str) -> bool:
        self._state = state
        self._expression = EXPRESSIONS.get(state, "neutral")
        self._reason = reason or STATE_LABELS.get(state, state)
        self._entered_at = self._clock()
        self._dispatch_renderer()
        self._emit()
        return True

    def _dispatch_renderer(self) -> None:
        """Tell the renderer about the new state — error-isolated."""
        if self.renderer is None:
            return
        try:
            self.renderer.show_state(
                self._state, expression=self._expression
            )
        except Exception as exc:  # noqa: BLE001 - animation never breaks tasks
            self.renderer_errors.append(
                f"{type(exc).__name__}: {exc}"
            )
            del self.renderer_errors[:-16]  # keep the list bounded

    def _emit(self) -> None:
        if self.on_change is not None:
            try:
                self.on_change(self._state, self._expression, self._reason)
            except Exception:  # noqa: BLE001 - a view bug must not propagate
                pass

    # ------------------------------------------------------------------ #
    # View snapshot (pure)
    # ------------------------------------------------------------------ #

    def snapshot(self) -> dict:
        """Everything a presentation layer needs, headless."""
        return {
            "state": self._state,
            "state_label": STATE_LABELS.get(self._state, self._state),
            "expression": self._expression,
            "reason": self._reason,
            "animated": self._state in ANIMATED_STATES,
            "auto_return_delay": self.auto_return_delay(),
            "renderer_errors": list(self.renderer_errors),
        }


__all__ = [
    "ALL_STATES",
    "ANIMATED_STATES",
    "AUTO_RETURN_DELAYS",
    "EXPRESSIONS",
    "FALLBACK_STATE",
    "STATE_CELEBRATING",
    "STATE_CODING",
    "STATE_ERROR",
    "STATE_GREETING",
    "STATE_IDLE",
    "STATE_LABELS",
    "STATE_LISTENING",
    "STATE_READING",
    "STATE_RESTING",
    "STATE_SLEEPING",
    "STATE_THINKING",
    "STATE_VERIFYING",
    "STATE_WAITING_FOR_APPROVAL",
    "STATE_WALKING",
    "STATE_WORKING",
    "STATE_WRITING",
    "STATE_WAKING_UP",
    "TRANSITIONS",
    "CharacterController",
    "InvalidCharacterState",
    "is_valid_transition",
]
