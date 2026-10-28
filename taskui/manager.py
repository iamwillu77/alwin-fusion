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
)
from ai_core.planner import MUTATING_ACTIONS, describe_action
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
        self._pipeline_factory = pipeline_factory

        self._lock = threading.RLock()
        self._record: Optional[TaskRecord] = None
        self._worker: Optional[threading.Thread] = None
        self._approval_event = threading.Event()
        self._approval_decision: Optional[str] = None
        self._approval_digest_shown: str = ""

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
        """
        with self._lock:
            record = self._record
            if record is None:
                return False
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
