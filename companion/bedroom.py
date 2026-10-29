"""Cairo bedroom renderer for the Alwin companion (v0.11.0).

Draws the illustrated bedroom that :mod:`companion.scene` describes —
wall, floor, window with sunrise, bed, bedside table, alarm clock, desk
with notebook/pencil/laptop, chair — using only cairo primitives.  It
is the *view* half of the scene layer: it reads geometry from the scene
model and never mutates it, never decides anything, and never touches
the task engine.

Drawing contract (pinned by tests):

* **pure cairo** — no GTK, no file/asset I/O at draw time, no network,
  no blur/particle filters; the only inputs are the scene snapshot
  (:class:`companion.scene.SceneView`) and a monotonic ``t`` (seconds),
* **deterministic** — the same (view, t) always produces the same
  pixels, so headless image-surface tests can assert on colour,
* **layered** — the scene is split into ``draw_scene_back`` (everything
  behind Alwin: wall, floor, window, furniture) and
  ``draw_scene_front`` (the near edge of the bed blanket, so he can
  appear tucked *under* it).  The character is drawn between the two,
* **uniform scaling** — everything is expressed in scene design units
  and transformed with :class:`companion.scene.SceneTransform`, so the
  room and the character scale together and never stretch.

Everything here is original artwork built from cairo paths.
"""

from __future__ import annotations

import math
from typing import Iterable, Tuple

import cairo

from companion.scene import SCENE_H, SCENE_W, SceneTransform, SceneView

RGB = Tuple[float, float, float]


def _rgb(hex_color: str) -> RGB:
    h = "".join(ch for ch in hex_color if ch in "0123456789abcdefABCDEF")
    return (int(h[0:2], 16) / 255.0,
            int(h[2:4], 16) / 255.0,
            int(h[4:6], 16) / 255.0)


def _mix(a: RGB, b: RGB, k: float) -> RGB:
    k = 0.0 if k < 0.0 else (1.0 if k > 1.0 else k)
    return (a[0] + (b[0] - a[0]) * k,
            a[1] + (b[1] - a[1]) * k,
            a[2] + (b[2] - a[2]) * k)


# --------------------------------------------------------------------------- #
# Palette — warm, original, morning-lit
# --------------------------------------------------------------------------- #

WALL_TOP = _rgb("#f7 e0 c8")
WALL_BASE = _rgb("#f2 d3 b4")
SKIRTING = _rgb("#fbf3 e7")
FLOOR_NEAR = _rgb("#d8 a8 78")
FLOOR_FAR = _rgb("#c0 8c 5c")
PLANK_LINE = _rgb("#a9 76 4c")
OUTLINE = _rgb("#7a 5a 40")

BED_WOOD = _rgb("#b9 8d 5e")
BED_WOOD_DK = _rgb("#96 6d 44")
MATTRESS = _rgb("#fd f6 ec")
PILLOW = _rgb("#ff fd f8")
BLANKET = _rgb("#8f c3 a8")
BLANKET_DK = _rgb("#6f a8 8c")

CURTAIN = _rgb("#e8 a9 a0")
CURTAIN_DK = _rgb("#d1 8b 83")
SKY_PREDAWN_TOP = _rgb("#5b 5b 8f")
SKY_PREDAWN_BOTTOM = _rgb("#c9 8f 9c")
SKY_MORNING_TOP = _rgb("#8f c4 e8")
SKY_MORNING_BOTTOM = _rgb("#ff d9 a0")
SUN = _rgb("#ff d2 6a")
HILL = _rgb("#8f b8 8a")

DESK_WOOD = _rgb("#c6 9a 6a")
DESK_WOOD_DK = _rgb("#9c 74 48")
CHAIR = _rgb("#a8 7d 55")
TABLE_WOOD = _rgb("#bd 92 60")
CLOCK_BODY = _rgb("#e0 57 4a")
CLOCK_FACE = _rgb("#ff f8 ec")
NOTEBOOK = _rgb("#d9 8a 5a")
PENCIL = _rgb("#f2 c1 4e")
PAPER = _rgb("#ff fd f6")
GADGET = _rgb("#8a 93 a6")
GADGET_HI = _rgb("#c9 d2 e0")
RUG = _rgb("#e6 b8 c2")
RUG_DK = _rgb("#d2 99 a6")

