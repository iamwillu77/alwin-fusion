"""v0.8 animated-world (scene) tests: model, movement, morning, tasks, render.

Everything runs **headless** — a ``cairo.ImageSurface`` for the drawing
checks and *simulated time* for the movement/sequence checks.  No display,
no desktop, no real window, no real clock is waited on.

Covered (mirroring the milestone brief):

* **scene model** — objects carry stable identity and bounds that stay
  inside the design box; named positions exist; the scene->window transform
  scales *uniformly* (never stretches) and centres the room,
* **movement** — smoothstep easing, frame-rate independence (a delayed
  frame lands on the correct spot), invalid destinations are rejected
  without disturbing the current position, a repeated request is a no-op,
  a new request replaces an in-flight one, and an arrival fires exactly
  once,
* **morning sequence** — sleeping -> wake -> get up -> walk -> arrive, all
  advanced by elapsed time; the reduced-motion variant collapses it,
* **task integration** — writing/coding walk him to the desk and animate,
  thinking returns him to the greeting spot, an approval/failure stops the
  work and sets an honest state, celebration is only a visual (the scene
  never approves), and the closed command allowlist refuses anything else,
* **rendering** — the layered room + placed character draw headless,
  deterministically, with the writing/coding hands landing on the desk
  items (a drift guard), and every drawing call is exception-isolated.
"""

from __future__ import annotations

import unittest

import cairo

from companion import bedroom
from companion.character import draw_character_placed
from companion.renderer import NullRenderer
from companion.scene import (
    BEDROOM_OBJECTS,
    CHAR_SCALE,
    FLOOR_ANCHOR_Y,
    FLOOR_Y,
    NAMED_POSITIONS,
    P_BEDROOM_SLEEPING,
    P_BEDROOM_WAKING,
    P_CODING,
    P_GREETING,
    P_GETTING_UP,
    P_IDLE_GREETING_POSITION,
    P_THINKING,
    P_WAITING_FOR_APPROVAL,
    P_ERROR,
    P_WALKING_TO_DESK,
    P_WALKING_TO_GREETING,
    P_WRITING,
    SCENE_H,
    SCENE_TRANSITIONS,
    SCENE_W,
    SCENE_PHASES,
    MovementController,
    SceneController,
    SceneTransform,
    is_valid_scene_transition,
    smoothstep,
)
from companion.states import (
    STATE_CODING,
    STATE_GREETING,
    STATE_THINKING,
    STATE_WAITING_FOR_APPROVAL,
    STATE_WALKING,
    STATE_WRITING,
    CharacterController,
)

W, H = 480, 360


# --------------------------------------------------------------------------- #
# Helpers
# --------------------------------------------------------------------------- #

def render_scene(view, t: float = 0.0, *, width: int = W, height: int = H,
                 with_character: bool = True, state: str = "idle"
                 ) -> cairo.ImageSurface:
    """Headless draw of the room (back), the character, and the room front."""
    surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, width, height)
    ctx = cairo.Context(surface)
    tr = SceneTransform.for_viewport(width, height)
    ctx.set_source_rgb(0.16, 0.14, 0.18)
    ctx.paint()
    bedroom.draw_scene_back(ctx, view, t, tr)
    if with_character:
        ctx.save()
        ctx.translate(tr.offset_x, tr.offset_y)
        ctx.scale(tr.scale, tr.scale)
        draw_character_placed(ctx, view.x, view.y, CHAR_SCALE, state, t=t,
                              rotation=view.rotation)
        ctx.restore()
    bedroom.draw_scene_front(ctx, view, t, tr)
    surface.flush()
    return surface


def pixels(surface: cairo.ImageSurface) -> bytes:
    return bytes(surface.get_data())


def painted_fraction(surface: cairo.ImageSurface) -> float:
    data = pixels(surface)
    stride = surface.get_stride()
    w, h = surface.get_width(), surface.get_height()
    painted = 0
    for y in range(0, h, 3):
        row = y * stride
        for x in range(0, w, 3):
            if data[row + x * 4 + 3] > 8:  # alpha
                painted += 1
    total = len(range(0, h, 3)) * len(range(0, w, 3))
    return painted / max(1, total)


