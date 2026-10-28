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

from typing import Dict, List, Optional

from ai_core.permissions import PermissionChecker
from integrations.filesystem.tools import ToolResult

STATUS_OK = "ok"
STATUS_FAILED = "failed"
STATUS_DENIED = "denied"
STATUS_SKIPPED = "skipped"
STATUS_DRY_RUN = "dry_run"


class TaskSequenceRunner:
    """Executes a validated sequence with stop-on-failure semantics."""

    def __init__(self, checker: PermissionChecker, dispatcher=None) -> None:
        self.checker = checker
        self.dispatcher = dispatcher

    # ------------------------------------------------------------------ #

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

    def run(self, action: dict, *, dry_run: bool = False) -> ToolResult:
        steps = list(action.get("steps") or [])
        total = len(steps)

        if dry_run:
            records = [
                self._record(step, STATUS_DRY_RUN, index=i, ok=None, error=None)
                for i, step in enumerate(steps, start=1)
            ]
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
                records.append(
                    self._record(
                        step,
                        STATUS_FAILED,
                        index=index,
                        ok=False,
                        executed=False,
                        changed=False,
                        message="",
                        error=f"{type(exc).__name__}: {exc}",
                        data={},
                    )
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

            records.append(
                self._record(
                    step,
                    STATUS_OK if result.ok else STATUS_FAILED,
                    index=index,
                    ok=result.ok,
                    executed=bool(result.ok),
                    changed=bool(result.changed),
                    message=result.message,
                    error=result.error,
                    data=dict(result.data),
                )
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
            return ToolResult(
                ok=False,
                message="",
                changed=any(bool(r.get("changed")) for r in records),
                error=(
                    f"{failure}. Step {failed_step} of {total} did not "
                    f"complete, so the task is NOT reported as successful; "
                    f"remaining steps were skipped."
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
