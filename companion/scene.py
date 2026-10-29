"""Bedroom scene model for the Alwin companion (v0.11.0).

This module is the **renderer-independent world model**: it knows *where*
Alwin is in his bedroom, *where* he can walk, and *which* scene commands
are legal — but it knows nothing about cairo, GTK, or the trusted task
engine.

Design rules (pinned by tests):

* **pure Python** — only ``math``/``dataclasses``/``typing``; no I/O, no
  threads, no timers of its own.  The host drives it with a monotonic
  ``now`` and it answers deterministically, so delayed or coalesced
  frames can never corrupt a movement (positions are a function of
  elapsed time, never of frame counts),
* the scene **never executes a task, never approves anything, and never
  modifies a task result** — it is a closed set of geometry + movement
  state; the trusted pipeline stays authoritative,
* the interaction surface is a **closed command allowlist**
  (:data:`SceneController.COMMANDS`): trusted internal code may wake the
  character, move him to a *named* position, begin a writing/coding
  activity, return to idle, or stop/reset the visual sequence.  Nothing
  else is reachable — there is no scripting, no object mutation, and no
  path from a click or text input to arbitrary behaviour,
* scene *phases* (this module) are navigation metadata only — the
  character animation states stay owned by
  :mod:`companion.states`; the two are layered, never duplicated,
* coordinates live in a fixed **scene design box** (480 x 360 units);
  :class:`SceneTransform` maps that box uniformly onto any window size,
  so the room and the character scale together and never stretch.

Coordinate system: x grows right, y grows down.  The origin (0, 0) is
the top-left of the scene box.  ``FLOOR_Y`` is the single walking plane
— a pair of feet anchored there always stands on the floor, at every
window size.  The bed is the one off-plane anchor (you lie *above* the
floor), reached only through the wake-up transition, never a walk.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Callable, Dict, Optional, Tuple

# --------------------------------------------------------------------------- #
# Scene geometry (design box units — uniform scaling handles real windows)
# --------------------------------------------------------------------------- #

SCENE_W = 480.0
SCENE_H = 360.0

#: Wall/floor split (where the skirting board sits).
WALL_Y = 246.0

#: The walking plane: feet anchored here always stand on the floor.
FLOOR_Y = 322.0

#: How close (scene units) a position must be to count as "at" a place.
POSITION_EPSILON = 3.0

#: Walking speed and duration bounds (scene units per second).
WALK_SPEED = 110.0
MIN_WALK_SECONDS = 0.45
MAX_WALK_SECONDS = 3.0

#: Seconds for the lying -> standing transition beside the bed.
GETUP_SECONDS = 1.0

#: Character scale inside the scene (matches companion.bedroom).
CHAR_SCALE = 0.65

#: In the character design box the ankle-to-sole drop leaves the sole
#: bottom at y=241.5 while the anchor (GROUND_Y) is 248 — so an anchor
#: placed exactly on the floor line would float the feet a few pixels
#: above it.  This is that drop, pre-multiplied by the scene scale, so
#: every floor anchor can be placed so the soles *touch* FLOOR_Y.
SOLE_DROP = 6.5 * CHAR_SCALE

#: The actual feet-anchor height for standing positions: the sole
#: bottom lands exactly on FLOOR_Y.
FLOOR_ANCHOR_Y = FLOOR_Y + SOLE_DROP

_BED_ROTATION = -math.pi / 2.0     # lying: rotated a quarter turn


def smoothstep(u: float) -> float:
    """Deterministic ease-in-out (3u^2 - 2u^3) on a clamped [0, 1] input."""
    if not u == u:                      # NaN guard: never move on bad time
        return 0.0
    u = 0.0 if u < 0.0 else (1.0 if u > 1.0 else u)
    return u * u * (3.0 - 2.0 * u)


@dataclass(frozen=True)
class SceneObject:
    """A stable scene thing: an identity plus axis-aligned bounds."""

    id: str
    x: float
    y: float
    w: float
    h: float

    @property
    def bounds(self) -> Tuple[float, float, float, float]:
        """``(x, y, w, h)`` — the object's extent in scene units."""
        return (self.x, self.y, self.w, self.h)

    def contains(self, px: float, py: float) -> bool:
        return (self.x <= px <= self.x + self.w
                and self.y <= py <= self.y + self.h)