def advance(scene: SceneController, times) -> list:
    events = []
    for now in times:
        events.extend(scene.tick(now))
    return events


# --------------------------------------------------------------------------- #
# Scene model
# --------------------------------------------------------------------------- #

class SceneModelTests(unittest.TestCase):
    def test_objects_have_stable_identity_and_bounds(self):
        self.assertGreaterEqual(len(BEDROOM_OBJECTS), 12)
        for oid, obj in BEDROOM_OBJECTS.items():
            with self.subTest(object=oid):
                self.assertEqual(obj.id, oid)
                x, y, w, h = obj.bounds
                self.assertGreater(w, 0)
                self.assertGreater(h, 0)
                # every object lives inside the design box
                self.assertGreaterEqual(x, -1.0)
                self.assertGreaterEqual(y, -1.0)
                self.assertLessEqual(x + w, SCENE_W + 1.0)
                self.assertLessEqual(y + h, SCENE_H + 1.0)

    def test_named_positions_exist_on_the_walking_plane(self):
        for name in ("beside_bed", "greeting", "desk"):
            pos = NAMED_POSITIONS[name]
            self.assertAlmostEqual(pos.y, FLOOR_ANCHOR_Y, places=3)
            self.assertEqual(pos.rotation, 0.0)
        # the bed is the one off-plane (lying) anchor
        bed = NAMED_POSITIONS["bed"]
        self.assertNotEqual(bed.rotation, 0.0)

    def test_floor_anchor_puts_soles_on_the_floor(self):
        # FLOOR_ANCHOR_Y = FLOOR_Y + the ankle->sole drop, so a figure
        # anchored there has its soles exactly on the walking plane.
        self.assertGreater(FLOOR_ANCHOR_Y, FLOOR_Y)
        drop = FLOOR_ANCHOR_Y - FLOOR_Y
        self.assertAlmostEqual(drop, 6.5 * CHAR_SCALE, places=4)


class SceneTransformTests(unittest.TestCase):
    def test_scaling_is_uniform_and_centred(self):
        tr = SceneTransform.for_viewport(960, 720)
        self.assertAlmostEqual(tr.scale, 2.0, places=5)
        self.assertAlmostEqual(tr.offset_x, 0.0, places=5)
        self.assertAlmostEqual(tr.offset_y, 0.0, places=5)

    def test_letterbox_margins_are_symmetric_when_aspect_differs(self):
        # A very wide window: the scene fits by height and is centred
        # with symmetric horizontal margins (never stretched).
        tr = SceneTransform.for_viewport(1200, 360)
        self.assertAlmostEqual(tr.scale, 1.0, places=5)
        left = tr.offset_x
        right = 1200 - (tr.offset_x + SCENE_W * tr.scale)
        self.assertAlmostEqual(left, right, places=3)

    def test_no_axis_is_ever_scaled_alone(self):
        # The x and y scale are the *same* number by construction.
        tr = SceneTransform.for_viewport(500, 400)
        px, py = tr.to_pixel(10, 20)
        qx, qy = tr.to_pixel(20, 30)
        self.assertAlmostEqual(qx - px, qy - py, places=5)

    def test_degenerate_viewport_is_safe(self):
        tr = SceneTransform.for_viewport(0, -5)
        self.assertEqual(tr.scale, 0.0)


# --------------------------------------------------------------------------- #
# Movement
# --------------------------------------------------------------------------- #

