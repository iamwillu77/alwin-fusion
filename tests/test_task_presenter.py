"""v0.5 presenter tests: the whole view-model, headless.

No GTK, no display, no threads — the presenter is pure, so every
interface behaviour (submission validation, plan badges, approval
affordances, step rendering, verification evidence, history rows,
status labels, untrusted-text bounding) is covered here.
"""

from __future__ import annotations

import unittest

from taskui import presenter
from taskui.lifecycle import TaskRecord
from taskui.status import (
    AIStatus,
    AutomationStatus,
    SystemStatus,
    STATUS_DEGRADED,
    STATUS_OK,
    STATUS_UNAVAILABLE,
)


def planned_record(**overrides) -> TaskRecord:
    rec = TaskRecord(task_id="abc123", command="Create a folder named X")
    rec.plan = {
        "action": "create_directory",
        "summary": "Create directory 'X' in Documents.",
        "target": "Documents/X",
        "mutating": True,
        "requires_approval": True,
        "digest": "0123456789abcdef",
    }
    rec.approval = "pending"
    # The manager sets this when the plan is shown for approval; the
    # presenter displays exactly the digest the approval binds to.
    rec.approval_digest = "0123456789abcdef"
    for key, value in overrides.items():
        setattr(rec, key, value)
    return rec


class SubmissionTests(unittest.TestCase):
    def test_empty_is_refused_with_guidance(self):
        error = presenter.validate_submission("   ", busy=False)
        self.assertIn("Type what you want", error)

    def test_none_is_refused(self):
        self.assertIsNotNone(presenter.validate_submission(None, busy=False))

    def test_too_long_is_refused(self):
        error = presenter.validate_submission("x" * 2001, busy=False)
        self.assertIn("too long", error)

    def test_busy_refuses_even_valid_input(self):
        error = presenter.validate_submission("list files", busy=True)
        self.assertIn("already running", error)

    def test_valid_input_passes(self):
        self.assertIsNone(presenter.validate_submission("list files",
                                                        busy=False))


class PlanViewTests(unittest.TestCase):
    def test_mutating_plan_is_badged_as_approval_required(self):
        rows = presenter.plan_rows(planned_record())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["badge"], presenter.BADGE_APPROVAL)
        self.assertIn("approval", rows[0]["badge_label"].lower())

    def test_read_only_plan_is_badged_read_only(self):
        rec = planned_record()
        rec.plan = dict(rec.plan, action="list_directory",
                        requires_approval=False, mutating=False)
        rows = presenter.plan_rows(rec)
        self.assertEqual(rows[0]["badge"], presenter.BADGE_READ_ONLY)

    def test_unverifiable_action_is_badged(self):
        rec = planned_record()
        rec.plan = dict(rec.plan, action="open_file", target="Documents/a.txt")
        rows = presenter.plan_rows(rec)
        self.assertEqual(rows[0]["badge"], presenter.BADGE_UNVERIFIABLE)

    def test_sequence_lists_every_step(self):
        rec = planned_record()
        rec.plan = dict(rec.plan, action="task_sequence")
        rec.steps = [
            {"index": 1, "action": "create_directory", "status": "pending"},
            {"index": 2, "action": "create_text_file", "status": "pending"},
            {"index": 3, "action": "open_file", "status": "pending"},
        ]
        rows = presenter.plan_rows(rec)
        self.assertEqual([r["index"] for r in rows], [1, 2, 3])
        # The unverifiable step inside the sequence keeps its badge.
        self.assertEqual(rows[2]["badge"], presenter.BADGE_UNVERIFIABLE)

    def test_no_plan_gives_no_rows(self):
        rec = TaskRecord(task_id="x", command="whatever")
        self.assertEqual(presenter.plan_rows(rec), [])

    def test_risk_notes_mention_approval_for_mutations(self):
        notes = " ".join(presenter.risk_notes(planned_record()))
        self.assertIn("approval", notes.lower())
        self.assertIn("invalidated", notes.lower())

    def test_dry_run_is_stated(self):
        notes = " ".join(presenter.risk_notes(planned_record(dry_run=True)))
        self.assertIn("nothing will be changed", notes)

    def test_unverifiable_risk_note_is_honest(self):
        rec = planned_record()
        rec.plan = dict(rec.plan, action="open_file")
        notes = " ".join(presenter.risk_notes(rec))
        self.assertIn("not proof", notes)


