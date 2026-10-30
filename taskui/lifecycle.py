"""Task lifecycle: states, validated transitions, one record per task.

The task experience (v0.5) sits *on top of* the trusted core: it never
executes anything itself.  A task record tracks a request through the
existing pipeline stages:

    draft -> planning -> [awaiting_approval -> approved] -> executing
         -> verifying -> completed | failed | needs_attention | cancelled

Every transition goes through :meth:`TaskRecord.transition`, which
validates it against the transition table — an impossible jump (e.g.
``planning -> completed`` or executing something that was never
planned) raises instead of being silently accepted.

Honesty rules encoded here:

* a task can only be *approved* while its validated plan exists
  (``awaiting_approval -> approved``), and approval is bound to that
  exact plan's digest by the pipeline (see ``ai_core.pipeline.ApprovalToken``),
* *completed* is only reachable from *verifying*, i.e. only after the
  independent verification stage ran — dispatching an action never
  completes a task by itself,
* *needs_attention* is the honest state for "the world changed but the
  result could not be fully verified" (failed verification after a
  successful execution, or a partially applied sequence),
* *cancelled* is only reachable before execution starts; once an
  action is running it is never interrupted, and cancellation never
  claims rollback (nothing is undone — nothing had run yet).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

# --------------------------------------------------------------------------- #
# States
# --------------------------------------------------------------------------- #

STATE_DRAFT = "draft"
STATE_PLANNING = "planning"
STATE_AWAITING_APPROVAL = "awaiting_approval"
STATE_APPROVED = "approved"
STATE_EXECUTING = "executing"
STATE_VERIFYING = "verifying"
STATE_COMPLETED = "completed"
STATE_FAILED = "failed"
STATE_CANCELLED = "cancelled"
STATE_NEEDS_ATTENTION = "needs_attention"

ALL_STATES = (
    STATE_DRAFT,
    STATE_PLANNING,
    STATE_AWAITING_APPROVAL,
    STATE_APPROVED,
    STATE_EXECUTING,
    STATE_VERIFYING,
    STATE_COMPLETED,
    STATE_FAILED,
    STATE_CANCELLED,
    STATE_NEEDS_ATTENTION,
)

TERMINAL_STATES = frozenset(
    {STATE_COMPLETED, STATE_FAILED, STATE_CANCELLED, STATE_NEEDS_ATTENTION}
)

#: The only legal state graph.  Anything not listed is invalid.
TRANSITIONS: Dict[str, frozenset] = {
    STATE_DRAFT: frozenset({STATE_PLANNING, STATE_CANCELLED}),
    STATE_PLANNING: frozenset(
        {
            STATE_AWAITING_APPROVAL,  # plan needs approval
            STATE_EXECUTING,          # read-only / dry run: nothing to approve
            STATE_CANCELLED,          # user cancelled before anything ran
            STATE_FAILED,             # unsupported / denied at plan time
        }
    ),
    STATE_AWAITING_APPROVAL: frozenset(
        {STATE_APPROVED, STATE_CANCELLED}
    ),
    # Cancel between approval and the actual start of execution: the
    # worker checks the cancel flag right before dispatching.
    STATE_APPROVED: frozenset({STATE_EXECUTING, STATE_CANCELLED}),
    # v0.12: EXECUTING -> CANCELLED exists ONLY for plan-boundary
    # cancellation (the controller checks the cancel flag BETWEEN
    # steps, when nothing is running).  The single-action guard in
    # TaskRecord.request_cancel still refuses cancellation while an
    # action executes, so this edge never interrupts a running action.
    STATE_EXECUTING: frozenset(
        {STATE_VERIFYING, STATE_FAILED, STATE_CANCELLED}
    ),
    # From verifying there is no path back to executing: verification
    # never re-runs an action, it only judges the recorded result.
    # v0.12 adds VERIFYING -> CANCELLED for the same plan-boundary
    # reason (a plan may be cancelled while its outcome is being
    # summarized between steps).
    STATE_VERIFYING: frozenset(
        {STATE_COMPLETED, STATE_FAILED, STATE_NEEDS_ATTENTION,
         STATE_CANCELLED}
    ),
    STATE_COMPLETED: frozenset(),
    STATE_FAILED: frozenset(),
    STATE_CANCELLED: frozenset(),
    STATE_NEEDS_ATTENTION: frozenset(),
}

STATE_LABELS: Dict[str, str] = {
    STATE_DRAFT: "Draft",
    STATE_PLANNING: "Planning…",
    STATE_AWAITING_APPROVAL: "Waiting for your approval",
    STATE_APPROVED: "Approved",
    STATE_EXECUTING: "Executing…",
    STATE_VERIFYING: "Verifying…",
    STATE_COMPLETED: "Completed",
    STATE_FAILED: "Failed",
    STATE_CANCELLED: "Cancelled",
    STATE_NEEDS_ATTENTION: "Needs attention",
}


class InvalidTransition(Exception):
    """A state change that the lifecycle does not allow."""


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class TaskRecord:
    """One user task, from submission to its honest final state.

    ``plan`` holds a *curated, display-safe* summary of the validated
    action (never raw file content); ``_preview`` keeps the live
    :class:`ai_core.pipeline.TaskPreview` for the worker thread only
    and is never serialized to history.
    """

    task_id: str
    command: str
    dry_run: bool = False
    state: str = STATE_DRAFT
    created_at: str = field(default_factory=utc_now)
    updated_at: str = field(default_factory=utc_now)
    timestamps: Dict[str, str] = field(default_factory=dict)

    # Plan (curated summary; set once planning succeeded)
    plan: Optional[dict] = None
    proposal: Optional[str] = None

    # Approval
    approval: str = "not_required"  # not_required|pending|approved|rejected|invalidated|dry_run
    approval_digest: str = ""

    # Progress / result (curated)
    steps: List[dict] = field(default_factory=list)
    result_message: Optional[str] = None
    verification: Optional[dict] = None  # {status, message, checks:[{name,passed}]}
    report_status: Optional[str] = None  # raw pipeline status (audit/debug)

    # v0.12: the validated multi-step plan driving this record (worker-
    # only runtime state; deliberately NOT serialized to history —
    # history keeps only the curated fields below).
    task_plan: Any = None
    error: Optional[str] = None
    notes: List[str] = field(default_factory=list)

    cancel_requested: bool = False

    # Live pipeline objects (worker thread only; never serialized)
    _preview: object = field(default=None, repr=False, compare=False)

    # ------------------------------------------------------------------ #

    @property
    def state_label(self) -> str:
        return STATE_LABELS.get(self.state, self.state)

    @property
    def is_terminal(self) -> bool:
        return self.state in TERMINAL_STATES

    def transition(self, new_state: str, *, at: Optional[str] = None) -> None:
        """Move to *new_state*, validating the lifecycle graph."""
        if new_state not in ALL_STATES:
            raise InvalidTransition(f"Unknown task state: {new_state!r}")
        allowed = TRANSITIONS[self.state]
        if new_state not in allowed:
            raise InvalidTransition(
                f"Invalid task transition {self.state!r} -> {new_state!r} "
                f"(allowed: {sorted(allowed) or 'none — terminal state'})."
            )
        self.state = new_state
        self.updated_at = at or utc_now()
        self.timestamps[new_state] = self.updated_at

    def request_cancel(self) -> None:
        """Flag a cancellation request (honoured only before execution)."""
        if self.is_terminal:
            raise InvalidTransition(
                f"Cannot cancel a task that already finished ({self.state})."
            )
        if self.state in (STATE_EXECUTING, STATE_VERIFYING):
            raise InvalidTransition(
                "Cannot cancel: the action is already executing; Alwin "
                "Fusion does not interrupt running actions and never "
                "claims to roll them back."
            )
        self.cancel_requested = True