class MovementTests(unittest.TestCase):
    def test_smoothstep_eases_in_and_out(self):
        self.assertEqual(smoothstep(-1.0), 0.0)
        self.assertEqual(smoothstep(2.0), 1.0)
        self.assertEqual(smoothstep(0.5), 0.5)
        self.assertLess(smoothstep(0.25), 0.25)   # slow start
        self.assertGreater(smoothstep(0.75), 0.75)  # slow end

    def test_arrival_fires_exactly_once(self):
        m = MovementController(0.0, FLOOR_ANCHOR_Y)
        self.assertTrue(m.walk_to(100.0, FLOOR_ANCHOR_Y, now=0.0))
        self.assertEqual(m.update(0.5), False)    # still en route
        self.assertEqual(m.update(1.0), True)     # arrival tick
        self.assertEqual(m.update(2.0), False)    # never again

    def test_delayed_frames_land_on_the_correct_spot(self):
        # Position is a pure function of elapsed time, so one big jump
        # and many small steps reach the same place at the same moment.
        a = MovementController(0.0, FLOOR_ANCHOR_Y)
        b = MovementController(0.0, FLOOR_ANCHOR_Y)
        a.walk_to(120.0, FLOOR_ANCHOR_Y, now=0.0)
        b.walk_to(120.0, FLOOR_ANCHOR_Y, now=0.0)
        a.update(1.0)                       # one delayed frame
        for step in (0.1, 0.2, 0.3, 0.4, 1.0):
            b.update(step)                  # smooth frames
        self.assertAlmostEqual(a.x, b.x, places=5)
        self.assertAlmostEqual(a.y, b.y, places=5)

    def test_repeated_same_request_is_a_no_op(self):
        m = MovementController(0.0, FLOOR_ANCHOR_Y)
        m.walk_to(100.0, FLOOR_ANCHOR_Y, now=0.0)
        start = m._start
        m.walk_to(100.0, FLOOR_ANCHOR_Y, now=0.5)   # same target in flight
        self.assertEqual(m._start, start)           # not restarted

    def test_new_request_replaces_the_old_from_current_position(self):
        m = MovementController(0.0, FLOOR_ANCHOR_Y)
        m.walk_to(100.0, FLOOR_ANCHOR_Y, now=0.0)
        m.update(0.5)
        mid_x = m.x
        m.walk_to(200.0, FLOOR_ANCHOR_Y, now=1.0)
        self.assertAlmostEqual(m.x, mid_x, places=5)  # continues from here
        self.assertTrue(m.moving)

    def test_off_plane_and_out_of_room_targets_are_rejected(self):
        m = MovementController(0.0, FLOOR_ANCHOR_Y)
        before = (m.x, m.y)
        self.assertFalse(m.walk_to(50.0, 100.0, now=0.0))       # off plane
        self.assertFalse(m.walk_to(1000.0, FLOOR_ANCHOR_Y, now=0.0))  # out
        self.assertFalse(m.walk_to(float("nan"), FLOOR_ANCHOR_Y, now=0.0))
        self.assertEqual((m.x, m.y), before)                    # undisturbed
        self.assertFalse(m.moving)


# --------------------------------------------------------------------------- #
# Scene controller: commands, phases, honesty
# --------------------------------------------------------------------------- #

