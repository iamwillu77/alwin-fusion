"""v0.6 character-state controller tests (headless, pure logic).

The state controller owns the companion's current animation state and
must uphold the milestone's rules:

* exactly 17 explicit states with a validated transition graph
  (v0.9 added ``speaking`` for voice output),
* invalid transitions never raise and never invent a state — they use
  the documented safe fallback (a calm state),
* a truthful outcome driven by the adapter (waiting_for_approval,
  celebrating, error) is never lost to a fallback,
* renderer failures are contained: an animation exception can never
  propagate into callers (and therefore never into task execution),
* the snapshot a view renders is complete, bounded and GTK-free.
"""

from __future__ import annotations

import unittest

from companion.states import (
    ALL_STATES,
    ANIMATED_STATES,
    AUTO_RETURN_DELAYS,
    EXPRESSIONS,
    FALLBACK_STATE,
    STATE_CELEBRATING,
    STATE_ERROR,
    STATE_GREETING,
    STATE_IDLE,
    STATE_LABELS,
    STATE_RESTING,
    STATE_SLEEPING,
    STATE_WAITING_FOR_APPROVAL,
    TRANSITIONS,
    CharacterController,
    is_valid_transition,
)
from companion.renderer import NullRenderer

#: The static states: no continuous animation (frame timer stops).
#: v0.7: idle is *animated* (subtle breathing/blinking at a slow
#: cadence), so only the sleeping-like states are truly static.
STATIC_STATES = frozenset({STATE_SLEEPING, STATE_RESTING})


class _ExplodingRenderer(NullRenderer):
    """A renderer whose animation code raises on every call."""

    def show_state(self, state, *, expression=None):
        raise RuntimeError("animation exploded")

    def set_expression(self, expression):
        raise RuntimeError("animation exploded")

    def tick(self, now):
        raise RuntimeError("animation exploded")


class _RecordingRenderer(NullRenderer):
    def __init__(self):
        super().__init__()
        self.attached = None
        self.detached = False
        self.expression_calls = []

    def attach(self, target):
        self.attached = target

    def detach(self):
        self.detached = True

    def set_expression(self, expression):
        self.expression_calls.append(expression)
        super().set_expression(expression)


class VocabularyTests(unittest.TestCase):
    def test_exactly_seventeen_states(self):
        self.assertEqual(len(ALL_STATES), 17)
        self.assertEqual(len(set(ALL_STATES)), 17, "states are unique")

    def test_required_state_names_present(self):
        required = {
            "sleeping", "waking_up", "greeting", "idle", "listening",
            "thinking", "speaking", "walking", "writing", "coding",
            "reading", "waiting_for_approval", "working", "verifying",
            "celebrating", "error", "resting",
        }
        self.assertEqual(set(ALL_STATES), required)

    def test_every_state_has_labels_expressions_and_edges(self):
        for state in ALL_STATES:
            with self.subTest(state=state):
                self.assertIn(state, TRANSITIONS)
                self.assertIn(state, STATE_LABELS)
                self.assertIn(state, EXPRESSIONS)
                self.assertTrue(STATE_LABELS[state].strip())
                # Every edge target must be a known state.
                self.assertLessEqual(TRANSITIONS[state], set(ALL_STATES))

    def test_static_and_animated_states_partition_the_vocabulary(self):
        self.assertEqual(ANIMATED_STATES | STATIC_STATES, set(ALL_STATES))
        self.assertFalse(ANIMATED_STATES & STATIC_STATES)

    def test_no_state_can_reach_itself_except_through_valid_edges(self):
        # Self-transitions are not part of the graph: re-entering the
        # same state is handled as a "refresh", not as a graph edge.
        for state in ALL_STATES:
            with self.subTest(state=state):
                self.assertNotIn(state, TRANSITIONS[state])

    def test_valid_and_invalid_edges(self):
        self.assertTrue(is_valid_transition("idle", "listening"))
        self.assertTrue(is_valid_transition("thinking", "verifying"))
        self.assertTrue(is_valid_transition("verifying", "celebrating"))
        self.assertFalse(is_valid_transition("sleeping", "working"))
        self.assertFalse(is_valid_transition("celebrating", "working"))
        self.assertFalse(is_valid_transition("idle", "greeting"))
        self.assertFalse(is_valid_transition("not-a-state", "idle"))


