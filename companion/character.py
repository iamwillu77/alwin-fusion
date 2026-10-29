"""Alwin's full-body 2D character drawing core (v0.9.0).

This module is the *art* of the companion: an original, full-body
cartoon boy drawn procedurally with cairo — large expressive head,
tousled dark-brown hair, big brown eyes, warm skin, a blue hoodie with
an original ALWIN emblem, two jointed arms with hands, legs and
red sneakers.  It replaces the v0.6 "floating pebble face" entirely.

Design rules (all pinned by tests):

* **pure cairo, no GTK, no I/O** — everything here draws onto a
  ``cairo.Context`` (real widget or an offscreen ``ImageSurface`` in
  tests), so the whole character is testable headless,
* **one character everywhere** — every state reuses the same shared
  component functions (head, hair, eyes, hoodie, arms, hands, legs,
  shoes) driven by a single float-only :class:`Pose`; no state draws
  its own private copy of the boy, so proportions, hair and clothing
  can never drift between animations,
* **animation is a pure function of elapsed time** — poses are
  computed from ``t`` (seconds in the state), never from a frame
  counter, so delayed frames stay correct and renders are
  deterministic (same inputs -> same pixels),
* **uniform scaling** — the whole design box is scaled uniformly to the
  widget and centred, so the character is never distorted and never
  clipped at any widget size,
* **honest presentation** — poses *depict* what the character is doing
  (writing, coding, celebrating); they never assert a task outcome.
  Only the trusted pipeline decides outcomes, and only it ever enters
  ``celebrating``.

The renderer (:mod:`companion.renderer2d`) owns timing, state blending
and assets; this module only computes poses and draws them.
"""

from __future__ import annotations

import math
from dataclasses import dataclass, fields
from typing import Dict, Optional, Tuple

import cairo

# --------------------------------------------------------------------------- #
# Design box — the character is authored in this coordinate system and scaled
# uniformly to whatever widget the host provides.
# --------------------------------------------------------------------------- #

DESIGN_W = 220.0
DESIGN_H = 260.0

CX = DESIGN_W / 2.0          # body centre line
GROUND_Y = 248.0             # where the shadow sits
HIP_Y = 190.0                # hip pivot line (legs attach here)
SHOULDER_Y = 132.0           # shoulder line
HEAD_CY = 66.0               # head centre (large-head proportions)
HEAD_RX = 47.0
HEAD_RY = 44.0

#: Two-segment arm geometry (shoulder -> elbow -> hand), design px.
ARM_L1 = 27.0
ARM_L2 = 25.0
HAND_R = 8.0

#: Smooth blend length used by the renderer when the state changes.
POSE_BLEND_SECONDS = 0.25


def _c(hex_color: str, alpha: float = 1.0) -> Tuple[float, float, float, float]:
    h = hex_color.lstrip("#")
    return (int(h[0:2], 16) / 255.0, int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0, alpha)


# Warm, original ALWIN palette (no external artwork, no copied characters).
SKIN = _c("#ffd9b8")
SKIN_SH = _c("#f2b78f")
SKIN_DEEP = _c("#e0a077")
HAIR = _c("#4a2f1d")
HAIR_MID = _c("#5f3d25")
HAIR_HI = _c("#7c5334")
IRIS = _c("#6b4226")
IRIS_DK = _c("#3d2412")
EYE_LINE = _c("#3a2418")
MOUTH_IN = _c("#8c4b3f")
TONGUE = _c("#d98a86")
BLUSH = _c("#f08f8f")
HOODIE = _c("#3e7bd4")
HOODIE_HI = _c("#6ea6e8")
HOODIE_DK = _c("#2a5aa6")
HOODIE_DEEP = _c("#1d427d")
PANTS = _c("#4c5670")
PANTS_DK = _c("#39415a")
SHOE = _c("#e0574a")
SHOE_DK = _c("#b13c33")
SOLE = _c("#f4f1ea")
EMBLEM_BG = _c("#ffffff")
WOOD = _c("#c89b6b")
WOOD_DK = _c("#a87b4f")
WOOD_DEEP = _c("#8a6242")
PAPER = _c("#fffdf6")
INK = _c("#5b74a8")
GADGET = _c("#3a4256")
GADGET_HI = _c("#58627a")
SHADOW = (0.0, 0.0, 0.0)
ACCENT_SPARK = _c("#ffd166")
ACCENT_SPARK2 = _c("#8fd3ff")
ACCENT_WARN = _c("#e0a13a")


# --------------------------------------------------------------------------- #
# Pose — a float-only snapshot of the whole character.
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class Pose:
    """Every animatable knob of the character (floats only).

    A pose is a pure value: poses can be linearly blended
    (:meth:`lerp`) to give smooth ~0.25 s transitions when the state
    changes, and are deterministic functions of (state, t).
    """

    # head / face
    head_dx: float = 0.0
    head_dy: float = 0.0
    head_tilt: float = 0.0        # degrees
    gaze_x: float = 0.0           # -1 .. 1
    gaze_y: float = 0.0           # -1 .. 1
    blink: float = 0.0            # 0 open .. 1 closed
    eye_open: float = 1.0         # openness scale (sleepy < 1 < surprised)
    brow_raise: float = 0.0
    brow_worry: float = 0.0       # + concerned, - focused
    mouth_smile: float = 0.25     # -1 frown .. 1 big smile
    mouth_open: float = 0.0
    blush: float = 0.3
    # body
    bob: float = 0.0              # vertical offset (breathing, jumping)
    sway: float = 0.0             # horizontal offset
    lean: float = 0.0             # torso lean degrees (+ forward)
    breath: float = 0.5           # 0..1 breathing phase
    # arms: hand targets in design space (elbows solved with 2-link IK)
    r_hand_x: float = 160.0
    r_hand_y: float = 186.0
    l_hand_x: float = 60.0
    l_hand_y: float = 188.0
    r_bend: float = 1.0           # elbow bend side (+1 out, -1 in)
    l_bend: float = -1.0
    # legs (degrees of swing from vertical)
    l_leg: float = 0.0
    r_leg: float = 0.0

    @classmethod
    def lerp(cls, a: "Pose", b: "Pose", u: float) -> "Pose":
        """Smoothly blend pose *a* to pose *b* (u in 0..1, smoothed)."""
        u = 0.0 if u <= 0.0 else (1.0 if u >= 1.0 else u * u * (3.0 - 2.0 * u))
        values = {}
        for f in fields(cls):
            va = getattr(a, f.name)
            vb = getattr(b, f.name)
            values[f.name] = va + (vb - va) * u
        return cls(**values)


#: The relaxed standing pose every state starts from.
def _relaxed() -> Pose:
    return Pose()


# --------------------------------------------------------------------------- #
# Timing helpers — deterministic, elapsed-time based (never frame-based).
# --------------------------------------------------------------------------- #


def _hash01(i: int, salt: int = 0) -> float:
    """Deterministic pseudo-random in [0, 1) (no RNG state, no I/O)."""
    x = (i * 374761393 + salt * 668265263 + 1442695041) & 0xFFFFFFFF
    x = ((x ^ (x >> 13)) * 1274126177) & 0xFFFFFFFF
    return ((x ^ (x >> 16)) & 0xFFFFFFFF) / 0x100000000


def blink_amount(t: float, salt: int = 0) -> float:
    """Natural blink rhythm as a function of elapsed time.

    Returns 0.0 (open) .. 1.0 (fully closed).  Blink moments are
    pseudo-random per cycle (2.8 s base) so the rhythm never looks
    metronomic, with an occasional double blink.  Because it is a pure
    function of elapsed seconds, blinks stay correct even when frames
    are delayed.
    """
    if t <= 0.0:
        return 0.0
    cycle = 3.4
    k = math.floor(t / cycle)
    best = 0.0
    for kk in (int(k) - 1, int(k)):
        if kk < 0:
            continue
        # blink near the start of each cycle, jittered by a stable hash
        base = kk * cycle + 1.2 + _hash01(kk, salt) * 1.8
        dur = 0.06  # half-closure -> ~0.12 s blink
        d = t - base
        if -dur <= d <= dur:
            best = max(best, 1.0 - abs(d) / dur)
        elif _hash01(kk, salt + 13) > 0.7:  # occasional quick double-blink
            d2 = t - (base + 0.18)
            if -dur <= d2 <= dur:
                best = max(best, 1.0 - abs(d2) / dur)
    return best


def _breath(t: float, period: float) -> float:
    """0..1 breathing phase (0 = inhale top, 1 = exhale bottom)."""
    return 0.5 + 0.5 * math.sin(2.0 * math.pi * t / period)


def _sine(t: float, period: float, phase: float = 0.0) -> float:
    return math.sin(2.0 * math.pi * t / period + phase)