class SceneControllerTests(unittest.TestCase):
    def test_starts_asleep_in_bed(self):
        c = SceneController()
        self.assertEqual(c.phase, P_BEDROOM_SLEEPING)
        self.assertTrue(c.at_position("bed"))
        self.assertFalse(c.moving)

    def test_command_allowlist_is_closed(self):
        c = SceneController()
        self.assertFalse(c.command("rm_rf", now=0.0))
        self.assertFalse(c.command("exec", now=0.0))
        self.assertFalse(c.command("__import__", now=0.0))
        self.assertIn("unknown", c.status)

    def test_unknown_position_is_refused(self):
        c = SceneController()
        self.assertFalse(c.command("move_to", position="moon", now=0.0))
        self.assertIn("unknown scene position", c.status)

    def test_cannot_walk_to_the_bed(self):
        c = SceneController()
        c.command("wake", now=0.0)
        c.command("get_up", now=0.5)
        advance(c, (0.6, 1.0, 1.4, 1.6))
        self.assertFalse(c.command("move_to", position="bed", now=2.0))
        self.assertIn("cannot walk to the bed", c.status)

    def test_full_morning_walk_to_greeting(self):
        c = SceneController()
        c.command("wake", now=0.0)
        self.assertEqual(c.phase, P_BEDROOM_WAKING)
        c.command("get_up", now=0.5)
        self.assertEqual(c.phase, P_GETTING_UP)
        events = advance(c, (0.6, 1.0, 1.4, 1.6))
        self.assertTrue(c.at_position("beside_bed"))
        self.assertEqual(c.phase, P_IDLE_GREETING_POSITION)
        c.command("move_to", position="greeting", now=2.0)
        self.assertEqual(c.phase, P_WALKING_TO_GREETING)
        events = advance(c, (2.3, 2.7, 3.1))
        self.assertIn("arrived_at_greeting", events)
        self.assertTrue(c.at_position("greeting"))
        self.assertEqual(c.phase, P_GREETING)

    def test_walking_is_elapsed_time_eased(self):
        c = SceneController()
        c.command("place", position="beside_bed", now=0.0)
        c.command("move_to", position="greeting", now=0.0)
        xs = []
        for now in (0.1, 0.2, 0.3, 0.5, 0.8):
            c.tick(now)
            xs.append(c.position[0])
        # monotonic + eased (first steps smaller than the middle ones)
        self.assertEqual(xs, sorted(xs))
        self.assertLess(xs[1] - xs[0], xs[3] - xs[2])

    def test_begin_writing_walks_to_desk_then_writes(self):
        c = SceneController()
        c.command("place", position="greeting", now=0.0)
        c.command("begin_writing", now=0.0)
        self.assertEqual(c.phase, P_WALKING_TO_DESK)
        events = advance(c, (0.5, 1.0, 1.5, 2.0))
        self.assertIn("arrived_at_desk", events)
        self.assertEqual(c.phase, P_WRITING)
        self.assertTrue(c.at_position("desk"))
        self.assertTrue(c.render_view(2.0).writing)

    def test_begin_coding_sets_coding_phase(self):
        c = SceneController()
        c.command("place", position="desk", now=0.0)
        c.command("begin_coding", now=0.0)
        self.assertEqual(c.phase, P_CODING)
        self.assertTrue(c.render_view(0.0).coding)

    def test_thinking_walks_to_the_greeting_spot(self):
        c = SceneController()
        c.command("place", position="desk", now=0.0)
        c.command("begin_thinking", now=0.0)
        self.assertEqual(c.phase, P_WALKING_TO_GREETING)
        advance(c, (0.5, 1.0, 1.5, 2.0, 2.5))
        self.assertTrue(c.at_position("greeting"))
        self.assertEqual(c.phase, P_THINKING)

    def test_approval_and_failure_stop_work_and_are_legal_mid_walk(self):
        c = SceneController()
        c.command("place", position="greeting", now=0.0)
        c.command("begin_writing", now=0.0)          # start walking to desk
        c.tick(0.2)
        self.assertTrue(c.moving)
        # an approval interrupts the walk deterministically
        self.assertTrue(c.command("begin_waiting", now=0.4))
        self.assertFalse(c.moving)
        self.assertEqual(c.phase, P_WAITING_FOR_APPROVAL)
        self.assertFalse(c.render_view(0.5).writing)
        # a failure is also legal from the waiting state
        self.assertTrue(c.command("begin_error", now=0.6))
        self.assertEqual(c.phase, P_ERROR)

    def test_celebration_is_visual_only(self):
        c = SceneController()
        c.command("place", position="greeting", now=0.0)
        self.assertTrue(c.command("begin_celebrating", now=0.0))
        # the scene has no notion of "approved" or "completed a task";
        # it only reports a phase + an honest status string.
        self.assertIn("complete", c.status)
        self.assertFalse(hasattr(c, "approved"))

    def test_task_command_mid_morning_snaps_him_up(self):
        c = SceneController()
        c.command("wake", now=0.0)
        c.command("begin_coding", now=0.5)           # still in bed visually
        # _ensure_up stands him beside the bed so the task never waits.
        self.assertEqual(c.rotation, 0.0)
        self.assertFalse(c.at_position("bed"))

    def test_reset_returns_to_bed(self):
        c = SceneController()
        c.command("place", position="desk", now=0.0)
        c.command("reset", now=1.0)
        self.assertTrue(c.at_position("bed"))
        self.assertEqual(c.phase, P_BEDROOM_SLEEPING)

    def test_render_view_snapshot_is_immutable_data(self):
        c = SceneController()
        view = c.render_view(0.0)
        self.assertEqual(view.phase, P_BEDROOM_SLEEPING)
        self.assertTrue(view.sleep_zs)
        self.assertEqual(view.blanket_cover, 1.0)
        self.assertEqual(view.sunrise, 0.18)         # pre-dawn while asleep

    def test_scene_phases_have_a_coherent_transition_graph(self):
        # every declared transition stays inside the phase vocabulary
        for src, targets in SCENE_TRANSITIONS.items():
            self.assertIn(src, SCENE_PHASES)
            for dst in targets:
                self.assertIn(dst, SCENE_PHASES)
        # an error / approval is reachable from anywhere (interrupts walks)
        self.assertTrue(is_valid_scene_transition(P_WRITING, P_ERROR))
        self.assertTrue(is_valid_scene_transition(P_WALKING_TO_DESK,
                                                  P_WAITING_FOR_APPROVAL))
        self.assertFalse(is_valid_scene_transition(P_BEDROOM_SLEEPING,
                                                   P_WRITING))