#: The wall/floor split used for the gradient (matches scene.WALL_Y).
SKY_H = 110.0


def _set(ctx: cairo.Context, rgb: RGB, alpha: float = 1.0) -> None:
    ctx.set_source_rgba(rgb[0], rgb[1], rgb[2], alpha)


def _rrect(ctx: cairo.Context, x: float, y: float, w: float, h: float,
           r: float) -> None:
    r = min(r, w / 2.0, h / 2.0)
    ctx.new_sub_path()
    ctx.arc(x + r, y + r, r, math.pi, 1.5 * math.pi)
    ctx.arc(x + w - r, y + r, r, 1.5 * math.pi, 2.0 * math.pi)
    ctx.arc(x + w - r, y + h - r, r, 0.0, 0.5 * math.pi)
    ctx.arc(x + r, y + h - r, r, 0.5 * math.pi, math.pi)
    ctx.close_path()


def _fill(ctx: cairo.Context, rgb: RGB, alpha: float = 1.0) -> None:
    _set(ctx, rgb, alpha)
    ctx.fill()


def _stroke(ctx: cairo.Context, rgb: RGB, width: float = 1.2,
            alpha: float = 1.0) -> None:
    _set(ctx, rgb, alpha)
    ctx.set_line_width(width)
    ctx.stroke()


def _apply_transform(ctx: cairo.Context, tr: SceneTransform) -> None:
    ctx.translate(tr.offset_x, tr.offset_y)
    ctx.scale(tr.scale, tr.scale)


# --------------------------------------------------------------------------- #
# Back layer — everything behind the character
# --------------------------------------------------------------------------- #


def draw_scene_back(ctx: cairo.Context, view: SceneView, t: float,
                    tr: SceneTransform) -> None:
    """Draw the room *behind* Alwin in scene units, scaled to the window."""
    ctx.save()
    _apply_transform(ctx, tr)
    _draw_wall(ctx, view)
    _draw_window(ctx, view, t)
    _draw_poster(ctx)
    _draw_floor(ctx)
    _draw_rug(ctx, view)
    _draw_bed_back(ctx, view)
    _draw_bedside(ctx, view, t)
    _draw_wall_clock(ctx, view, t)
    _draw_desk(ctx, view, t)
    _draw_chair(ctx)
    ctx.restore()


def draw_scene_front(ctx: cairo.Context, view: SceneView, t: float,
                     tr: SceneTransform) -> None:
    """Draw the room *in front of* the character (the near blanket edge)."""
    ctx.save()
    _apply_transform(ctx, tr)
    _draw_bed_front(ctx, view, t)
    ctx.restore()


def _draw_wall(ctx: cairo.Context, view: SceneView) -> None:
    grad = cairo.LinearGradient(0.0, 0.0, 0.0, SCENE_H * 0.68)
    grad.add_color_stop_rgba(0.0, *WALL_TOP, 1.0)
    grad.add_color_stop_rgba(1.0, *WALL_BASE, 1.0)
    ctx.set_source(grad)
    ctx.rectangle(0.0, 0.0, SCENE_W, SCENE_H * 0.68)
    ctx.fill()
    # skirting board
    _rrect(ctx, -1.0, SCENE_H * 0.645, SCENE_W + 2.0, 16.0, 3.0)
    _fill(ctx, SKIRTING)
    _stroke(ctx, _mix(SKIRTING, OUTLINE, 0.25), 1.0, 0.6)
    del view


