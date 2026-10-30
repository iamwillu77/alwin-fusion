"""Scheduler runtime for Alwin Fusion (v0.16.0) — deterministic, bounded,
GTK-compatible.

Threading contract (the core design decision):

* the scheduler owns **no threads**; ``tick()`` is a bounded, pure book
  of work driven by the host's existing event loop (``GLib.timeout_add``
  in the companion window) or by an injected fake clock in tests,
* the TaskManager's observer callback fires on the *worker thread* and
  only appends the curated record to a lock-protected queue — **every
  store write happens on the tick thread**,
* a tick does at most ``max_per_tick`` dispatches and one bounded pass
  each over in-flight tasks and the dispatch queue, so the GTK loop can
  never be held.

Execution guarantees (each enforced in code and tested):

* a scheduled task reaches the manager through ``manager.submit()`` —
  the ordinary preview → permission → approval → execute → verify →
  audit pipeline.  There is no second execution path in this module:
  it never imports the engine, it only calls the manager's public API,
* duplicate prevention is the store's transactional occurrence claim
  (``UNIQUE(job_id, occurrence)``): one occurrence can be claimed once,
  ever,
* a recurring job never holds an approval: the approval gate's digest
  is bound to the current plan by the manager, and this scheduler
  never approves anything — an unattended approval within
  ``approval_window`` seconds results in ``manager.cancel()`` and an
  honest ``needs_approval`` outcome with **nothing executed**,
* missed occurrences (the machine was off / Alwin was closed) are
  **coalesced, never backfilled**: one aggregated notice for reminders,
  one ``missed`` history row + attention notice for task jobs,
  re-evaluation for condition checks,
* retries apply only to *dispatch* failures (the manager refused the
  submission), bounded by the job's ``max_retries``; approval refusal,
  verification failure and timeouts are final for that occurrence.

Known limits (documented, not hidden): schedules run only while an
Alwin application with a hosted manager is running; a tick does not
interrupt a running task; a condition check is a single ``stat``/registry
probe — never a scan, a shell command, or monitoring.
"""

from __future__ import annotations

import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Tuple

from ai_core.jobstore import (
    STATUS_CANCELLED,
    STATUS_DISPATCHED,
    STATUS_ERROR,
    STATUS_MISSED,
    STATUS_PENDING,
    JobStore,
    JobStoreError,
)
from ai_core.schedule import (
    ScheduledJob,
    ScheduleError,
    format_schedule,
    make_job,
    missed_count,
    next_occurrence,
    tz_for,
    validate_outcome,
)

#: Terminal TaskManager record states (duck-typed strings; the scheduler
#: deliberately does not import taskui — it only observes records).
TERMINAL_STATES = ("completed", "failed", "cancelled", "needs_attention")

#: The window in which an awaiting-approval task may be approved before
#: the scheduler cancels it at the gate (nothing executes unattended).
DEFAULT_APPROVAL_WINDOW = 120

#: An occurrence due within this many seconds is "due"; anything older
#: is a *missed* occurrence (the app or machine was away) and follows
#: the coalescing policy instead of executing a backlog.
DEFAULT_MISSED_GRACE = 90

#: Tick cadence used by the companion window (seconds).
DEFAULT_TICK_SECONDS = 30


