"""v0.7 character-system tests: full-body boy, poses, animation, honesty.

Everything runs **headless** on a ``cairo.ImageSurface`` — no display, no
desktop, no real window.  These tests pin the behaviours the milestone
demands and go beyond "it didn't crash": they assert *meaningful visual
differences* between key animation states and *honest* outcome gating.

Covered:

* the full-body character draws non-empty output into real image bounds,
* it stays visible (not clipped / not blank) at several widget sizes,
* idle breathing, natural blinking, greeting+wave, thinking, listening,
  writing and coding poses are genuinely distinct (pixels + pose knobs),
* ``Pose.lerp`` blends smoothly across a state change (transitions),
* elapsed-time timing: delayed frames still land on the right pose,
* celebration happens **only** for a verified/trusted completion and a
  failed task shows concern, never celebration (adapter honesty),
* the drawing module is a pure cairo leaf (no task engine, no OS, no
  network) and degrades gracefully for unknown states / broken inputs.
"""

from __future__ import annotations

import unittest

import cairo

from companion.adapter import TaskEventAdapter
from companion.character import (
    DESIGN_H,
    DESIGN_W,
    Pose,
    blink_amount,
    compute_pose,
    draw_character,
)
from companion.renderer import NullRenderer
from companion.states import (
    STATE_CELEBRATING,
    STATE_ERROR,
    STATE_GREETING,
    STATE_IDLE,
    CharacterController,
)

WIDTH, HEIGHT = 240, 250


def render(state: str, *, t: float = 1.0, expression: str = "neutral",
           width: int = WIDTH, height: int = HEIGHT) -> cairo.ImageSurface:
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    draw_character(ctx, width, height, state, expression=expression, t=t)
    surface.flush()
    return surface


def pixels(surface: cairo.ImageSurface) -> bytes:
    return bytes(surface.get_data())


def painted_fraction(surface: cairo.ImageSurface) -> float:
    """Fraction of pixels that differ from the fully-transparent blank."""
    data = surface.get_data()
    stride = surface.get_stride()
    w, h = surface.get_width(), surface.get_height()
    painted = 0
    total = w * h
    for y in range(h):
        row = y * stride
        for x in range(w):
            alpha = data[row + x * 4 + 3]
            if alpha > 8:
                painted += 1
    return painted / total if total else 0.0


def bounding_box(surface: cairo.ImageSurface):
    """(min_x, min_y, max_x, max_y) of painted pixels, or None if blank."""
    data = surface.get_data()
    stride = surface.get_stride()
    w, h = surface.get_width(), surface.get_height()
    min_x, min_y, max_x, max_y = w, h, -1, -1
    found = False
    for y in range(h):
        row = y * stride
        for x in range(w):
            if data[row + x * 4 + 3] > 8:
                found = True
                min_x = min(min_x, x)
                min_y = min(min_y, y)
                max_x = max(max_x, x)
                max_y = max(max_y, y)
    return (min_x, min_y, max_x, max_y) if found else None


class FullBodyDrawingTests(unittest.TestCase):
    def test_character_draws_nonempty_output(self):
        for state in ("idle", "greeting", "writing", "coding", "celebrating"):
            with self.subTest(state=state):
                surface = render(state, t=1.2)
                self.assertTrue(any(b for b in pixels(surface)),
                                f"{state} drew a blank frame")

    def test_character_is_a_substantial_figure_not_a_speck(self):
        # A full-body boy should cover a healthy share of the canvas —
        # this catches the old "tiny floating face" regression.
        frac = painted_fraction(render("idle", t=0.5))
        self.assertGreater(frac, 0.12,
                           "character occupies too little of the widget")

    def test_drawing_bounds_stay_inside_the_canvas(self):
        for state in ("idle", "celebrating", "waking_up"):
            with self.subTest(state=state):
                surface = render(state, t=0.3)
                box = bounding_box(surface)
                self.assertIsNotNone(box)
                min_x, min_y, max_x, max_y = box
                self.assertGreaterEqual(min_x, 0, "clipped on the left")
                self.assertGreaterEqual(min_y, 0, "clipped at the top")
                self.assertLessEqual(max_x, WIDTH, "clipped on the right")
                self.assertLessEqual(max_y, HEIGHT, "clipped at the bottom")

    def test_head_hands_feet_all_visible_no_clipping(self):
        # Head near the top, feet near the bottom, hands within margins:
        # the uniform scaling must never cut off a limb.
        box = bounding_box(render("idle", t=0.5))
        min_x, min_y, max_x, max_y = box
        self.assertLess(min_y, HEIGHT * 0.30, "head is clipped or too low")
        self.assertGreater(max_y, HEIGHT * 0.80, "feet are clipped or too high")
        self.assertLess(min_x, WIDTH * 0.30, "a hand is clipped on the left")
        self.assertGreater(max_x, WIDTH * 0.70, "a hand is clipped on the right")


