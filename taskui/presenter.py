"""Presenter: pure view-model for the task experience (v0.5).

This module turns task records, capability status and history entries
into small display-ready dictionaries.  It is deliberately **pure and
headless**: no GTK, no threads, no I/O, no side effects — which is why
the whole "interface" behaviour can be covered by automated tests
without a display or a desktop session.

Trust rules enforced here:

* everything rendered comes from curated fields of a
  :class:`~taskui.lifecycle.TaskRecord` (never raw model output or raw
  file content), and long strings are bounded before display,
* the plan shown is the planner's actual validated action — the
  presenter only annotates it (badges, risk notes), it never rewrites
  or relaxes it,
* approval affordances appear only in ``awaiting_approval`` state, and
  carry the exact plan digest the approval will be bound to,
* untrusted text (model responses, window titles, file content,
  history records) is truncated and never interpreted as markup: the
  GTK layer inserts it as plain text (``set_text``, never markup).
"""

from __future__ import annotations

from typing import Dict, List, Optional

from ai_core.command_parser import SUPPORTED_COMMAND_EXAMPLES
from taskui.lifecycle import (
    STATE_AWAITING_APPROVAL,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_EXECUTING,
    STATE_FAILED,
    STATE_NEEDS_ATTENTION,
    STATE_VERIFYING,
    TERMINAL_STATES,
    TaskRecord,
)
from taskui.status import SystemStatus

# Display bounds (the presenter never renders unbounded strings).
MAX_ROW_CHARS = 200
MAX_ERROR_CHARS = 400
MAX_NOTE_CHARS = 300
MAX_SUGGESTIONS = 6

# Badge kinds (kept as constants so views cannot invent new ones).
BADGE_READ_ONLY = "read_only"
BADGE_APPROVAL = "approval"
BADGE_BLOCKED = "blocked"
BADGE_UNSUPPORTED = "unsupported"
BADGE_UNVERIFIABLE = "unverifiable"

BADGE_LABELS = {
    BADGE_READ_ONLY: "Read-only — runs without approval",
    BADGE_APPROVAL: "Changes something — needs your approval",
    BADGE_BLOCKED: "Blocked by policy",
    BADGE_UNSUPPORTED: "Not supported",
    BADGE_UNVERIFIABLE: "Result cannot be fully verified",
}

#: Actions whose outcome the verifier can only partially confirm (a
#: launched process is not proof its interface appeared).  Shown
#: honestly as a limitation badge, never hidden.
UNVERIFIABLE_ACTIONS = frozenset(
    {"open_file", "activate_control", "enter_text", "save_document"}
)

# Step status presentation.
STEP_MARKS = {
    "pending": "○",
    "running": "▸",
    "ok": "✔",
    "dry_run": "◌",
    "failed": "✘",
    "denied": "✘",
    "skipped": "⇥",
    # v0.12 plan-step states
    "ready": "◌",
    "awaiting_approval": "◌",
    "succeeded": "✔",
    "cancelled": "⇥",
    "verification_failed": "◌",
}

STEP_LABELS = {
    "pending": "Pending",
    "running": "Running…",
    "ok": "Done",
    "dry_run": "Would run (dry run)",
    "failed": "Failed",
    "denied": "Denied",
    "skipped": "Skipped",
    # v0.12 plan-step states (honest distinctions)
    "ready": "Ready",
    "awaiting_approval": "Awaiting approval",
    "succeeded": "Succeeded",
    "cancelled": "Cancelled",
    "verification_failed": "Ran, not verified",
}

# Verification presentation.
VERIFY_MARKS = {"passed": "✔", "failed": "✘", None: "○"}
VERIFY_LABELS = {
    "passed": "Verified",
    "failed": "Verification failed",
    "skipped": "Verification skipped",
    None: "Not verified",
}

# Overall-task tone, used by views for colouring.
TONE_NEUTRAL = "neutral"
TONE_POSITIVE = "positive"
TONE_WARNING = "warning"
TONE_NEGATIVE = "negative"

STATE_TONES = {
    "draft": TONE_NEUTRAL,
    "planning": TONE_NEUTRAL,
    "awaiting_approval": TONE_WARNING,
    "approved": TONE_NEUTRAL,
    "executing": TONE_NEUTRAL,
    "verifying": TONE_NEUTRAL,
    "completed": TONE_POSITIVE,
    "failed": TONE_NEGATIVE,
    "cancelled": TONE_NEUTRAL,
    "needs_attention": TONE_WARNING,
}


def _bounded(value, limit: int) -> str:
    """Plain, bounded text — the only shape untrusted text may take."""
    if not isinstance(value, str):
        return ""
    return value if len(value) <= limit else value[: limit - 1] + "…"


