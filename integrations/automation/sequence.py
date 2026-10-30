"""Validated multi-step task execution (v0.4).

A ``task_sequence`` action carries steps that were each validated by
:func:`ai_core.planner.validate_action` at plan time.  This runner adds
runtime safeguards:

* every step's **permission decision is re-checked** immediately before
  it runs (defence in depth — the world may have changed),
* the first failure (denial, dispatch error, handler error) **stops the
  sequence**: dependent steps are marked ``skipped``, never executed,
* a partial run is reported as a failure — complete task success is
  never claimed when a required step failed (all steps are required),
* dry-run executes nothing and says so,
* per-step results (including each handler's ``data``) are recorded so
  the verification stage can re-verify every step independently.

Approval happens once, in the pipeline, for the whole sequence: the
proposal shown to the user enumerates every step.
"""

from __future__ import annotations

from typing import Callable, Dict, List, Optional

from ai_core.permissions import PermissionChecker
from integrations.automation.base import FAILURE_CATEGORIES
from integrations.filesystem.tools import ToolResult

#: v0.19: safe, bounded next-action hints per failure category
#: (fixed strings — never derived from application or document text).
_NEXT_ACTION_HINTS = {
    "unsupported": (
        "The adapter does not expose this operation; use a supported "
        "operation or the trusted document tools instead."
    ),
    "app_missing": (
        "Start the application (or check its capability in Settings), "
        "then run the remaining steps as a new, separately approved "
        "plan."
    ),
    "bus_error": (
        "The accessibility bus or application disappeared mid-step; "
        "re-check application status, then re-run the remaining steps "
        "as a new plan — the completed steps are not repeated."
    ),
    "window_gone": (
        "The application window closed; reopen the document through "
        "the normal open action, then continue with a new plan."
    ),
    "stale_target": (
        "The target control changed or vanished; re-inspect the "
        "application's controls and retry with a fresh, explicitly "
        "approved plan."
    ),
    "ambiguous": (
        "Several controls matched; narrow the locator (exact "
        "accessible name) and re-run as a new plan — nothing was "
        "attempted."
    ),
    "refused": (
        "The application refused the action; inspect its current "
        "state manually before planning any further steps."
    ),
    "denied": (
        "The permission stage denied this step; nothing was executed "
        "for it — adjust the request or the permitted roots instead "
        "of retrying."
    ),
}

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_DENIED = "denied"
STATUS_SKIPPED = "skipped"
STATUS_DRY_RUN = "dry_run"

#: Statuses reported to an optional ``step_observer`` as the sequence
#: progresses: ``running`` (step started), then ``ok``/``failed``/
#: ``denied``/``skipped``/``dry_run`` once judged.
STATUS_RUNNING = "running"


