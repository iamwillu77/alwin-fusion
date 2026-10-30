"""Task manager: drives the trusted pipeline on behalf of a UI (v0.5).

The manager is the *only* thing a UI talks to.  It never executes
actions itself — every task runs through the existing
:class:`ai_core.pipeline.TaskPipeline` (preview -> approval ->
run_previewed), so the permission, approval, execution, verification
and audit stages are exactly the ones the CLI uses.  There is no
second execution engine and no UI-granted permission: the approval
the UI collects is handed back to the *pipeline*, which binds it to
the digest of the validated plan the user actually saw
(:class:`ai_core.pipeline.ApprovalToken`).

Threading model (kept deliberately simple):

* ``submit()`` records the request, moves the task to *planning* and
  starts **one worker thread** per task (only one task runs at a time),
* planning, LLM fallback, execution and verification all happen on
  that worker thread; the UI thread only reads records,
* when a plan needs approval, the worker blocks on an event the UI
  releases via :meth:`approve` / :meth:`reject` / :meth:`cancel`,
* the UI subscribes with ``on_update`` and is called **from the worker
  thread** — GTK code must marshal to its main loop (``GLib.idle_add``),
* cancellation is honoured only before execution starts; a running
  action is never interrupted and cancellation never claims rollback.

Everything that can be injected (pipeline factory, approval waits,
history, status probes) is injectable so the automated tests run the
real manager against sandboxes and fakes — no desktop, no display.
"""

from __future__ import annotations

import threading
import time
import uuid
from typing import Callable, Dict, List, Optional

from ai_core.config import Config
from ai_core.pipeline import (
    STATUS_DENIED,
    STATUS_DRY_RUN,
    STATUS_EXIT,
    STATUS_FAILED,
    STATUS_REJECTED,
    STATUS_SUCCESS,
    STATUS_UNSUPPORTED,
    ApprovalToken,
    TaskPipeline,
    TaskPreview,
    plan_digest,
)
from ai_core.planner import MUTATING_ACTIONS, describe_action
from ai_core.task_plan import (
    FAILED_STATES,
    MAX_DERIVED_CHARS,
    STEP_AWAITING_APPROVAL,
    STEP_CANCELLED,
    STEP_FAILED,
    STEP_PENDING,
    STEP_READY,
    STEP_RUNNING,
    STEP_SUCCEEDED,
    STEP_SKIPPED,
    STEP_VERIFICATION_FAILED,
    TaskPlan,
)
from ai_core.task_planning import PlanningError, TaskPlanningService
from taskui.history import (
    TaskHistory,
    summarize_verification,
)
from taskui.lifecycle import (
    STATE_APPROVED,
    STATE_AWAITING_APPROVAL,
    STATE_CANCELLED,
    STATE_COMPLETED,
    STATE_EXECUTING,
    STATE_FAILED,
    STATE_NEEDS_ATTENTION,
    STATE_PLANNING,
    STATE_VERIFYING,
    InvalidTransition,
    TaskRecord,
    utc_now,
)

#: v0.12 plan-execution bounds (configurable ceilings; never unbounded).
MAX_PLAN_SECONDS = 300.0
MAX_REPLANS = 1
#: Read-only steps may be retried ONCE (they have no side effects);
#: mutating steps are NEVER retried automatically (irreversibility rule).
MAX_READ_RETRIES = 1
#: Total steps across the initial plan plus bounded replans.
MAX_TOTAL_PLAN_STEPS = 24

#: Decision values released to a blocked approval gate.
APPROVE = "approve"
REJECT = "reject"
CANCEL = "cancel"

#: How long ``wait_*`` helpers poll by default (tests + UI progress).
WAIT_POLL = 0.02


class ManagerError(Exception):
    """Raised for invalid manager usage (e.g. double submission)."""