@dataclass(frozen=True)
class NamedPosition:
    """A place Alwin can be sent to — feet anchor + lying rotation."""

    name: str
    x: float
    y: float
    rotation: float = 0.0

    @property
    def feet(self) -> Tuple[float, float]:
        return (self.x, self.y)


#: Every scene object with stable identity and bounds (all inside the box).
BEDROOM_OBJECTS: Dict[str, SceneObject] = {
    o.id: o
    for o in (
        SceneObject("wall", 0.0, 0.0, SCENE_W, WALL_Y),
        SceneObject("floor", 0.0, WALL_Y, SCENE_W, SCENE_H - WALL_Y),
        SceneObject("window", 34.0, 40.0, 116.0, 110.0),
        SceneObject("curtains", 22.0, 34.0, 142.0, 122.0),
        SceneObject("sunrise", 62.0, 60.0, 62.0, 62.0),
        SceneObject("poster", 250.0, 88.0, 54.0, 42.0),
        SceneObject("bed", 14.0, 268.0, 188.0, 54.0),
        SceneObject("headboard", 8.0, 236.0, 20.0, 62.0),
        SceneObject("pillow", 42.0, 268.0, 54.0, 18.0),
        SceneObject("blanket", 96.0, 250.0, 108.0, 72.0),
        SceneObject("bedside_table", 210.0, 292.0, 38.0, 30.0),
        SceneObject("alarm_clock", 217.0, 275.0, 24.0, 17.0),
        SceneObject("desk", 318.0, 288.0, 126.0, 34.0),
        SceneObject("chair", 414.0, 258.0, 44.0, 64.0),
        SceneObject("notebook", 351.0, 279.0, 29.0, 10.0),
        SceneObject("pencil", 336.0, 283.0, 16.0, 5.0),
        SceneObject("laptop", 377.0, 262.0, 36.0, 27.0),
        SceneObject("rug", 200.0, 310.0, 116.0, 30.0),
    )
}

#: Named positions Alwin can be sent to (feet anchors; ``bed`` is the
#: lying anchor and is never a *walk* destination — you walk *from* it).
NAMED_POSITIONS: Dict[str, NamedPosition] = {
    "bed": NamedPosition("bed", 196.0, 250.0, _BED_ROTATION),
    "beside_bed": NamedPosition("beside_bed", 150.0, FLOOR_ANCHOR_Y),
    "greeting": NamedPosition("greeting", 258.0, FLOOR_ANCHOR_Y),
    "desk": NamedPosition("desk", 380.0, FLOOR_ANCHOR_Y),
}

#: The initial scene: asleep in bed, pre-dawn.
INITIAL_POSITION = "bed"


# --------------------------------------------------------------------------- #
# Scene-to-window transform
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SceneTransform:
    """Uniform scene-box -> pixel mapping for one window size.

    The scene is scaled by a single factor (``min(w/W, h/H)``) and
    centred; the leftover space is symmetric margins that the caller
    fills with the surround colour.  Nothing is ever scaled on one axis
    alone, so the room and the character keep their proportions at any
    window size.
    """

    scale: float
    offset_x: float
    offset_y: float

    @classmethod
    def for_viewport(cls, width: float, height: float) -> "SceneTransform":
        if width <= 0.0 or height <= 0.0:
            return cls(0.0, 0.0, 0.0)
        s = min(width / SCENE_W, height / SCENE_H)
        return cls(s, (width - SCENE_W * s) / 2.0,
                   (height - SCENE_H * s) / 2.0)

    def to_pixel(self, x: float, y: float) -> Tuple[float, float]:
        return (self.offset_x + x * self.scale,
                self.offset_y + y * self.scale)

    @property
    def content_pixel_size(self) -> Tuple[float, float]:
        return (SCENE_W * self.scale, SCENE_H * self.scale)


# --------------------------------------------------------------------------- #
# Movement (elapsed-time interpolation; frame-rate independent)
# --------------------------------------------------------------------------- #