def _envelope(t: float, attack: float, hold: float, release: float) -> float:
    """0 -> 1 over *attack* s, 1 for *hold* s, 1 -> 0 over *release* s."""
    if t < 0.0:
        return 0.0
    if t < attack:
        return t / attack
    if t < attack + hold:
        return 1.0
    if t < attack + hold + release:
        return 1.0 - (t - attack - hold) / release
    return 0.0


# --------------------------------------------------------------------------- #
# Pose computation — pure, per state.
# --------------------------------------------------------------------------- #


def _apply_expression(p: Pose, expression: str) -> Pose:
    """Expression baseline: face knobs an expression sets by default."""
    kw: Dict[str, float] = {}
    if expression == "happy":
        kw = {"mouth_smile": 0.8, "blush": 0.5, "brow_raise": 0.18}
    elif expression == "proud":
        kw = {"mouth_smile": 0.9, "brow_raise": 0.3, "blush": 0.5,
              "eye_open": 0.92}
    elif expression == "surprised":
        kw = {"eye_open": 1.28, "mouth_open": 0.55, "mouth_smile": 0.0,
              "brow_raise": 0.6}
    elif expression == "worried":
        kw = {"brow_worry": 1.0, "mouth_smile": -0.45, "eye_open": 1.06}
    elif expression == "hopeful":
        kw = {"brow_raise": 0.38, "mouth_smile": 0.4, "gaze_y": -0.15,
              "blush": 0.4}
    elif expression == "sleepy":
        kw = {"eye_open": 0.15, "mouth_smile": 0.1, "brow_raise": -0.1}
    elif expression == "focused":
        kw = {"eye_open": 0.9, "mouth_smile": 0.12, "brow_raise": -0.15,
              "brow_worry": -0.35}
    elif expression == "thoughtful":
        kw = {"brow_raise": 0.32, "mouth_smile": 0.15, "gaze_y": -0.35}
    elif expression == "attentive":
        kw = {"eye_open": 1.14, "brow_raise": 0.3, "mouth_smile": 0.32}
    elif expression == "determined":
        kw = {"eye_open": 0.96, "brow_raise": -0.2, "brow_worry": -0.5,
              "mouth_smile": 0.28}
    if not kw:
        return p
    values = {f.name: getattr(p, f.name) for f in fields(Pose)}
    values.update(kw)
    return Pose(**values)


def _base(expression: str, t: float, period: float = 3.6) -> Pose:
    """Relaxed standing pose + breathing + blinking + micro-movements."""
    br = _breath(t, period)
    p = _apply_expression(_relaxed(), expression)
    return Pose(
        **{**{f.name: getattr(p, f.name) for f in fields(Pose)},
           "bob": -1.6 * br,
           "breath": br,
           "blink": blink_amount(t),
           "sway": 0.8 * _sine(t, 5.3),
        })


def _pose_idle(t: float, expression: str) -> Pose:
    p = _base(expression, t, 3.6)
    head_micro = 0.9 * _sine(t, 4.1) + 0.5 * _sine(t, 2.7, 1.1)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "head_dx": 0.7 * _sine(t, 6.2),
                   "head_tilt": 1.2 * _sine(t, 4.7, 0.6) + head_micro * 0.4,
                   "l_hand_x": 60.0, "l_hand_y": 186.0 + 0.8 * _sine(t, 3.6),
                   "r_hand_x": 160.0, "r_hand_y": 186.0 + 0.8 * _sine(t, 3.6, 0.4)})


def _pose_sleeping(t: float, expression: str) -> Pose:
    p = _base("sleepy", t, 5.4)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 0.0, "blink": 0.0,
                   "head_dx": 3.5, "head_dy": 5.0, "head_tilt": 7.0,
                   "mouth_smile": 0.08, "lean": 3.0,
                   "bob": -1.0 - 1.4 * p.breath,
                   "l_hand_x": 76.0, "l_hand_y": 196.0,
                   "r_hand_x": 144.0, "r_hand_y": 196.0,
                   "sway": 0.4 * _sine(t, 7.0)})


def _pose_resting(t: float, expression: str) -> Pose:
    p = _base("sleepy", t, 5.0)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 0.1, "blink": 0.0,
                   "head_tilt": 3.0, "mouth_smile": 0.15,
                   "l_hand_y": 190.0, "r_hand_y": 190.0})


def _pose_waking_up(t: float, expression: str) -> Pose:
    stretch = _envelope(t, 0.7, 0.9, 0.7)     # big morning stretch
    eye = min(1.0, 0.25 + t / 1.1)            # eyes open as he wakes
    p = _base("surprised", t, 3.2)
    yawn = _envelope(t, 0.5, 0.6, 0.6)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": eye * p.eye_open,
                   "mouth_open": 0.6 * yawn,
                   "mouth_smile": 0.3 * (1.0 - yawn) + 0.15,
                   "brow_raise": 0.5 * stretch + 0.1,
                   "bob": -3.0 * stretch - 1.2 * p.breath,
                   "lean": -4.0 * stretch,
                   "head_tilt": -3.0 * stretch,
                   "l_hand_x": 64.0 + (1.0 - stretch) * 4.0,
                   "l_hand_y": 188.0 - stretch * 96.0,
                   "r_hand_x": 156.0 - (1.0 - stretch) * 4.0,
                   "r_hand_y": 188.0 - stretch * 96.0,
                   "l_bend": -1.0, "r_bend": 1.0})


def _pose_greeting(t: float, expression: str) -> Pose:
    p = _base("happy", t, 3.4)
    raise_up = _envelope(t, 0.35, 4.3, 1.0)    # raise -> wave -> lower
    wave = math.sin(t * 9.0) * raise_up
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "mouth_smile": 0.95, "blush": 0.55,
                   "brow_raise": 0.3,
                   "head_tilt": 5.0 * raise_up + 1.0 * _sine(t, 5.0),
                   "sway": 1.4 * _sine(t, 3.0),
                   "r_hand_x": 152.0 + raise_up * (14.0 + 7.0 * wave),
                   "r_hand_y": 188.0 - raise_up * (108.0 - 5.0 * abs(wave)),
                   "r_bend": 1.0,
                   "l_hand_x": 70.0, "l_hand_y": 186.0})


def _pose_listening(t: float, expression: str) -> Pose:
    p = _base("attentive", t, 3.4)
    nod = math.sin(t * 2.1)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 1.16, "brow_raise": 0.34,
                   "mouth_smile": 0.36,
                   "head_tilt": 4.0 + 1.2 * nod,
                   "head_dy": 0.6 * nod,
                   "lean": 2.0 + 0.8 * nod,
                   "l_hand_x": 92.0, "l_hand_y": 182.0,
                   "r_hand_x": 128.0, "r_hand_y": 182.0,
                   "l_bend": -1.0, "r_bend": 1.0})


def _pose_thinking(t: float, expression: str) -> Pose:
    p = _base("thoughtful", t, 3.8)
    ponder = math.sin(t * 1.3)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "gaze_x": 0.45 + 0.1 * ponder,
                   "gaze_y": -0.62,
                   "brow_raise": 0.42, "mouth_smile": 0.12,
                   "head_tilt": -4.5 + 1.0 * ponder,
                   "head_dy": -1.0,
                   "lean": -2.5,
                   # right hand to chin, left hand supporting the elbow
                   "r_hand_x": 126.0, "r_hand_y": 108.0,
                   "r_bend": -1.0,
                   "l_hand_x": 92.0, "l_hand_y": 178.0,
                   "l_bend": -1.0,
                   "sway": 0.6 * ponder})


def _pose_speaking(t: float, expression: str) -> Pose:
    """v0.9 simple talking animation (documented): a friendly face
    whose mouth opens and closes at syllable rate while the head nods
    gently.  Pure and deterministic like every other pose."""
    p = _base("happy", t, 3.6)
    talk = math.sin(t * 9.0)           # syllable-rate mouth movement
    mouth = 0.30 + 0.20 * abs(talk)    # 0.30..0.50 open while speaking
    nod = math.sin(t * 2.4)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 1.06, "brow_raise": 0.26,
                   "mouth_smile": 0.5, "mouth_open": mouth,
                   "head_dy": 0.5 * nod,
                   "head_tilt": 1.5 * nod,
                   "lean": 1.5 + 0.5 * nod,
                   "l_hand_x": 96.0, "l_hand_y": 184.0,
                   "r_hand_x": 124.0, "r_hand_y": 184.0})


def _pose_walking(t: float, expression: str) -> Pose:
    p = _base("determined", t, 3.2)
    phase = t * 4.6
    swing = math.sin(phase)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 1.0, "mouth_smile": 0.35,
                   "lean": 3.0,
                   "bob": -2.4 * abs(math.sin(phase)) - 1.0 * p.breath,
                   "sway": 1.2 * swing,
                   "l_leg": 15.0 * swing, "r_leg": -15.0 * swing,
                   "l_hand_x": 62.0 - 9.0 * swing,
                   "r_hand_x": 158.0 + 9.0 * swing,
                   "l_hand_y": 186.0 - 3.0 * abs(swing),
                   "r_hand_y": 186.0 - 3.0 * abs(swing)})