class TaskManager:
    """One task at a time; every task through the trusted pipeline."""

    def __init__(
        self,
        config: Config,
        *,
        history: Optional[TaskHistory] = None,
        llm_client=None,
        use_llm: bool = False,
        pipeline_factory: Optional[Callable[[TaskRecord], TaskPipeline]] = None,
        automation_tools=None,
        app_tools=None,
        desktop_tools=None,
        on_update: Optional[Callable[[TaskRecord], None]] = None,
        step_observer: Optional[Callable[..., None]] = None,
    ) -> None:
        self.config = config
        self.history = history or TaskHistory(config.state_dir / "task_history.jsonl")
        self.llm_client = llm_client
        self.use_llm = bool(use_llm and llm_client is not None)
        self.on_update = on_update
        self._external_step_observer = step_observer

        self._automation_tools = automation_tools
        self._app_tools = app_tools
        self._desktop_tools = desktop_tools
        self._pipeline_factory = pipeline_factory

        self._lock = threading.RLock()
        self._record: Optional[TaskRecord] = None
        self._worker: Optional[threading.Thread] = None
        self._approval_event = threading.Event()
        self._approval_decision: Optional[str] = None
        self._approval_digest_shown: str = ""
        #: Additional read-only subscribers (e.g. the v0.6 companion).
        #: ``on_update`` keeps its single-slot semantics; observers are
        #: additive, error-isolated, and receive the same TaskRecord —
        #: they cannot influence, approve or change any task outcome.
        self._observers: List[Callable[[TaskRecord], None]] = []

    # ------------------------------------------------------------------ #
    # Intake
    # ------------------------------------------------------------------ #

    @property
    def current(self) -> Optional[TaskRecord]:
        with self._lock:
            return self._record

    @property
    def busy(self) -> bool:
        record = self.current
        return record is not None and not record.is_terminal

    # ------------------------------------------------------------------ #
    # Read-only subscribers (v0.6 companion; cannot influence tasks)
    # ------------------------------------------------------------------ #

    def add_observer(self, callback: Callable[[TaskRecord], None]) -> None:
        """Subscribe to task updates without taking the ``on_update`` slot.

        Observers receive the same curated :class:`TaskRecord` from the
        worker thread (marshal to a UI loop before touching widgets).
        They are strictly read-only: the manager never reads a return
        value, never asks an observer for permission, and an observer
        exception is swallowed so it can never affect a task.
        """
        with self._lock:
            if callback is not None and callback not in self._observers:
                self._observers.append(callback)

    def remove_observer(self, callback: Callable[[TaskRecord], None]) -> None:
        with self._lock:
            if callback in self._observers:
                self._observers.remove(callback)

    def submit(self, command: str, *, dry_run: bool = False) -> TaskRecord:
        """Validate intake and start the task worker thread.

        Intake validation is deliberately narrow (the *real* validation
        is the parser/planner): empty or over-long requests are refused
        here so the UI never even asks the pipeline about them.
        """
        text = (command or "").strip()
        if not text:
            raise ManagerError("The request is empty — type what you want to do.")
        if len(text) > 2000:
            raise ManagerError(
                "The request is too long (2000 characters maximum)."
            )
        with self._lock:
            if self._record is not None and not self._record.is_terminal:
                raise ManagerError(
                    "A task is already running; wait for it to finish."
                )
            record = TaskRecord(
                task_id=self._new_task_id(),
                command=text,
                dry_run=bool(dry_run),
            )
            record.transition(STATE_PLANNING)
            self._record = record
            self._approval_event.clear()
            self._approval_decision = None
            self._approval_digest_shown = ""
            self._worker = threading.Thread(
                target=self._run_task,
                args=(record,),
                name=f"alwin-task-{record.task_id}",
                daemon=True,
            )
            self._worker.start()
        self._notify(record)
        return record

    @staticmethod
    def _new_task_id() -> str:
        return uuid.uuid4().hex[:12]

    # ------------------------------------------------------------------ #
    # Approval / rejection / cancellation (called from the UI thread)
    # ------------------------------------------------------------------ #

    def approve(self, digest: Optional[str] = None) -> bool:
        """Approve the *displayed* plan of the awaiting task.

        ``digest`` is what the UI displayed to the user; when it does
        not match the plan the worker is holding, the approval is
        refused — approval always applies to one exact validated plan
        (rule: a changed plan invalidates the approval).
        """
        with self._lock:
            record = self._record
            if record is None or record.state != STATE_AWAITING_APPROVAL:
                return False
            if digest is not None and digest != self._approval_digest_shown:
                return False
            record.approval = "approved"
            self._approval_decision = APPROVE
            self._approval_event.set()
        self._notify(record)
        return True

    def reject(self) -> bool:
        """Reject the awaiting plan; the task is cancelled, nothing runs."""
        with self._lock:
            record = self._record
            if record is None or record.state != STATE_AWAITING_APPROVAL:
                return False
            record.approval = "rejected"
            self._approval_decision = REJECT
            self._approval_event.set()
        self._notify(record)
        return True

    def cancel(self) -> bool:
        """Request cancellation — honoured only before execution starts.

        Returns False (with the honest reason on the record) once an
        action is executing or later: running actions are never
        interrupted, and no rollback is ever claimed.

        v0.12 exception (plans): for a record carrying a ``task_plan``
        the request becomes a *boundary flag* — the currently running
        step finishes (never interrupted), and the controller stops
        before the next step, reporting exactly what completed.  The
        approval gate still unblocks immediately.
        """
        with self._lock:
            record = self._record
            if record is None:
                return False
            if record.task_plan is not None and not record.is_terminal:
                record.cancel_requested = True
                if record.state == STATE_AWAITING_APPROVAL:
                    record.approval = "rejected"
                    self._approval_decision = CANCEL
                    self._approval_event.set()
                self._notify(record)
                return True
            try:
                record.request_cancel()
            except InvalidTransition as exc:
                record.notes.append(str(exc))
                self._notify(record)
                return False
            # Unblock a worker waiting at the approval gate.
            if record.state == STATE_AWAITING_APPROVAL:
                record.approval = "rejected"
                self._approval_decision = CANCEL
                self._approval_event.set()
        self._notify(record)
        return True

    # ------------------------------------------------------------------ #
    # Worker thread
    # ------------------------------------------------------------------ #

    def _build_pipeline(self, record: TaskRecord) -> TaskPipeline:
        if self._pipeline_factory is not None:
            return self._pipeline_factory(record)

        from ai_core.permissions import PermissionChecker
        from integrations import build_default_dispatcher

        checker = PermissionChecker(self.config)

        # Live per-step progress for sequences: the dispatcher's
        # sequence runner reports each step boundary here.
        def _step(index, total, action_name, status, error=None):
            self._on_step(record, index, total, action_name, status, error)
            if self._external_step_observer is not None:
                self._external_step_observer(
                    index, total, action_name, status, error=error
                )

        dispatcher = build_default_dispatcher(
            checker,
            app_tools=self._app_tools,
            automation_tools=self._automation_tools,
            desktop_tools=self._desktop_tools,
            step_observer=_step,
        )
        return TaskPipeline(
            self.config,
            checker=checker,
            dispatcher=dispatcher,
            use_llm=self.use_llm,
            llm_client=self.llm_client,
        )

    def _run_task(self, record: TaskRecord) -> None:
        try:
            pipeline = self._build_pipeline(record)
            preview = pipeline.preview(record.command, dry_run=record.dry_run)
            record._preview = preview

            # ---- Planning outcome ------------------------------------ #
            if preview.status == STATUS_EXIT:
                self._finish_cancelled(record, "Nothing to do (exit request).")
                return
            if not preview.planned:
                # v0.12: an unsupported single command may be a complex
                # objective the local model can structure into a plan.
                # Planning is attempted ONLY for the unsupported status
                # (never for denied/failed previews) and ONLY with the
                # attached local model; every outcome is honest.
                if preview.status == STATUS_UNSUPPORTED:
                    proposal_outcome = self._try_propose_plan(record)
                    if proposal_outcome == "handled":
                        return
                    if isinstance(proposal_outcome, TaskPlan):
                        self._run_plan(record, pipeline, proposal_outcome)
                        return
                report = pipeline.run_previewed(preview)  # audits the attempt
                self._finalize(record, report)
                return

            self._adopt_plan(record, preview)

            # ---- Cancellation before approval ------------------------ #
            if record.cancel_requested:
                self._finish_cancelled(
                    record, "Cancelled during planning; nothing was executed."
                )
                return

            # ---- Approval gate --------------------------------------- #
            if preview.requires_approval:
                record.approval = "pending"
                record.approval_digest = preview.plan_digest
                self._approval_digest_shown = preview.plan_digest
                record.transition(STATE_AWAITING_APPROVAL)
                self._notify(record)

                self._approval_event.wait()
                decision = self._approval_decision
                if decision != APPROVE:
                    report = pipeline.run_previewed(preview, approval=False)
                    if decision == CANCEL or record.cancel_requested:
                        self._finish_cancelled(
                            record,
                            "Cancelled at the approval step; nothing was "
                            "executed.",
                            report=report,
                        )
                    else:
                        self._finalize(record, report)
                    return

                record.transition(STATE_APPROVED)
                self._notify(record)
                if record.cancel_requested:
                    self._finish_cancelled(
                        record,
                        "Cancelled after approval but before execution; "
                        "nothing was executed.",
                    )
                    return
                approval = ApprovalToken(preview.plan_digest)
            else:
                approval = None

            # ---- Execution + verification ---------------------------- #
            record.transition(STATE_EXECUTING)
            self._notify(record)

            def _progress(stage: str) -> None:
                if stage == "verifying" and record.state == STATE_EXECUTING:
                    record.transition(STATE_VERIFYING)
                    self._notify(record)

            report = pipeline.run_previewed(
                preview, approval=approval, progress=_progress
            )
            self._finalize(record, report)
        except Exception as exc:  # noqa: BLE001 - the UI must see every error
            self._fail(record, f"Internal task error: {type(exc).__name__}: {exc}")

    # ------------------------------------------------------------------ #
    # v0.12 plan execution (same worker, same pipeline, same approvals)
    #
    # A plan is ONE user-approved unit (mirroring the task_sequence
    # precedent: the proposal enumerates every step and the user
    # approves once, bound to the plan digest).  Each step then runs
    # through the EXISTING preview_action -> run_previewed chain, so
    # every step is permission-checked and digest-verified exactly like
    # a typed command.  Execution is sequential (max concurrency 1):
    # the architecture has a single worker and non-thread-safe desktop
    # integrations, so concurrency is deliberately not claimed.
    # ------------------------------------------------------------------ #

    def _planning_service(self) -> Optional[TaskPlanningService]:
        """The local-model planner (None when --llm is not attached)."""
        if not self.use_llm or self.llm_client is None:
            return None
        try:
            return TaskPlanningService(self.llm_client)
        except Exception:  # noqa: BLE001 - planning must never break submit
            return None

    def _try_propose_plan(self, record: TaskRecord):
        """Ask the local model to structure an unsupported objective.

        Returns a :class:`TaskPlan`, the sentinel ``"handled"`` (the
        outcome was already reported on the record) or ``None`` (fall
        through to the existing unsupported path).
        """
        service = self._planning_service()
        if service is None:
            return None
        try:
            proposal = service.propose(record.command)
        except Exception as exc:  # noqa: BLE001 - degrade to unsupported
            record.notes.append(
                f"Planning error: {type(exc).__name__}: {exc}"[:300])
            return None

        if proposal.kind == "plan" and proposal.plan is not None:
            record._planning = service      # runtime-only (not history)
            return proposal.plan
        if proposal.kind == "clarify":
            self._fail(
                record,
                f"Clarification needed: {proposal.question} "
                "Nothing was executed.",
            )
            return "handled"
        if proposal.kind == "decline":
            self._finish_cancelled(
                record,
                f"{proposal.reason} Nothing was executed.",
            )
            return "handled"
        # unavailable: note it, then keep the existing honest path.
        if proposal.reason:
            record.notes.append(proposal.reason[:300])
        return None

    @staticmethod
    def _plan_step_entry(index: int, step) -> Dict[str, object]:
        return {
            "index": index,
            "id": step.id,
            "action": step.action.get("action", "?"),
            "summary": step.summary,
            "status": STEP_PENDING,
            "expected": step.expected,
            "error": "",
            "verification": None,
        }

    def _plan_step_state(self, record: TaskRecord, step, status: str,
                         *, error: str = "",
                         verification: Optional[dict] = None) -> None:
        """Update the curated steps view + result message (notify)."""
        with self._lock:
            step.state = status
            for entry in record.steps:
                if entry.get("id") == step.id:
                    entry["status"] = status
                    if error:
                        entry["error"] = str(error)[:400]
                    if verification is not None:
                        entry["verification"] = verification
            counts = record.task_plan.status_summary() \
                if record.task_plan is not None else {}
            record.result_message = (
                f"Plan «{record.task_plan.objective[:120]}» — "
                f"{counts.get(STEP_SUCCEEDED, 0)} succeeded, "
                f"{counts.get(STEP_FAILED, 0)} failed, "
                f"{counts.get(STEP_SKIPPED, 0)} skipped of "
                f"{len(record.task_plan.steps) if record.task_plan else 0}."
            )
        self._notify(record)

    @staticmethod
    def _curated_verification(report) -> Optional[Dict[str, object]]:
        """Evidence the UI/history may show — never raw file content."""
        raw = getattr(report, "verification", None)
        if not isinstance(raw, dict):
            return None
        checks = []
        for check in (raw.get("checks") or [])[:8]:
            if isinstance(check, dict):
                checks.append({
                    "name": str(check.get("name", ""))[:80],
                    "passed": bool(check.get("passed")),
                })
        return {
            "status": raw.get("status"),
            "message": str(raw.get("message", ""))[:300],
            "checks": checks,
        }

    @staticmethod
    def _step_result_data(report) -> Dict[str, Any]:
        execution = getattr(report, "execution", None)
        data = getattr(execution, "data", None)
        return dict(data) if isinstance(data, dict) else {}

    def _resolve_step_action(self, step, results: Dict[str, Any],
                             service: Optional[TaskPlanningService],
                             language: Optional[str] = None) -> dict:
        """Substitute derivations from verified earlier results.

        Mechanical, bounded and re-validated (preview_action validates
        again).  Raises for missing sources/fields or summarization
        failures — the caller marks the step failed honestly.
        """
        from ai_core.planner import validate_action

        def substitute(value):
            if isinstance(value, dict):
                return {k: substitute(v) for k, v in value.items()}
            if isinstance(value, list):
                return [substitute(v) for v in value]
            if isinstance(value, str) and value in step.derives:
                deriv = step.derives[value]
                source = results.get(deriv.from_step)
                if source is None:
                    raise PlanningError(
                        f"Depends on step '{deriv.from_step}', which did "
                        "not succeed."
                    )
                pulled = source.get("data", {}).get(deriv.field)
                if pulled is None:
                    raise PlanningError(
                        f"Step '{deriv.from_step}' produced no "
                        f"'{deriv.field}'."
                    )
                text = str(pulled)[:MAX_DERIVED_CHARS]
                if deriv.summarize:
                    if service is None:
                        raise PlanningError(
                            "Summarizing content needs the local model "
                            "(--llm)."
                        )
                    text = service.summarize_text(text, language=language)
                return text
            return value

        resolved = substitute(step.action)
        return validate_action(resolved)

    def _run_plan(self, record: TaskRecord, pipeline: TaskPipeline,
                  plan: TaskPlan) -> None:
        """Execute a validated plan step by step (worker thread)."""
        service = getattr(record, "_planning", None)
        record.task_plan = plan
        record._replans_left = MAX_REPLANS
        with self._lock:
            record.plan = {
                "action": "task_plan",
                "summary": plan.objective,
                "target": "",
                "mutating": plan.requires_approval,
                "requires_approval": plan.requires_approval,
                "digest": plan_digest(plan.to_dict()),
            }
            record.proposal = "Plan: " + "; ".join(
                f"{s.id}. {s.summary}" for s in plan.steps
            )[:900]
            record.steps = [self._plan_step_entry(i + 1, s)
                            for i, s in enumerate(plan.steps)]
        self._notify(record)

        try:
            self._run_plan_steps(record, pipeline, plan, service)
        except Exception as exc:  # noqa: BLE001 - the UI must see errors
            self._fail(record,
                       f"Plan execution error: {type(exc).__name__}: {exc}")

    def _run_plan_steps(self, record, pipeline, plan,
                        service) -> None:
        # ---- one plan-level approval (task_sequence precedent) --------- #
        if plan.requires_approval and not record.dry_run:
            digest = plan_digest(plan.to_dict())
            with self._lock:
                record.approval = "pending"
                record.approval_digest = digest
                self._approval_digest_shown = digest
            record.transition(STATE_AWAITING_APPROVAL)
            self._notify(record)
            self._approval_event.wait()
            decision = self._approval_decision
            if decision == APPROVE:
                record.transition(STATE_APPROVED)
                self._notify(record)
            else:
                reason = ("Cancelled at approval; nothing was executed."
                          if decision == CANCEL else
                          "Rejected before execution; nothing ran.")
                self._finish_cancelled(record, reason)
                return

        if record.cancel_requested:
            self._finish_cancelled(
                record, "Cancelled before execution; nothing ran.")
            return

        record.transition(STATE_EXECUTING)
        self._notify(record)

        results: Dict[str, Dict[str, Any]] = {}
        succeeded_digests: Dict[str, str] = {}   # mutating digests done
        failed_mutation_digests: set = set()
        succeeded_summaries: List[str] = []
        stop_reason: Optional[str] = None        # terminal-stop reasons
        failed_step = None
        zero_execution_failure = False
        deadline = time.monotonic() + MAX_PLAN_SECONDS
        replans_left = MAX_REPLANS
        recovered = False

        index = 0
        processed = 0
        while index < len(plan.steps):
            processed += 1
            if processed > MAX_TOTAL_PLAN_STEPS * 2:   # cycle safety
                stop_reason = stop_reason or "failed"
                break
            step = plan.steps[index]
            index += 1
            if step.state != STEP_PENDING:
                continue

            # ---- boundaries: cancel / time budget --------------------- #
            if record.cancel_requested:
                # index-1: the popped-but-unstarted step is remaining too.
                self._cancel_remaining(record, plan, index - 1,
                                       "The user cancelled the plan; no "
                                       "further step started.")
                stop_reason = "cancelled"
                break
            if time.monotonic() > deadline:
                self._cancel_remaining(record, plan, index - 1,
                                       "The plan exceeded its time budget; "
                                       "no further step started.")
                stop_reason = "timeout"
                break

            # ---- dependencies (incl. derivation sources) -------------- #
            blocked_by = None
            for dep in step.depends_on:
                parent = plan.step_by_id(dep)
                if parent is None or parent.state in FAILED_STATES:
                    blocked_by = dep
                    break
            if blocked_by is None:
                for deriv in step.derives.values():
                    parent = plan.step_by_id(deriv.from_step)
                    if parent is None or parent.state not in (
                            STEP_SUCCEEDED,):
                                blocked_by = deriv.from_step
                                break
            if blocked_by is not None:
                self._plan_step_state(
                    record, step, STEP_SKIPPED,
                    error=f"Depends on step '{blocked_by}', which did "
                          f"not succeed.")
                continue

            # ---- resolve derivations (bounded, re-validated) ---------- #
            try:
                action = self._resolve_step_action(
                    step, results, service,
                    language=getattr(record, "_plan_language", None),
                )
            except PlanningError as exc:
                self._plan_step_state(record, step, STEP_FAILED,
                                      error=str(exc))
                failed_step = step
                if step.mutating:
                    stop_reason = "failed"
                    self._skip_remaining(record, plan, index,
                                         "Not started after an earlier "
                                         "failure.")
                    break
                continue
            except Exception as exc:  # noqa: BLE001 - validation errors
                self._plan_step_state(
                    record, step, STEP_FAILED,
                    error=f"Step inputs are invalid: {exc}")
                failed_step = step
                if step.mutating:
                    stop_reason = "failed"
                    self._skip_remaining(record, plan, index,
                                         "Not started after an earlier "
                                         "failure.")
                    break
                continue

            # ---- preview (permission + proposal + digest) ------------- #
            try:
                preview = pipeline.preview_action(
                    action,
                    clean_command=f"{plan.objective} — step: {step.summary}",
                    dry_run=record.dry_run,
                )
            except Exception as exc:  # noqa: BLE001
                self._plan_step_state(record, step, STEP_FAILED,
                                      error=f"Preview failed: {exc}")
                failed_step = step
                # Preview never executed anything: stopping is safe
                # (and the one bounded replan may offer an alternative).
                stop_reason = "failed-preview"
                zero_execution_failure = True
                if self._try_replan(record, plan, service,
                                    succeeded_summaries, failed_step,
                                    succeeded_digests,
                                    failed_mutation_digests):
                    stop_reason = None
                    failed_step = None
                    recovered = True
                    continue
                self._skip_remaining(record, plan, index,
                                     "Not started after an earlier "
                                     "failure.")
                break

            if preview.status is not None:
                # denied / unsupported / failed at preview = ZERO
                # execution — stop conservatively, offering the one
                # bounded replan FIRST (dependent steps stay PENDING
                # until then so recovery can still use them).
                self._plan_step_state(
                    record, step, STEP_FAILED,
                    error=str(preview.error or preview.status))
                failed_step = step
                zero_execution_failure = True
                stop_reason = "failed-preview"
                if self._try_replan(record, plan, service,
                                    succeeded_summaries, failed_step,
                                    succeeded_digests,
                                    failed_mutation_digests):
                    stop_reason = None
                    failed_step = None
                    recovered = True
                    continue
                self._skip_remaining(record, plan, index,
                                     "Not started after an earlier "
                                     "failure.")
                break

            # ---- execute (existing chain; plan approval already bound) #
            self._plan_step_state(record, step, STEP_RUNNING)
            token = ApprovalToken(preview.plan_digest)
            attempts = 0
            while True:
                attempts += 1
                try:
                    report = pipeline.run_previewed(preview, approval=token)
                except Exception as exc:  # noqa: BLE001
                    report = None
                    self._plan_step_state(record, step, STEP_FAILED,
                                          error=f"Execution failed: {exc}")
                    failed_step = step
                    break
                status = report.status
                if status in (STATUS_SUCCESS, STATUS_DRY_RUN):
                    verification = self._curated_verification(report)
                    if (isinstance(verification, dict)
                            and verification.get("status") == "failed"):
                        self._plan_step_state(
                            record, step, STEP_VERIFICATION_FAILED,
                            error=str(verification.get("message")
                                     or "verification failed"),
                            verification=verification,
                        )
                    else:
                        results[step.id] = {
                            "data": self._step_result_data(report),
                            "digest": preview.plan_digest,
                        }
                        if step.mutating:
                            succeeded_digests[preview.plan_digest] = step.id
                        succeeded_summaries.append(step.summary)
                        self._plan_step_state(
                            record, step, STEP_SUCCEEDED,
                            verification=verification,
                        )
                    failed_step = None
                    break
                # Non-success: bounded retry ONLY for read-only steps
                # (no side effects possible); mutations never retry.
                if (not step.mutating
                        and attempts <= MAX_READ_RETRIES):
                    self._plan_step_state(
                        record, step, STEP_RUNNING,
                        error=f"Retrying after: "
                              f"{(report.error or status)[:200]}")
                    continue
                self._plan_step_state(record, step, STEP_FAILED,
                                      error=str(report.error or status))
                failed_step = step
                if step.mutating:
                    failed_mutation_digests.add(preview.plan_digest)
                break

            if failed_step is not None:
                state = failed_step.state
                if state == STEP_VERIFICATION_FAILED or failed_step.mutating:
                    stop_reason = stop_reason or "failed"
                    self._skip_remaining(record, plan, index,
                                         "Not started after an earlier "
                                         "failure.")
                    break
                # read-only failure: keep going (independent steps stay
                # safe; dependents were skipped above/below by gates)

            # natural loop end: index advances every iteration and the
            # while condition stops when steps are exhausted.

        self._finalize_plan(record, plan, stop_reason=stop_reason,
                            failed_step=failed_step,
                            succeeded=succeeded_summaries,
                            recovered=recovered)


    def _try_replan(self, record, plan, service, completed_summaries,
                    failed_step, succeeded_digests,
                    failed_mutation_digests) -> bool:
        """Phase-6 bounded replan after a ZERO-execution failure.

        Returns True when replacement steps were appended (the loop
        resumes). Rules: at most MAX_REPLANS per task; a mutating step
        that already succeeded OR failed is never re-proposed (trusted
        digest check, not a prompt promise); totals stay bounded.
        """
        budget = getattr(record, "_replans_left", MAX_REPLANS)
        if budget <= 0 or service is None or failed_step is None:
            return False
        # No "pending steps must remain" guard on purpose: the FAILED
        # step itself may be the whole remainder — the replan is what
        # proposes replacement work for it.
        proposal = service.replan_remaining(
            plan.objective, completed_summaries, failed_step.summary)
        if proposal.kind != "plan" or proposal.plan is None:
            return False
        appended = 0
        for new_step in proposal.plan.steps:
            if len(plan.steps) >= MAX_TOTAL_PLAN_STEPS:
                break
            if new_step.mutating:
                digest = plan_digest(new_step.action)
                if (digest in succeeded_digests
                        or digest in failed_mutation_digests):
                    continue          # never repeat executed work
            plan.steps.append(new_step)
            record.steps.append(self._plan_step_entry(
                len(record.steps) + 1, new_step))
            appended += 1
        if not appended:
            return False
        record._replans_left = budget - 1
        with self._lock:
            record.notes.append(
                f"Recovered with {appended} replacement step(s) after "
                "an earlier step failed before anything ran."[:300])
        self._notify(record)
        return True

    def _cancel_remaining(self, record, plan, index: int,
                          reason: str) -> None:
        for later in plan.steps[index:]:
            if later.state == STEP_PENDING:
                later.state = STEP_CANCELLED
                self._set_step_view_status(record, later, STEP_CANCELLED,
                                           error=reason)

    def _skip_remaining(self, record, plan, index: int,
                        reason: str) -> None:
        for later in plan.steps[index:]:
            if later.state == STEP_PENDING:
                later.state = STEP_SKIPPED
                self._set_step_view_status(record, later, STEP_SKIPPED,
                                           error=reason)

    def _set_step_view_status(self, record, step, status: str,
                              error: str = "") -> None:
        """Curated view update (no notify storm; final view notified)."""
        for entry in record.steps:
            if entry.get("id") == step.id:
                entry["status"] = status
                if error:
                    entry["error"] = str(error)[:400]

    def _finalize_plan(self, record: TaskRecord, plan: TaskPlan, *,
                       stop_reason: Optional[str],
                       failed_step,
                       succeeded: List[str],
                       recovered: bool = False) -> None:
        """Map step outcomes to the honest record lifecycle + history."""
        counts = plan.status_summary()
        total = len(plan.steps)
        done = counts.get(STEP_SUCCEEDED, 0)
        failed = counts.get(STEP_FAILED, 0) + counts.get(
            STEP_VERIFICATION_FAILED, 0)
        skipped = counts.get(STEP_SKIPPED, 0)
        cancelled = counts.get(STEP_CANCELLED, 0)

        parts = [f"{done}/{total} steps succeeded"]
        if stop_reason == "timeout":
            parts.append("the plan exceeded its time budget")
        elif stop_reason == "cancelled":
            parts.append("cancelled by the user")
        elif stop_reason == "rejected":
            parts.append("rejected before execution")
        if failed:
            parts.append(f"{failed} failed")
        if skipped:
            parts.append(f"{skipped} skipped")
        if cancelled:
            parts.append(f"{cancelled} cancelled")
        if failed_step is not None and failed_step.error:
            parts.append(f"first failure in '{failed_step.id}': "
                         f"{str(failed_step.error)[:160]}")
        summary = "; ".join(parts)

        # Verification summary: evidence per step, never raw content.
        checks = [
            {"name": f"{s.id}: {s.expected[:80] or s.summary[:80]}",
             "passed": s.state == STEP_SUCCEEDED}
            for s in plan.steps
        ]
        any_verify_failed = any(
            s.state == STEP_VERIFICATION_FAILED for s in plan.steps)
        mutation_applied = any(
            s.mutating and s.state == STEP_SUCCEEDED
            for s in plan.steps)
        mutation_failed = any(
            s.mutating and s.state in (STEP_FAILED,
                                       STEP_VERIFICATION_FAILED)
            for s in plan.steps)
        all_done = done == total

        with self._lock:
            record.result_message = summary[:500]
            record.verification = {
                "status": ("failed" if any_verify_failed
                           else ("ok" if all_done else "partial")),
                "message": ("All steps verified." if all_done
                            else summary)[:300],
                "checks": checks[:32],
            }

        try:
            if stop_reason == "cancelled":
                record.transition(STATE_CANCELLED)
            elif stop_reason == "rejected":
                record.transition(STATE_CANCELLED)
            elif all_done and not any_verify_failed:
                record.transition(STATE_VERIFYING)
                record.transition(STATE_COMPLETED)
            elif any_verify_failed or mutation_applied or (
                    mutation_failed and stop_reason != "failed-preview"
                ) or (stop_reason == "timeout" and mutation_applied):
                # World may have changed or a result is unverified:
                # the user must look — never "completed".  A
                # preview-stage failure never executed anything, so it
                # maps to a clean failure instead.
                record.transition(STATE_VERIFYING)
                record.transition(STATE_NEEDS_ATTENTION)
            elif recovered:
                # Replacement steps ran, but an earlier step failed
                # before executing: the world may match the goal — the
                # user must verify rather than see a false "completed".
                record.transition(STATE_VERIFYING)
                record.transition(STATE_NEEDS_ATTENTION)
            elif stop_reason == "timeout":
                record.transition(STATE_CANCELLED)
            else:
                record.transition(STATE_FAILED)
        except InvalidTransition:
            # Defensive: land honestly wherever the lifecycle allows.
            try:
                record.transition(STATE_FAILED)
            except InvalidTransition:
                pass

        self._write_history_best_effort(record)
        self._notify(record)

    # ------------------------------------------------------------------ #
    # Record curation (never raw payloads)
    # ------------------------------------------------------------------ #

    def _adopt_plan(self, record: TaskRecord, preview: TaskPreview) -> None:
        action = preview.action or {}
        name = action.get("action", "?")
        target = (
            action.get("path")
            or action.get("application")
            or action.get("integration")
            or ""
        )
        record.plan = {
            "action": name,
            "summary": preview.proposal or "",
            "target": str(target),
            "mutating": name in MUTATING_ACTIONS,
            "requires_approval": preview.requires_approval,
            "digest": preview.plan_digest,
        }
        record.proposal = preview.proposal
        # Steps for a planned sequence (display before execution).
        steps = action.get("steps")
        if isinstance(steps, list) and action.get("action") == "task_sequence":
            planned = []
            for index, step in enumerate(steps, start=1):
                if not isinstance(step, dict):
                    continue
                planned.append(
                    {
                        "index": index,
                        "action": step.get("action", "?"),
                        "summary": describe_action(step, home=self.config.home),
                        "status": "pending",
                    }
                )
            record.steps = planned

    def _on_step(
        self,
        record: TaskRecord,
        index: int,
        total: int,
        action_name: str,
        status: str,
        error=None,
    ) -> None:
        """Live per-step progress from the sequence runner (worker thread)."""
        with self._lock:
            for step in record.steps:
                if step.get("index") == index:
                    step["status"] = status
                    if error:
                        step["error"] = str(error)[:400]
            if status == "running":
                record.result_message = (
                    f"Running step {index} of {total}: {action_name}"
                )
        self._notify(record)

    @staticmethod
    def _any_step_changed(execution) -> bool:
        """True when a sequence applied at least one step before failing.

        ``ExecutionResult`` carries per-step records (each with the
        handler's own ``changed`` flag) for ``task_sequence`` actions;
        a partial application means part of the world moved, which the
        task must surface as needs_attention rather than a clean failure.
        """
        steps = execution.data.get("steps") if execution.data else None
        if not isinstance(steps, list):
            return False
        return any(
            isinstance(step, dict) and step.get("changed") for step in steps
        )

    def _finalize(self, record: TaskRecord, report) -> None:
        """Map one pipeline report onto the lifecycle — honestly."""
        record.report_status = report.status
        record.notes.extend(report.notes)
        record.verification = summarize_verification(report.verification)

        steps = (report.execution.data.get("steps") if report.execution else None)
        if isinstance(steps, list) and steps:
            from taskui.history import summarize_steps

            record.steps = summarize_steps(steps)

        message = report.execution.message if report.execution else None
        record.result_message = message or record.result_message

        if report.status == STATUS_SUCCESS:
            self._advance(record, STATE_VERIFYING)  # no-op when already there
            self._advance(record, STATE_COMPLETED)
        elif report.status == STATUS_DRY_RUN:
            record.notes.append("Dry run: nothing was changed.")
            self._advance(record, STATE_VERIFYING)
            self._advance(record, STATE_COMPLETED)
        elif report.status == STATUS_REJECTED:
            record.error = report.error
            self._finish_cancelled(record, report.error, report=report)
        elif report.status in (STATUS_DENIED, STATUS_UNSUPPORTED):
            record.error = report.error
            self._fail(record, report.error, report=report)
        elif report.status == STATUS_FAILED:
            record.error = report.error
            execution_ok = bool(report.execution and report.execution.ok)
            verification_failed = bool(
                report.verification and report.verification.get("status") == "failed"
            )
            # "partially changed" = a sequence that applied at least one
            # step (per-step records carry each handler's `changed`)
            # before a later step failed, so part of the world moved.
            partially_changed = bool(
                report.execution
                and not execution_ok
                and self._any_step_changed(report.execution)
            )
            if execution_ok and verification_failed:
                # The action ran, the world may have changed, but the
                # result could not be verified: never "completed".
                self._advance(record, STATE_VERIFYING)
                self._finish_needs_attention(record, report.error, report=report)
            elif partially_changed:
                # e.g. a sequence that applied some steps then failed:
                # part of the world changed; the user must look.
                self._finish_needs_attention(record, report.error, report=report)
            else:
                self._fail(record, report.error, report=report)
        else:  # pragma: no cover - defensive: unknown status fails honestly
            self._fail(record, f"Unexpected pipeline status: {report.status}")

        self._write_history_best_effort(record)
        self._notify(record)

    # ------------------------------------------------------------------ #
    # State helpers (all transitions validated by the lifecycle)
    # ------------------------------------------------------------------ #

    def _advance(self, record: TaskRecord, state: str) -> None:
        try:
            record.transition(state)
        except InvalidTransition:
            return  # already past this state (e.g. verifying happened live)
        self._notify(record)

    def _finish_cancelled(
        self, record: TaskRecord, reason: str, report=None
    ) -> None:
        record.error = reason
        if report is not None:
            record.report_status = report.status
        if not record.is_terminal:
            record.transition(STATE_CANCELLED)
        self._write_history_best_effort(record)
        self._notify(record)

    def _fail(self, record: TaskRecord, reason: str, report=None) -> None:
        record.error = reason or "The task failed."
        if report is not None:
            record.report_status = report.status
        if not record.is_terminal:
            record.transition(STATE_FAILED)
        self._write_history_best_effort(record)
        self._notify(record)

    def _finish_needs_attention(
        self, record: TaskRecord, reason: str, report=None
    ) -> None:
        record.error = reason or "The result could not be verified."
        if report is not None:
            record.report_status = report.status
        if not record.is_terminal:
            # verifying -> needs_attention is a legal transition; if we
            # never entered verifying (sequence partial failure), route
            # through it honestly via failed when the graph requires it.
            try:
                record.transition(STATE_VERIFYING)
            except InvalidTransition:
                pass
            record.transition(STATE_NEEDS_ATTENTION)
        self._write_history_best_effort(record)
        self._notify(record)

    def _write_history_best_effort(self, record: TaskRecord) -> None:
        # _finalize writes history once at the end; the early-exit
        # paths (cancel/fail before run_previewed) write here.
        if getattr(record, "_history_written", False):
            return
        try:
            self.history.record(record)
            record._history_written = True  # type: ignore[attr-defined]
        except Exception as exc:  # noqa: BLE001 - surfaced, never hidden
            record.notes.append(f"Task history write failed: {exc}")

    # ------------------------------------------------------------------ #
    # Synchronization helpers (UI progress + tests)
    # ------------------------------------------------------------------ #

    def _notify(self, record: TaskRecord) -> None:
        if self.on_update is not None:
            try:
                self.on_update(record)
            except Exception:  # noqa: BLE001 - a UI bug must not kill a task
                pass
        # Read-only subscribers (e.g. the companion): same isolation —
        # an observer exception is never allowed to affect a task.
        with self._lock:
            observers = list(self._observers)
        for observer in observers:
            try:
                observer(record)
            except Exception:  # noqa: BLE001 - same rule as on_update
                pass

    def wait_for_state(
        self, *states: str, timeout: float = 10.0, record: Optional[TaskRecord] = None
    ) -> bool:
        """Poll until the record reaches one of *states* (or terminal)."""
        import time

        target = record or self.current
        if target is None:
            return False
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if target.state in states:
                return True
            time.sleep(WAIT_POLL)
        return target.state in states

    def wait_terminal(self, timeout: float = 15.0) -> bool:
        """Wait for the current task to reach a terminal state."""
        target = self.current
        if target is None:
            return True
        worker = self._worker
        if worker is not None:
            worker.join(timeout=max(0.0, timeout))
        return target.is_terminal

    def history_rows(self, limit: int = 50) -> List[dict]:
        return self.history.summary_rows(limit=limit)

    def history_detail(self, task_id: str) -> Optional[dict]:
        return self.history.detail(task_id)