class TaskSequenceRunner:
    """Executes a validated sequence with stop-on-failure semantics."""

    def __init__(
        self,
        checker: PermissionChecker,
        dispatcher=None,
        *,
        step_observer: Optional[Callable[..., None]] = None,
    ) -> None:
        self.checker = checker
        self.dispatcher = dispatcher
        #: Optional ``(index, total, action_name, status, error=None)``
        #: callback invoked at each step boundary.  Observation only:
        #: the observer cannot influence, skip or approve anything.
        self.step_observer = step_observer

    # ------------------------------------------------------------------ #

    def _observe(
        self, index: int, total: int, step: dict, status: str, error=None
    ) -> None:
        if self.step_observer is None:
            return
        try:
            self.step_observer(
                index, total, step.get("action", "?"), status, error=error
            )
        except Exception:  # noqa: BLE001 - an observer bug never stops a task
            pass

    @staticmethod
    def _record(step: dict, status: str, **extra) -> Dict:
        record = {
            "index": 0,
            "action": step.get("action", "?"),
            "status": status,
        }
        record.update(extra)
        return record

    def _skip_rest(
        self, steps: List[dict], records: List[Dict], start: int, reason: str
    ) -> None:
        total = len(steps)
        for offset, step in enumerate(steps[start:], start=start + 1):
            records.append(
                self._record(
                    step,
                    STATUS_SKIPPED,
                    index=offset,
                    ok=None,
                    error=f"not executed: {reason}",
                )
            )
            self._observe(offset, total, step, STATUS_SKIPPED)

    def run(self, action: dict, *, dry_run: bool = False) -> ToolResult:
        steps = list(action.get("steps") or [])
        total = len(steps)

        if dry_run:
            records = []
            for i, step in enumerate(steps, start=1):
                records.append(
                    self._record(step, STATUS_DRY_RUN, index=i, ok=None,
                                 error=None)
                )
                self._observe(i, total, step, STATUS_DRY_RUN)
            return ToolResult(
                ok=True,
                message=(
                    f"Would run {total} step(s) in order (dry run: no step "
                    f"was executed)."
                ),
                changed=False,
                data={"steps": records, "total": total, "completed": 0,
                      "failed_step": None, "dry_run": True},
            )

        records: List[Dict] = []
        completed = 0
        failed_step: Optional[int] = None
        failure = ""

        for index, step in enumerate(steps, start=1):
            # 1. Permission re-check for this exact step.
            self._observe(index, total, step, STATUS_RUNNING)
            try:
                decision = self.checker.check_action(step)
            except Exception as exc:  # noqa: BLE001 - a check error denies
                decision = None
                reason = f"permission check errored ({type(exc).__name__}: {exc})"
            else:
                reason = decision.reason
            if decision is None or not decision.allowed:
                records.append(
                    self._record(
                        step, STATUS_DENIED, index=index, ok=False, error=reason
                    )
                )
                self._observe(index, total, step, STATUS_DENIED, error=reason)
                failed_step = index
                failure = (
                    f"Step {index} of {total} was denied by the permission "
                    f"stage: {reason}"
                )
                self._skip_rest(
                    steps, records, index, "an earlier step was denied"
                )
                break

            # 2. Execute through the trusted dispatcher.
            if self.dispatcher is None:  # pragma: no cover - wiring error
                records.append(
                    self._record(
                        step,
                        STATUS_FAILED,
                        index=index,
                        ok=False,
                        error="no dispatcher is wired to this runner",
                    )
                )
                failed_step = index
                failure = f"Step {index} of {total} could not be dispatched."
                self._skip_rest(
                    steps, records, index, "an earlier step failed"
                )
                break
            try:
                result = self.dispatcher.dispatch(step, dry_run=False)
            except Exception as exc:  # noqa: BLE001 - reported, not hidden
                error_text = f"{type(exc).__name__}: {exc}"
                records.append(
                    self._record(
                        step,
                        STATUS_FAILED,
                        index=index,
                        ok=False,
                        executed=False,
                        changed=False,
                        message="",
                        error=error_text,
                        data={},
                    )
                )
                self._observe(
                    index, total, step, STATUS_FAILED, error=error_text
                )
                failed_step = index
                failure = (
                    f"Step {index} of {total} failed: "
                    f"{type(exc).__name__}: {exc}".rstrip(".")
                )
                self._skip_rest(
                    steps, records, index, "an earlier step failed"
                )
                break

            step_status = STATUS_OK if result.ok else STATUS_FAILED
            records.append(
                self._record(
                    step,
                    step_status,
                    index=index,
                    ok=result.ok,
                    executed=bool(result.ok),
                    changed=bool(result.changed),
                    message=result.message,
                    error=result.error,
                    data=dict(result.data),
                )
            )
            self._observe(
                index,
                total,
                step,
                step_status,
                error=None if result.ok else (result.error or result.message),
            )
            if not result.ok:
                failed_step = index
                reason = (
                    result.error or result.message or "unknown error"
                ).rstrip(".")
                failure = f"Step {index} of {total} failed: {reason}"
                self._skip_rest(steps, records, index, "an earlier step failed")
                break
            completed += 1

        data = {
            "steps": records,
            "total": total,
            "completed": completed,
            "failed_step": failed_step,
        }
        if failed_step is not None:
            # v0.19: an explicit partial-completion block — what
            # finished, whether earlier steps changed state, the
            # failure category, and a SAFE next action.  Fixed bounded
            # strings only: no step contents, nothing that could
            # influence a permission decision.
            changed = any(bool(r.get("changed")) for r in records)
            failed_record = records[failed_step - 1]
            category = str(
                (failed_record.get("data") or {}).get("category")
                or ("denied" if failed_record.get("status") == STATUS_DENIED
                    else "unknown")
            )
            if category not in FAILURE_CATEGORIES:
                category = "unknown"
            data["partial"] = {
                "completed": completed,
                "total": total,
                "failed_step": failed_step,
                "changed": changed,
                "category": category,
                "next_action": _NEXT_ACTION_HINTS.get(
                    category,
                    "Inspect the failed step's error, then re-run the "
                    "remaining steps as a new, separately approved plan "
                    "(nothing is retried automatically)."),
            }
            return ToolResult(
                ok=False,
                message="",
                changed=changed,
                error=(
                    f"{failure}. Step {failed_step} of {total} did not "
                    f"complete, so the task is NOT reported as successful; "
                    f"remaining steps were skipped."
                    + (" Earlier steps DID change state; their results "
                       f"stand ({completed} of {total} completed)."
                       if changed and completed else "")
                ),
                data=data,
            )
        changed = any(bool(r.get("changed")) for r in records)
        return ToolResult(
            ok=True,
            message=f"All {total} step(s) of the sequence completed in order.",
            changed=changed,
            data=data,
        )