# --------------------------------------------------------------------------- #
# Submission validation (shared by every view)
# --------------------------------------------------------------------------- #

MAX_REQUEST_CHARS = 2000


def validate_submission(text: Optional[str], *, busy: bool) -> Optional[str]:
    """Return an intake error, or ``None`` when the request may be sent.

    Mirrors the manager's own intake rules so the UI can explain the
    problem *before* anything starts; the manager re-validates anyway.
    """
    if busy:
        return (
            "A task is already running — wait for it to finish before "
            "sending another request."
        )
    clean = (text or "").strip()
    if not clean:
        return "Type what you want to do, then press Submit."
    if len(clean) > MAX_REQUEST_CHARS:
        return f"The request is too long ({MAX_REQUEST_CHARS} characters maximum)."
    return None


# --------------------------------------------------------------------------- #
# Plan presentation
# --------------------------------------------------------------------------- #


def _action_row(index: int, action_name: str, summary: str, target: str,
                requires_approval: bool) -> dict:
    mutating = requires_approval  # only mutating actions require approval
    if action_name in UNVERIFIABLE_ACTIONS:
        badge = BADGE_UNVERIFIABLE
    elif requires_approval:
        badge = BADGE_APPROVAL
    else:
        badge = BADGE_READ_ONLY
    return {
        "index": index,
        "action": action_name,
        "summary": _bounded(summary, MAX_ROW_CHARS),
        "target": _bounded(target, MAX_ROW_CHARS),
        "badge": badge,
        "badge_label": BADGE_LABELS[badge],
        "mutating": mutating,
    }


def plan_rows(record: TaskRecord) -> List[dict]:
    """The plan exactly as the planner produced it, annotated for display.

    For a ``task_sequence`` every validated step is listed (the user
    approves the whole sequence, bound to one digest).  Nothing here
    adds, removes or relaxes an action: rows are derived from the
    *validated* action stored on the record.
    """
    plan = record.plan
    if not plan:
        return []
    if plan.get("action") in ("task_sequence", "task_plan") \
            and record.steps:
        rows = []
        for step in record.steps:
            rows.append(
                _action_row(
                    step.get("index", 0) or (len(rows) + 1),
                    str(step.get("action", "?")),
                    str(step.get("summary", "")),
                    "",
                    bool(plan.get("requires_approval")),
                )
            )
        return rows
    return [
        _action_row(
            1,
            str(plan.get("action", "?")),
            str(plan.get("summary", "")),
            str(plan.get("target", "")),
            bool(plan.get("requires_approval")),
        )
    ]


def risk_notes(record: TaskRecord) -> List[str]:
    """Honest limitations relevant to this specific plan."""
    notes: List[str] = []
    plan = record.plan or {}
    rows = plan_rows(record)
    if any(r["action"] in UNVERIFIABLE_ACTIONS for r in rows):
        notes.append(
            "Launching or controlling an application is reported honestly: "
            "a started process is not proof that its window appeared, and "
            "Alwin Fusion verifies what it can (file digests, control "
            "read-back) rather than claiming more."
        )
    if plan.get("mutating"):
        notes.append(
            "This task changes something on disk or on the desktop. "
            "Nothing runs until you approve this exact plan; if the plan "
            "changes, the approval is invalidated."
        )
    if record.dry_run:
        notes.append("Dry run: nothing will be changed, even after approval.")
    if plan.get("action") in {"inspect_application", "find_control", "read_control"}:
        notes.append(
            "Application state can be unknown (for example when the "
            "desktop hides an application's interface); that is reported "
            "as-is instead of guessed."
        )
    desktop_actions = {
        "launch_application",
        "application_status",
        "active_window",
        "focus_application",
        "close_application",
    }
    if any(r["action"] in desktop_actions for r in rows):
        notes.append(
            "Desktop operations are verified by re-reading real state, "
            "never assumed: a launch is only \"done\" once /proc shows the "
            "process, a close only once the window leaves the accessibility "
            "bus. On a desktop that hides window state (for example some "
            "Wayland sessions), Alwin Fusion reports \"unavailable\" or "
            "\"unverified\" rather than claiming success."
        )
    if any(
        r["action"] in {"launch_application", "focus_application", "close_application"}
        for r in rows
    ):
        notes.append(
            "Launching, focusing or closing an application needs your "
            "approval. Closing never force-quits: an unsaved-work dialog "
            "keeps the window open and is reported, not overridden."
        )
    return notes


# --------------------------------------------------------------------------- #
# Whole-task view
# --------------------------------------------------------------------------- #