def _pose_writing(t: float, expression: str) -> Pose:
    p = _base("focused", t, 3.5)
    scribble = math.sin(t * 7.0)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 0.92, "gaze_y": 0.5,
                   "head_dy": 3.0, "head_tilt": -3.0,
                   "lean": 11.0,
                   "l_hand_x": 78.0, "l_hand_y": 188.0,   # holding the page
                   "r_hand_x": 98.0 + 3.5 * scribble,
                   "r_hand_y": 184.0 + 1.8 * math.sin(t * 14.0),
                   "l_bend": -1.0, "r_bend": 1.0})


def _pose_coding(t: float, expression: str) -> Pose:
    p = _base("focused", t, 3.3)
    tap_l = max(0.0, math.sin(t * 8.2))
    tap_r = max(0.0, math.sin(t * 8.2 + math.pi))
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 0.9, "gaze_y": 0.34,
                   "head_dy": 1.5, "lean": 6.0,
                   "mouth_smile": 0.16,
                   "l_hand_x": 92.0, "l_hand_y": 187.0 + 2.4 * tap_l,
                   "r_hand_x": 130.0, "r_hand_y": 187.0 + 2.4 * tap_r,
                   "l_bend": -1.0, "r_bend": 1.0})


def _pose_reading(t: float, expression: str) -> Pose:
    p = _base("focused", t, 3.6)
    scan = math.sin(t * 1.6)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 0.95, "gaze_x": 0.35 * scan, "gaze_y": 0.25,
                   "head_dy": 1.0, "head_tilt": -1.5,
                   "l_hand_x": 86.0, "l_hand_y": 156.0,
                   "r_hand_x": 134.0, "r_hand_y": 156.0,
                   "l_bend": -1.0, "r_bend": 1.0})


def _pose_waiting(t: float, expression: str) -> Pose:
    p = _base("hopeful", t, 3.7)
    raise_up = _envelope(t, 0.3, 10.0, 0.5)
    sway = math.sin(t * 1.4)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "brow_raise": 0.45, "mouth_smile": 0.38,
                   "head_tilt": 3.0 + 1.0 * sway,
                   "sway": 1.0 * sway,
                   "r_hand_x": 152.0 + raise_up * 14.0,
                   "r_hand_y": 188.0 - raise_up * 92.0,
                   "r_bend": 1.0,
                   "l_hand_x": 62.0, "l_hand_y": 186.0})


def _pose_working(t: float, expression: str) -> Pose:
    p = _base("determined", t, 3.1)
    turn = math.sin(t * 3.2)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 0.98, "mouth_smile": 0.42,
                   "brow_worry": -0.3,
                   "head_tilt": -2.0 + 1.0 * turn,
                   "bob": -1.4 * p.breath - 0.8 * abs(turn),
                   "r_hand_x": 158.0, "r_hand_y": 148.0 + 5.0 * turn,
                   "r_bend": 1.0,
                   "l_hand_x": 68.0, "l_hand_y": 190.0})


def _pose_verifying(t: float, expression: str) -> Pose:
    p = _base("attentive", t, 3.4)
    sweep = math.sin(t * 1.7)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 1.1, "gaze_x": 0.55 * sweep,
                   "brow_raise": 0.3, "mouth_smile": 0.22,
                   "head_tilt": 2.0 * sweep,
                   "r_hand_x": 148.0, "r_hand_y": 96.0,
                   "r_bend": -1.0,
                   "l_hand_x": 62.0, "l_hand_y": 186.0})


def _pose_celebrating(t: float, expression: str) -> Pose:
    p = _base("proud", t, 2.4)
    jump = abs(math.sin(t * 4.2))
    cheer = math.sin(t * 8.0)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 1.0, "mouth_smile": 1.0, "mouth_open": 0.45,
                   "brow_raise": 0.5, "blush": 0.55,
                   "bob": -10.0 * jump - 1.0 * p.breath,
                   "lean": -2.0 + 1.5 * cheer,
                   "head_tilt": 3.0 * cheer,
                   "l_hand_x": 66.0 - 3.0 * cheer,
                   "l_hand_y": 96.0 + 4.0 * abs(cheer),
                   "r_hand_x": 154.0 + 3.0 * cheer,
                   "r_hand_y": 96.0 + 4.0 * abs(cheer),
                   "l_leg": -6.0 - 4.0 * jump, "r_leg": 6.0 + 4.0 * jump})


def _pose_error(t: float, expression: str) -> Pose:
    p = _base("worried", t, 4.0)
    shake = math.sin(t * 12.0)
    return Pose(**{**{f.name: getattr(p, f.name) for f in fields(Pose)},
                   "eye_open": 1.08, "brow_worry": 1.0,
                   "mouth_smile": -0.5,
                   "head_tilt": -6.0 + 0.8 * shake,
                   "head_dy": 1.0,
                   "lean": -3.0,
                   "sway": 0.5 * shake,
                   # hand at the back of the head (sheepish scratch)
                   "r_hand_x": 152.0, "r_hand_y": 78.0,
                   "r_bend": -1.0,
                   "l_hand_x": 66.0, "l_hand_y": 188.0})


#: Per-state pose builders (unknown states fall back to idle, never crash).
_STATE_POSES = {
    "idle": _pose_idle,
    "sleeping": _pose_sleeping,
    "resting": _pose_resting,
    "waking_up": _pose_waking_up,
    "greeting": _pose_greeting,
    "listening": _pose_listening,
    "thinking": _pose_thinking,
    "speaking": _pose_speaking,
    "walking": _pose_walking,
    "writing": _pose_writing,
    "coding": _pose_coding,
    "reading": _pose_reading,
    "waiting_for_approval": _pose_waiting,
    "working": _pose_working,
    "verifying": _pose_verifying,
    "celebrating": _pose_celebrating,
    "error": _pose_error,
}


def compute_pose(state: str, t: float = 0.0, expression: str = "neutral") -> Pose:
    """The pose for *state* at elapsed time *t* (pure and deterministic)."""
    builder = _STATE_POSES.get(state, _pose_idle)
    return builder(t, expression)


# --------------------------------------------------------------------------- #
# Cairo primitives.
# --------------------------------------------------------------------------- #


def _set(ctx, color, alpha: Optional[float] = None) -> None:
    if alpha is None:
        ctx.set_source_rgba(*color)
    else:
        ctx.set_source_rgba(color[0], color[1], color[2], alpha)


def _ellipse(ctx, cx, cy, rx, ry) -> None:
    ctx.save()
    ctx.translate(cx, cy)
    ctx.scale(max(rx, 0.01), max(ry, 0.01))
    ctx.arc(0, 0, 1.0, 0, 2.0 * math.pi)
    ctx.restore()


def _round_rect(ctx, x, y, w, h, r) -> None:
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + w - r, y + r, r, -math.pi / 2.0, 0.0)
    ctx.arc(x + w - r, y + h - r, r, 0.0, math.pi / 2.0)
    ctx.arc(x + r, y + h - r, r, math.pi / 2.0, math.pi)
    ctx.arc(x + r, y + r, r, math.pi, 3.0 * math.pi / 2.0)
    ctx.close_path()


def _limb(ctx, x1, y1, x2, y2, r1, r2) -> None:
    """A tapered rounded capsule from (x1, y1) to (x2, y2)."""
    dx, dy = x2 - x1, y2 - y1
    length = math.hypot(dx, dy)
    if length < 1e-6:
        _ellipse(ctx, x1, y1, r1, r1)
        return
    ux, uy = dx / length, dy / length
    px, py = -uy, ux
    phi = math.atan2(py, px)
    ctx.new_sub_path()
    ctx.arc(x1, y1, r1, phi, phi + math.pi)
    ctx.line_to(x2 - px * r2, y2 - py * r2)
    ctx.arc(x2, y2, r2, phi + math.pi, phi + 2.0 * math.pi)
    ctx.close_path()


def _ik(sx, sy, tx, ty, l1, l2, bend: float):
    """Two-link IK: elbow and hand positions reaching toward the target."""
    dx, dy = tx - sx, ty - sy
    d = math.hypot(dx, dy)
    max_d = l1 + l2 - 0.6
    if d > max_d:
        if d > 1e-6:
            dx, dy = dx * max_d / d, dy * max_d / d
        d = max_d
        tx, ty = sx + dx, sy + dy
    d = max(d, 2.0)
    cos_a = (l1 * l1 + d * d - l2 * l2) / (2.0 * l1 * d)
    a = math.acos(max(-1.0, min(1.0, cos_a)))
    base = math.atan2(dx, dy)
    ang = base + bend * a
    ex, ey = sx + l1 * math.sin(ang), sy + l1 * math.cos(ang)
    ang2 = math.atan2(tx - ex, ty - ey)
    hx, hy = ex + l2 * math.sin(ang2), ey + l2 * math.cos(ang2)
    return (ex, ey), (hx, hy)