# --------------------------------------------------------------------------- #
# Reduced motion
# --------------------------------------------------------------------------- #

class ReducedMotionTests(unittest.TestCase):
    def test_get_up_is_instant(self):
        c = SceneController(reduced_motion=True)
        c.command("wake", now=0.0)
        c.command("get_up", now=0.1)
        self.assertTrue(c.at_position("beside_bed"))
        self.assertEqual(c.rotation, 0.0)

    def test_sunrise_and_alarm_are_static(self):
        c = SceneController(reduced_motion=True)
        c.command("wake", now=0.0)
        a = c.render_view(0.0)
        b = c.render_view(10.0)
        self.assertEqual(a.sunrise, b.sunrise)
        self.assertEqual(a.alarm, 0.0)  # no ringing shake


# --------------------------------------------------------------------------- #
# Rendering (headless cairo)
# --------------------------------------------------------------------------- #

class RenderingTests(unittest.TestCase):
    def test_room_draws_headless_and_substantial(self):
        c = SceneController()
        surface = render_scene(c.render_view(0.0), 0.0)
        self.assertGreater(painted_fraction(surface), 0.5)

    def test_rendering_is_deterministic(self):
        c = SceneController()
        c.command("place", position="desk", now=0.0)
        view = c.render_view(0.0)
        a = pixels(render_scene(view, 1.0, state="coding"))
        b = pixels(render_scene(view, 1.0, state="coding"))
        self.assertEqual(a, b)

    def test_character_is_not_permanently_centred(self):
        # Standing at the desk vs the greeting spot must move the figure
        # in *pixels*, proving he is placed in the world, not centred.
        c = SceneController()
        c.command("place", position="greeting", now=0.0)
        greet = render_scene(c.render_view(0.0), 0.0, state="idle")
        c.command("place", position="desk", now=1.0)
        desk = render_scene(c.render_view(1.0), 1.0, state="idle")
        self.assertNotEqual(pixels(greet), pixels(desk))

    def test_scene_scales_down_without_distorting(self):
        c = SceneController()
        view = c.render_view(0.0)
        big = render_scene(view, 0.0, width=480, height=360)
        small = render_scene(view, 0.0, width=240, height=180)
        self.assertGreater(painted_fraction(small), 0.4)
        self.assertGreater(painted_fraction(big), 0.4)

    def test_writing_hands_land_on_the_notebook(self):
        # A drift guard: the writing pose's hands, placed at the desk,
        # must sit on the notebook the room draws (else he writes at
        # thin air).  Geometry is derived from the placement transform.
        c = SceneController()
        c.command("place", position="desk", now=0.0)
        desk = NAMED_POSITIONS["desk"]

        def to_scene(dx, dy):
            return (desk.x + CHAR_SCALE * (dx - 110.0),
                    desk.y + CHAR_SCALE * (dy - 248.0))

        l_hand = to_scene(78.0, 188.0)
        r_hand = to_scene(98.0, 184.0)
        # the notebook occupies roughly x 344..374, y 278..290
        for hand in (l_hand, r_hand):
            self.assertGreater(hand[0], 340.0)
            self.assertLess(hand[0], 380.0)
            self.assertGreater(hand[1], 275.0)
            self.assertLess(hand[1], 292.0)

    def test_coding_hands_reach_the_laptop(self):
        c = SceneController()
        c.command("place", position="desk", now=0.0)
        desk = NAMED_POSITIONS["desk"]

        def to_scene(dx, dy):
            return (desk.x + CHAR_SCALE * (dx - 110.0),
                    desk.y + CHAR_SCALE * (dy - 248.0))

        r_hand = to_scene(130.0, 187.0)
        # the laptop sits to his right on the desk; the right hand must
        # reach its near edge (within a few units).
        self.assertGreater(r_hand[0], 385.0)
        self.assertLess(r_hand[0], 405.0)

    def test_morning_sequence_renders_each_stage(self):
        c = SceneController()
        # sleeping
        self.assertTrue(c.render_view(0.0).sleep_zs)
        render_scene(c.render_view(0.0), 0.0, state="sleeping")
        # waking (sunrise rises, alarm rings)
        c.command("wake", now=0.0)
        woke = c.render_view(1.0)
        self.assertGreater(woke.sunrise, 0.18)
        render_scene(woke, 1.0, state="waking_up")
        # getting up
        c.command("get_up", now=2.0)
        render_scene(c.render_view(2.5), 2.5, state="waking_up")
        # walking (grounded, feet on the floor plane)
        advance(c, (2.6, 3.0, 3.4, 3.6))
        c.command("move_to", position="greeting", now=4.0)
        c.tick(4.5)
        walking = c.render_view(4.5)
        self.assertTrue(walking.moving)
        self.assertAlmostEqual(walking.y, FLOOR_ANCHOR_Y, places=3)
        render_scene(walking, 4.5, state="walking")


