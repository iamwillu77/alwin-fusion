"""v0.5 task-lifecycle tests (no desktop, no display, pure logic)."""

from __future__ import annotations

import unittest

from taskui.lifecycle import (
    ALL_STATES,
    STATE_APPROVED,
    STATE_AWAITING_APPROVAL,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_DRAFT,
    STATE_EXECUTING,
    STATE_FAILED,
    STATE_NEEDS_ATTENTION,
    STATE_PLANNING,
    STATE_VERIFYING,
    TERMINAL_STATES,
    TRANSITIONS,
    InvalidTransition,
    TaskRecord,
)


def record(**kwargs) -> TaskRecord:
    kwargs.setdefault("task_id", "t1")
    kwargs.setdefault("command", "do something")
    return TaskRecord(**kwargs)


class StateGraphTests(unittest.TestCase):
    def test_all_states_are_covered_by_the_transition_table(self):
        self.assertEqual(set(ALL_STATES), set(TRANSITIONS))

    def test_terminal_states_have_no_exits(self):
        for state in TERMINAL_STATES:
            with self.subTest(state=state):
                self.assertEqual(TRANSITIONS[state], frozenset())

    def test_unknown_state_is_rejected(self):
        rec = record()
        with self.assertRaises(InvalidTransition):
            rec.transition("exploded")

    def test_draft_to_completed_is_invalid(self):
        rec = record()
        with self.assertRaises(InvalidTransition):
            rec.transition(STATE_COMPLETED)

    def test_draft_to_executing_is_invalid(self):
        # A task can never execute before planning and validation.
        rec = record()
        with self.assertRaises(InvalidTransition):
            rec.transition(STATE_EXECUTING)

    def test_planning_to_completed_is_invalid(self):
        # Completion is only reachable after verification.
        rec = record(state=STATE_PLANNING)
        with self.assertRaises(InvalidTransition):
            rec.transition(STATE_COMPLETED)

    def test_completed_is_reachable_only_via_verifying(self):
        for state in ALL_STATES:
            if state == STATE_VERIFYING:
                continue
            rec = record(state=state)
            try:
                rec.transition(STATE_COMPLETED)
            except InvalidTransition:
                continue  # expected for every non-verifying state
            self.fail(f"{state} must not transition straight to completed")

    def test_executing_cannot_jump_to_completed(self):
        rec = record(state=STATE_EXECUTING)
        with self.assertRaises(InvalidTransition):
            rec.transition(STATE_COMPLETED)

    def test_verifying_cannot_go_back_to_executing(self):
        # Verification judges a recorded result; it never re-runs.
        rec = record(state=STATE_VERIFYING)
        with self.assertRaises(InvalidTransition):
            rec.transition(STATE_EXECUTING)

    def test_approval_flow_happy_path(self):
        rec = record()
        for state in (
            STATE_PLANNING,
            STATE_AWAITING_APPROVAL,
            STATE_APPROVED,
            STATE_EXECUTING,
            STATE_VERIFYING,
            STATE_COMPLETED,
        ):
            rec.transition(state)
        self.assertEqual(rec.state, STATE_COMPLETED)
        self.assertTrue(rec.is_terminal)

    def test_rejection_path_cancels(self):
        rec = record()
        rec.transition(STATE_PLANNING)
        rec.transition(STATE_AWAITING_APPROVAL)
        rec.transition(STATE_CANCELLED)
        self.assertTrue(rec.is_terminal)

    def test_needs_attention_only_from_executing_or_verifying(self):
        for state in ALL_STATES:
            if state in (STATE_EXECUTING, STATE_VERIFYING):
                continue
            rec = record(state=state)
            with self.subTest(state=state):
                with self.assertRaises(InvalidTransition):
                    rec.transition(STATE_NEEDS_ATTENTION)

    def test_timestamps_recorded_per_state(self):
        rec = record()
        rec.transition(STATE_PLANNING)
        self.assertIn(STATE_PLANNING, rec.timestamps)
        self.assertIn("planning", rec.state_label.lower())


class CancellationTests(unittest.TestCase):
    def test_cancel_before_execution_is_allowed(self):
        rec = record(state=STATE_AWAITING_APPROVAL)
        rec.request_cancel()
        self.assertTrue(rec.cancel_requested)

    def test_cancel_while_executing_is_refused(self):
        rec = record(state=STATE_EXECUTING)
        with self.assertRaises(InvalidTransition):
            rec.request_cancel()
        self.assertFalse(rec.cancel_requested)

    def test_cancel_while_verifying_is_refused(self):
        rec = record(state=STATE_VERIFYING)
        with self.assertRaises(InvalidTransition):
            rec.request_cancel()

    def test_cancel_after_completion_is_refused(self):
        rec = record(state=STATE_COMPLETED)
        with self.assertRaises(InvalidTransition):
            rec.request_cancel()


if __name__ == "__main__":
    unittest.main()