def _draw_floor(ctx: cairo.Context) -> None:
    top = SCENE_H * 0.68
    grad = cairo.LinearGradient(0.0, top, 0.0, SCENE_H)
    grad.add_color_stop_rgba(0.0, *FLOOR_FAR, 1.0)
    grad.add_color_stop_rgba(1.0, *FLOOR_NEAR, 1.0)
    ctx.set_source(grad)
    ctx.rectangle(0.0, top, SCENE_W, SCENE_H - top)
    ctx.fill()
    # plank seams: horizontal lines spreading toward the viewer
    y = top + 10.0
    step = 8.0
    i = 0
    while y < SCENE_H:
        _set(ctx, PLANK_LINE, 0.35)
        ctx.set_line_width(1.0)
        ctx.move_to(0.0, y)
        ctx.line_to(SCENE_W, y)
        ctx.stroke()
        # staggered vertical seams
        x = (i % 2) * 40.0 - 20.0
        while x < SCENE_W:
            ctx.move_to(x, y)
            ctx.line_to(x, y + step)
            ctx.stroke()
            x += 80.0
        y += step
        step *= 1.28
        i += 1


def _draw_window(ctx: cairo.Context, view: SceneView, t: float) -> None:
    x, y, w, h = 34.0, 40.0, 116.0, 104.0
    # glass: pre-dawn -> morning sky, blended by the sunrise amount
    sky_top = _mix(SKY_PREDAWN_TOP, SKY_MORNING_TOP, view.sunrise)
    sky_bot = _mix(SKY_PREDAWN_BOTTOM, SKY_MORNING_BOTTOM, view.sunrise)
    grad = cairo.LinearGradient(0.0, y, 0.0, y + h)
    grad.add_color_stop_rgba(0.0, *sky_top, 1.0)
    grad.add_color_stop_rgba(1.0, *sky_bot, 1.0)
    _rrect(ctx, x, y, w, h, 6.0)
    ctx.set_source(grad)
    ctx.fill_preserve()
    ctx.save()
    ctx.clip()
    # sun rising with the sunrise amount
    sun_y = y + h - 18.0 - view.sunrise * (h - 34.0)
    _set(ctx, SUN, 0.35 + 0.55 * view.sunrise)
    ctx.arc(x + 30.0, sun_y, 16.0, 0.0, 2.0 * math.pi)
    ctx.fill()
    # distant hills
    _set(ctx, HILL, 0.75)
    ctx.move_to(x, y + h)
    ctx.curve_to(x + 30.0, y + h - 26.0, x + 50.0, y + h - 8.0,
                 x + 80.0, y + h - 20.0)
    ctx.curve_to(x + 100.0, y + h - 28.0, x + 110.0, y + h - 10.0,
                 x + w, y + h - 16.0)
    ctx.line_to(x + w, y + h)
    ctx.close_path()
    ctx.fill()
    # two slow clouds (deterministic drift)
    for ci, (cx0, cy0, cw) in enumerate(((20.0, 22.0, 40.0),
                                         (74.0, 36.0, 30.0))):
        cx = cx0 + 4.0 * math.sin(t * 0.12 + ci * 2.1)
        _set(ctx, (1.0, 1.0, 1.0), 0.5 + 0.2 * view.sunrise)
        ctx.move_to(cx, cy0)
        ctx.curve_to(cx + cw * 0.2, cy0 - 8.0, cx + cw * 0.5, cy0 - 8.0,
                     cx + cw * 0.6, cy0)
        ctx.curve_to(cx + cw * 0.9, cy0 + 1.0, cx + cw, cy0 + 6.0,
                     cx + cw * 0.8, cy0 + 8.0)
        ctx.line_to(cx + cw * 0.15, cy0 + 8.0)
        ctx.curve_to(cx - 4.0, cy0 + 7.0, cx - 6.0, cy0 + 3.0, cx, cy0)
        ctx.close_path()
        ctx.fill()
    ctx.restore()
    _stroke(ctx, _mix(BED_WOOD, OUTLINE, 0.4), 3.0)
    # mullions
    _set(ctx, _mix(BED_WOOD, OUTLINE, 0.4))
    ctx.set_line_width(3.0)
    ctx.move_to(x + w / 2.0, y)
    ctx.line_to(x + w / 2.0, y + h)
    ctx.move_to(x, y + h / 2.0)
    ctx.line_to(x + w, y + h / 2.0)
    ctx.stroke()
    # sill
    _rrect(ctx, x - 6.0, y + h, w + 12.0, 8.0, 3.0)
    _fill(ctx, _mix(SKIRTING, (1.0, 1.0, 1.0), 0.2))
    _stroke(ctx, _mix(BED_WOOD, OUTLINE, 0.35), 1.0, 0.7)
    # curtains (left and right panels, tied back)
    for side in (0.0, 1.0):
        cx = x - 12.0 + side * (w + 24.0)
        ctx.save()
        _rrect(ctx, cx, y - 8.0, 18.0, h + 20.0, 7.0)
        _fill(ctx, CURTAIN)
        _set(ctx, CURTAIN_DK, 0.5)
        ctx.set_line_width(1.2)
        for fx in (6.0, 12.0):
            ctx.move_to(cx + fx, y - 6.0)
            ctx.line_to(cx + fx, y + h + 10.0)
            ctx.stroke()
        ctx.restore()
    # curtain rod
    _set(ctx, _mix(BED_WOOD, OUTLINE, 0.5))
    ctx.set_line_width(3.0)
    ctx.move_to(x - 16.0, y - 8.0)
    ctx.line_to(x + w + 16.0, y - 8.0)
    ctx.stroke()