def _rot(px, py, ox, oy, deg: float):
    """Rotate point (px, py) about (ox, oy) by *deg* degrees."""
    if deg == 0.0:
        return px, py
    r = math.radians(deg)
    s, c = math.sin(r), math.cos(r)
    dx, dy = px - ox, py - oy
    return ox + dx * c - dy * s, oy + dx * s + dy * c


# --------------------------------------------------------------------------- #
# Shared components — one head, one hoodie, one pair of arms for every state.
# --------------------------------------------------------------------------- #


def _draw_shadow(ctx, ox: float, oy: float, pose: Pose) -> None:
    lift = max(0.0, -pose.bob)                     # jumping shrinks it
    rx = 38.0 - lift * 0.9
    _ellipse(ctx, CX + ox * 0.5, GROUND_Y + 2.0, max(rx, 16.0), 6.5)
    _set(ctx, SHADOW, 0.13)
    ctx.fill()


def _draw_leg(ctx, hip_x: float, hip_y: float, deg: float) -> None:
    r = math.radians(deg)
    ankle_x = hip_x + 38.0 * math.sin(r)
    ankle_y = hip_y + 38.0 * math.cos(r)
    # trousers
    _limb(ctx, hip_x, hip_y, ankle_x, ankle_y, 10.0, 7.4)
    grad = cairo.LinearGradient(hip_x - 9, hip_y, hip_x + 9, hip_y)
    grad.add_color_stop_rgba(0.0, *PANTS_DK)
    grad.add_color_stop_rgba(0.45, *PANTS)
    grad.add_color_stop_rgba(1.0, *PANTS_DK)
    ctx.set_source(grad)
    ctx.fill_preserve()
    _set(ctx, PANTS_DK, 0.9)
    ctx.set_line_width(1.1)
    ctx.stroke()
    # shoe (rotates with the leg; white sole + toe cap)
    ctx.save()
    ctx.translate(ankle_x, ankle_y)
    ctx.rotate(r)
    _round_rect(ctx, -11.5, 1.0, 23.0, 12.5, 5.6)
    grad2 = cairo.LinearGradient(0, 1.0, 0, 13.5)
    grad2.add_color_stop_rgba(0.0, *SHOE)
    grad2.add_color_stop_rgba(1.0, *SHOE_DK)
    ctx.set_source(grad2)
    ctx.fill_preserve()
    _set(ctx, SHOE_DK, 0.8)
    ctx.set_line_width(1.0)
    ctx.stroke()
    _round_rect(ctx, -11.5, 8.6, 23.0, 5.4, 2.4)     # sole
    _set(ctx, SOLE)
    ctx.fill()
    _ellipse(ctx, 6.6, 6.4, 4.6, 3.4)                # toe cap highlight
    _set(ctx, SOLE, 0.55)
    ctx.fill()
    ctx.save()                                        # laces
    _set(ctx, SOLE, 0.85)
    ctx.set_line_width(1.2)
    for i in range(2):
        ctx.move_to(-4.5 + i * 4.5, 3.2)
        ctx.line_to(-2.5 + i * 4.5, 6.0)
        ctx.stroke()
    ctx.restore()
    ctx.restore()


def _draw_torso(ctx, hx: float, hy: float, pose: Pose) -> None:
    """The blue hoodie body, drawn from the rotated shoulder/hip points."""
    slx, sly = hx - 33.0, hy
    srx, sry = hx + 33.0, hy
    lhx, lhy = CX - 30.0 + pose.sway, HIP_Y + pose.bob
    rhx, rhy = CX + 30.0 + pose.sway, HIP_Y + pose.bob
    ctx.move_to(slx, sly)
    ctx.curve_to(slx - 5.0, sly + 22.0, lhx - 8.0, lhy - 24.0, lhx, lhy + 4.0)
    ctx.line_to(rhx, rhy + 4.0)
    ctx.curve_to(rhx + 8.0, rhy - 24.0, srx + 5.0, sry + 22.0, srx, sry)
    ctx.curve_to(srx - 8.0, sry - 7.0, slx + 8.0, sly - 7.0, slx, sly)
    ctx.close_path()
    grad = cairo.LinearGradient(0, sly - 6.0, 0, rhy + 4.0)
    grad.add_color_stop_rgba(0.0, *HOODIE_HI)
    grad.add_color_stop_rgba(0.45, *HOODIE)
    grad.add_color_stop_rgba(1.0, *HOODIE_DK)
    ctx.set_source(grad)
    ctx.fill_preserve()
    _set(ctx, HOODIE_DEEP, 0.55)
    ctx.set_line_width(1.2)
    ctx.stroke()
    # soft side shading for a rounded, 3D-ish look
    ctx.save()
    ctx.clip()
    shade = cairo.LinearGradient(hx - 36.0, 0, hx + 36.0, 0)
    shade.add_color_stop_rgba(0.0, 0.0, 0.0, 0.0, 0.10)
    shade.add_color_stop_rgba(0.3, 0.0, 0.0, 0.0, 0.0)
    shade.add_color_stop_rgba(0.72, 0.0, 0.0, 0.0, 0.0)
    shade.add_color_stop_rgba(1.0, 0.0, 0.0, 0.0, 0.12)
    ctx.set_source(shade)
    ctx.paint()
    ctx.restore()
    # kangaroo pocket + hem band
    _round_rect(ctx, CX - 22.0 + pose.sway, lhy - 26.0, 44.0, 20.0, 7.0)
    _set(ctx, HOODIE_DK, 0.85)
    ctx.fill()
    _round_rect(ctx, CX - 31.0 + pose.sway, lhy - 8.0, 62.0, 11.0, 5.0)
    _set(ctx, HOODIE_DEEP, 0.75)
    ctx.fill()
    # zipper line + drawstrings
    _set(ctx, HOODIE_DEEP, 0.55)
    ctx.set_line_width(1.4)
    ctx.move_to(hx, hy + 2.0)
    ctx.line_to(hx, lhy - 26.0)
    ctx.stroke()
    for side in (-1.0, 1.0):
        ctx.set_line_width(1.8)
        _set(ctx, SOLE, 0.9)
        ctx.move_to(hx + side * 5.0, hy + 1.0)
        ctx.line_to(hx + side * 6.5, hy + 20.0)
        ctx.stroke()
        _ellipse(ctx, hx + side * 6.5, hy + 21.5, 1.9, 1.9)
        ctx.fill()
    # original ALWIN emblem: a white badge with a chevron "A" mark.
    ex, ey = hx, hy + 34.0
    _ellipse(ctx, ex, ey, 11.0, 11.0)
    _set(ctx, EMBLEM_BG, 0.96)
    ctx.fill()
    _set(ctx, HOODIE_DEEP, 0.9)
    ctx.set_line_width(1.6)
    ctx.move_to(ex - 6.0, ey + 5.0)
    ctx.line_to(ex, ey - 5.5)
    ctx.line_to(ex + 6.0, ey + 5.0)
    ctx.stroke()
    ctx.set_line_width(1.4)
    ctx.move_to(ex - 3.2, ey + 1.2)
    ctx.line_to(ex + 3.2, ey + 1.2)
    ctx.stroke()