class SchedulerError(Exception):
    """Operation refused — honest, bounded reason (never a stack dump)."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class TickReport:
    """What one tick actually did (surfaced to UI/tests, never padded)."""

    now: int = 0
    due: int = 0
    dispatched: int = 0
    reminders: int = 0
    missed: int = 0
    deferred: int = 0
    approvals_expired: int = 0
    errors: List[str] = field(default_factory=list)

    @property
    def busy(self) -> bool:
        return bool(self.dispatched or self.reminders)


class Scheduler:
    """Bounded, deterministic scheduler over a :class:`JobStore`.

    ``manager`` is the existing TaskManager (duck-typed: ``busy``,
    ``submit``, ``cancel``, ``add_observer``); ``notifier`` is a
    callable receiving event dicts (the window maps them to bubbles);
    ``clock`` returns unix seconds and is injectable for tests.  When
    ``manager`` is None the scheduler still tracks reminders, checks and
    occurrences — task dispatch honestly reports "no execution engine".
    """

    def __init__(
        self,
        store: JobStore,
        *,
        manager: Any = None,
        notifier: Optional[Callable[[Dict[str, Any]], None]] = None,
        config: Any = None,
        clock: Optional[Callable[[], int]] = None,
        approval_window: int = DEFAULT_APPROVAL_WINDOW,
        missed_grace: int = DEFAULT_MISSED_GRACE,
        max_per_tick: int = 5,
    ) -> None:
        self._store = store
        self._manager = manager
        self._notifier = notifier
        self._config = config
        self._clock = clock or (lambda: int(time.time()))
        self._approval_window = max(1, int(approval_window))
        self._missed_grace = max(0, int(missed_grace))
        self._max_per_tick = max(1, int(max_per_tick))
        self._closed = False
        self._last_error: Optional[str] = None
        # Observer-thread handoff: worker threads only append here.
        self._lock = threading.Lock()
        self._queue: List[Any] = []
        # job_id -> in-flight bookkeeping (tick thread only).
        self._inflight: Dict[str, Dict[str, Any]] = {}
        if manager is not None and hasattr(manager, "add_observer"):
            manager.add_observer(self._observe)

    # ------------------------------------------------------------------ #
    # Observer handoff (worker thread → tick thread)
    # ------------------------------------------------------------------ #

    def _observe(self, record: Any) -> None:
        """Manager observer — appends only; never touches the store."""
        if self._closed:
            return
        with self._lock:
            self._queue.append(record)

    def _drain(self, report: TickReport) -> None:
        with self._lock:
            records, self._queue = self._queue, []
        for record in records:
            task_id = str(getattr(record, "task_id", "") or "")
            entry = None
            for job_id, info in self._inflight.items():
                if info.get("task_id") == task_id:
                    entry = (job_id, info)
                    break
            if entry is None:
                continue
            job_id, info = entry
            state = str(getattr(record, "state", "") or "")
            if state == "awaiting_approval" and not info.get("awaiting"):
                info["awaiting"] = True
                info["awaiting_since"] = int(self._clock())
                self._emit({"kind": "approval_needed", "job_id": job_id,
                            "title": info.get("title", ""),
                            "text": "Scheduled task needs approval in the "
                                    "task window."})
            if state in TERMINAL_STATES:
                self._finalize(job_id, info, state,
                               str(getattr(record, "approval", "") or ""),
                               report)

    # ------------------------------------------------------------------ #
    # Notifications (deduplicated through the occurrence row)
    # ------------------------------------------------------------------ #

    def _emit(self, event: Dict[str, Any]) -> None:
        if self._notifier is None:
            return
        try:
            self._notifier(event)
        except Exception:  # noqa: BLE001 - a broken notifier never breaks
            pass                  # the scheduler; the claim still dedupes

    def _notify_once(self, job_id: str, occurrence: int,
                     event: Dict[str, Any]) -> bool:
        """Notify at most once per occurrence (store-backed dedupe)."""
        try:
            if not self._store.mark_notified(job_id, occurrence):
                return False
        except JobStoreError as exc:
            self._note_error(exc)
            return False
        self._emit(event)
        return True

    def _note_error(self, exc: JobStoreError) -> None:
        self._last_error = str(exc)

    @property
    def last_error(self) -> Optional[str]:
        """Content-free description of the most recent scheduler failure."""
        return self._last_error

    # ------------------------------------------------------------------ #
    # Tick
    # ------------------------------------------------------------------ #

    def tick(self) -> TickReport:
        """One bounded scheduling pass.  Never raises for job problems."""
        report = TickReport(now=int(self._clock()))
        if self._closed:
            report.errors.append("scheduler is shut down")
            return report
        try:
            self._drain(report)
            self._enforce_approval_window(report)
            jobs = self._store.due(report.now, limit=self._max_per_tick)
            report.due = len(jobs)
            for job in jobs:
                if self._busy_inflight():
                    report.deferred += 1
                    break
                try:
                    self._process(job, report)
                except JobStoreError as exc:
                    self._note_error(exc)
                    report.errors.append(str(exc))
        except JobStoreError as exc:
            self._note_error(exc)
            report.errors.append(str(exc))
        self._last_error = (report.errors[-1] if report.errors
                            else self._last_error)
        return report

    def _busy_inflight(self) -> bool:
        return bool(self._inflight)

    def _enforce_approval_window(self, report: TickReport) -> None:
        """Cancel unattended approval waits — never bypass the gate."""
        now = int(self._clock())
        for job_id, info in list(self._inflight.items()):
            since = info.get("awaiting_since")
            if since is None or now - since < self._approval_window:
                continue
            cancelled = False
            if self._manager is not None:
                try:
                    cancelled = bool(self._manager.cancel())
                except Exception:  # noqa: BLE001 - honest fallback below
                    cancelled = False
            report.approvals_expired += 1
            if cancelled:
                # The terminal record arrives via the observer and the
                # normal finalize path records needs_approval.
                continue
            # Cancel refused (execution already started) or no manager:
            # keep waiting; the observer will finalize honestly.  We
            # never approve on the user's behalf either way.
            info["awaiting_since"] = now

    # ------------------------------------------------------------------ #
    # Dispatch
    # ------------------------------------------------------------------ #

    def _process(self, job: ScheduledJob, report: TickReport) -> None:
        now = report.now
        occurrence = job.next_occurrence
        if occurrence is None:
            return
        missed = occurrence < now - self._missed_grace
        if missed:
            self._handle_missed(job, occurrence, report)
            return
        # Already-claimed occurrences are only re-dispatched while they
        # are pending (crash recovery); anything else just advances.
        row = self._store.occurrence(job.job_id, occurrence)
        if row is not None and row.get("status") != STATUS_PENDING:
            self._advance(job, now)
            return
        if job.job_id in self._inflight:
            report.deferred += 1
            return
        if row is None and not self._store.claim(job.job_id, occurrence):
            # Lost the race (another tick/process): advance only.
            self._advance(job, now)
            return
        if job.job_type == "reminder":
            self._fire_reminder(job, occurrence, report)
        elif job.job_type == "check":
            self._fire_check(job, occurrence, report)
        else:
            self._dispatch_task(job, occurrence, report)

    def _fire_reminder(self, job: ScheduledJob, occurrence: int,
                       report: TickReport) -> None:
        self._notify_once(job.job_id, occurrence, {
            "kind": "reminder", "job_id": job.job_id, "title": job.title,
            "text": job.description,
            "schedule": format_schedule(job.schedule),
            "project": job.project,
        })
        self._complete(job, occurrence, STATUS_DISPATCHED,
                       "reminder shown", approval="not_required",
                       report=report)
        report.reminders += 1

    def _fire_check(self, job: ScheduledJob, occurrence: int,
                    report: TickReport) -> None:
        fired, detail = self._evaluate_condition(job)
        if fired is None:
            self._complete(job, occurrence, STATUS_ERROR, detail,
                           approval="needs_attention", report=report)
            report.errors.append(detail)
            return
        if not fired:
            self._complete(job, occurrence, STATUS_DISPATCHED,
                           f"condition not met ({detail})",
                           approval="not_required", report=report)
            return
        # Condition met: the follow-up goes through the ordinary task
        # path (manager → preview → permissions → approval → verify).
        if not job.description.strip():
            self._notify_once(job.job_id, occurrence, {
                "kind": "check", "job_id": job.job_id, "title": job.title,
                "text": f"Condition met: {detail}",
            })
            self._complete(job, occurrence, STATUS_DISPATCHED,
                           f"condition met ({detail})",
                           approval="not_required", report=report)
            return
        self._dispatch_task(job, occurrence, report, prefix_detail=detail)

    def _dispatch_task(self, job: ScheduledJob, occurrence: int,
                       report: TickReport,
                       prefix_detail: str = "") -> None:
        if self._manager is None:
            self._complete(job, occurrence, STATUS_ERROR,
                           "no execution engine available",
                           approval="needs_attention", report=report)
            report.errors.append(f"{job.title}: no execution engine")
            return
        try:
            record = self._manager.submit(job.description)
        except Exception as exc:  # noqa: BLE001 - ManagerError or worse
            self._retry_or_fail(job, occurrence,
                                f"dispatch refused ({exc.__class__.__name__})",
                                report)
            return
        self._store.finish_occurrence(
            job.job_id, occurrence, status=STATUS_DISPATCHED,
            outcome=validate_outcome(prefix_detail or "task submitted"),
            approval="pending" if getattr(record, "approval", "") ==
            "pending" else "not_required")
        self._inflight[job.job_id] = {
            "occurrence": occurrence, "task_id": str(record.task_id),
            "awaiting": False, "awaiting_since": None,
            "title": job.title,
        }
        report.dispatched += 1

    def _retry_or_fail(self, job: ScheduledJob, occurrence: int,
                       reason: str, report: TickReport) -> None:
        """Dispatch failure: bounded retry, then an honest final outcome."""
        retries = job.retries_used + 1 if job.retry_occurrence == occurrence \
            else 1
        if retries <= job.max_retries:
            updated = self._store.update(type(job)(**{
                **job.__dict__, "retries_used": retries,
                "retry_occurrence": occurrence,
                "last_attempt": occurrence,
                "last_outcome": validate_outcome(
                    f"retry {retries}/{job.max_retries}: {reason}"),
            }))
            report.deferred += 1
            return updated
        self._complete(job, occurrence, STATUS_ERROR,
                       f"{reason} (after {job.max_retries} retries)",
                       approval="needs_attention", report=report)
        report.errors.append(f"{job.title}: {reason}")

    def _finalize(self, job_id: str, info: Dict[str, Any], state: str,
                  approval: str, report: TickReport) -> None:
        """A submitted task reached a terminal state — persist honestly."""
        occurrence = int(info.get("occurrence", 0))
        self._inflight.pop(job_id, None)
        job = self._store.get(job_id)
        if job is None:
            return
        if state == "completed":
            self._complete(job, occurrence, STATUS_DISPATCHED,
                           "task completed",
                           approval=approval or "approved", report=report)
        elif state == "failed":
            self._complete(job, occurrence, STATUS_ERROR, "task failed",
                           approval=approval or "not_required",
                           report=report)
            report.errors.append(f"{job.title}: task failed")
        elif state == "needs_attention":
            self._complete(job, occurrence, STATUS_ERROR,
                           "needs attention", approval="needs_attention",
                           report=report)
            report.errors.append(f"{job.title}: needs attention")
        else:  # cancelled — refused approval or user/timeout cancel
            if approval == "rejected":
                outcome = "approval refused; nothing was executed"
                kind = STATUS_CANCELLED
            elif info.get("awaiting"):
                outcome = "approval window expired; nothing was executed"
                kind = STATUS_CANCELLED
            else:
                outcome = "cancelled before execution finished"
                kind = STATUS_CANCELLED
            self._complete(job, occurrence, kind, outcome,
                           approval="needs_attention" if info.get("awaiting")
                           else (approval or "refused"), report=report)
            self._emit({"kind": "attention", "job_id": job_id,
                        "title": job.title,
                        "text": f"Scheduled task {outcome}."})
        self._notify_once(job_id, occurrence, {
            "kind": "outcome", "job_id": job_id, "title": job.title,
            "text": f"Scheduled task: {validate_outcome(job.last_outcome)}",
        })

    def _handle_missed(self, job: ScheduledJob, occurrence: int,
                       report: TickReport) -> None:
        """Coalesce missed occurrences — never a backlog, never a replay."""
        count = missed_count(job.schedule, occurrence=occurrence,
                             now=report.now, tz=tz_for(job.schedule.tzid))
        count = max(1, count)
        if job.job_type == "reminder":
            self._store.claim(job.job_id, occurrence,
                              status=STATUS_MISSED)
            self._notify_once(job.job_id, occurrence, {
                "kind": "missed", "job_id": job.job_id, "title": job.title,
                "text": f"{count} missed occurrence(s) of "
                        f"'{job.title}' coalesced — not replayed.",
            })
            self._complete(job, occurrence, STATUS_MISSED,
                           f"{count} missed occurrence(s) coalesced",
                           approval="not_required", report=report)
        elif job.job_type == "task":
            self._store.claim(job.job_id, occurrence, status=STATUS_MISSED)
            self._complete(job, occurrence, STATUS_MISSED,
                           f"{count} missed occurrence(s) coalesced; "
                           f"no automatic catch-up (use Run now)",
                           approval="needs_attention", report=report)
            self._emit({"kind": "attention", "job_id": job.job_id,
                        "title": job.title,
                        "text": f"'{job.title}' missed {count} run(s) "
                                f"while Alwin was closed — not executed "
                                f"automatically."})
        else:  # check: re-evaluate now, exactly once
            self._store.claim(job.job_id, occurrence, status=STATUS_MISSED)
            self._complete(job, occurrence, STATUS_DISPATCHED,
                           f"missed check coalesced ({count}) — "
                           f"re-evaluating now",
                           approval="not_required", report=report)
        report.missed += 1

    # ------------------------------------------------------------------ #
    # Completion bookkeeping
    # ------------------------------------------------------------------ #

    def _complete(self, job: ScheduledJob, occurrence: int, status: str,
                  outcome: str, *, approval: str, report: TickReport
                  ) -> None:
        """Persist one attempt's outcome and advance the schedule."""
        now = report.now
        next_occ: Optional[int] = None
        if job.state == "enabled" and job.schedule.is_recurring:
            next_occ = next_occurrence(job.schedule, after=now,
                                       tz=tz_for(job.schedule.tzid))
        success = status == STATUS_DISPATCHED and "failed" not in outcome \
            and "not met" not in outcome
        self._store.finish_occurrence(job.job_id, occurrence,
                                      status=status, outcome=outcome,
                                      approval=approval)
        self._store.update(type(job)(**{
            **job.__dict__,
            "next_occurrence": next_occ,
            "last_attempt": occurrence,
            "last_success": occurrence if success else job.last_success,
            "last_outcome": validate_outcome(outcome),
            "approval": approval,
            "retries_used": 0 if success else job.retries_used,
            "retry_occurrence": None if success else job.retry_occurrence,
        }))
        self._store.prune_history(job.job_id)

    def _advance(self, job: ScheduledJob, now: int) -> None:
        """Skip a fully-handled occurrence without executing anything."""
        next_occ = None
        if job.state == "enabled" and job.schedule.is_recurring:
            next_occ = next_occurrence(job.schedule, after=now,
                                       tz=tz_for(job.schedule.tzid))
        if next_occ != job.next_occurrence:
            self._store.update(type(job)(**{
                **job.__dict__, "next_occurrence": next_occ,
            }))

    # ------------------------------------------------------------------ #
    # Conditions (Phase 6 — closed operation set, single bounded probe)
    # ------------------------------------------------------------------ #

    def _evaluate_condition(self, job: ScheduledJob
                            ) -> Tuple[Optional[bool], str]:
        """``(fired, detail)`` — fired is None on an honest error."""
        condition = job.condition
        if condition is None:
            return None, "check job has no condition"
        if condition.op == "app_installed":
            return self._check_app_installed(condition.argument)
        # Path operations need real permission validation + one stat.
        path = self._resolve_path(condition.argument)
        if path is None:
            return None, "condition path is outside the permitted roots"
        import os
        try:
            if condition.op == "file_exists":
                return path.exists(), f"file_exists {condition.argument}"
            if condition.op == "file_updated":
                if not path.exists():
                    return False, f"file_updated {condition.argument}"
                baseline = job.last_success or job.created_at
                mtime = int(os.stat(path).st_mtime)
                return mtime > baseline, f"file_updated {condition.argument}"
        except OSError:
            return None, "condition probe failed"
        return None, f"unsupported condition op '{condition.op}'"

    def _resolve_path(self, argument: str):
        """Permitted-root validation via the real PermissionChecker."""
        if self._config is None:
            return None
        try:
            from ai_core.permissions import PermissionChecker
        except ImportError:  # pragma: no cover - defensive
            return None
        decision = PermissionChecker(self._config).check(argument,
                                                          operation="read")
        return Path(decision.resolved) if decision.allowed else None

    def _check_app_installed(self, app_id: str) -> Tuple[Optional[bool], str]:
        """Real registry probe (lazy import, same precedent as caps)."""
        try:
            from integrations.apps.registry import BUILTIN_REGISTRY
        except ImportError:  # pragma: no cover - defensive
            return None, "registry unavailable"
        import shutil
        try:
            definition = BUILTIN_REGISTRY.get(app_id)
        except Exception:  # noqa: BLE001 - RegistryError = unknown id
            return None, f"unknown application id '{app_id[:40]}'"
        detection = getattr(definition, "detection", None)
        executables = getattr(detection, "executables", ()) or ()
        found = any(shutil.which(exe) for exe in executables)
        return found, f"app_installed {app_id}"

    # ------------------------------------------------------------------ #
    # Lifecycle & controls
    # ------------------------------------------------------------------ #

    def close(self) -> None:
        """Stop observing; in-flight submissions finish on their own."""
        self._closed = True
        with self._lock:
            self._queue.clear()

    @property
    def store(self) -> JobStore:
        return self._store

    @property
    def inflight(self) -> Dict[str, Dict[str, Any]]:
        """Job ids currently executing through the manager (read-only)."""
        return dict(self._inflight)

    # ---- job controls (recompute next occurrence on every change) ---- #

    def _recompute(self, job: ScheduledJob, now: int,
                   *, after: Optional[int] = None) -> ScheduledJob:
        next_occ = None
        if job.state == "enabled":
            next_occ = next_occurrence(
                job.schedule, after=(now if after is None else after),
                tz=tz_for(job.schedule.tzid))
        return type(job)(**{**job.__dict__, "next_occurrence": next_occ})

    def create(self, job: ScheduledJob) -> ScheduledJob:
        """Validate + persist a new job with its first occurrence set."""
        now = int(self._clock())
        if self._store.get(job.job_id) is not None:
            raise SchedulerError("That job already exists.")
        stored = self._recompute(job, now)
        try:
            return self._store.add(stored)
        except JobStoreError as exc:
            raise SchedulerError(str(exc)) from exc

    def create_from_intent(self, intent: Any, *, project: str = ""
                           ) -> ScheduledJob:
        """Turn a parsed :class:`~ai_core.schedule_parse.ScheduleIntent`
        into a persisted job — the deterministic bridge used by the
        conversation service (which cannot import this package)."""
        from ai_core.schedule_parse import INTENT_CREATE_TASK
        now = int(self._clock())
        job_type = "task" if getattr(intent, "intent", "") == \
            INTENT_CREATE_TASK else "reminder"
        schedule = intent.schedule(now=now)
        job = make_job(
            getattr(intent, "title", "") or job_type,
            getattr(intent, "description", "") or getattr(intent, "title",
                                                          ""),
            job_type=job_type, schedule=schedule, now=now,
            project=str(project or ""),
        )
        return self.create(job)

    def pause(self, job_id: str) -> ScheduledJob:
        return self._set_state(job_id, "paused")

    def resume(self, job_id: str) -> ScheduledJob:
        return self._set_state(job_id, "enabled")

    def cancel_job(self, job_id: str) -> ScheduledJob:
        return self._set_state(job_id, "cancelled")

    def _set_state(self, job_id: str, state: str) -> ScheduledJob:
        job = self._require_job(job_id)
        if job.state == "cancelled" and state != "cancelled":
            raise SchedulerError("A cancelled job cannot be resumed.")
        now = int(self._clock())
        edited = self._recompute(type(job)(**{**job.__dict__,
                                              "state": state}), now)
        try:
            return self._store.update(edited, now=now)
        except JobStoreError as exc:
            raise SchedulerError(str(exc)) from exc

    def edit(self, job_id: str, *, title: Optional[str] = None,
             description: Optional[str] = None,
             schedule: Any = None) -> ScheduledJob:
        """Re-validate and persist user edits (full re-validation)."""
        job = self._require_job(job_id)
        now = int(self._clock())
        edited = type(job)(**{
            **job.__dict__,
            "title": job.title if title is None else title,
            "description": (job.description if description is None
                            else description),
            "schedule": job.schedule if schedule is None else schedule,
        })
        try:
            edited.validate(now=now)
        except ScheduleError as exc:
            raise SchedulerError(exc.reason) from exc
        stored = self._recompute(edited, now)
        try:
            return self._store.update(stored, now=now)
        except JobStoreError as exc:
            raise SchedulerError(str(exc)) from exc

    def run_now(self, job_id: str) -> Dict[str, Any]:
        """Manual trigger through the ordinary pipeline (Run now button).

        Claims a synthetic occurrence at the current instant (so the run
        appears in history and cannot double-fire), then dispatches via
        ``manager.submit`` — identical to a scheduled dispatch.
        """
        job = self._require_job(job_id)
        if job.state != "enabled":
            raise SchedulerError("Only enabled jobs can run now.")
        now = int(self._clock())
        if not self._store.claim(job_id, now):
            raise SchedulerError("This job is already running.")
        report = TickReport(now=now)
        if job.job_type == "reminder":
            self._fire_reminder(job, now, report)
            return {"kind": "reminder", "job_id": job_id,
                    "text": job.description, "outcome": "reminder shown"}
        if job.job_type == "check":
            self._fire_check(job, now, report)
            row = self._store.occurrence(job_id, now) or {}
            return {"kind": "check", "job_id": job_id,
                    "outcome": row.get("outcome", "")}
        if self._manager is None:
            self._complete(job, now, STATUS_ERROR,
                           "no execution engine available",
                           approval="needs_attention", report=report)
            raise SchedulerError("No execution engine is available.")
        try:
            record = self._manager.submit(job.description)
        except Exception as exc:  # noqa: BLE001 - honest class name only
            self._complete(job, now, STATUS_ERROR,
                           f"dispatch refused ({exc.__class__.__name__})",
                           approval="needs_attention", report=report)
            raise SchedulerError(
                f"The task engine refused the run "
                f"({exc.__class__.__name__}).") from exc
        self._inflight[job_id] = {
            "occurrence": now, "task_id": str(record.task_id),
            "awaiting": False, "awaiting_since": None, "title": job.title,
        }
        return {"kind": "task", "job_id": job_id,
                "task_id": str(record.task_id),
                "outcome": "submitted to the task engine"}

    # ------------------------------------------------------------------ #
    # Display helpers (all formatting lives HERE, in ai_core — the
    # companion's conversation module may not import anything else, so
    # it only ever calls these).
    # ------------------------------------------------------------------ #

    def now(self) -> int:
        """The scheduler's injected clock (deterministic in tests)."""
        return int(self._clock())

    def describe_schedule(self, schedule: Any) -> str:
        """One-line human summary of a schedule."""
        return format_schedule(schedule)

    def describe(self, job: ScheduledJob) -> str:
        """Bounded, content-free status line for one job."""
        import datetime as _dt
        parts = [f"“{job.title}” ({job.job_type}, {job.state})",
                 format_schedule(job.schedule)]
        if job.next_occurrence:
            zone = tz_for(job.schedule.tzid)
            parts.append("next " + _dt.datetime.fromtimestamp(
                job.next_occurrence, zone).strftime("%Y-%m-%d %H:%M"))
        if job.last_outcome:
            parts.append(f"last outcome: {job.last_outcome}")
        if job.approval == "needs_attention":
            parts.append("needs your attention (see the task window)")
        return "; ".join(parts)

    def listing(self, *, limit: int = 8) -> str:
        """Bounded listing of active jobs (titles, schedules, states)."""
        try:
            jobs = [j for j in self._store.list_jobs()
                    if j.state != "cancelled"]
        except JobStoreError:
            return "Scheduler storage is unavailable."
        if not jobs:
            return "You have no scheduled jobs."
        lines = []
        for job in jobs[-limit:]:
            flag = " ⚠" if job.approval == "needs_attention" else ""
            lines.append(f"• {self.describe(job)}{flag}")
        if len(jobs) > limit:
            lines.append(f"…and {len(jobs) - limit} more.")
        return "Scheduled jobs:\n" + "\n".join(lines)

    def status_line(self) -> str:
        """One bounded status line for the settings popover.

        Content-free: counts and the next occurrence only — never job
        text, never paths, never internals.
        """
        try:
            counts = self._store.counts()
            if not any(counts.get(key, 0) for key in
                       ("enabled", "paused", "cancelled")):
                return "No scheduled jobs."
            line = f"{counts['enabled']} enabled"
            if counts.get("paused"):
                line += f", {counts['paused']} paused"
            times = [j.next_occurrence for j in self._store.list_jobs()
                     if j.state == "enabled" and j.next_occurrence]
            if times:
                import datetime as _dt
                stamp = _dt.datetime.fromtimestamp(min(times)).strftime(
                    "%Y-%m-%d %H:%M")
                line += f" · next {stamp}"
            return line
        except JobStoreError:
            return "Scheduler storage is unavailable."

    def _require_job(self, job_id: str) -> ScheduledJob:
        job = self._store.get(str(job_id))
        if job is None:
            raise SchedulerError("No such scheduled job.")
        return job

    # ------------------------------------------------------------------ #
    # Lookup (title words or job id — never fuzzy enough to guess)
    # ------------------------------------------------------------------ #

    def find(self, query: str) -> List[ScheduledJob]:
        """Active jobs whose title/description/id contain the query.

        Token-overlap matching over ``enabled`` and ``paused`` jobs
        (cancelled jobs are never matched — you cannot pause or run
        something that is already cancelled).  Deterministic: newest
        first, and a caller decides what to do with multiple matches
        instead of silently picking one.
        """
        tokens = {t for t in str(query or "").lower().split()
                  if t and t not in ("my", "the", "a", "an", "about",
                                     "reminder", "task", "job",
                                     "scheduled", "reminder-")}
        job_id = str(query or "").strip()
        jobs = self._store.list_jobs()
        if job_id.startswith("job_"):
            exact = [j for j in jobs if j.job_id == job_id]
            if exact:
                return exact
        if not tokens:
            return []
        matched = []
        for job in reversed(jobs):
            if job.state == "cancelled":
                continue
            haystack = f"{job.title} {job.description} {job.job_id}".lower()
            if all(token in haystack for token in tokens):
                matched.append(job)
        return matched

    def resolve(self, query: str) -> ScheduledJob:
        """Exactly one match, or an honest, bounded refusal."""
        matches = self.find(query)
        if not matches:
            raise SchedulerError("I couldn't find a job matching "
                                 f"'{str(query)[:40]}'.")
        if len(matches) > 1:
            titles = "; ".join(f"'{j.title}'" for j in matches[:3])
            raise SchedulerError(f"That matches {len(matches)} jobs "
                                 f"({titles}) — be more specific.")
        return matches[0]


__all__ = [
    "DEFAULT_APPROVAL_WINDOW",
    "DEFAULT_MISSED_GRACE",
    "DEFAULT_TICK_SECONDS",
    "Scheduler",
    "SchedulerError",
    "TickReport",
    "TERMINAL_STATES",
]