def _draw_poster(ctx: cairo.Context) -> None:
    x, y, w, h = 250.0, 88.0, 54.0, 42.0
    _rrect(ctx, x, y, w, h, 3.0)
    _fill(ctx, _mix(SKIRTING, (1.0, 1.0, 1.0), 0.1))
    ctx.save()
    _rrect(ctx, x + 4.0, y + 4.0, w - 8.0, h - 8.0, 2.0)
    ctx.clip()
    grad = cairo.LinearGradient(0.0, y, 0.0, y + h)
    grad.add_color_stop_rgba(0.0, *SKY_MORNING_TOP, 1.0)
    grad.add_color_stop_rgba(1.0, *SKY_MORNING_BOTTOM, 1.0)
    ctx.set_source(grad)
    ctx.paint()
    _set(ctx, SUN, 0.9)
    ctx.arc(x + 16.0, y + 16.0, 7.0, 0.0, 2.0 * math.pi)
    ctx.fill()
    _set(ctx, HILL, 0.9)
    ctx.move_to(x + 4.0, y + h - 4.0)
    ctx.line_to(x + 20.0, y + 20.0)
    ctx.line_to(x + 34.0, y + h - 4.0)
    ctx.close_path()
    ctx.fill()
    ctx.restore()
    _stroke(ctx, _mix(BED_WOOD, OUTLINE, 0.5), 2.0)


def _draw_rug(ctx: cairo.Context, view: SceneView) -> None:
    x, y, w, h = 200.0, 310.0, 116.0, 30.0
    _set(ctx, RUG_DK, 0.5)
    ctx.save()
    ctx.translate(x + w / 2.0, y + h / 2.0)
    ctx.scale(1.0, h / w * 1.6)
    ctx.arc(0.0, 0.0, w / 2.0, 0.0, 2.0 * math.pi)
    ctx.restore()
    ctx.fill()
    ctx.save()
    ctx.translate(x + w / 2.0, y + h / 2.0 - 2.0)
    ctx.scale(1.0, h / w * 1.6)
    _set(ctx, RUG)
    ctx.arc(0.0, 0.0, w / 2.0 - 6.0, 0.0, 2.0 * math.pi)
    ctx.restore()
    ctx.fill()


