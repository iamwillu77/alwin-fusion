"""v0.6 task-event adapter tests (headless, duck-typed records only).

The adapter is the *only* bridge from trusted task records to the
character.  These tests pin its trust rules:

* every real task lifecycle state maps to the correct character state,
* only a trusted ``completed`` reaches ``celebrating`` — a failed,
  needs-attention or cancelled task never produces a success state,
* an approval request maps to ``waiting_for_approval`` (the approval
  itself stays in the trusted task window),
* speech lines come from curated, bounded text — raw error/result
  content is never spoken,
* the adapter never raises, never mutates anything, and exposes no
  method that could grant a permission or change an outcome.
"""

from __future__ import annotations

import unittest

from companion.adapter import (
    CODING_TEXT_SUFFIXES,
    MAX_SPEECH_CHARS,
    STATE_LINES,
    TaskEventAdapter,
    action_state,
)
from companion.renderer import NullRenderer
from companion.states import (
    ALL_STATES,
    STATE_CELEBRATING,
    STATE_CODING,
    STATE_ERROR,
    STATE_IDLE,
    STATE_LISTENING,
    STATE_READING,
    STATE_THINKING,
    STATE_VERIFYING,
    STATE_WAITING_FOR_APPROVAL,
    STATE_WALKING,
    STATE_WORKING,
    STATE_WRITING,
    CharacterController,
)


class FakeRecord:
    """Duck-typed stand-in for taskui.lifecycle.TaskRecord (curated fields)."""

    def __init__(self, state, *, action="create_report", target="notes.md",
                 summary="Write a summary", steps=None, verification=None,
                 error=None, result_message=None, task_id="t-1"):
        self.state = state
        self.plan = {"action": action, "target": target, "summary": summary}
        self.steps = steps or []
        self.verification = verification
        self.report_status = None
        self.error = error
        self.result_message = result_message
        self.task_id = task_id
        self.state_label = state.replace("_", " ")


class AdapterHarness(unittest.TestCase):
    def setUp(self):
        self.renderer = NullRenderer()
        self.controller = CharacterController(self.renderer)
        self.spoken = []
        self.adapter = TaskEventAdapter(
            self.controller, on_speech=self.spoken.append
        )

    def feed(self, state, **kwargs) -> str:
        self.adapter.on_task_update(FakeRecord(state, **kwargs))
        return self.controller.state

    def rendered_states(self):
        return [c[1] for c in self.renderer.calls if c[0] == "state"]