def task_view(record: Optional[TaskRecord], *, busy: bool = False) -> dict:
    """Everything one task panel needs to render (pure snapshot)."""
    if record is None:
        return {
            "present": False,
            "can_submit": not busy,
            "submit_error": None,
        }

    state = record.state
    steps = []
    for step in record.steps:
        status = str(step.get("status", "pending"))
        steps.append(
            {
                "index": step.get("index"),
                "action": _bounded(str(step.get("action", "?")), 60),
                "summary": _bounded(str(step.get("summary", "")), MAX_ROW_CHARS),
                "status": status,
                "mark": STEP_MARKS.get(status, "?"),
                "label": STEP_LABELS.get(status, status),
                "error": _bounded(str(step.get("error") or ""), MAX_ERROR_CHARS),
            }
        )

    verification = None
    if isinstance(record.verification, dict):
        vstatus = record.verification.get("status")
        if vstatus not in VERIFY_LABELS:
            vstatus = None
        checks = []
        raw_checks = record.verification.get("checks")
        if isinstance(raw_checks, list):
            for check in raw_checks[:32]:
                if not isinstance(check, dict):
                    continue
                passed = bool(check.get("passed"))
                checks.append(
                    {
                        "name": _bounded(str(check.get("name", "")), 80),
                        "passed": passed,
                        "mark": "✔" if passed else "✘",
                        "detail": _bounded(str(check.get("detail", "")), 160),
                    }
                )
        verification = {
            "status": vstatus,
            "mark": VERIFY_MARKS.get(vstatus, "○"),
            "label": VERIFY_LABELS.get(vstatus, "Not verified"),
            "message": _bounded(str(record.verification.get("message", "")),
                                MAX_ERROR_CHARS),
            "checks": checks,
        }

    awaiting = state == STATE_AWAITING_APPROVAL
    cancellable = (
        state not in TERMINAL_STATES
        and state not in (STATE_EXECUTING, STATE_VERIFYING)
    )

    return {
        "present": True,
        "task_id": record.task_id,
        "request": _bounded(record.command, MAX_REQUEST_CHARS),
        "dry_run": record.dry_run,
        "state": state,
        "state_label": record.state_label,
        "tone": STATE_TONES.get(state, TONE_NEUTRAL),
        "is_terminal": record.is_terminal,
        "plan_rows": plan_rows(record),
        "risk_notes": risk_notes(record),
        "approval": record.approval,
        "approval_digest": record.approval_digest,
        "awaiting_approval": awaiting,
        "can_approve": awaiting,
        "can_reject": awaiting,
        "can_cancel": cancellable and not record.cancel_requested,
        "cancel_requested": record.cancel_requested,
        "steps": steps,
        "result_message": _bounded(str(record.result_message or ""),
                                   MAX_ERROR_CHARS),
        "verification": verification,
        "error": _bounded(str(record.error or ""), MAX_ERROR_CHARS),
        "notes": [_bounded(str(n), MAX_NOTE_CHARS) for n in record.notes[:8]],
        "can_submit": not busy,
        "submit_error": None if not busy else validate_submission("x", busy=True),
    }


# --------------------------------------------------------------------------- #
# Status presentation
# --------------------------------------------------------------------------- #


def status_view(status: Optional[SystemStatus]) -> dict:
    """Honest capability summary for the header/status area."""
    if status is None:
        return {
            "ai_label": "Status not checked",
            "ai_detail": "",
            "ai_tone": TONE_NEUTRAL,
            "automation_label": "",
            "offline_note": "",
        }
    ai_tone = {
        "ok": TONE_POSITIVE,
        "degraded": TONE_WARNING,
        "unavailable": TONE_WARNING,
        "unknown": TONE_NEUTRAL,
    }.get(status.ai.status, TONE_NEUTRAL)
    return {
        "ai_label": status.ai.label,
        "ai_detail": _bounded(status.ai.detail, MAX_ERROR_CHARS),
        "ai_tone": ai_tone,
        "automation_label": status.automation.label,
        "automation_detail": _bounded(status.automation.detail, MAX_ERROR_CHARS),
        "offline_note": status.offline_detail,
        "ai_model": status.ai.model_configured,
    }


def suggestions_for(record: Optional[TaskRecord]) -> List[str]:
    """Safe supported alternatives when a request could not be planned.

    Shown for unsupported/denied requests so the interface stays useful
    honestly, instead of pretending to do something it cannot.
    """
    if record is None or record.report_status not in ("unsupported", "denied"):
        return []
    return list(SUPPORTED_COMMAND_EXAMPLES[:MAX_SUGGESTIONS])


# --------------------------------------------------------------------------- #
# History presentation
# --------------------------------------------------------------------------- #