class TransitionTests(unittest.TestCase):
    def setUp(self):
        self.renderer = _RecordingRenderer()
        self.controller = CharacterController(self.renderer)

    def test_starts_calm_and_idle(self):
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertEqual(self.controller.expression, EXPRESSIONS[STATE_IDLE])

    def test_valid_transition_is_reached_and_dispatched(self):
        ok = self.controller.enter("waiting_for_approval", reason="plan")
        self.assertTrue(ok)
        self.assertEqual(self.controller.state, STATE_WAITING_FOR_APPROVAL)
        self.assertEqual(self.renderer.state, STATE_WAITING_FOR_APPROVAL)
        self.assertEqual(self.controller.reason, "plan")

    def test_expression_follows_the_state(self):
        self.controller.force("celebrating")
        self.assertEqual(self.controller.expression, "proud")

    def test_invalid_transition_from_calm_state_changes_nothing(self):
        ok = self.controller.enter("greeting")
        self.assertFalse(ok, "the invalid request must not be claimed")
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertEqual(self.renderer.state, STATE_IDLE)

    def test_invalid_transition_falls_back_to_a_calm_state(self):
        self.controller.force("thinking")
        ok = self.controller.enter("greeting")  # only reachable from waking_up
        self.assertFalse(ok)
        self.assertIn(self.controller.state, (STATE_IDLE, STATE_RESTING))
        self.assertIn(self.controller.state, ALL_STATES)

    def test_truthful_target_survives_via_one_calm_hop(self):
        # A companion caught mid-greeting must still show a trusted
        # approval request instead of losing it to a fallback.
        self.controller.force("greeting")
        ok = self.controller.enter(STATE_WAITING_FOR_APPROVAL)
        self.assertTrue(ok)
        self.assertEqual(self.controller.state, STATE_WAITING_FOR_APPROVAL)

    def test_unknown_state_never_raises_and_never_claims_success(self):
        self.controller.force("sleeping")
        ok = self.controller.enter("nonsense-state")
        self.assertFalse(ok)
        self.assertIn(self.controller.state, ALL_STATES)
        self.assertNotEqual(self.controller.state, "nonsense-state")

    def test_unknown_state_from_calm_stays_put(self):
        ok = self.controller.enter("another-bogus-state")
        self.assertFalse(ok)
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_reentering_same_state_refreshes_reason_only(self):
        self.controller.enter("listening", reason="first")
        entered_before = self.controller.entered_at
        self.controller.enter("listening", reason="second")
        self.assertEqual(self.controller.state, "listening")
        self.assertEqual(self.controller.reason, "second")
        self.assertEqual(self.controller.entered_at, entered_before)

    def test_force_validates_state_membership(self):
        self.assertFalse(self.controller.force("bogus"))
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.assertTrue(self.controller.force(STATE_SLEEPING, reason="night"))
        self.assertEqual(self.controller.state, STATE_SLEEPING)

    def test_outcome_states_are_reachable_from_a_calm_start(self):
        # Robustness: attaching mid-task must not swallow a trusted result.
        self.assertTrue(self.controller.enter(STATE_CELEBRATING))
        self.assertEqual(self.controller.state, STATE_CELEBRATING)
        self.controller.force(STATE_IDLE)
        self.assertTrue(self.controller.enter(STATE_ERROR))
        self.assertEqual(self.controller.state, STATE_ERROR)

    def test_outcome_states_return_calmly(self):
        self.controller.force("celebrating")
        self.assertTrue(self.controller.enter(STATE_IDLE))
        self.assertEqual(self.controller.state, STATE_IDLE)
        self.controller.force("error")
        self.assertTrue(self.controller.enter(STATE_IDLE))
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_auto_return_delays_exist_for_transient_states(self):
        for state in (STATE_CELEBRATING, STATE_ERROR, STATE_GREETING):
            with self.subTest(state=state):
                self.assertIn(state, AUTO_RETURN_DELAYS)
                self.assertIsInstance(AUTO_RETURN_DELAYS[state], float)
        for state in (STATE_IDLE, STATE_WAITING_FOR_APPROVAL):
            with self.subTest(state=state):
                # These hold until something else happens.
                self.assertIsNone(AUTO_RETURN_DELAYS.get(state))
        self.controller.force("celebrating")
        self.assertEqual(self.controller.auto_return_delay(), 4.0)

    def test_seconds_in_state_uses_the_injected_clock(self):
        now = [100.0]
        controller = CharacterController(clock=lambda: now[0])
        controller.enter("thinking")
        now[0] = 105.5
        self.assertEqual(controller.seconds_in_state(), 5.5)