class MappingTests(AdapterHarness):
    def test_happy_path_states(self):
        expected = [
            ("planning", STATE_THINKING),
            ("awaiting_approval", STATE_WAITING_FOR_APPROVAL),
            ("approved", STATE_WALKING),
            ("executing", STATE_WRITING),          # create_report
            ("verifying", STATE_VERIFYING),
            ("completed", STATE_CELEBRATING),
        ]
        for task_state, char_state in expected:
            with self.subTest(task_state=task_state):
                self.assertEqual(self.feed(task_state), char_state)

    def test_failure_path_states(self):
        for task_state, char_state in (
            ("planning", STATE_THINKING),
            ("executing", STATE_WRITING),
            ("failed", STATE_ERROR),
        ):
            with self.subTest(task_state=task_state):
                self.assertEqual(self.feed(task_state), char_state)

    def test_needs_attention_is_concern_not_celebration(self):
        self.assertEqual(self.feed("needs_attention"), STATE_ERROR)
        self.assertNotIn(STATE_CELEBRATING, self.rendered_states())

    def test_cancelled_is_calm_idle(self):
        self.assertEqual(self.feed("cancelled"), STATE_IDLE)
        self.assertNotIn(STATE_CELEBRATING, self.rendered_states())
        self.assertNotIn(STATE_ERROR, self.rendered_states())

    def test_unknown_record_state_is_honest_idle(self):
        self.assertEqual(self.feed("some-future-state"), STATE_IDLE)
        self.assertNotIn(STATE_CELEBRATING, self.rendered_states())

    def test_only_completed_drives_celebrating_over_full_lifecycles(self):
        # Failure lifecycle: celebrating must never be rendered at all.
        for state in ("planning", "awaiting_approval", "approved",
                      "executing", "verifying", "failed"):
            self.feed(state)
        self.assertNotIn(STATE_CELEBRATING, self.rendered_states())
        self.assertEqual(self.controller.state, STATE_ERROR)

        # Success lifecycle: exactly one celebration, at the end.
        controller2 = CharacterController(NullRenderer())
        adapter2 = TaskEventAdapter(controller2)
        for state in ("planning", "approved", "executing", "verifying",
                      "completed"):
            adapter2.on_task_update(FakeRecord(state))
        renderer_states = [c[1] for c in controller2.renderer.calls
                           if c[0] == "state"]
        self.assertEqual(renderer_states.count(STATE_CELEBRATING), 1)
        self.assertEqual(renderer_states[-1], STATE_CELEBRATING)

    def test_verification_failure_never_completes_to_celebrating(self):
        # needs_attention after work: honest concern, no success animation.
        for state in ("planning", "executing", "verifying", "needs_attention"):
            self.feed(state)
        self.assertEqual(self.controller.state, STATE_ERROR)
        self.assertNotIn(STATE_CELEBRATING, self.rendered_states())

    def test_approval_state_is_reachable_from_any_reasonable_state(self):
        # An approval request attached mid-flow must always surface.
        for start in ("greeting", "thinking", "sleeping", "idle"):
            with self.subTest(start=start):
                controller = CharacterController(NullRenderer())
                adapter = TaskEventAdapter(controller)
                controller.force(start)
                adapter.on_task_update(FakeRecord("awaiting_approval"))
                self.assertEqual(controller.state,
                                 STATE_WAITING_FOR_APPROVAL)

    def test_approval_speech_points_to_the_task_window(self):
        self.feed("awaiting_approval")
        self.assertIn("task window", self.adapter.speech)
        self.assertTrue(self.spoken, "approval state produces speech")

    def test_executing_states_from_action_type(self):
        cases = [
            ("read_file", "doc.txt", STATE_READING),
            ("list_directory", "Documents", STATE_READING),
            ("inspect_application", "LibreOffice", STATE_READING),
            ("create_report", "report.odt", STATE_WRITING),
            ("save_document", "doc.odt", STATE_WRITING),
            ("enter_text", "hello", STATE_WRITING),
            ("create_directory", "Folder", STATE_WORKING),
            ("detect_applications", "", STATE_WORKING),
            ("open_file", "x.pdf", STATE_WORKING),
            ("create_text_file", "app.py", STATE_CODING),
            ("create_text_file", "script.SH", STATE_CODING),
            ("create_text_file", "notes.md", STATE_WRITING),
            ("create_text_file", "letter.txt", STATE_WRITING),
        ]
        for action, target, expected in cases:
            with self.subTest(action=action, target=target):
                self.assertEqual(
                    self.feed("executing", action=action, target=target),
                    expected,
                )

    def test_action_state_is_conservative_about_code(self):
        self.assertEqual(action_state("create_text_file", target="a.py"),
                         STATE_CODING)
        self.assertEqual(action_state("create_text_file", target="README"),
                         STATE_WRITING)
        self.assertEqual(action_state("create_text_file", target="a.txt"),
                         STATE_WRITING)
        # Unrecognised-but-validated future actions stay honest "working".
        self.assertEqual(action_state("future_validated_action"),
                         STATE_WORKING)

    def test_coding_suffixes_are_a_bounded_known_list(self):
        self.assertTrue(CODING_TEXT_SUFFIXES)
        self.assertLess(len(CODING_TEXT_SUFFIXES), 40)

    def test_sequence_reports_the_running_step_kind(self):
        steps = [
            {"action": "read_file", "summary": "done", "status": "done"},
            {"action": "create_text_file", "summary": "main.py",
             "status": "running"},
        ]
        self.assertEqual(
            self.feed("executing", action="task_sequence", steps=steps),
            STATE_CODING,
        )

    def test_sequence_without_running_step_is_plain_working(self):
        steps = [{"action": "read_file", "summary": "done", "status": "done"}]
        self.assertEqual(
            self.feed("executing", action="task_sequence", steps=steps),
            STATE_WORKING,
        )