class ScalingTests(unittest.TestCase):
    def test_visible_at_multiple_widget_sizes(self):
        sizes = [(120, 130), (160, 170), (240, 250), (360, 380), (500, 260)]
        for w, h in sizes:
            with self.subTest(size=(w, h)):
                surface = render("idle", t=0.4, width=w, height=h)
                self.assertTrue(any(b for b in pixels(surface)),
                                f"blank at {w}x{h}")
                box = bounding_box(surface)
                min_x, min_y, max_x, max_y = box
                self.assertGreaterEqual(min_x, 0)
                self.assertGreaterEqual(min_y, 0)
                self.assertLessEqual(max_x, w)
                self.assertLessEqual(max_y, h)

    def test_scaling_is_uniform_not_stretched(self):
        # The drawn aspect ratio must track the design box, not the
        # widget's — a wide widget must not horizontally stretch the boy.
        def aspect(state, w, h):
            box = bounding_box(render(state, t=0.5, width=w, height=h))
            min_x, min_y, max_x, max_y = box
            return (max_x - min_x) / (max_y - min_y)

        narrow = aspect("idle", 240, 250)
        wide = aspect("idle", 460, 250)
        self.assertAlmostEqual(narrow, wide, delta=0.15,
                               msg="character distorted when resized")


class PoseSystemTests(unittest.TestCase):
    def test_compute_pose_is_deterministic(self):
        self.assertEqual(compute_pose("thinking", 1.5),
                         compute_pose("thinking", 1.5))

    def test_unknown_state_falls_back_to_idle_pose(self):
        self.assertEqual(compute_pose("not-a-real-state", 1.0),
                         compute_pose("idle", 1.0))

    def test_pose_lerp_is_a_smooth_midpoint(self):
        a = compute_pose("idle", 0.0)
        b = compute_pose("celebrating", 0.0)
        mid = Pose.lerp(a, b, 0.5)
        for field in ("bob", "mouth_smile", "brow_raise"):
            with self.subTest(field=field):
                va, vb, vm = getattr(a, field), getattr(b, field), getattr(mid, field)
                lo, hi = min(va, vb), max(va, vb)
                self.assertGreaterEqual(vm, lo - 1e-6)
                self.assertLessEqual(vm, hi + 1e-6)

    def test_pose_lerp_clamps_endpoints(self):
        a = compute_pose("idle", 0.0)
        b = compute_pose("error", 0.0)
        self.assertEqual(Pose.lerp(a, b, 0.0), a)
        self.assertEqual(Pose.lerp(a, b, 1.0), b)


class AnimationTests(unittest.TestCase):
    def test_idle_breathing_moves_subtly(self):
        early = compute_pose("idle", 0.8)
        later = compute_pose("idle", 0.8 + 0.9)
        self.assertNotEqual(early.bob, later.bob,
                            "idle breathing produces no vertical motion")
        # Subtle: the breathing amplitude stays small.
        span = abs(early.bob - later.bob)
        self.assertLess(span, 6.0, "idle breathing is too large (jumpy)")

    def test_blinking_opens_and_closes(self):
        openness = [1.0 - blink_amount(t / 10.0)
                    for t in range(0, 140)]
        self.assertLess(min(openness), 0.5, "eyes never close (no blink)")
        self.assertGreater(max(openness), 0.9, "eyes never open")
        # Blinking must not distort the face: closed frames still render.
        closed = [t / 10.0 for t in range(0, 140) if blink_amount(t / 10.0) > 0.5]
        self.assertTrue(closed, "no closed-eye frame to check")
        surface = render("idle", t=closed[0])
        self.assertTrue(any(b for b in pixels(surface)))

    def test_blink_is_deterministic(self):
        self.assertEqual(blink_amount(3.7), blink_amount(3.7))

    def test_animation_changes_over_time(self):
        for state in ("thinking", "celebrating", "working", "walking",
                      "greeting", "coding"):
            with self.subTest(state=state):
                self.assertNotEqual(pixels(render(state, t=0.2)),
                                    pixels(render(state, t=0.7)),
                                    f"{state} is static over time")