class MovementController:
    """Where Alwin is, and how he is getting somewhere else.

    A movement is a *request* with a start time, a duration and a
    target.  The current position is a pure function of elapsed time
    (smoothstep-eased), so a delayed or dropped frame simply advances
    him to the correct spot — movement never "overshoots" by frames.
    Repeating the same request while it is in flight is a no-op, and a
    new request **replaces** the old one from wherever he currently is.
    """

    def __init__(self, x: float, y: float) -> None:
        self.x = x
        self.y = y
        self._from_x = x
        self._from_y = y
        self._target: Optional[Tuple[float, float]] = None
        self._start = 0.0
        self._duration = 0.0

    @property
    def moving(self) -> bool:
        return self._target is not None

    @property
    def target(self) -> Optional[Tuple[float, float]]:
        return self._target

    def stop(self) -> None:
        """Cancel movement in place (he stays where he currently is)."""
        self._target = None

    def walk_to(self, x: float, y: float, now: float,
                speed: float = WALK_SPEED) -> bool:
        """Start (or no-op) a walk.  Returns False for invalid targets.

        Valid destinations are on the walking plane within the scene
        margins; anything else is rejected **without** disturbing the
        current position — the caller reports the limitation honestly.
        """
        if not (_finite(x) and _finite(y) and _finite(now)):
            return False
        if abs(y - FLOOR_ANCHOR_Y) > POSITION_EPSILON:
            return False                    # not on the walking plane
        if not (20.0 <= x <= SCENE_W - 20.0):
            return False                    # outside the walkable room
        if (not self.moving
                and abs(x - self.x) <= POSITION_EPSILON
                and abs(y - self.y) <= POSITION_EPSILON):
            return True                     # already there: no restart
        if (self.moving and self._target is not None
                and abs(x - self._target[0]) <= POSITION_EPSILON
                and abs(y - self._target[1]) <= POSITION_EPSILON):
            return True                     # same request in flight: no-op
        distance = math.hypot(x - self.x, y - self.y)
        duration = distance / speed if speed > 0.0 else MAX_WALK_SECONDS
        duration = min(MAX_WALK_SECONDS, max(MIN_WALK_SECONDS, duration))
        self._from_x, self._from_y = self.x, self.y
        self._target = (x, y)
        self._start = now
        self._duration = duration
        return True

    def update(self, now: float) -> bool:
        """Advance to *now*; returns True exactly once per arrival."""
        if self._target is None:
            return False
        tx, ty = self._target
        if now <= self._start:
            u = 0.0
        else:
            u = (now - self._start) / self._duration
        k = smoothstep(u)
        self.x = self._from_x + (tx - self._from_x) * k
        self.y = self._from_y + (ty - self._from_y) * k
        if u >= 1.0:
            self.x, self.y = tx, ty
            self._target = None
            return True
        return False


def _finite(v: float) -> bool:
    return v == v and math.isfinite(v)


# --------------------------------------------------------------------------- #
# Scene phases (navigation metadata layered over the character states)
# --------------------------------------------------------------------------- #

P_BEDROOM_SLEEPING = "bedroom_sleeping"
P_BEDROOM_WAKING = "bedroom_waking"
P_GETTING_UP = "getting_up"
P_WALKING_TO_GREETING = "walking_to_greeting"
P_GREETING = "greeting"
P_IDLE_GREETING_POSITION = "idle_greeting_position"
P_WALKING_TO_DESK = "walking_to_desk"
P_IDLE_DESK_POSITION = "idle_desk_position"
P_WRITING = "writing"
P_CODING = "coding"
P_THINKING = "thinking"
P_WAITING_FOR_APPROVAL = "waiting_for_approval"
P_CELEBRATING = "celebrating"
P_ERROR = "error"

SCENE_PHASES = frozenset({
    P_BEDROOM_SLEEPING, P_BEDROOM_WAKING, P_GETTING_UP,
    P_WALKING_TO_GREETING, P_GREETING, P_IDLE_GREETING_POSITION,
    P_WALKING_TO_DESK, P_IDLE_DESK_POSITION, P_WRITING, P_CODING,
    P_THINKING, P_WAITING_FOR_APPROVAL, P_CELEBRATING, P_ERROR,
})