class EventTests(AdapterHarness):
    def test_user_submit_listens(self):
        self.adapter.on_user_submit()
        self.assertEqual(self.controller.state, STATE_LISTENING)
        self.assertTrue(self.spoken)

    def test_planning_event_thinks(self):
        self.adapter.on_planning()
        self.assertEqual(self.controller.state, STATE_THINKING)

    def test_idle_user_rests_after_the_threshold(self):
        self.controller.force(STATE_IDLE)
        self.adapter.on_idle(self.adapter.rest_after_seconds + 1)
        self.assertEqual(self.controller.state, "resting")

    def test_idle_user_stays_when_quiet_enough(self):
        self.adapter.on_idle(10.0)
        self.assertEqual(self.controller.state, STATE_IDLE)

    def test_never_rests_mid_task(self):
        self.controller.force("working")
        self.adapter.on_idle(10_000.0)
        self.assertEqual(self.controller.state, STATE_WORKING)

    def test_user_activity_wakes_a_resting_character(self):
        self.controller.force("resting")
        self.adapter.on_user_activity()
        self.assertEqual(self.controller.state, STATE_LISTENING)

    def test_user_activity_does_not_disturb_task_states(self):
        self.controller.force("thinking")
        self.adapter.on_user_activity()
        self.assertEqual(self.controller.state, STATE_THINKING)


class SpeechTests(AdapterHarness):
    def test_every_curated_line_is_bounded(self):
        for state, line in STATE_LINES.items():
            with self.subTest(state=state):
                self.assertLessEqual(len(line), MAX_SPEECH_CHARS)

    def test_state_lines_only_use_known_states(self):
        self.assertLessEqual(set(STATE_LINES), set(ALL_STATES))

    def test_long_speech_is_bounded_before_display(self):
        self.adapter._emit("x" * 5000)
        self.assertLessEqual(len(self.adapter.speech), MAX_SPEECH_CHARS)

    def test_error_internals_are_never_spoken(self):
        self.feed("failed", error="secret-token-12345 at /home/user/.ssh",
                  result_message="SECRET_DOCUMENT_CONTENT")
        self.assertNotIn("secret-token-12345", self.adapter.speech)
        self.assertNotIn("SECRET_DOCUMENT_CONTENT", self.adapter.speech)
        for line in self.spoken:
            self.assertNotIn(".ssh", line)

    def test_file_content_is_never_spoken(self):
        self.feed("executing", summary="Create notes.md")
        for line in self.spoken:
            self.assertNotIn("\n", line)
            self.assertLessEqual(len(line), MAX_SPEECH_CHARS)

    def test_speech_callback_exception_does_not_propagate(self):
        def _boom(_text):
            raise RuntimeError("view bug")

        adapter = TaskEventAdapter(self.controller, on_speech=_boom)
        adapter.on_task_update(FakeRecord("planning"))   # must not raise
        self.assertEqual(self.controller.state, STATE_THINKING)


class RobustnessAndTrustTests(AdapterHarness):
    def test_adapter_never_raises_for_broken_records(self):
        class Broken:
            state = "executing"

            @property
            def plan(self):
                raise RuntimeError("corrupt record")

        for record in (Broken(), object(), None, 42, {"state": "failed"}):
            with self.subTest(record=type(record).__name__):
                self.adapter.on_task_update(record)   # must not raise
        self.assertIn(self.controller.state, ALL_STATES)

    def test_adapter_returns_nothing(self):
        # A return value could tempt a caller into using the companion
        # to gate a task; the adapter is a pure side-effect-free mapper.
        self.assertIsNone(self.adapter.on_task_update(FakeRecord("planning")))
        self.assertIsNone(self.adapter.on_user_submit())
        self.assertIsNone(self.adapter.on_idle(0.0))

    def test_adapter_exposes_no_approval_or_execution_surface(self):
        for name in ("approve", "reject", "approve_task", "execute", "run",
                     "grant", "permission", "cancel_task", "submit"):
            with self.subTest(name=name):
                self.assertFalse(hasattr(self.adapter, name))

    def test_adapter_holds_no_manager_reference(self):
        for attribute in ("manager", "pipeline", "task_manager", "app"):
            with self.subTest(attribute=attribute):
                self.assertFalse(hasattr(self.adapter, attribute))

    def test_adapter_is_pure_view_side_of_the_boundary(self):
        # It only ever drives the character controller.
        self.assertIs(self.adapter.controller, self.controller)

    def test_terminal_states_after_outcome_return_to_calm(self):
        self.feed("completed")
        self.assertEqual(self.controller.state, STATE_CELEBRATING)
        # The presentation layer returns it; the adapter must accept a
        # subsequent calm task without graph violations.
        self.controller.enter(STATE_IDLE)
        self.assertEqual(self.feed("planning"), STATE_THINKING)


if __name__ == "__main__":
    unittest.main()