def _draw_arm(ctx, sx: float, sy: float, target, bend: float):
    """Sleeved two-segment arm with a hand; returns the hand position."""
    tx, ty = target
    (ex, ey), (hx, hy) = _ik(sx, sy, tx, ty, ARM_L1, ARM_L2, bend)
    # upper arm + forearm (hoodie sleeve)
    _limb(ctx, sx, sy, ex, ey, 11.0, 8.8)
    _limb(ctx, ex, ey, hx, hy, 8.6, 6.6)
    grad = cairo.LinearGradient(sx - 10.0, sy, sx + 10.0, sy)
    grad.add_color_stop_rgba(0.0, *HOODIE_DK)
    grad.add_color_stop_rgba(0.5, *HOODIE)
    grad.add_color_stop_rgba(1.0, *HOODIE_DK)
    ctx.set_source(grad)
    ctx.fill_preserve()
    _set(ctx, HOODIE_DEEP, 0.45)
    ctx.set_line_width(1.0)
    ctx.stroke()
    # shoulder cap
    _ellipse(ctx, sx, sy, 11.0, 11.0)
    _set(ctx, HOODIE)
    ctx.fill_preserve()
    _set(ctx, HOODIE_DEEP, 0.4)
    ctx.set_line_width(1.0)
    ctx.stroke()
    # cuff + hand (round, warm, thumb bump)
    ang = math.atan2(hx - ex, hy - ey)
    ctx.save()
    ctx.translate(hx - 5.6 * math.sin(ang), hy - 5.6 * math.cos(ang))
    ctx.rotate(-ang)
    _round_rect(ctx, -6.2, -3.4, 12.4, 6.8, 3.0)
    _set(ctx, HOODIE_DEEP, 0.9)
    ctx.fill()
    ctx.restore()
    _ellipse(ctx, hx, hy, HAND_R, HAND_R * 1.06)
    grad2 = cairo.LinearGradient(hx - 7, hy - 7, hx + 5, hy + 7)
    grad2.add_color_stop_rgba(0.0, *_mix(SKIN, (1, 1, 1, 1), 0.25))
    grad2.add_color_stop_rgba(1.0, *SKIN_SH)
    ctx.set_source(grad2)
    ctx.fill_preserve()
    _set(ctx, SKIN_DEEP, 0.55)
    ctx.set_line_width(1.0)
    ctx.stroke()
    thumb_x = hx + 5.4 * math.sin(ang + 1.1)
    thumb_y = hy + 5.4 * math.cos(ang + 1.1)
    _ellipse(ctx, thumb_x, thumb_y, 3.0, 3.2)
    _set(ctx, SKIN)
    ctx.fill()
    return hx, hy


def _mix(a, b, t: float):
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(4))


def _draw_head(ctx, hx: float, hy: float, pose: Pose, t: float) -> None:
    """The head: ears, face, big brown eyes, brows, nose, mouth, hair."""
    tilt = math.radians(pose.head_tilt)
    ctx.save()
    ctx.translate(hx, hy)
    ctx.rotate(tilt)
    ctx.translate(-hx, -hy)
    # ears (tucked beside the face, below the hair line)
    for side in (-1.0, 1.0):
        _ellipse(ctx, hx + side * (HEAD_RX - 1.0), hy + 12.0, 5.2, 6.6)
        _set(ctx, SKIN_SH)
        ctx.fill_preserve()
        _set(ctx, SKIN_DEEP, 0.5)
        ctx.set_line_width(1.0)
        ctx.stroke()
    # head shape with a soft radial gradient (light from upper-left)
    grad = cairo.RadialGradient(hx - 16.0, hy - 20.0, 8.0, hx, hy, 62.0)
    grad.add_color_stop_rgba(0.0, *_mix(SKIN, (1, 1, 1, 1), 0.35))
    grad.add_color_stop_rgba(0.62, *SKIN)
    grad.add_color_stop_rgba(1.0, *SKIN_SH)
    _ellipse(ctx, hx, hy + 6.0, HEAD_RX, HEAD_RY)
    ctx.set_source(grad)
    ctx.fill_preserve()
    _set(ctx, SKIN_DEEP, 0.4)
    ctx.set_line_width(1.1)
    ctx.stroke()
    # chin/neck shadow at the bottom of the face
    ctx.save()
    ctx.clip()
    sh = cairo.LinearGradient(0, hy + 14.0, 0, hy + HEAD_RY + 8.0)
    sh.add_color_stop_rgba(0.0, SKIN_DEEP[0], SKIN_DEEP[1], SKIN_DEEP[2], 0.0)
    sh.add_color_stop_rgba(1.0, SKIN_DEEP[0], SKIN_DEEP[1], SKIN_DEEP[2], 0.35)
    ctx.set_source(sh)
    ctx.paint()
    ctx.restore()
    # ---- face ---------------------------------------------------------- #
    spacing = 20.0
    eye_y = hy + 4.0
    openness = max(0.0, pose.eye_open * (1.0 - pose.blink))
    for side in (-1.0, 1.0):
        ex = hx + side * spacing + pose.gaze_x * 2.4
        _draw_eye(ctx, ex, eye_y, openness, pose, side)
    # brows
    for side in (-1.0, 1.0):
        bx = hx + side * spacing
        by = eye_y - 13.5 - pose.brow_raise * 3.0
        inner = bx - side * 7.5
        outer = bx + side * 8.5
        # inner ends rise when worried (+1), drop when focused (-1)
        inner_y = by - pose.brow_worry * 2.0
        ctx.set_line_width(3.4)
        ctx.set_line_cap(1)  # round
        _set(ctx, HAIR)
        ctx.move_to(inner, inner_y)
        ctx.curve_to(bx - side * 2.0, by - 1.6, bx + side * 3.0, by - 1.2,
                     outer, by + 1.4)
        ctx.stroke()
    # nose
    _ellipse(ctx, hx + 1.5, hy + 13.5, 3.4, 2.6)
    _set(ctx, SKIN_SH, 0.9)
    ctx.fill()
    _ellipse(ctx, hx + 0.6, hy + 12.4, 1.8, 1.3)
    _set(ctx, _mix(SKIN, (1, 1, 1, 1), 0.4), 0.9)
    ctx.fill()
    # mouth
    _draw_mouth(ctx, hx + 1.0, hy + 24.0, pose)
    # cheeks
    for side in (-1.0, 1.0):
        _ellipse(ctx, hx + side * 30.5, hy + 15.0, 7.2, 4.6)
        _set(ctx, BLUSH, 0.30 * pose.blush * 2.0)
        ctx.fill()
    # ---- hair (drawn last: tousled cap + fringe over the forehead) ----- #
    _draw_hair(ctx, hx, hy)
    ctx.restore()


def _draw_eye(ctx, ex: float, ey: float, openness: float, pose: Pose,
              side: float) -> None:
    if openness <= 0.14:
        # closed eye: a gentle downward arc
        ctx.save()
        ctx.set_line_width(2.6)
        ctx.set_line_cap(1)
        _set(ctx, EYE_LINE)
        ctx.arc(ex, ey - 0.5, 8.6, math.pi * 0.16, math.pi * 0.84)
        ctx.stroke()
        ctx.restore()
        return
    ry = 10.4 * min(1.05, openness)
    rx = 9.4
    ctx.save()
    _ellipse(ctx, ex, ey, rx, ry)
    ctx.clip()
    # sclera
    grad = cairo.LinearGradient(0, ey - ry, 0, ey + ry)
    grad.add_color_stop_rgba(0.0, 1.0, 1.0, 1.0, 1.0)
    grad.add_color_stop_rgba(1.0, *_c("#e8ded2"))
    ctx.set_source(grad)
    ctx.paint()
    # iris (brown) with gaze offset
    gx = ex + pose.gaze_x * 3.4
    gy = ey + pose.gaze_y * 2.6 + 0.6
    irad = 6.9
    iris_grad = cairo.RadialGradient(gx - 1.6, gy - 2.0, 1.0, gx, gy, irad)
    iris_grad.add_color_stop_rgba(0.0, *_mix(IRIS, (1, 1, 1, 1), 0.35))
    iris_grad.add_color_stop_rgba(0.7, *IRIS)
    iris_grad.add_color_stop_rgba(1.0, *IRIS_DK)
    _ellipse(ctx, gx, gy, irad, irad)
    ctx.set_source(iris_grad)
    ctx.fill()
    # pupil + highlights
    _ellipse(ctx, gx, gy, 3.4, 3.4)
    _set(ctx, _c("#241209"))
    ctx.fill()
    _ellipse(ctx, gx - 2.2, gy - 2.4, 2.2, 2.2)
    _set(ctx, (1.0, 1.0, 1.0, 0.95))
    ctx.fill()
    _ellipse(ctx, gx + 2.0, gy + 2.2, 1.0, 1.0)
    _set(ctx, (1.0, 1.0, 1.0, 0.6))
    ctx.fill()
    # upper lid shadow
    lid = cairo.LinearGradient(0, ey - ry, 0, ey - ry + 5.0)
    lid.add_color_stop_rgba(0.0, 0.1, 0.05, 0.02, 0.35)
    lid.add_color_stop_rgba(1.0, 0.1, 0.05, 0.02, 0.0)
    ctx.set_source(lid)
    ctx.paint()
    ctx.restore()
    # lash line along the top edge
    ctx.save()
    ctx.set_line_width(2.4)
    ctx.set_line_cap(1)
    _set(ctx, EYE_LINE)
    ctx.save()
    _ellipse(ctx, ex, ey, rx + 0.4, ry + 0.4)
    ctx.clip()
    ctx.move_to(ex - rx - 1.0, ey - ry * 0.45)
    ctx.curve_to(ex - rx * 0.4, ey - ry - 1.6, ex + rx * 0.4, ey - ry - 1.6,
                 ex + rx + 1.0, ey - ry * 0.45)
    ctx.stroke()
    ctx.restore()
    ctx.restore()