def _draw_bed_back(ctx: cairo.Context, view: SceneView) -> None:
    # headboard
    _rrect(ctx, 6.0, 232.0, 22.0, 70.0, 6.0)
    _fill(ctx, BED_WOOD)
    _stroke(ctx, BED_WOOD_DK, 1.2)
    # bed base (under the mattress)
    _rrect(ctx, 16.0, 288.0, 184.0, 34.0, 5.0)
    _fill(ctx, BED_WOOD)
    _stroke(ctx, BED_WOOD_DK, 1.2)
    # mattress
    _rrect(ctx, 20.0, 270.0, 178.0, 24.0, 8.0)
    _fill(ctx, MATTRESS)
    _stroke(ctx, _mix(MATTRESS, OUTLINE, 0.3), 1.0)
    # pillow (under the head when lying)
    _rrect(ctx, 32.0, 256.0, 56.0, 18.0, 7.0)
    _fill(ctx, PILLOW)
    _stroke(ctx, _mix(PILLOW, OUTLINE, 0.25), 1.0)
    # blanket base lying flat on the bed
    _rrect(ctx, 100.0, 268.0, 98.0, 26.0, 6.0)
    _fill(ctx, BLANKET)
    _stroke(ctx, BLANKET_DK, 1.0)
    del view


def _draw_bed_front(ctx: cairo.Context, view: SceneView, t: float) -> None:
    """The near blanket edge, drawn over the character when tucked in.

    The blanket is drawn as two stacked rounded bands so it reads as a
    real duvet pulled up to his chest, and its height grows with
    ``blanket_cover`` (1.0 = fully tucked, legs hidden; 0.0 = he is up
    and it lies flat).
    """
    cover = view.blanket_cover
    if cover <= 0.02:
        return
    # a body-hugging blanket: wide, tall enough to hide the legs
    h = 26.0 + 28.0 * cover
    top = 294.0 - h
    _rrect(ctx, 104.0, top, 92.0, h, 7.0)
    _fill(ctx, _mix(BLANKET, BLANKET_DK, 0.12))
    _stroke(ctx, BLANKET_DK, 1.2)
    # a lighter folded hem along the top edge
    _rrect(ctx, 104.0, top, 92.0, 9.0, 4.0)
    _fill(ctx, _mix(BLANKET, (1.0, 1.0, 1.0), 0.22))


def _draw_bedside(ctx: cairo.Context, view: SceneView, t: float) -> None:
    # table
    _rrect(ctx, 210.0, 288.0, 38.0, 34.0, 3.0)
    _fill(ctx, TABLE_WOOD)
    _stroke(ctx, BED_WOOD_DK, 1.1)
    _set(ctx, BED_WOOD_DK, 0.6)
    ctx.set_line_width(1.0)
    ctx.move_to(212.0, 300.0)
    ctx.line_to(246.0, 300.0)
    ctx.stroke()
    _set(ctx, _mix(TABLE_WOOD, OUTLINE, 0.5))
    ctx.arc(229.0, 294.0, 2.0, 0.0, 2.0 * math.pi)
    ctx.fill()
    # alarm clock (rings while the alarm amount is on)
    shake = view.alarm * 1.6 * math.sin(t * 22.0)
    cx, cy = 229.0 + shake, 280.0
    _rrect(ctx, cx - 12.0, cy - 8.0, 24.0, 16.0, 5.0)
    _fill(ctx, CLOCK_BODY)
    _stroke(ctx, _mix(CLOCK_BODY, OUTLINE, 0.4), 1.1)
    _set(ctx, CLOCK_FACE)
    ctx.arc(cx, cy, 5.6, 0.0, 2.0 * math.pi)
    ctx.fill()
    _set(ctx, _mix(CLOCK_BODY, OUTLINE, 0.5))
    ctx.set_line_width(1.2)
    ctx.move_to(cx, cy)
    ctx.line_to(cx, cy - 3.4)
    ctx.move_to(cx, cy)
    ctx.line_to(cx + 2.6, cy)
    ctx.stroke()
    # bells
    for bx in (cx - 8.0, cx + 8.0):
        _set(ctx, CLOCK_BODY)
        ctx.arc(bx, cy - 8.0, 3.0, math.pi, 2.0 * math.pi)
        ctx.fill()
    del view


