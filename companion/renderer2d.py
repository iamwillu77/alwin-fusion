"""2D character renderer for the Alwin companion (v0.7.0).

The first implementation of :class:`companion.renderer.CharacterRenderer`:
a lightweight, **original** procedural vector character drawn with
cairo — Alwin is a soft rounded companion (a friendly warm-white pebble
shape with expressive eyes, blush, a state-tinted antenna orb, and two
little hands).  No image assets are required to run it, no new
dependencies are added (cairo ships with PyGObject), and it is fully
drawable **headless** onto a ``cairo.ImageSurface`` for tests.

Replaceable assets: every state may be overridden by dropping a
``frame.png`` into ``companion/assets/character/<state>/``.  When an
asset exists it is shown instead of the procedural drawing; when it is
missing the procedural character renders (graceful fallback, never a
crash).  Assets are optional and individually replaceable per state —
see the README in the assets directory.

Honesty notes (aligned with the project's rules):

* the renderer **plays** animations; it never infers task outcomes.
  "celebrating" is only ever *entered* by the controller because the
  trusted pipeline reported a verified success,
* ``tick()`` is host-driven (the GTK layer owns the frame timer, which
  runs only while a state animates and stops when nothing changes),
* this module imports GTK lazily/guardedly, so importing it headless
  (tests, ``--check``) never requires a display; the drawing core
  (:func:`draw_character`) is pure cairo and display-free,
* walking here is a 2D waddle transition — a visual cue that work is
  starting, not a claim of a literal 3D walk.
"""

from __future__ import annotations

import math
import pathlib
from typing import Callable, Dict, Optional, Tuple

import cairo

from companion.renderer import CharacterRenderer, RendererUnavailable
from companion.states import ALL_STATES

try:  # pragma: no cover - exercised only when a display exists
    import gi

    gi.require_version("Gtk", "4.0")
    from gi.repository import GLib, Gtk
except (ImportError, ValueError):  # pragma: no cover - headless fallback
    GLib = None  # type: ignore[assignment]
    Gtk = None  # type: ignore[assignment]

# --------------------------------------------------------------------------- #
# Palette — warm, welcoming, original ALWIN branding (no macOS assets).
# --------------------------------------------------------------------------- #

BODY_TOP = (1.000, 0.984, 0.957)    # warm white
BODY_BOTTOM = (0.960, 0.918, 0.855)  # soft cream
BODY_EDGE = (0.851, 0.788, 0.698)
EYE = (0.180, 0.204, 0.267)          # soft near-black
EYE_HIGHLIGHT = (1.0, 1.0, 1.0)
BLUSH = (0.957, 0.663, 0.627)
MOUTH = (0.373, 0.290, 0.267)
SHADOW = (0.0, 0.0, 0.0)
CHEST_GLOW = (0.231, 0.655, 0.627)  # teal brand accent

#: Accent colour per state (antenna orb + chest glow + props).
STATE_ACCENTS: Dict[str, Tuple[float, float, float]] = {
    "sleeping": (0.478, 0.541, 0.698),
    "waking_up": (0.910, 0.639, 0.271),
    "greeting": (0.910, 0.541, 0.322),
    "idle": (0.231, 0.655, 0.627),
    "listening": (0.310, 0.686, 0.831),
    "thinking": (0.424, 0.388, 0.780),
    "walking": (0.231, 0.655, 0.627),
    "writing": (0.290, 0.545, 0.831),
    "coding": (0.267, 0.741, 0.490),
    "reading": (0.588, 0.475, 0.831),
    "waiting_for_approval": (0.910, 0.639, 0.271),
    "working": (0.231, 0.655, 0.627),
    "verifying": (0.310, 0.686, 0.831),
    "celebrating": (0.294, 0.741, 0.490),
    "error": (0.851, 0.310, 0.290),
    "resting": (0.478, 0.541, 0.698),
}


# --------------------------------------------------------------------------- #
# Pure drawing core (headless-testable: cairo only, no GTK)
#
# The full-body character artwork lives in :mod:`companion.character`.
# This module re-exports ``draw_character`` (for compatibility) and hosts
# the GTK renderer that drives it with smooth pose blending and adaptive
# frame cadence.
# --------------------------------------------------------------------------- #

from companion.character import (  # noqa: E402  (re-export for tests/hosts)
    POSE_BLEND_SECONDS,
    Pose,
    compute_pose,
    draw_character,
)