def history_rows(rows: List[dict]) -> List[dict]:
    """Compact, bounded rows for the history list."""
    out = []
    for row in rows[:64]:
        if not isinstance(row, dict):
            continue
        state = str(row.get("state", ""))
        out.append(
            {
                "task_id": _bounded(str(row.get("task_id", "")), 64),
                "finished_at": _bounded(str(row.get("finished_at", "")), 40),
                "request": _bounded(str(row.get("request", "")), MAX_ROW_CHARS),
                "state": state,
                "state_label": _bounded(str(row.get("state_label", state)), 40),
                "tone": STATE_TONES.get(state, TONE_NEUTRAL),
                "action": _bounded(str(row.get("action", "")), 60),
                "error": _bounded(str(row.get("error") or ""), MAX_ERROR_CHARS),
            }
        )
    return out


def history_detail(detail: Optional[dict]) -> dict:
    """Full inspection view of one history record (display only).

    History is never re-runnable: this view exposes no action beyond
    reading, and the manager has no path that executes a record.
    """
    if not isinstance(detail, dict):
        return {"present": False}
    steps = []
    for step in (detail.get("steps") or [])[:32]:
        if not isinstance(step, dict):
            continue
        status = str(step.get("status", ""))
        steps.append(
            {
                "index": step.get("index"),
                "action": _bounded(str(step.get("action", "")), 60),
                "status": status,
                "mark": STEP_MARKS.get(status, "?"),
                "label": STEP_LABELS.get(status, status),
                "error": _bounded(str(step.get("error") or ""), MAX_ERROR_CHARS),
            }
        )
    verification = detail.get("verification")
    vstatus = None
    vmessage = ""
    vchecks: List[dict] = []
    if isinstance(verification, dict):
        vstatus = verification.get("status")
        if vstatus not in VERIFY_LABELS:
            vstatus = None
        vmessage = _bounded(str(verification.get("message", "")), MAX_ERROR_CHARS)
        for check in (verification.get("checks") or [])[:32]:
            if not isinstance(check, dict):
                continue
            passed = bool(check.get("passed"))
            vchecks.append(
                {
                    "name": _bounded(str(check.get("name", "")), 80),
                    "passed": passed,
                    "mark": "✔" if passed else "✘",
                    "detail": _bounded(str(check.get("detail", "")), 160),
                }
            )
    plan = detail.get("plan") if isinstance(detail.get("plan"), dict) else {}
    state = str(detail.get("state", ""))
    return {
        "present": True,
        "task_id": _bounded(str(detail.get("task_id", "")), 64),
        "created_at": _bounded(str(detail.get("created_at", "")), 40),
        "finished_at": _bounded(str(detail.get("finished_at", "")), 40),
        "request": _bounded(str(detail.get("request", "")), MAX_REQUEST_CHARS),
        "state": state,
        "state_label": _bounded(str(detail.get("state_label", state)), 40),
        "tone": STATE_TONES.get(state, TONE_NEUTRAL),
        "dry_run": bool(detail.get("dry_run")),
        "plan_action": _bounded(str(plan.get("action", "")), 60),
        "plan_summary": _bounded(str(plan.get("summary", "")), MAX_ROW_CHARS),
        "approval": _bounded(str(detail.get("approval", "")), 20),
        "steps": steps,
        "result_message": _bounded(str(detail.get("result_message", "")),
                                   MAX_ERROR_CHARS),
        "verification_status": vstatus,
        "verification_label": VERIFY_LABELS.get(vstatus, "Not verified"),
        "verification_mark": VERIFY_MARKS.get(vstatus, "○"),
        "verification_message": vmessage,
        "verification_checks": vchecks,
        "error": _bounded(str(detail.get("error") or ""), MAX_ERROR_CHARS),
        "notes": [
            _bounded(str(n), MAX_NOTE_CHARS)
            for n in (detail.get("notes") or [])[:8]
        ],
        "replayable": False,  # history is display-only, always
    }


__all__ = [
    "BADGE_APPROVAL",
    "BADGE_BLOCKED",
    "BADGE_LABELS",
    "BADGE_READ_ONLY",
    "BADGE_UNVERIFIABLE",
    "BADGE_UNSUPPORTED",
    "MAX_REQUEST_CHARS",
    "STATE_TONES",
    "STEP_LABELS",
    "STEP_MARKS",
    "TONE_NEGATIVE",
    "TONE_NEUTRAL",
    "TONE_POSITIVE",
    "TONE_WARNING",
    "VERIFY_LABELS",
    "history_detail",
    "history_rows",
    "plan_rows",
    "risk_notes",
    "status_view",
    "suggestions_for",
    "task_view",
    "validate_submission",
]