#: Base phase graph.  On top of this, a walk request or an outcome
#: (error / waiting for approval) is legal from *anywhere* — a failed
#: task must interrupt a long walk deterministically instead of waiting
#: for it to finish.
SCENE_TRANSITIONS: Dict[str, frozenset] = {
    P_BEDROOM_SLEEPING: frozenset({P_BEDROOM_WAKING, P_GETTING_UP}),
    P_BEDROOM_WAKING: frozenset({P_GETTING_UP}),
    P_GETTING_UP: frozenset({P_IDLE_GREETING_POSITION}),
    P_WALKING_TO_GREETING: frozenset({P_GREETING}),
    P_GREETING: frozenset({P_IDLE_GREETING_POSITION, P_THINKING}),
    P_IDLE_GREETING_POSITION: frozenset({P_THINKING, P_CELEBRATING}),
    P_IDLE_DESK_POSITION: frozenset({P_WRITING, P_CODING, P_THINKING,
                                     P_CELEBRATING}),
    P_WRITING: frozenset({P_IDLE_GREETING_POSITION, P_THINKING,
                          P_CELEBRATING}),
    P_CODING: frozenset({P_IDLE_GREETING_POSITION, P_THINKING,
                         P_CELEBRATING}),
    P_THINKING: frozenset({P_IDLE_GREETING_POSITION, P_WRITING, P_CODING}),
    P_WAITING_FOR_APPROVAL: frozenset({P_WRITING, P_CODING, P_THINKING,
                                       P_IDLE_GREETING_POSITION}),
    P_CELEBRATING: frozenset({P_IDLE_GREETING_POSITION, P_THINKING}),
    P_ERROR: frozenset({P_THINKING, P_IDLE_GREETING_POSITION}),
    P_WALKING_TO_DESK: frozenset({P_WRITING, P_CODING,
                                  P_IDLE_GREETING_POSITION}),
}

#: Legal from every phase: travel and truthful outcomes.
_ALWAYS_FROM_ANYWHERE = frozenset({
    P_WALKING_TO_GREETING, P_WALKING_TO_DESK,
    P_ERROR, P_WAITING_FOR_APPROVAL,
})


def is_valid_scene_transition(current: str, new: str) -> bool:
    if new not in SCENE_PHASES or current not in SCENE_PHASES:
        return False
    if new == current:
        return True
    if new in _ALWAYS_FROM_ANYWHERE:
        return True
    return new in SCENE_TRANSITIONS.get(current, frozenset())


# --------------------------------------------------------------------------- #
# Scene controller: the closed command surface
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class SceneView:
    """A immutable snapshot the renderer draws from."""

    x: float
    y: float
    rotation: float
    moving: bool
    phase: str
    sunrise: float          # 0.0 pre-dawn .. 1.0 full morning
    alarm: float            # 0.0 still .. 1.0 ringing
    blanket_cover: float    # 1.0 fully covered .. 0.0 off
    sleep_zs: bool          # draw the floating z's
    writing: bool           # animate the notebook page
    coding: bool            # animate the laptop glyphs