def _draw_mouth(ctx, mx: float, my: float, pose: Pose) -> None:
    smile = max(-1.0, min(1.0, pose.mouth_smile))
    open_ = max(0.0, min(1.0, pose.mouth_open))
    if open_ > 0.12 and smile > 0.2:
        # open, happy mouth
        w = 9.0 + 4.0 * smile
        h = 3.0 + 8.0 * open_
        ctx.save()
        ctx.move_to(mx - w, my - 1.0)
        ctx.curve_to(mx - w * 0.5, my + h * 1.35, mx + w * 0.5, my + h * 1.35,
                     mx + w, my - 1.0)
        ctx.curve_to(mx + w * 0.45, my - 0.2 + h * 0.28,
                     mx - w * 0.45, my - 0.2 + h * 0.28, mx - w, my - 1.0)
        ctx.close_path()
        _set(ctx, MOUTH_IN)
        ctx.fill_preserve()
        ctx.clip()
        _ellipse(ctx, mx, my + h * 0.95, w * 0.62, h * 0.5)   # tongue
        _set(ctx, TONGUE, 0.9)
        ctx.fill()
        ctx.restore()
        return
    # closed mouth: a curve whose curvature follows the smile
    drop = -smile * 5.2
    ctx.save()
    ctx.set_line_width(2.6)
    ctx.set_line_cap(1)
    _set(ctx, _c("#8a5344"))
    ctx.move_to(mx - 7.5, my + drop * 0.3 - 1.0)
    ctx.curve_to(mx - 3.0, my + drop + 1.8, mx + 3.0, my + drop + 1.8,
                 mx + 7.5, my + drop * 0.3 - 1.0)
    ctx.stroke()
    if smile > 0.55:   # little smile dimples
        for side in (-1.0, 1.0):
            _ellipse(ctx, mx + side * 8.6, my + drop * 0.32 - 2.2, 1.1, 1.1)
            _set(ctx, SKIN_DEEP, 0.6)
            ctx.fill()
    ctx.restore()


def _draw_hair(ctx, hx: float, hy: float) -> None:
    """Tousled dark-brown hair: a cap over the skull with spiky tufts."""
    def pt(x, y):
        return hx + x, hy + y
    grad = cairo.LinearGradient(hx - 30, hy - 50, hx + 20, hy - 10)
    grad.add_color_stop_rgba(0.0, *HAIR_HI)
    grad.add_color_stop_rgba(0.45, *HAIR_MID)
    grad.add_color_stop_rgba(1.0, *HAIR)
    ctx.move_to(*pt(-46.0, -8.0))                      # left temple
    ctx.curve_to(*pt(-52.0, -30.0), *pt(-38.0, -44.0), *pt(-24.0, -48.0))
    ctx.line_to(*pt(-21.0, -60.0))                     # spike tuft 1
    ctx.line_to(*pt(-10.0, -49.0))
    ctx.curve_to(*pt(-2.0, -54.0), *pt(6.0, -53.0), *pt(13.0, -48.0))
    ctx.line_to(*pt(24.0, -59.0))                      # spike tuft 2
    ctx.line_to(*pt(27.0, -45.0))
    ctx.curve_to(*pt(40.0, -42.0), *pt(48.0, -30.0), *pt(47.0, -10.0))
    ctx.line_to(*pt(46.0, 0.0))                        # right sideburn base
    ctx.line_to(*pt(41.0, -14.0))                      # fringe, right to left
    ctx.curve_to(*pt(33.0, -2.0), *pt(24.0, -6.0), *pt(17.0, -14.0))
    ctx.line_to(*pt(9.0, -3.0))                        # fringe tip
    ctx.curve_to(*pt(-2.0, -16.0), *pt(-8.0, -8.0), *pt(-17.0, -15.0))
    ctx.line_to(*pt(-27.0, -5.0))                      # fringe tip 2
    ctx.curve_to(*pt(-36.0, -14.0), *pt(-40.0, -6.0), *pt(-46.0, -8.0))
    ctx.close_path()
    ctx.set_source(grad)
    ctx.fill_preserve()
    _set(ctx, HAIR, 0.9)
    ctx.set_line_width(1.0)
    ctx.stroke()
    # glossy highlight arc (3D-ish sheen)
    ctx.save()
    ctx.clip()
    ctx.set_line_width(4.0)
    ctx.set_line_cap(1)
    _set(ctx, HAIR_HI, 0.55)
    ctx.move_to(*pt(-30.0, -38.0))
    ctx.curve_to(*pt(-16.0, -46.0), *pt(4.0, -47.0), *pt(16.0, -41.0))
    ctx.stroke()
    ctx.restore()


def _draw_neck(ctx, hx: float, hy: float) -> None:
    _round_rect(ctx, hx - 9.0, hy + 26.0, 18.0, 22.0, 7.0)
    grad = cairo.LinearGradient(0, hy + 26.0, 0, hy + 48.0)
    grad.add_color_stop_rgba(0.0, *SKIN_SH)
    grad.add_color_stop_rgba(1.0, *SKIN_DEEP)
    ctx.set_source(grad)
    ctx.fill()


# --------------------------------------------------------------------------- #
# Props (honest, decorative depictions — never claims about outcomes).
# --------------------------------------------------------------------------- #


def _prop_thought_dots(ctx, x: float, y: float, t: float) -> None:
    for i in range(3):
        phase = (t * 1.3 + i * 0.5) % 2.2
        alpha = max(0.0, min(1.0, 1.6 - phase)) * 0.75
        r = 2.6 + i * 2.4
        _ellipse(ctx, x + i * 12.0, y - i * 11.0 - phase * 3.0, r, r)
        _set(ctx, HOODIE_DK, alpha)
        ctx.fill()


def _prop_sparkles(ctx, t: float) -> None:
    for i in range(6):
        ang = i * (2.0 * math.pi / 6.0) + t * 0.9
        sx = CX + math.cos(ang) * (66.0 + 6.0 * math.sin(t * 3.0 + i))
        sy = 120.0 + math.sin(ang) * 74.0
        s = 3.2 + 2.2 * abs(math.sin(t * 5.0 + i * 1.7))
        ctx.save()
        ctx.translate(sx, sy)
        ctx.rotate(ang)
        _set(ctx, ACCENT_SPARK if i % 2 == 0 else ACCENT_SPARK2, 0.9)
        ctx.move_to(0.0, -s)
        ctx.line_to(s * 0.3, -s * 0.3)
        ctx.line_to(s, 0.0)
        ctx.line_to(s * 0.3, s * 0.3)
        ctx.line_to(0.0, s)
        ctx.line_to(-s * 0.3, s * 0.3)
        ctx.line_to(-s, 0.0)
        ctx.line_to(-s * 0.3, -s * 0.3)
        ctx.close_path()
        ctx.fill()
        ctx.restore()


def _z_glyph(ctx, x: float, y: float, size: float, alpha: float) -> None:
    ctx.save()
    ctx.set_line_width(2.2)
    ctx.set_line_cap(1)
    _set(ctx, HOODIE_DK, alpha)
    ctx.move_to(x, y)
    ctx.line_to(x + size, y)
    ctx.line_to(x, y + size)
    ctx.line_to(x + size, y + size)
    ctx.stroke()
    ctx.restore()


def _prop_zs(ctx, x: float, y: float, t: float) -> None:
    for i in range(3):
        phase = (t * 0.6 + i * 0.4) % 2.4
        alpha = max(0.0, min(1.0, 1.5 - phase * 0.8)) * 0.85
        _z_glyph(ctx, x + i * 13.0, y - i * 14.0 - phase * 8.0,
                 6.0 + i * 2.5, alpha)


def _prop_sweat_drop(ctx, x: float, y: float, t: float) -> None:
    slide = (math.sin(t * 2.2) + 1.0) * 0.5
    dy = slide * 7.0
    ctx.save()
    ctx.move_to(x, y + dy)
    ctx.curve_to(x - 4.4, y + dy + 6.0, x - 3.6, y + dy + 11.0, x,
                 y + dy + 11.0)
    ctx.curve_to(x + 3.6, y + dy + 11.0, x + 4.4, y + dy + 6.0, x, y + dy)
    ctx.close_path()
    _set(ctx, _c("#8fc7f2"), 0.55 + 0.25 * math.sin(t * 4.0))
    ctx.fill()
    _ellipse(ctx, x - 1.2, y + dy + 4.0, 1.1, 1.6)
    _set(ctx, (1.0, 1.0, 1.0, 0.8))
    ctx.fill()
    ctx.restore()