#: Accent colour per state (kept for compatibility with v0.6 hosts).
STATE_ACCENTS = {
    "sleeping": (0.478, 0.541, 0.698),
    "waking_up": (0.910, 0.639, 0.271),
    "greeting": (0.910, 0.541, 0.322),
    "idle": (0.231, 0.655, 0.627),
    "listening": (0.310, 0.686, 0.831),
    "thinking": (0.424, 0.388, 0.780),
    "walking": (0.231, 0.655, 0.627),
    "writing": (0.290, 0.545, 0.831),
    "coding": (0.267, 0.741, 0.490),
    "reading": (0.588, 0.475, 0.831),
    "waiting_for_approval": (0.910, 0.639, 0.271),
    "working": (0.231, 0.655, 0.627),
    "verifying": (0.310, 0.686, 0.831),
    "celebrating": (0.294, 0.741, 0.490),
    "error": (0.851, 0.310, 0.290),
    "resting": (0.478, 0.541, 0.698),
}


# --------------------------------------------------------------------------- #
# Optional per-state asset override
# --------------------------------------------------------------------------- #

#: Default asset root: <package>/assets/character/<state>/frame.png
DEFAULT_ASSET_ROOT = pathlib.Path(__file__).resolve().parent / "assets" / "character"


class AssetStore:
    """Resolves optional per-state image overrides; never crashes.

    A state's asset is ``<root>/<state>/frame.png``.  When it exists
    and loads, the renderer shows it; when it does not, the procedural
    character renders instead (documented graceful fallback).  Assets
    are individually replaceable per state — drop in a new PNG and that
    state changes, nothing else.
    """

    def __init__(self, root: "pathlib.Path | None" = None) -> None:
        self.root = pathlib.Path(root) if root else DEFAULT_ASSET_ROOT
        self._textures: Dict[str, object] = {}

    def available_states(self):
        """States that currently have a real image asset on disk."""
        out = []
        for state in ALL_STATES:
            if (self.root / state / "frame.png").is_file():
                out.append(state)
        return out

    def texture_for(self, state: str):
        """Load/cache the state's texture, or None (headless-safe)."""
        if state in self._textures:
            return self._textures[state]
        path = self.root / state / "frame.png"
        if not path.is_file() or Gtk is None:
            self._textures[state] = None
            return None
        try:
            from gi.repository import Gdk

            texture = Gdk.Texture.new_from_filename(str(path))
        except Exception:  # noqa: BLE001 - a bad asset degrades, never crashes
            texture = None
        self._textures[state] = texture
        return texture


# --------------------------------------------------------------------------- #
# The GTK renderer
# --------------------------------------------------------------------------- #

#: Frame rate while a lively state animates (kept modest for a small
#: machine).  Static states draw once and the timer stops entirely.
FRAME_INTERVAL_MS = 80  # ~12 fps
#: Slower cadence for the subtle idle state (breathing + blinking still
#: read as alive, but at a fraction of the redraw cost).
FRAME_INTERVAL_MS_SLOW = 160  # ~6 fps