def _draw_desk(ctx: cairo.Context, view: SceneView, t: float) -> None:
    # desktop
    _rrect(ctx, 316.0, 286.0, 130.0, 10.0, 3.0)
    grad = cairo.LinearGradient(0.0, 286.0, 0.0, 296.0)
    grad.add_color_stop_rgba(0.0, *_mix(DESK_WOOD, (1.0, 1.0, 1.0), 0.25), 1.0)
    grad.add_color_stop_rgba(1.0, *DESK_WOOD_DK, 1.0)
    ctx.set_source(grad)
    ctx.fill_preserve()
    _stroke(ctx, _mix(DESK_WOOD, OUTLINE, 0.4), 1.0)
    # apron + legs
    _rrect(ctx, 322.0, 296.0, 118.0, 7.0, 2.0)
    _fill(ctx, DESK_WOOD_DK)
    for lx in (326.0, 426.0):
        _rrect(ctx, lx, 303.0, 12.0, 24.0, 3.0)
        _fill(ctx, _mix(DESK_WOOD_DK, OUTLINE, 0.2))

    # notebook (left) with a scribble that only advances while writing
    _rrect(ctx, 344.0, 278.0, 30.0, 10.0, 2.5)
    _fill(ctx, NOTEBOOK)
    _stroke(ctx, _mix(NOTEBOOK, OUTLINE, 0.4), 1.0)
    _rrect(ctx, 344.0, 284.0, 30.0, 5.0, 2.0)
    _fill(ctx, PAPER)
    _set(ctx, _mix(OUTLINE, PAPER, 0.2), 0.7)
    ctx.set_line_width(0.9)
    prog = (math.sin(t * 6.0) * 0.5 + 0.5) if view.writing else 1.0
    ctx.move_to(347.0, 286.5)
    ctx.line_to(347.0 + 12.0 + 10.0 * prog, 286.5)
    ctx.stroke()
    for i in range(3):                      # spiral binding
        _set(ctx, GADGET, 0.7)
        ctx.arc(346.0 + i * 3.0, 283.0, 0.9, 0.0, 2.0 * math.pi)
        ctx.fill()

    # pencil resting on the desk (left, clear of the laptop)
    ctx.save()
    ctx.translate(338.0, 289.0)
    ctx.rotate(-0.12)
    _rrect(ctx, -8.0, -1.6, 16.0, 3.2, 1.0)
    _fill(ctx, PENCIL)
    _set(ctx, _mix(PENCIL, OUTLINE, 0.4))
    ctx.move_to(8.0, -1.6)
    ctx.line_to(11.0, 0.0)
    ctx.line_to(8.0, 1.6)
    ctx.close_path()
    _fill(ctx, _rgb("#e8d5b0"))
    ctx.restore()

    # laptop (right), lid leaning back, glyphs while coding
    _rrect(ctx, 404.0, 282.0, 34.0, 6.0, 2.0)
    _fill(ctx, GADGET_HI)
    _stroke(ctx, GADGET, 1.0)
    ctx.move_to(407.0, 282.0)
    ctx.line_to(435.0, 282.0)
    ctx.line_to(432.0, 262.0)
    ctx.line_to(410.0, 262.0)
    ctx.close_path()
    lgrad = cairo.LinearGradient(0.0, 262.0, 0.0, 282.0)
    lgrad.add_color_stop_rgba(0.0, *_mix(GADGET, (1.0, 1.0, 1.0), 0.18), 1.0)
    lgrad.add_color_stop_rgba(1.0, *GADGET, 1.0)
    ctx.set_source(lgrad)
    ctx.fill_preserve()
    _stroke(ctx, _mix(GADGET, OUTLINE, 0.4), 1.0)
    _set(ctx, _mix(GADGET_HI, (1.0, 1.0, 1.0), 0.3), 0.9)
    ctx.arc(421.0, 272.0, 3.0, 0.0, 2.0 * math.pi)
    ctx.fill()
    if view.coding:                        # little floating </> glyphs
        _draw_coding_glyphs(ctx, t)