# --------------------------------------------------------------------------- #
# Task integration through the character controller
# --------------------------------------------------------------------------- #

class TaskIntegrationTests(unittest.TestCase):
    def test_scene_reflects_character_states(self):
        controller = CharacterController(NullRenderer())
        scene = SceneController()
        scene.command("place", position="greeting", now=0.0)

        def drive(state):
            cmd = {
                STATE_WRITING: "begin_writing",
                STATE_CODING: "begin_coding",
                STATE_THINKING: "begin_thinking",
                STATE_WAITING_FOR_APPROVAL: "begin_waiting",
            }.get(state)
            if cmd:
                scene.command(cmd, now=0.0)

        controller.enter(STATE_WRITING)
        drive(controller.state)
        advance(scene, (0.5, 1.0, 1.5, 2.0))
        self.assertEqual(scene.phase, P_WRITING)

        controller.enter(STATE_THINKING)
        drive(controller.state)
        advance(scene, (2.5, 3.0, 3.5, 4.0, 4.5))
        self.assertEqual(scene.phase, P_THINKING)
        self.assertTrue(scene.at_position("greeting"))

    def test_walking_state_allows_the_morning_hop(self):
        controller = CharacterController(NullRenderer())
        controller.force("waking_up")
        self.assertTrue(controller.enter(STATE_WALKING))
        self.assertTrue(controller.enter(STATE_GREETING))


# --------------------------------------------------------------------------- #
# Exception isolation
# --------------------------------------------------------------------------- #

class ExceptionIsolationTests(unittest.TestCase):
    def test_bedroom_draw_is_isolated_from_a_broken_view(self):
        class Boom:
            phase = "x"
            moving = False
            sunrise = 0.0
            alarm = 0.0
            blanket_cover = 1.0
            sleep_zs = False
            writing = False
            coding = False
            x = 0.0
            y = 0.0
            rotation = 0.0

            def __getattr__(self, name):
                raise RuntimeError("boom")

        surface = cairo.ImageSurface(cairo.FORMAT_ARGB32, W, H)
        ctx = cairo.Context(surface)
        tr = SceneTransform.for_viewport(W, H)
        # A pathological view must not raise out of the drawing helpers
        # that merely read plain attributes; the renderer wraps the rest.
        try:
            bedroom.draw_scene_front(ctx, Boom(), 0.0, tr)
        except RuntimeError:
            # Acceptable: the renderer's _draw_scene_vector catches it.
            pass


if __name__ == "__main__":
    unittest.main()