class SceneController:
    """Alwin's bedroom: position, movement, wake-up, and commands.

    The interaction surface is :data:`COMMANDS` — a closed allowlist of
    visual actions.  Every command validates its arguments (position
    names come from :data:`NAMED_POSITIONS` only) and answers honestly
    with ``True``/``False`` plus a human-readable ``status``.  There is
    no command that can execute, approve, or modify anything outside
    this scene.
    """

    COMMANDS = frozenset({
        "wake", "get_up", "move_to", "begin_writing", "begin_coding",
        "begin_thinking", "begin_waiting", "begin_error", "begin_celebrating",
        "return_idle", "place", "stop", "reset",
    })

    #: Commands that carry extra required arguments.
    _NEEDS_POSITION = frozenset({"move_to", "place"})

    def __init__(self, *, reduced_motion: bool = False) -> None:
        start = NAMED_POSITIONS[INITIAL_POSITION]
        self.movement = MovementController(start.x, start.y)
        self.rotation = start.rotation
        self.phase = P_BEDROOM_SLEEPING
        self.status = "sleeping"
        self.reduced_motion = reduced_motion
        self._wake_started: Optional[float] = None
        self._getup_started: Optional[float] = None
        self._getup_from: Tuple[float, float, float] = (0.0, 0.0, 0.0)
        self._destination: Optional[str] = None
        self._arrival_phase: str = P_GREETING
        self._on_event: Optional[Callable[[str], None]] = None

    # -- observation ------------------------------------------------------- #

    @property
    def position(self) -> Tuple[float, float]:
        return (self.movement.x, self.movement.y)

    @property
    def moving(self) -> bool:
        return self.movement.moving

    @property
    def destination(self) -> Optional[str]:
        return self._destination

    def at_position(self, name: str) -> bool:
        """True when *named* place exists and he is standing on it."""
        pos = NAMED_POSITIONS.get(name)
        if pos is None or self.movement.moving:
            return False
        return (abs(self.movement.x - pos.x) <= POSITION_EPSILON
                and abs(self.movement.y - pos.y) <= POSITION_EPSILON
                and abs(self.rotation - pos.rotation) < 0.01)

    # -- animation parameters (pure functions of time) --------------------- #

    def sunrise(self, now: float) -> float:
        if self.reduced_motion:
            return 0.35 if self.phase == P_BEDROOM_SLEEPING else 1.0
        if self.phase == P_BEDROOM_SLEEPING:
            return 0.18
        if self.phase in (P_BEDROOM_WAKING, P_GETTING_UP):
            start = self._wake_started if self._wake_started is not None else now
            return 0.18 + 0.82 * smoothstep((now - start) / 2.4)
        return 1.0

    def alarm(self, now: float) -> float:
        if self.reduced_motion or self.phase != P_BEDROOM_WAKING:
            return 0.0
        start = self._wake_started if self._wake_started is not None else now
        return 1.0 if (now - start) < 1.4 else 0.0

    def blanket_cover(self, now: float) -> float:
        del now
        return min(1.0, abs(self.rotation) / (math.pi / 2.0))

    def render_view(self, now: float) -> SceneView:
        return SceneView(
            x=self.movement.x,
            y=self.movement.y,
            rotation=self.rotation,
            moving=self.movement.moving,
            phase=self.phase,
            sunrise=self.sunrise(now),
            alarm=self.alarm(now),
            blanket_cover=self.blanket_cover(now),
            sleep_zs=self.phase == P_BEDROOM_SLEEPING,
            writing=self.phase == P_WRITING,
            coding=self.phase == P_CODING,
        )

    # -- commands (the closed interaction surface) ------------------------- #

    def command(self, name: str, *, position: Optional[str] = None,
                now: float = 0.0) -> bool:
        """Run one allowlisted scene command.  Unknown names are refused."""
        if name not in self.COMMANDS:
            self.status = f"unknown scene command: {name!r}"
            return False
        if name in self._NEEDS_POSITION and position not in NAMED_POSITIONS:
            self.status = f"unknown scene position: {position!r}"
            return False
        handler = getattr(self, f"_cmd_{name}")
        return handler(now=now, position=position)

    def _cmd_wake(self, now: float,
                  position: Optional[str] = None) -> bool:
        del position
        if self.phase != P_BEDROOM_SLEEPING:
            self.status = "already awake"
            return False
        self.phase = P_BEDROOM_WAKING
        self._wake_started = now
        self.status = "waking up"
        return True

    def _cmd_get_up(self, now: float,
                    position: Optional[str] = None) -> bool:
        del position
        if self.phase not in (P_BEDROOM_SLEEPING, P_BEDROOM_WAKING):
            self.status = "already up"
            return False
        bed = NAMED_POSITIONS["bed"]
        stand = NAMED_POSITIONS["beside_bed"]
        self._getup_from = (bed.x, bed.y, bed.rotation)
        self.phase = P_GETTING_UP
        if self.reduced_motion:
            self.movement.x, self.movement.y = stand.x, stand.y
            self.movement.stop()
            self.rotation = 0.0
            self.status = "up (reduced motion)"
            return True
        self._getup_started = now
        self.status = "getting up"
        return True

    def _cmd_move_to(self, now: float,
                     position: Optional[str] = None) -> bool:
        pos = NAMED_POSITIONS[position or ""]
        if pos.rotation != 0.0:
            self.status = "cannot walk to the bed — get up first"
            return False
        self._ensure_up()
        if not self.movement.walk_to(pos.x, pos.y, now):
            self.status = "destination unreachable"
            return False
        self._destination = pos.name
        self._arrival_phase = {
            "greeting": P_GREETING,
            "desk": P_IDLE_DESK_POSITION,
        }.get(pos.name, P_IDLE_GREETING_POSITION)
        self.phase = (P_WALKING_TO_DESK if pos.name == "desk"
                      else P_WALKING_TO_GREETING)
        self.status = f"walking to {pos.name}"
        return True

    def _cmd_begin_writing(self, now: float,
                           position: Optional[str] = None) -> bool:
        return self._begin_activity(P_WRITING, "desk", now)

    def _cmd_begin_coding(self, now: float,
                          position: Optional[str] = None) -> bool:
        return self._begin_activity(P_CODING, "desk", now)

    def _cmd_begin_thinking(self, now: float,
                            position: Optional[str] = None) -> bool:
        return self._begin_activity(P_THINKING, "greeting", now)

    def _begin_activity(self, activity: str, spot: str, now: float) -> bool:
        dest = NAMED_POSITIONS[spot]
        self._ensure_up()
        if self.at_position(spot):
            self.phase = activity
            self.status = activity.replace("_", " ")
            return True
        if not self.movement.walk_to(dest.x, dest.y, now):
            self.status = "destination unreachable"
            return False
        self._destination = spot
        self._arrival_phase = activity
        self.phase = (P_WALKING_TO_DESK if spot == "desk"
                      else P_WALKING_TO_GREETING)
        self.status = f"walking to {spot} for {activity}"
        return True

    # These three are legal from anywhere (including mid-walk): a task
    # outcome interrupts travel deterministically instead of waiting for
    # a walk to finish.  They stop the work animation and set an honest
    # visual state — they never approve, complete or alter a task.
    def _outcome_visual(self, phase: str, status: str,
                        now: float, position: Optional[str]) -> bool:
        del now, position
        was_desk = self._destination == "desk"
        self._ensure_up()
        self.movement.stop()
        self._destination = None
        self._arrival_phase = (P_IDLE_DESK_POSITION if was_desk
                               else P_IDLE_GREETING_POSITION)
        self.phase = phase
        self.status = status
        return True

    def _cmd_begin_waiting(self, now: float,
                           position: Optional[str] = None) -> bool:
        return self._outcome_visual(P_WAITING_FOR_APPROVAL,
                                    "waiting for approval",
                                    now, position)

    def _cmd_begin_error(self, now: float,
                         position: Optional[str] = None) -> bool:
        return self._outcome_visual(P_ERROR, "needs attention", now, position)

    def _cmd_begin_celebrating(self, now: float,
                               position: Optional[str] = None) -> bool:
        return self._outcome_visual(P_CELEBRATING, "task complete",
                                    now, position)

    def _cmd_return_idle(self, now: float,
                         position: Optional[str] = None) -> bool:
        del position
        greeting = NAMED_POSITIONS["greeting"]
        self._ensure_up()
        if self.at_position("greeting"):
            self.phase = P_IDLE_GREETING_POSITION
            self.status = "idle"
            return True
        if not self.movement.walk_to(greeting.x, greeting.y, now):
            self.status = "destination unreachable"
            return False
        self._destination = "greeting"
        self._arrival_phase = P_IDLE_GREETING_POSITION
        self.phase = P_WALKING_TO_GREETING
        self.status = "walking to greeting position"
        return True

    def _cmd_place(self, now: float,
                   position: Optional[str] = None) -> bool:
        """Teleport (reduced-motion / recovery only) to a named place."""
        del now
        pos = NAMED_POSITIONS[position or ""]
        self.movement.stop()
        self.movement.x, self.movement.y = pos.x, pos.y
        self.rotation = pos.rotation
        self._destination = None
        self._getup_started = None
        if pos.rotation != 0.0:
            self.phase = P_BEDROOM_SLEEPING
            self.status = "placed in bed"
        else:
            self.phase = P_IDLE_GREETING_POSITION
            self.status = f"placed at {pos.name}"
        return True

    def _cmd_stop(self, now: float,
                  position: Optional[str] = None) -> bool:
        """Freeze the visuals where they are (approval, failure, close)."""
        del now, position
        was_desk = (self._destination == "desk")
        self.movement.stop()
        self._getup_started = None
        self._destination = None
        if self.phase in (P_WALKING_TO_DESK, P_WALKING_TO_GREETING):
            self.phase = (P_IDLE_DESK_POSITION if was_desk
                          else P_IDLE_GREETING_POSITION)
        self.status = "stopped"
        return True

    def _cmd_reset(self, now: float,
                   position: Optional[str] = None) -> bool:
        """Back to the initial scene: asleep in bed.  Visuals only."""
        del now, position
        bed = NAMED_POSITIONS["bed"]
        self.movement.stop()
        self.movement.x, self.movement.y = bed.x, bed.y
        self.rotation = bed.rotation
        self.phase = P_BEDROOM_SLEEPING
        self._wake_started = None
        self._getup_started = None
        self._destination = None
        self.status = "reset to bed"
        return True

    def _ensure_up(self) -> None:
        """A task never waits for the morning sequence.

        If a work command arrives while he is still in bed, the visual
        wake-up is skipped (a coherent cut, not a fake animation): he
        simply stands beside the bed and walks from there.
        """
        if self.rotation != 0.0 or self.phase in (
                P_BEDROOM_SLEEPING, P_BEDROOM_WAKING, P_GETTING_UP):
            stand = NAMED_POSITIONS["beside_bed"]
            self.movement.stop()
            self.movement.x, self.movement.y = stand.x, stand.y
            self.rotation = 0.0
            self._getup_started = None

    # -- ticking (host-driven; arrival events fire here) ------------------- #

    def tick(self, now: float) -> Tuple[str, ...]:
        """Advance the scene to *now*; returns events that fired.

        Events are plain strings (``arrived_at_<position>``) — the host
        (window) decides what they mean for the character state.  This
        module never touches the character controller or the task
        engine.
        """
        events = []
        if self.phase == P_GETTING_UP and self._getup_started is not None:
            if self.reduced_motion:
                u = 1.0
            else:
                u = ((now - self._getup_started) / GETUP_SECONDS
                     if now > self._getup_started else 0.0)
            k = smoothstep(u)
            fx, fy, fr = self._getup_from
            stand = NAMED_POSITIONS["beside_bed"]
            self.movement.x = fx + (stand.x - fx) * k
            self.movement.y = fy + (stand.y - fy) * k
            self.rotation = fr * (1.0 - k)
            if u >= 1.0:
                self.movement.x, self.movement.y = stand.x, stand.y
                self.rotation = 0.0
                self._getup_started = None
                self.phase = P_IDLE_GREETING_POSITION
        if self.movement.update(now):
            name = self._destination or ""
            self._destination = None
            if self.phase in (P_WALKING_TO_DESK, P_WALKING_TO_GREETING):
                self.phase = self._arrival_phase
            self.status = f"arrived at {name}" if name else "arrived"
            events.append(f"arrived_at_{name}" if name else "arrived")
        return tuple(events)

    def describe(self) -> str:
        """A short, honest status line for the companion UI."""
        return self.status


__all__ = [
    "BEDROOM_OBJECTS",
    "CHAR_SCALE",
    "FLOOR_ANCHOR_Y",
    "FLOOR_Y",
    "GETUP_SECONDS",
    "INITIAL_POSITION",
    "MAX_WALK_SECONDS",
    "MIN_WALK_SECONDS",
    "MovementController",
    "NAMED_POSITIONS",
    "NamedPosition",
    "P_BEDROOM_SLEEPING",
    "P_BEDROOM_WAKING",
    "P_CELEBRATING",
    "P_CODING",
    "P_ERROR",
    "P_GETTING_UP",
    "P_GREETING",
    "P_IDLE_DESK_POSITION",
    "P_IDLE_GREETING_POSITION",
    "P_THINKING",
    "P_WAITING_FOR_APPROVAL",
    "P_WALKING_TO_DESK",
    "P_WALKING_TO_GREETING",
    "P_WRITING",
    "SCENE_H",
    "SCENE_TRANSITIONS",
    "SCENE_W",
    "SCENE_PHASES",
    "SceneController",
    "SceneObject",
    "SceneTransform",
    "SceneView",
    "WALL_Y",
    "is_valid_scene_transition",
    "smoothstep",
]