class TaskViewTests(unittest.TestCase):
    def test_absent_task(self):
        view = presenter.task_view(None)
        self.assertFalse(view["present"])
        self.assertTrue(view["can_submit"])

    def test_awaiting_approval_offers_controls_with_digest(self):
        rec = planned_record(state="awaiting_approval")
        view = presenter.task_view(rec)
        self.assertTrue(view["awaiting_approval"])
        self.assertTrue(view["can_approve"])
        self.assertTrue(view["can_reject"])
        self.assertEqual(view["approval_digest"], "0123456789abcdef")
        self.assertEqual(view["tone"], presenter.TONE_WARNING)

    def test_no_approval_controls_outside_awaiting(self):
        for state in ("planning", "executing", "completed", "failed"):
            rec = planned_record(state=state)
            view = presenter.task_view(rec)
            self.assertFalse(view["can_approve"], state)
            self.assertFalse(view["can_reject"], state)

    def test_completed_tone_is_positive(self):
        rec = planned_record(state="completed", report_status="success")
        view = presenter.task_view(rec)
        self.assertEqual(view["tone"], presenter.TONE_POSITIVE)

    def test_needs_attention_tone_is_warning(self):
        rec = planned_record(state="needs_attention")
        view = presenter.task_view(rec)
        self.assertEqual(view["tone"], presenter.TONE_WARNING)

    def test_steps_are_labeled_and_marked(self):
        rec = planned_record(state="executing")
        rec.steps = [
            {"index": 1, "action": "a", "status": "ok"},
            {"index": 2, "action": "b", "status": "running"},
            {"index": 3, "action": "c", "status": "failed", "error": "boom"},
        ]
        view = presenter.task_view(rec)
        self.assertEqual([s["mark"] for s in view["steps"]], ["✔", "▸", "✘"])
        self.assertEqual(view["steps"][2]["error"], "boom")

    def test_verification_evidence_rendered(self):
        rec = planned_record(state="completed")
        rec.verification = {
            "status": "passed",
            "message": "The directory exists.",
            "checks": [{"name": "exists", "passed": True, "detail": "Documents/X"}],
        }
        view = presenter.task_view(rec)
        self.assertEqual(view["verification"]["status"], "passed")
        self.assertEqual(view["verification"]["mark"], "✔")
        self.assertEqual(view["verification"]["checks"][0]["name"], "exists")

    def test_failed_verification_is_not_presented_as_verified(self):
        rec = planned_record(state="needs_attention")
        rec.verification = {"status": "failed", "message": "digest mismatch",
                            "checks": []}
        view = presenter.task_view(rec)
        self.assertEqual(view["verification"]["status"], "failed")
        self.assertNotEqual(view["verification"]["label"], "Verified")

    def test_untrusted_error_text_is_bounded(self):
        rec = planned_record(state="failed", error="E" * 100000)
        view = presenter.task_view(rec)
        self.assertLess(len(view["error"]), 1000)

    def test_untrusted_text_is_plain_not_markup(self):
        # The presenter only produces plain strings; the GTK view uses
        # set_text.  We assert the data itself carries no markup contract.
        rec = planned_record(state="failed",
                             error="<b>bold</b> <script>alert(1)</script>")
        view = presenter.task_view(rec)
        self.assertIn("<script>", view["error"])  # preserved as inert text

    def test_cancel_affordance_only_before_execution(self):
        self.assertTrue(presenter.task_view(
            planned_record(state="awaiting_approval"))["can_cancel"])
        self.assertFalse(presenter.task_view(
            planned_record(state="executing"))["can_cancel"])
        self.assertFalse(presenter.task_view(
            planned_record(state="completed"))["can_cancel"])