class RendererIsolationTests(unittest.TestCase):
    def test_renderer_exception_never_escapes(self):
        renderer = _ExplodingRenderer()
        controller = CharacterController(renderer)
        # These are the calls the window/adapter make in normal flow;
        # none may raise, or an animation bug could break presentation.
        controller.enter("thinking")
        controller.enter("working")
        controller.enter("celebrating")
        controller.force("sleeping")
        self.assertEqual(controller.state, STATE_SLEEPING)

    def test_renderer_errors_are_recorded_not_hidden(self):
        controller = CharacterController(_ExplodingRenderer())
        controller.enter("thinking")
        self.assertTrue(controller.renderer_errors)
        self.assertIn("RuntimeError", controller.renderer_errors[0])
        snapshot = controller.snapshot()
        self.assertTrue(snapshot["renderer_errors"])

    def test_renderer_errors_list_is_bounded(self):
        controller = CharacterController(_ExplodingRenderer())
        for _ in range(50):
            controller.enter("thinking")
            controller.enter("working")
        self.assertLessEqual(len(controller.renderer_errors), 16)

    def test_on_change_callback_exception_is_contained(self):
        def _boom(state, expression, reason):
            raise ValueError("view bug")

        controller = CharacterController(on_change=_boom)
        controller.enter("listening")
        self.assertEqual(controller.state, "listening")

    def test_no_renderer_means_headless_use(self):
        controller = CharacterController()
        controller.enter("thinking")
        self.assertEqual(controller.state, "thinking")
        self.assertEqual(controller.renderer_errors, [])

    def test_controller_drives_any_renderer_through_the_interface(self):
        renderer = _RecordingRenderer()
        controller = CharacterController(renderer)
        controller.enter("waiting_for_approval")
        renderer.set_expression("hopeful")
        renderer.tick(1.0)
        controller.force("idle")
        self.assertEqual(renderer.state, STATE_IDLE)
        self.assertEqual(renderer.expression_calls, ["hopeful"])
        self.assertEqual(renderer.calls[0][0], "state")
        # attach/detach are part of the renderer contract.
        marker = object()
        renderer.attach(marker)
        self.assertIs(renderer.attached, marker)
        renderer.detach()
        self.assertTrue(renderer.detached)


class SnapshotTests(unittest.TestCase):
    def test_snapshot_exposes_everything_a_view_needs(self):
        controller = CharacterController()
        controller.enter("waiting_for_approval", reason="needs approval")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["state"], "waiting_for_approval")
        self.assertEqual(snapshot["state_label"], "waiting for your approval")
        self.assertEqual(snapshot["expression"], "hopeful")
        self.assertEqual(snapshot["reason"], "needs approval")
        self.assertTrue(snapshot["animated"])
        self.assertIsNone(snapshot["auto_return_delay"])
        self.assertEqual(snapshot["renderer_errors"], [])

    def test_snapshot_static_state_is_not_marked_animated(self):
        controller = CharacterController()
        controller.force(STATE_SLEEPING, reason="test")
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["state"], STATE_SLEEPING)
        self.assertFalse(snapshot["animated"])

    def test_snapshot_idle_state_is_marked_animated(self):
        # v0.7: idle breathes and blinks, so it animates (slow cadence).
        controller = CharacterController()
        snapshot = controller.snapshot()
        self.assertEqual(snapshot["state"], STATE_IDLE)
        self.assertTrue(snapshot["animated"])

    def test_snapshot_is_a_plain_dict(self):
        snapshot = CharacterController().snapshot()
        self.assertIsInstance(snapshot, dict)
        self.assertIsInstance(snapshot["state"], str)
        self.assertIsInstance(snapshot["state_label"], str)
        self.assertIsInstance(snapshot["expression"], str)
        self.assertIsInstance(snapshot["animated"], bool)
        self.assertIsInstance(snapshot["renderer_errors"], list)
        self.assertTrue(all(isinstance(e, str)
                            for e in snapshot["renderer_errors"]))


if __name__ == "__main__":
    unittest.main()