class Character2DRenderer(CharacterRenderer):
    """GTK4 + cairo renderer driving a :class:`Gtk.DrawingArea`.

    The renderer owns no timers of its own until :meth:`start` is
    called by the host window, and :meth:`stop` halts them; while a
    static state is shown (idle, sleeping, resting) the frame timer
    pauses, so an idle companion costs essentially nothing.
    """

    name = "2D vector (cairo)"

    def __init__(self, *, asset_root: "pathlib.Path | None" = None) -> None:
        if Gtk is None:  # pragma: no cover - defensive
            raise RendererUnavailable(
                "GTK4 (PyGObject Gtk 4.0) is required for the 2D renderer."
            )
        self.assets = AssetStore(asset_root)
        self.area: Optional["Gtk.DrawingArea"] = None
        self._state = "idle"
        self._expression = "neutral"
        self._state_started: float = 0.0
        self._now: float = 0.0
        self._timer: Optional[int] = None
        self._attached = False
        # smooth state transitions: remember the last drawn pose and blend
        self._last_pose: Optional[Pose] = None
        self._blend_started: float = 0.0

    # ---- interface ----------------------------------------------------- #

    @property
    def is_available(self) -> bool:
        return Gtk is not None

    def attach(self, target) -> None:
        """Bind to a Gtk.DrawingArea (or create one when target is None)."""
        if target is None:
            self.area = Gtk.DrawingArea()
            self.area.set_content_width(240)
            self.area.set_content_height(260)
        else:
            self.area = target
        self.area.set_draw_func(self._on_draw)
        self._attached = True

    def detach(self) -> None:
        self.stop()
        self.area = None
        self._attached = False

    def show_state(self, state: str, *, expression: Optional[str] = None) -> None:
        self.validate_state(state)
        if state != self._state:
            # capture the pose currently on screen and blend to the new one
            self._last_pose = self._current_pose()
            self._blend_started = self._now
        self._state = state
        self._expression = self.validate_expression(
            expression or self._expression
        )
        self._state_started = self._now
        self._sync_timer()
        self._queue_draw()

    def set_expression(self, expression: str) -> None:
        self._expression = self.validate_expression(expression)
        self._queue_draw()

    def tick(self, now: float) -> None:
        """Advance to *now* (host-driven; also used directly in tests)."""
        self._now = now
        self._queue_draw()

    # ---- frame timer (host-owned cadence) -------------------------------- #

    def start(self) -> None:
        """Begin the frame timer if the current state animates."""
        self._sync_timer()

    def stop(self) -> None:
        if self._timer is not None and GLib is not None:
            GLib.source_remove(self._timer)
            self._timer = None

    def _animating(self) -> bool:
        # Only the two "sleeping-like" states render one frame and stop
        # (eyes closed, z's drifting is drawn once).  Every other state
        # — including the subtle idle breathing/blinking — keeps the
        # timer alive at its cadence so the character always reads alive.
        return self._state not in ("sleeping", "resting")

    def _frame_interval_ms(self) -> int:
        # Subtle states (idle breathing/blinking) tick slower; lively
        # states get the full cadence.  Idle stays "alive" but cheap.
        return FRAME_INTERVAL_MS_SLOW if self._state == "idle" else FRAME_INTERVAL_MS

    def _current_pose(self) -> Pose:
        t = max(0.0, self._now - self._state_started)
        return compute_pose(self._state, t, self._expression)

    def _sync_timer(self) -> None:
        if GLib is None:  # pragma: no cover - headless
            return
        if self._animating() and self._timer is None:
            self._timer = GLib.timeout_add(self._frame_interval_ms(),
                                           self._on_frame)
        elif not self._animating() and self._timer is not None:
            self.stop()

    def _on_frame(self) -> bool:
        import time as _time

        self._now = _time.monotonic()
        self._queue_draw()
        return True  # keep the timer alive while animating

    def _queue_draw(self) -> None:
        if self.area is not None:
            self.area.queue_draw()

    # ---- drawing --------------------------------------------------------- #

    def _on_draw(self, _area, cr, width, height) -> None:  # pragma: no cover
        # Background: nothing — the window CSS supplies the warm panel,
        # so the character composites over whatever the host renders.
        t = max(0.0, self._now - self._state_started)
        texture = self.assets.texture_for(self._state)
        if texture is not None:
            # Asset override: show the real asset for this state,
            # scaled to fit.  (Gdk.cairo_set_source_for_texture is the
            # GTK4 way to paint a texture onto a cairo context.)
            tex_w = texture.get_width()
            tex_h = texture.get_height()
            scale = min(width / max(1, tex_w), height / max(1, tex_h))
            cr.save()
            cr.translate((width - tex_w * scale) / 2,
                         (height - tex_h * scale) / 2)
            cr.scale(scale, scale)
            try:
                from gi.repository import Gdk

                Gdk.cairo_set_source_for_texture(cr, texture)
                cr.paint()
            except Exception:  # noqa: BLE001 - bad asset: fall back to vector
                cr.restore()
                self._draw_vector(cr, width, height)
                return
            cr.restore()
            return
        self._draw_vector(cr, width, height)

    def _draw_vector(self, cr, width, height) -> None:
        """Draw the procedural character with a smooth pose blend."""
        t = max(0.0, self._now - self._state_started)
        target = compute_pose(self._state, t, self._expression)
        pose = target
        if self._last_pose is not None:
            blend = self._now - self._blend_started
            if blend < POSE_BLEND_SECONDS:
                u = blend / POSE_BLEND_SECONDS
                pose = Pose.lerp(self._last_pose, target, u)
            else:
                self._last_pose = None
        try:
            draw_character(cr, width, height, self._state,
                           expression=self._expression, t=t, pose=pose)
        except Exception:  # noqa: BLE001 - a drawing bug degrades, never crashes
            pass


__all__ = [
    "Character2DRenderer",
    "DEFAULT_ASSET_ROOT",
    "FRAME_INTERVAL_MS",
    "FRAME_INTERVAL_MS_SLOW",
    "AssetStore",
    "Pose",
    "compute_pose",
    "draw_character",
]