def _draw_coding_glyphs(ctx: cairo.Context, t: float) -> None:
    for i, kind in enumerate(("lt", "slash", "gt")):
        phase = (t * 1.1 + i * 0.7) % 2.6
        alpha = max(0.0, min(1.0, 1.9 - phase)) * 0.8
        gx = 414.0 + i * 12.0
        gy = 254.0 - phase * 6.0
        _set(ctx, _rgb("#7ee0a8") if kind != "slash" else SUN, alpha)
        ctx.set_line_width(2.0)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        if kind == "lt":
            ctx.move_to(gx + 3.0, gy - 4.0)
            ctx.line_to(gx - 3.0, gy)
            ctx.line_to(gx + 3.0, gy + 4.0)
        elif kind == "gt":
            ctx.move_to(gx - 3.0, gy - 4.0)
            ctx.line_to(gx + 3.0, gy)
            ctx.line_to(gx - 3.0, gy + 4.0)
        else:
            ctx.move_to(gx + 2.0, gy - 4.5)
            ctx.line_to(gx - 2.0, gy + 4.5)
        ctx.stroke()


def _draw_chair(ctx: cairo.Context) -> None:
    x = 418.0
    _rrect(ctx, x, 292.0, 40.0, 8.0, 3.0)
    _fill(ctx, CHAIR)
    _stroke(ctx, _mix(CHAIR, OUTLINE, 0.4), 1.0)
    _rrect(ctx, x + 4.0, 258.0, 32.0, 36.0, 5.0)
    _fill(ctx, _mix(CHAIR, (1.0, 1.0, 1.0), 0.12))
    _stroke(ctx, _mix(CHAIR, OUTLINE, 0.4), 1.0)
    for lx in (x + 3.0, x + 33.0):
        _rrect(ctx, lx, 300.0, 4.0, 26.0, 2.0)
        _fill(ctx, _mix(CHAIR, OUTLINE, 0.25))


def _draw_wall_clock(ctx: cairo.Context, view: SceneView, t: float) -> None:
    cx, cy, r = 392.0, 103.0, 16.0
    _set(ctx, _mix(DESK_WOOD, OUTLINE, 0.35), 0.22)
    ctx.arc(cx, cy + 2.0, r + 1.0, 0.0, 2.0 * math.pi)
    ctx.fill()
    _set(ctx, CHAIR)
    ctx.arc(cx, cy, r, 0.0, 2.0 * math.pi)
    ctx.fill()
    _set(ctx, PAPER)
    ctx.arc(cx, cy, r - 3.0, 0.0, 2.0 * math.pi)
    ctx.fill()
    for i in range(12):
        a = i * math.pi / 6.0
        _set(ctx, _mix(OUTLINE, PAPER, 0.3), 0.7)
        ctx.set_line_width(1.2 if i % 3 else 2.0)
        ctx.move_to(cx + math.cos(a) * (r - 6.0),
                    cy + math.sin(a) * (r - 6.0))
        ctx.line_to(cx + math.cos(a) * (r - 3.6),
                    cy + math.sin(a) * (r - 3.6))
        ctx.stroke()
    # hands: a calm morning time, with a gentle live tick on the second hand
    hands = ((-1.1, 7.0, 2.2, OUTLINE),
             (0.4, 9.5, 1.8, OUTLINE),
             ((t % 60.0) * math.tau / 60.0 - math.pi / 2.0, 10.5, 1.0,
              CLOCK_BODY))
    for ang, ln, wdt, col in hands:
        _set(ctx, col)
        ctx.set_line_width(wdt)
        ctx.set_line_cap(cairo.LINE_CAP_ROUND)
        ctx.move_to(cx, cy)
        ctx.line_to(cx + math.cos(ang) * ln, cy + math.sin(ang) * ln)
        ctx.stroke()
    _set(ctx, CLOCK_BODY)
    ctx.arc(cx, cy, 1.8, 0.0, 2.0 * math.pi)
    ctx.fill()
    del view


__all__ = [
    "draw_scene_back",
    "draw_scene_front",
]