def _glyph_chevrons(ctx, x: float, y: float, t: float) -> None:
    """Decorative code symbols (< / >) floating up — pure decoration."""
    for i, kind in enumerate(("lt", "slash", "gt")):
        phase = (t * 1.1 + i * 0.7) % 2.6
        alpha = max(0.0, min(1.0, 1.9 - phase)) * 0.8
        gx = x + i * 16.0
        gy = y - phase * 6.0
        ctx.save()
        ctx.set_line_width(2.4)
        ctx.set_line_cap(1)
        _set(ctx, ACCENT_SPARK if i == 1 else _c("#7ee0a8"), alpha)
        if kind == "lt":
            ctx.move_to(gx + 4.0, gy - 5.0)
            ctx.line_to(gx - 4.0, gy)
            ctx.line_to(gx + 4.0, gy + 5.0)
        elif kind == "gt":
            ctx.move_to(gx - 4.0, gy - 5.0)
            ctx.line_to(gx + 4.0, gy)
            ctx.line_to(gx - 4.0, gy + 5.0)
        else:
            ctx.move_to(gx + 2.5, gy - 5.5)
            ctx.line_to(gx - 2.5, gy + 5.5)
        ctx.stroke()
        ctx.restore()


def _prop_dots_badge(ctx, x: float, y: float, t: float) -> None:
    """A small pulsing '…' near the raised hand (waiting)."""
    for i in range(3):
        pulse = 0.55 + 0.4 * math.sin(t * 3.0 - i * 0.6)
        _ellipse(ctx, x + i * 8.0, y, 2.2, 2.2)
        _set(ctx, HOODIE_DK, pulse)
        ctx.fill()


def _prop_gear(ctx, x: float, y: float, t: float) -> None:
    ctx.save()
    ctx.translate(x, y)
    ctx.rotate(t * 1.2)
    _set(ctx, ACCENT_SPARK, 0.85)
    ctx.set_line_width(2.2)
    ctx.arc(0.0, 0.0, 6.6, 0.0, 2.0 * math.pi)
    ctx.stroke()
    for i in range(6):
        ang = i * math.pi / 3.0
        ctx.move_to(math.cos(ang) * 6.6, math.sin(ang) * 6.6)
        ctx.line_to(math.cos(ang) * 10.0, math.sin(ang) * 10.0)
        ctx.stroke()
    ctx.restore()


def _prop_sun(ctx, x: float, y: float, t: float) -> None:
    _ellipse(ctx, x, y, 7.0, 7.0)
    _set(ctx, ACCENT_SPARK, 0.9)
    ctx.fill()
    ctx.save()
    _set(ctx, ACCENT_SPARK, 0.6)
    ctx.set_line_width(1.8)
    ctx.set_line_cap(1)
    for i in range(8):
        ang = i * math.pi / 4.0 + t * 0.7
        ctx.move_to(x + math.cos(ang) * 9.5, y + math.sin(ang) * 9.5)
        ctx.line_to(x + math.cos(ang) * 13.0, y + math.sin(ang) * 13.0)
        ctx.stroke()
    ctx.restore()


def _prop_magnifier(ctx, hx: float, hy: float, t: float) -> None:
    """A magnifying glass held in the hand (verifying)."""
    ctx.save()
    ctx.set_line_width(3.0)
    _set(ctx, GADGET)
    ctx.move_to(hx + 1.0, hy + 2.0)
    ctx.line_to(hx + 7.0, hy + 9.0)
    ctx.stroke()
    _ellipse(ctx, hx - 3.0, hy - 4.0, 9.6, 9.6)
    _set(ctx, _c("#cfe6ff"), 0.35)
    ctx.fill()
    _set(ctx, GADGET)
    ctx.set_line_width(3.2)
    _ellipse(ctx, hx - 3.0, hy - 4.0, 9.6, 9.6)
    ctx.stroke()
    _set(ctx, (1.0, 1.0, 1.0, 0.55))
    ctx.set_line_width(1.8)
    ctx.move_to(hx - 8.0, hy - 7.0)
    ctx.line_to(hx - 5.0, hy - 10.0)
    ctx.stroke()
    ctx.restore()


def _prop_pencil(ctx, hx: float, hy: float, t: float) -> None:
    ctx.save()
    ctx.translate(hx + 2.0, hy + 2.0)
    ctx.rotate(-0.9 + 0.06 * math.sin(t * 14.0))
    _round_rect(ctx, -2.4, -12.0, 4.8, 16.0, 1.4)
    _set(ctx, _c("#f2c14e"))
    ctx.fill_preserve()
    _set(ctx, _c("#c99a35"), 0.8)
    ctx.set_line_width(0.9)
    ctx.stroke()
    ctx.move_to(-2.4, 4.0)
    ctx.line_to(0.0, 8.6)                      # sharpened tip
    ctx.line_to(2.4, 4.0)
    ctx.close_path()
    _set(ctx, _c("#e8d5b0"))
    ctx.fill()
    _ellipse(ctx, 0.0, 8.2, 1.0, 1.2)          # graphite point
    _set(ctx, _c("#404040"))
    ctx.fill()
    ctx.restore()


def _prop_wrench(ctx, hx: float, hy: float, t: float) -> None:
    ctx.save()
    ctx.translate(hx, hy)
    ctx.rotate(0.6 + 0.10 * math.sin(t * 3.2))
    _round_rect(ctx, -2.2, -2.0, 4.4, 14.0, 2.0)
    _set(ctx, _c("#9aa4b8"))
    ctx.fill_preserve()
    _set(ctx, _c("#6f7a90"), 0.8)
    ctx.set_line_width(0.9)
    ctx.stroke()
    _ellipse(ctx, 0.0, -4.6, 5.4, 5.4)
    _set(ctx, _c("#9aa4b8"))
    ctx.fill()
    _ellipse(ctx, 0.0, -4.6, 2.6, 2.6)         # the jaw opening
    _set(ctx, HOODIE_DK, 0.55)
    ctx.fill()
    ctx.restore()


def _draw_book(ctx, cx_: float, cy_: float, t: float) -> None:
    wobble = 0.03 * math.sin(t * 1.6)
    ctx.save()
    ctx.translate(cx_, cy_)
    ctx.rotate(wobble)
    _round_rect(ctx, -24.0, -16.0, 48.0, 30.0, 3.0)
    _set(ctx, _c("#d98a5a"))
    ctx.fill_preserve()
    _set(ctx, _c("#a8613a"), 0.8)
    ctx.set_line_width(1.0)
    ctx.stroke()
    _round_rect(ctx, -20.0, -13.0, 40.0, 24.0, 2.0)
    _set(ctx, PAPER)
    ctx.fill()
    _set(ctx, INK, 0.6)
    ctx.set_line_width(1.2)
    for i in range(3):
        ctx.move_to(-16.0, -7.0 + i * 7.0)
        ctx.line_to(16.0 - (i % 2) * 8.0, -7.0 + i * 7.0)
        ctx.stroke()
    ctx.restore()


# --------------------------------------------------------------------------- #
# Desk scene (writing / coding) — drawn between body and arms so the forearms
# rest naturally on the desktop.
# --------------------------------------------------------------------------- #


def _draw_desk(ctx) -> None:
    _round_rect(ctx, 26.0, 196.0, 168.0, 12.0, 4.0)
    grad = cairo.LinearGradient(0, 196.0, 0, 208.0)
    grad.add_color_stop_rgba(0.0, *_mix(WOOD, (1, 1, 1, 1), 0.25))
    grad.add_color_stop_rgba(1.0, *WOOD_DK)
    ctx.set_source(grad)
    ctx.fill_preserve()
    _set(ctx, WOOD_DEEP, 0.7)
    ctx.set_line_width(1.0)
    ctx.stroke()
    _round_rect(ctx, 34.0, 208.0, 152.0, 8.0, 2.0)          # apron
    _set(ctx, WOOD_DK)
    ctx.fill()
    for lx in (40.0, 168.0):                                 # legs
        _round_rect(ctx, lx, 216.0, 12.0, 30.0, 3.0)
        _set(ctx, WOOD_DEEP)
        ctx.fill()


def _draw_notebook(ctx, t: float) -> None:
    ctx.save()
    _round_rect(ctx, 66.0, 182.0, 44.0, 15.0, 3.0)
    _set(ctx, _c("#d98a5a"))
    ctx.fill_preserve()
    _set(ctx, _c("#a8613a"), 0.8)
    ctx.set_line_width(1.0)
    ctx.stroke()
    _round_rect(ctx, 66.0, 190.0, 44.0, 8.0, 2.5)          # page stack
    _set(ctx, PAPER)
    ctx.fill()
    _set(ctx, INK, 0.65)
    ctx.set_line_width(1.1)
    for i in range(2):
        prog = min(1.0, max(0.0, (t * 1.4 + i * 0.8) % 3.0))
        ctx.move_to(72.0, 193.0 + i * 3.5)
        ctx.line_to(72.0 + 32.0 * prog, 193.0 + i * 3.5)
        ctx.stroke()
    for i in range(4):                                       # spiral binding
        _ellipse(ctx, 68.5 + i * 3.2, 189.0, 1.1, 1.4)
        _set(ctx, GADGET, 0.7)
        ctx.fill()
    ctx.restore()