class StatusViewTests(unittest.TestCase):
    def test_none_status_is_neutral(self):
        view = presenter.status_view(None)
        self.assertEqual(view["ai_tone"], presenter.TONE_NEUTRAL)

    def test_ok_is_positive(self):
        status = SystemStatus(
            ai=AIStatus(status=STATUS_OK, detail="ready",
                        model_configured="llama3"),
            automation=AutomationStatus(status=STATUS_OK, detail="present"),
        )
        view = presenter.status_view(status)
        self.assertEqual(view["ai_tone"], presenter.TONE_POSITIVE)
        self.assertEqual(view["ai_label"], "Local AI ready")

    def test_unavailable_is_warning_with_detail(self):
        status = SystemStatus(
            ai=AIStatus(status=STATUS_UNAVAILABLE, detail="no ollama"),
            automation=AutomationStatus(status=STATUS_UNAVAILABLE,
                                        detail="no gi"),
        )
        view = presenter.status_view(status)
        self.assertEqual(view["ai_tone"], presenter.TONE_WARNING)
        self.assertEqual(view["ai_detail"], "no ollama")
        self.assertEqual(view["automation_label"],
                         "Desktop automation unavailable")

    def test_degraded_is_warning(self):
        status = SystemStatus(ai=AIStatus(status=STATUS_DEGRADED, detail="d"))
        self.assertEqual(presenter.status_view(status)["ai_tone"],
                         presenter.TONE_WARNING)


class SuggestionsTests(unittest.TestCase):
    def test_unsupported_request_offers_supported_alternatives(self):
        rec = planned_record(state="failed", report_status="unsupported")
        suggestions = presenter.suggestions_for(rec)
        self.assertTrue(suggestions)
        # Every suggestion must be an actually-supported command.
        from ai_core.command_parser import SUPPORTED_COMMAND_EXAMPLES

        for suggestion in suggestions:
            self.assertIn(suggestion, SUPPORTED_COMMAND_EXAMPLES)
        # Several distinct alternatives, not one repeated.
        self.assertGreaterEqual(len(suggestions), 4)

    def test_denied_request_also_gets_alternatives(self):
        rec = planned_record(state="failed", report_status="denied")
        self.assertTrue(presenter.suggestions_for(rec))

    def test_no_suggestions_for_other_outcomes(self):
        self.assertEqual(presenter.suggestions_for(None), [])
        self.assertEqual(presenter.suggestions_for(
            planned_record(state="completed", report_status="success")), [])


class HistoryViewTests(unittest.TestCase):
    def test_rows_are_bounded_and_toned(self):
        rows = presenter.history_rows([
            {"task_id": "t1", "finished_at": "2026-10-27",
             "request": "R" * 10000, "state": "completed",
             "state_label": "Completed", "action": "create_directory",
             "error": ""},
            "junk",
        ])
        self.assertEqual(len(rows), 1)
        self.assertLess(len(rows[0]["request"]), 1000)
        self.assertEqual(rows[0]["tone"], presenter.TONE_POSITIVE)

    def test_absent_detail(self):
        self.assertFalse(presenter.history_detail(None)["present"])
        self.assertFalse(presenter.history_detail("garbage")["present"])

    def test_detail_is_never_replayable(self):
        detail = presenter.history_detail({
            "task_id": "t1", "state": "completed", "state_label": "Completed",
            "created_at": "a", "finished_at": "b", "request": "req",
            "dry_run": False, "approval": "approved",
            "plan": {"action": "create_directory", "summary": "s"},
            "steps": [{"index": 1, "action": "a", "status": "ok"}],
            "result_message": "m", "error": "", "notes": [],
            "verification": {"status": "passed", "message": "",
                             "checks": [{"name": "n", "passed": True}]},
        })
        self.assertTrue(detail["present"])
        self.assertFalse(detail["replayable"])
        self.assertEqual(detail["verification_status"], "passed")
        self.assertEqual(len(detail["steps"]), 1)


if __name__ == "__main__":
    unittest.main()