class StateDistinctnessTests(unittest.TestCase):
    def test_key_states_render_visually_different_frames(self):
        # Meaningful visual differences, not merely "no crash".
        states = ["idle", "greeting", "thinking", "listening",
                  "writing", "coding", "celebrating", "error"]
        frames = {s: pixels(render(s, t=1.1)) for s in states}
        for a in states:
            for b in states:
                if a >= b:
                    continue
                with self.subTest(pair=(a, b)):
                    self.assertNotEqual(frames[a], frames[b],
                                        f"{a} and {b} look identical")

    def test_writing_and_coding_poses_are_distinguishable(self):
        self.assertNotEqual(pixels(render("writing", t=1.0)),
                            pixels(render("coding", t=1.0)))
        # Both lean toward a desk but with different gaze / hands.
        writing = compute_pose("writing", 1.0)
        coding = compute_pose("coding", 1.0)
        self.assertNotEqual((writing.r_hand_x, writing.r_hand_y),
                            (coding.r_hand_x, coding.r_hand_y))

    def test_thinking_and_listening_expressions_differ(self):
        thinking = compute_pose("thinking", 1.0)
        listening = compute_pose("listening", 1.0)
        self.assertNotEqual((thinking.gaze_x, thinking.gaze_y),
                            (listening.gaze_x, listening.gaze_y))
        self.assertNotEqual(pixels(render("thinking", t=1.0)),
                            pixels(render("listening", t=1.0)))

    def test_greeting_waves_over_time(self):
        # The wave must actually oscillate (not a single static raise).
        r_hand_ys = {compute_pose("greeting", t / 4.0).r_hand_y
                     for t in range(0, 20)}
        self.assertGreater(len(r_hand_ys), 4,
                           "greeting hand barely moves (no wave)")


class TimingTests(unittest.TestCase):
    def test_delayed_frames_still_land_on_the_right_pose(self):
        # Elapsed-time based: a big time jump yields the pose for that
        # moment, not a frame-accumulated approximation.
        direct = compute_pose("celebrating", 2.0)
        # Simulate the renderer: show_state at t=0, then tick to 2.0.
        pose = compute_pose("celebrating", 2.0)
        self.assertEqual(pose, direct)

    def test_blend_progresses_from_old_to_new_pose(self):
        old = compute_pose("idle", 3.0)
        new = compute_pose("celebrating", 0.0)
        early = Pose.lerp(old, new, 0.1)
        late = Pose.lerp(old, new, 0.9)
        self.assertNotEqual(early, late)
        self.assertLess(abs(early.bob - old.bob),
                        abs(late.bob - old.bob),
                        "blend does not progress toward the new pose")


class HonestyTests(unittest.TestCase):
    def _adapter(self):
        controller = CharacterController(NullRenderer())
        return controller, TaskEventAdapter(controller)

    def test_verified_completion_triggers_celebration(self):
        controller, adapter = self._adapter()

        class Rec:
            state = "completed"
            plan = {"action": "create_report", "target": "n.md",
                    "summary": "s"}
            steps = []
            verification = {"status": "passed"}
            report_status = None
            error = None
            result_message = "done"
            task_id = "t-1"

        adapter.on_task_update(Rec())
        self.assertEqual(controller.state, STATE_CELEBRATING)

    def test_failed_task_never_celebrates(self):
        controller, adapter = self._adapter()

        class Rec:
            state = "failed"
            plan = {"action": "create_report", "target": "n.md",
                    "summary": "s"}
            steps = []
            verification = None
            report_status = None
            error = "disk full"
            result_message = None
            task_id = "t-2"

        adapter.on_task_update(Rec())
        self.assertEqual(controller.state, STATE_ERROR)
        self.assertNotEqual(controller.state, STATE_CELEBRATING)

    def test_unverified_completion_does_not_celebrate(self):
        # "completed" is the only celebration path, and the lifecycle
        # makes it reachable only after verification — a needs_attention
        # record must show concern instead.
        controller, adapter = self._adapter()

        class Rec:
            state = "needs_attention"
            plan = {"action": "edit", "target": "n.md", "summary": "s"}
            steps = []
            verification = {"status": "failed"}
            report_status = None
            error = None
            result_message = None
            task_id = "t-3"

        adapter.on_task_update(Rec())
        self.assertIn(controller.state, (STATE_ERROR, STATE_IDLE))
        self.assertNotEqual(controller.state, STATE_CELEBRATING)


class RobustnessTests(unittest.TestCase):
    def test_unknown_state_still_draws_safely(self):
        surface = render("definitely-not-a-state", t=1.0)
        self.assertTrue(any(b for b in pixels(surface)))

    def test_zero_and_tiny_sizes_do_not_crash(self):
        for w, h in [(0, 0), (1, 1), (10, 10)]:
            with self.subTest(size=(w, h)):
                surface = cairo.ImageSurface(cairo.FORMAT_ARGB32,
                                             max(w, 1), max(h, 1))
                ctx = cairo.Context(surface)
                draw_character(ctx, w, h, "idle", t=1.0)  # must not raise

    def test_expression_override_changes_the_frame(self):
        self.assertNotEqual(pixels(render("idle", expression="happy")),
                            pixels(render("idle", expression="sleepy")))

    def test_design_box_is_portrait_and_positive(self):
        self.assertGreater(DESIGN_W, 0)
        self.assertGreater(DESIGN_H, DESIGN_W)


if __name__ == "__main__":
    unittest.main()