def _draw_laptop(ctx, t: float) -> None:
    ctx.save()
    # base (keyboard deck)
    _round_rect(ctx, 106.0, 190.0, 54.0, 7.0, 2.5)
    _set(ctx, GADGET_HI)
    ctx.fill_preserve()
    _set(ctx, GADGET, 0.8)
    ctx.set_line_width(1.0)
    ctx.stroke()
    # lid, leaning back toward him — we see its back
    ctx.move_to(110.0, 190.0)
    ctx.line_to(158.0, 190.0)
    ctx.line_to(154.0, 156.0)
    ctx.line_to(113.0, 156.0)
    ctx.close_path()
    grad = cairo.LinearGradient(0, 156.0, 0, 190.0)
    grad.add_color_stop_rgba(0.0, *_mix(GADGET, (1, 1, 1, 1), 0.18))
    grad.add_color_stop_rgba(1.0, *GADGET)
    ctx.set_source(grad)
    ctx.fill_preserve()
    _set(ctx, _c("#2a3040"), 0.8)
    ctx.set_line_width(1.0)
    ctx.stroke()
    _ellipse(ctx, 133.5, 170.0, 4.4, 4.4)                    # lid emblem
    _set(ctx, HOODIE, 0.85)
    ctx.fill()
    ctx.restore()
    _glyph_chevrons(ctx, 122.0, 146.0, t)                    # decorative


# --------------------------------------------------------------------------- #
# Assembly
# --------------------------------------------------------------------------- #


def _draw_scene(ctx, state: str, pose: Pose, t: float, *,
                furniture: bool = True, floating: bool = True,
                shadow: bool = True) -> None:
    """Assemble one character frame in design-box coordinates.

    ``furniture`` controls the *desk props* (desk + notebook/laptop) that
    the standalone ``draw_character`` API draws around Alwin.  The
    animated-world renderer turns this off and supplies a real room
    (``companion.bedroom``) instead, so the notebook/laptop are not drawn
    twice.  ``floating`` toggles the ambient props (z's, thought dots,
    sparkles, …) — off while he lies rotated in bed, where the room
    draws upright z's above the pillow.  ``shadow`` toggles the ground
    shadow ellipse — off while lying (the bed casts its own).
    Held props (pencil, book, magnifier) always follow the character.
    """
    ox, oy = pose.sway, pose.bob
    if shadow:
        _draw_shadow(ctx, ox, oy, pose)

    hip_cx = CX + ox
    hip_cy = HIP_Y + oy
    _draw_leg(ctx, hip_cx - 11.0, hip_cy, pose.l_leg)
    _draw_leg(ctx, hip_cx + 11.0, hip_cy, pose.r_leg)

    # torso pivot (leaning rotates shoulders/head about the hips)
    sh_l = _rot(CX - 33.0 + ox, SHOULDER_Y + oy, hip_cx, hip_cy, pose.lean)
    sh_r = _rot(CX + 33.0 + ox, SHOULDER_Y + oy, hip_cx, hip_cy, pose.lean)
    head_c = _rot(CX + ox, HEAD_CY + oy, hip_cx, hip_cy, pose.lean)
    head_x = head_c[0] + pose.head_dx
    head_y = head_c[1] + pose.head_dy

    # hood resting behind the neck (behind the head)
    _ellipse(ctx, head_c[0], SHOULDER_Y + oy - 2.0, 30.0, 13.0)
    _set(ctx, HOODIE_DK)
    ctx.fill()

    _draw_torso(ctx, head_c[0], head_c[1] + 56.0, pose)
    _draw_neck(ctx, head_x, head_y)
    _draw_head(ctx, head_x, head_y, pose, t)

    desk = state in ("writing", "coding")
    # arms+props first; for desk states, the desk is drawn over the legs,
    # then the forearms+hands are re-drawn so they rest on the desktop.
    hand_l = _draw_arm(ctx, sh_l[0], sh_l[1],
                       (pose.l_hand_x + ox, pose.l_hand_y + oy), pose.l_bend)
    hand_r = _draw_arm(ctx, sh_r[0], sh_r[1],
                       (pose.r_hand_x + ox, pose.r_hand_y + oy), pose.r_bend)
    if desk and furniture:
        _draw_desk(ctx)
        if state == "writing":
            _draw_notebook(ctx, t)
        else:
            _draw_laptop(ctx, t)
        # redraw forearms+hands over the desk so they read as "on" it
        _draw_arm(ctx, sh_l[0], sh_l[1],
                  (pose.l_hand_x + ox, pose.l_hand_y + oy), pose.l_bend)
        hand_r = _draw_arm(ctx, sh_r[0], sh_r[1],
                           (pose.r_hand_x + ox, pose.r_hand_y + oy), pose.r_bend)

    # held props (attached to hands)
    if state == "writing":
        _prop_pencil(ctx, hand_r[0], hand_r[1], t)
    elif state == "reading":
        _draw_book(ctx, (hand_l[0] + hand_r[0]) / 2.0,
                   (hand_l[1] + hand_r[1]) / 2.0 + 4.0, t)
    elif state == "verifying":
        _prop_magnifier(ctx, hand_r[0], hand_r[1], t)
    elif state == "working":
        _prop_wrench(ctx, hand_r[0], hand_r[1] - 6.0, t)
        _prop_gear(ctx, 172.0, 120.0 + 3.0 * math.sin(t * 2.0), t)

    # floating props
    if not floating:
        pass
    elif state == "thinking":
        _prop_thought_dots(ctx, head_x + 46.0, head_y - 44.0, t)
    elif state == "celebrating":
        _prop_sparkles(ctx, t)
    elif state == "sleeping":
        _prop_zs(ctx, head_x + 40.0, head_y - 44.0, t)
    elif state == "error":
        _prop_sweat_drop(ctx, head_x + 44.0, head_y - 24.0, t)
    elif state == "waiting_for_approval":
        _prop_dots_badge(ctx, head_x + 52.0, head_y - 40.0, t)
    elif state == "waking_up":
        _prop_sun(ctx, head_x + 54.0, head_y - 42.0, t)


def draw_character(ctx, width: float, height: float, state: str, *,
                   expression: str = "neutral", t: float = 0.0,
                   pose: Optional[Pose] = None) -> Pose:
    """Draw Alwin (full-body cartoon boy) into a widget-sized context.

    The design box is scaled **uniformly** and centred, so the character
    is never distorted or clipped at any size.  ``t`` is seconds spent
    in *state*; when *pose* is given (the renderer blends across state
    changes) it is used verbatim.  Returns the pose that was drawn.
    """
    used = pose if pose is not None else compute_pose(state, t, expression)
    scale = min(width / DESIGN_W, height / DESIGN_H) if width > 0 and height > 0 else 1.0
    ctx.save()
    ctx.translate((width - DESIGN_W * scale) / 2.0,
                  (height - DESIGN_H * scale) / 2.0)
    ctx.scale(scale, scale)
    try:
        _draw_scene(ctx, state, used, t)
    finally:
        ctx.restore()
    return used


def draw_character_placed(ctx, x: float, y: float, scale: float, state: str,
                          *, expression: str = "neutral", t: float = 0.0,
                          pose: Optional[Pose] = None,
                          rotation: float = 0.0,
                          furniture: bool = False,
                          floating_props: bool = True,
                          shadow: bool = True) -> Pose:
    """Draw Alwin anchored at a **world** position (the animated world).

    ``x``/``y`` is his feet anchor in *world* (scene) units, ``scale``
    converts world units to pixels uniformly, and ``rotation`` rotates the
    whole figure about his feet (used for the lying-in-bed pose).  This is
    how the character can stand anywhere in a room instead of being stuck
    at the centre of the window.

    ``furniture=False`` (the default here) skips the standalone desk
    props, because the room renderer already draws a real desk; pass
    ``True`` only when drawing Alwin on his own without a room.
    ``floating_props``/``shadow`` suppress the ambient z's and the ground
    shadow while he lies rotated in bed (the room draws those itself).

    ``t`` is seconds spent in *state*; when *pose* is given (the renderer
    blends across state changes) it is used verbatim.  Returns the pose
    that was drawn.
    """
    used = pose if pose is not None else compute_pose(state, t, expression)
    ctx.save()
    ctx.translate(x, y)
    if rotation:
        ctx.rotate(rotation)
    ctx.scale(scale, scale)
    ctx.translate(-CX, -GROUND_Y)
    try:
        _draw_scene(ctx, state, used, t, furniture=furniture,
                    floating=floating_props, shadow=shadow)
    finally:
        ctx.restore()
    return used


__all__ = [
    "DESIGN_H",
    "DESIGN_W",
    "POSE_BLEND_SECONDS",
    "Pose",
    "blink_amount",
    "compute_pose",
    "draw_character",
    "draw_character_placed",
]
